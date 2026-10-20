"""
Ce que les alertes ouvertes affirment encore, apres un changement de listes.

POURQUOI CE MODULE EXISTE
-------------------------
Deux constats, une meme classe de defaut : une alerte disait une chose que
la mise en production d'une liste venait de rendre fausse, et rien ne le
signalait.

1. Les RADIATIONS ne declenchaient rien. A chaque mise en production d'une
   version, les fiches RETIREES par rapport a la precedente passaient sans
   trace : le re-criblage (fiskr/rescreen.py) ne regarde que les fiches
   nouvelles ou modifiees, et ni les alertes ni l'approbation ne lisaient les
   retraits du delta ; `delisted_on` etait stocke et lu nulle part. Une alerte
   OUVERTE sur une personne radiee restait en file sans mention, et un client
   CONFIRME — gele — n'etait jamais signale a degeler. Les lignes directrices
   DGT/ACPR sur le gel des avoirs (18 mars 2026) prevoient pourtant une
   procedure de levee (§87) et demandent de filtrer « sans delai » a
   l'abrogation (§123).

2. Les alertes CADUQUES restaient en file comme si de rien n'etait. Constate
   dans un vrai navigateur : le cahier de tests annoncait « Alertes resolues
   par la liste candidate (8) » ; apres l'approbation, les 26 alertes etaient
   toujours ouvertes, et le valideur de l'alerte n° 20 lisait « 100 % · DOB :
   donnee manquante » pendant que l'historique disait « REDETECTED score 95 ».

CE QUI EST FAIT, ET CE QUI NE L'EST PAS
---------------------------------------
- `constater_les_radiations` (a la mise en production d'une version) : chaque
  alerte OUVERTE sur une fiche radiee recoit un evenement DELISTED et devient
  caduque, avec le motif de cloture propose ; chaque mesure de gel ACTIVE sur
  une fiche radiee devient « radiation a examiner » (fiskr/gels.py) et une
  notification immediate part.
- `examiner_les_caduques` (apres le re-criblage de cette mise en production,
  d'une correction ou d'un ajout manuel de fiche, et apres un lookback) :
  une alerte OUVERTE dont la fiche vient d'etre re-criblee et dont le couple
  ne franchit plus le seuil porte son dernier score et son dernier arbre, et
  devient caduque.

Rien n'est clos ni leve par le produit. Une alerte caduque reste ouverte : un
humain la clot, avec le motif propose. Une mesure « radiation a examiner »
reste en vigueur : la levee suit un acte (LD §209-213) que seul un humain
peut verifier — une radiation d'une liste n'est pas l'abrogation de toutes
les mesures qui visent la personne (un autre regime peut la designer encore).

UNE RADIATION APPARENTE N'EST PAS UNE RADIATION
-----------------------------------------------
Le delta identifie une fiche par son `entity_id` (fiskr/delta.py) : une fiche
dont l'identifiant change d'une version a l'autre y apparait RETIREE puis
AJOUTEE. Ce n'est pas la personne qui sort de la liste. Une fiche retiree est
tenue pour RE-IDENTIFIEE, et aucune radiation n'est annoncee, quand la
nouvelle version ajoute une fiche du meme type qui porte la meme reference
officielle, ou le meme nom (normalise, ordre des mots indifferent) avec des
dates de naissance compatibles. L'alerte ouverte recoit alors un evenement
REIDENTIFIED qui nomme le nouvel identifiant : le doute est dit, pas tranche
en silence. Le critere penche vers « pas de radiation » : annoncer a tort la
radiation d'une personne gelee inviterait a la degeler.
"""
import logging
import unicodedata
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from fiskr.database import (Alert, AlertEvent, ALERT_OPEN_STATUSES, ClientEntity,
                            MesureDeGel, Snapshot, WatchlistEntity)

logger = logging.getLogger("fiskr.caducite")

UTILISATEUR = "systeme"

# Evenements d'alerte ecrits ici (alert_events.action) : DELISTED, REIDENTIFIED,
# LAPSED — et LAPSE_CLEARED dans fiskr/alerts.py. Ecrits en LITTERAUX dans les
# appels : c'est ainsi que tests/test_libelles_derives.py les releve pour
# exiger leur libelle a l'ecran.

# Ce que l'on lit d'une fiche retiree ou ajoutee : de quoi la reconnaitre sous
# un autre identifiant, et la date de radiation que la source porte parfois.
_COLONNES = ("entity_id", "primary_name", "entity_type", "dates_of_birth",
             "official_reference", "delisted_on")

# SQLite plafonne les parametres d'une requete
_TRANCHE = 800


def _tranches(valeurs: List[Any]) -> Iterable[List[Any]]:
    for debut in range(0, len(valeurs), _TRANCHE):
        yield valeurs[debut:debut + _TRANCHE]


def compteurs_de_radiation() -> Dict[str, int]:
    """Forme UNIQUE du compte rendu des radiations (memes raisons que
    `rescreen.compteurs_de_recriblage` : une cle absente ne dit pas zero)."""
    return {"removed": 0, "delisted": 0, "reidentified": 0,
            "open_alerts_delisted": 0, "gel_measures_flagged": 0}


# ------------------------------------------------------------ re-identification

def _nom_normalise(nom: Optional[str]) -> str:
    sans_accents = "".join(c for c in unicodedata.normalize("NFKD", nom or "")
                           if not unicodedata.combining(c))
    mots = "".join(c if c.isalnum() else " " for c in sans_accents.casefold()).split()
    return " ".join(sorted(mots))


def _dates(fiche: Dict[str, Any]) -> Set[str]:
    from fiskr.scoring import _valeurs, parse_dob
    sortie = set()
    for valeur in _valeurs(fiche.get("dates_of_birth")):
        date = parse_dob(valeur)
        sortie.add(date.strftime("%Y-%m-%d") if date else str(valeur).strip())
    return {d for d in sortie if d}


def _index_des_ajouts(ajoutees: List[Dict[str, Any]]):
    par_reference: Dict[str, Dict[str, Any]] = {}
    par_nom: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for fiche in ajoutees:
        reference = (fiche.get("official_reference") or "").strip().casefold()
        if reference:
            par_reference.setdefault(reference, fiche)
        nom = _nom_normalise(fiche.get("primary_name"))
        if nom:
            par_nom.setdefault((fiche.get("entity_type") or "", nom), []).append(fiche)
    return par_reference, par_nom


def _meme_personne(retiree: Dict[str, Any], index) -> Optional[Tuple[Dict[str, Any], str]]:
    """La fiche ajoutee qui reprend la personne d'une fiche retiree, et le
    critere qui l'etablit — ou None."""
    par_reference, par_nom = index
    reference = (retiree.get("official_reference") or "").strip().casefold()
    if reference and reference in par_reference:
        return par_reference[reference], "même référence officielle"
    nom = _nom_normalise(retiree.get("primary_name"))
    dates = _dates(retiree)
    for candidate in par_nom.get((retiree.get("entity_type") or "", nom), []) if nom else []:
        autres = _dates(candidate)
        # Deux dates connues et disjointes : deux personnes
        if not dates or not autres or dates & autres:
            return candidate, "même nom et dates de naissance compatibles"
    return None


# ------------------------------------------------------------ radiations

def _encore_en_production(db, file_type: str, ids: List[str]) -> Set[str]:
    """Identifiants que la liste porte encore en production sous un autre lot
    (ajouts manuels) : retires de cette version, pas de la production."""
    lots = [s.snapshot_id for s in db.query(Snapshot.snapshot_id).filter(
        Snapshot.file_type == file_type, Snapshot.status == "READY").all()]
    if not lots or not ids:
        return set()
    trouves: Set[str] = set()
    for tranche in _tranches(ids):
        trouves.update(r[0] for r in db.query(WatchlistEntity.entity_id).filter(
            WatchlistEntity.snapshot_id.in_(lots),
            WatchlistEntity.entity_id.in_(tranche),
            WatchlistEntity.excluded.isnot(True)).all())
    return trouves


def _par_fiche(db, modele, colonne, file_type: str, ids: List[str], *filtres) -> list:
    lignes = []
    for tranche in _tranches(ids):
        lignes.extend(db.query(modele).filter(
            modele.list_type == file_type, colonne.in_(tranche), *filtres).all())
    return lignes


def _motif_de_radiation(fiche: Dict[str, Any], file_type: str, lot: Snapshot,
                        maintenant: datetime) -> str:
    date_source = (f" (date de radiation portée par la source : {fiche['delisted_on']})"
                   if fiche.get("delisted_on") else "")
    return (f"Fiche {fiche['entity_id']} « {fiche.get('primary_name') or ''} » radiée de "
            f"{file_type} : absente de la version {lot.snapshot_id} mise en production le "
            f"{maintenant:%d/%m/%Y %H:%M} UTC{date_source}.")


def constater_les_radiations(db, file_type: str, nouveau_lot: str,
                             lot_precedent: Optional[str],
                             maintenant: Optional[datetime] = None) -> Dict[str, int]:
    """
    A la mise en production de `nouveau_lot`, ce que deviennent les fiches que
    `lot_precedent` portait et qu'il ne porte plus.

    Les retraits sont ceux du delta (meme anti-jointure sur `entity_id`), moins
    les fiches que la liste porte encore sous un autre lot en production, moins
    les re-identifications (voir l'en-tete). Seules les alertes et les mesures
    de la MEME liste sont touchees : deux listes peuvent employer le meme
    identifiant pour deux fiches differentes. Idempotent : une alerte deja
    signalee radiee, une mesure deja signalee, ne le sont pas deux fois.
    """
    from fiskr import gels
    from fiskr.delta import fiches_sans_homologue

    compte = compteurs_de_radiation()
    if not lot_precedent or lot_precedent == nouveau_lot:
        return compte
    lot = db.query(Snapshot).filter(Snapshot.snapshot_id == nouveau_lot).first()
    if lot is None:
        return compte
    retirees = fiches_sans_homologue(db, lot_precedent, nouveau_lot, _COLONNES)
    compte["removed"] = len(retirees)
    if not retirees:
        return compte
    encore = _encore_en_production(db, file_type, [f["entity_id"] for f in retirees])
    retirees = [f for f in retirees if f["entity_id"] not in encore]

    index = _index_des_ajouts(fiches_sans_homologue(db, nouveau_lot, lot_precedent, _COLONNES))
    radiees: Dict[str, Dict[str, Any]] = {}
    reidentifiees: Dict[str, Tuple[Dict[str, Any], str]] = {}
    for fiche in retirees:
        trouvee = _meme_personne(fiche, index)
        if trouvee is None:
            radiees[fiche["entity_id"]] = fiche
        else:
            reidentifiees[fiche["entity_id"]] = trouvee
    compte["delisted"] = len(radiees)
    compte["reidentified"] = len(reidentifiees)

    now = maintenant or datetime.utcnow()
    evenements: List[AlertEvent] = []

    for alerte in _par_fiche(db, Alert, Alert.watchlist_entity_id, file_type, list(radiees),
                             Alert.status.in_(ALERT_OPEN_STATUSES), Alert.delisted_at.is_(None)):
        motif = _motif_de_radiation(radiees[alerte.watchlist_entity_id], file_type, lot, now)
        alerte.delisted_at = now
        alerte.lapsed_since = alerte.lapsed_since or now
        alerte.lapsed_reason = motif
        evenements.append(AlertEvent(
            alert_id=alerte.id, username=UTILISATEUR, action="DELISTED",
            detail=motif + " L'alerte reste ouverte : un humain la clôt (motif proposé)."))
        compte["open_alerts_delisted"] += 1

    if reidentifiees:
        deja = {(e.alert_id) for e in db.query(AlertEvent.alert_id).filter(
            AlertEvent.action == "REIDENTIFIED",
            AlertEvent.detail.like(f"%{nouveau_lot}%")).all()}
        for alerte in _par_fiche(db, Alert, Alert.watchlist_entity_id, file_type,
                                 list(reidentifiees), Alert.status.in_(ALERT_OPEN_STATUSES)):
            if alerte.id in deja:
                continue
            nouvelle, critere = reidentifiees[alerte.watchlist_entity_id]
            evenements.append(AlertEvent(
                alert_id=alerte.id, username=UTILISATEUR, action="REIDENTIFIED",
                detail=(f"Fiche {alerte.watchlist_entity_id} absente de la version "
                        f"{nouveau_lot}, qui ajoute {nouvelle['entity_id']} « "
                        f"{nouvelle.get('primary_name') or ''} » ({critere}) : identifiant "
                        f"vraisemblablement renommé par la source — aucune radiation "
                        f"annoncée. Vérifiez le rapprochement.")))

    a_notifier = []
    for mesure in _par_fiche(db, MesureDeGel, MesureDeGel.watchlist_entity_id, file_type,
                             list(radiees), MesureDeGel.statut != gels.STATUT_LEVEE,
                             MesureDeGel.radiation_constatee_le.is_(None)):
        motif = _motif_de_radiation(radiees[mesure.watchlist_entity_id], file_type, lot, now)
        if gels.signaler_radiation(db, mesure, detail=motif, maintenant=now):
            a_notifier.append(mesure)
            compte["gel_measures_flagged"] += 1

    db.add_all(evenements)
    db.commit()
    for mesure in a_notifier:
        gels.notifier_radiation(db, mesure)
    if compte["delisted"] or compte["reidentified"]:
        logger.info(f"Radiations {file_type} ({nouveau_lot}) : {compte['delisted']} fiche(s) "
                    f"radiée(s), {compte['reidentified']} re-identifiée(s), "
                    f"{compte['open_alerts_delisted']} alerte(s) ouverte(s) signalée(s), "
                    f"{compte['gel_measures_flagged']} mesure(s) de gel à examiner.")
    return compte


# ------------------------------------------------------------ caducite

def _clients_du_referentiel(db, ids: Set[str]) -> Dict[str, Dict[str, Any]]:
    """La fiche la plus recente de chaque client, dans le referentiel en
    production — la meme source que le re-criblage (`_client_dicts`)."""
    lots = {s.snapshot_id: s.uploaded_at for s in db.query(Snapshot).filter(
        Snapshot.file_type == "CLIENT_BASE", Snapshot.status == "READY").all()}
    if not lots or not ids:
        return {}
    retenues: Dict[str, Tuple[Any, Dict[str, Any]]] = {}
    for tranche in _tranches(sorted(ids)):
        for ligne in db.query(ClientEntity).filter(
                ClientEntity.snapshot_id.in_(list(lots)),
                ClientEntity.client_id.in_(tranche)).all():
            quand = lots.get(ligne.snapshot_id) or datetime.min
            garde = retenues.get(ligne.client_id)
            if garde is None or quand >= garde[0]:
                retenues[ligne.client_id] = (quand, {
                    c.name: getattr(ligne, c.name) for c in ligne.__table__.columns})
    return {cid: fiche for cid, (_q, fiche) in retenues.items()}


def examiner_les_caduques(db, file_type: str, fiches: List[Dict[str, Any]],
                          franchies: Set[Tuple[Any, Any]], *, version: str,
                          maintenant: Optional[datetime] = None) -> int:
    """
    Apres un passage qui a re-crible des fiches (`fiches` : celles qu'il vient
    de cribler, `franchies` : les couples (client, fiche) qu'il a trouves
    au-dessus du seuil) — re-criblage d'une mise en production, d'une
    correction ou d'un ajout manuel de fiche, lookback —, chaque alerte
    OUVERTE du criblage clients dont la fiche a ete re-criblee et dont le
    couple n'est PAS dans `franchies` :
    - est recalculee avec la fiche nouvelle — meme moteur, meme
      configuration que le re-criblage —, la decision journalisee comme toute
      autre ; l'alerte porte ce dernier score et ce dernier arbre ;
    - devient caduque, avec le motif de cloture propose.

    Ne touche pas : une alerte dont le client n'est pas au referentiel en
    production (criblage temps reel d'un prospect : rien ne le re-crible), ni
    un couple que le recalcul trouve encore au-dessus du seuil (le criblage ne
    l'a pas compare, cles de blocking disjointes : le dire caduque affirmerait
    un seuil qu'aucun criblage n'a mesure). Rend le nombre d'alertes rendues
    caduques.
    """
    from fiskr.config import config
    from fiskr.database import log_compliance_decisions, production_watchlist_reference
    from fiskr.empreintes import empreinte_client
    from fiskr.scoring import match_entities

    par_id = {f["entity_id"]: f for f in fiches if f.get("entity_id")}
    if not par_id:
        return 0

    def couple(alerte: Alert) -> Tuple[Any, ...]:
        # Une alerte de partie liee (fiskr/parties.py) se juge sur SA partie :
        # le re-criblage note (client, fiche, partie) quand une partie franchit.
        cle = (alerte.client_id, alerte.watchlist_entity_id)
        return cle + ((alerte.partie_liee_id,) if alerte.partie_liee_id is not None else ())

    # Les alertes ouvertes de la liste, rapprochees ICI des fiches criblees :
    # une requete quel que soit leur nombre. Le lookback en passe l'univers
    # entier (des centaines de milliers de fiches) : des tranches d'IN
    # auraient relu la file des alertes ouvertes un millier de fois.
    candidates = [a for a in db.query(Alert).filter(
        Alert.list_type == file_type,
        Alert.status.in_(ALERT_OPEN_STATUSES),
        (Alert.channel == "SCREENING") | (Alert.channel.is_(None))).all()
        if a.watchlist_entity_id in par_id
        and couple(a) not in franchies]
    if not candidates:
        return 0
    clients = _clients_du_referentiel(db, {a.client_id for a in candidates if a.client_id})
    from fiskr.database import PartieLiee
    from fiskr.parties import contexte, profil_de_criblage
    ids_parties = sorted({a.partie_liee_id for a in candidates if a.partie_liee_id is not None})
    parties = {p.id: p for p in db.query(PartieLiee).filter(
        PartieLiee.id.in_(ids_parties)).all()} if ids_parties else {}
    wl_version, wl_hash = production_watchlist_reference(db)
    now = maintenant or datetime.utcnow()
    caduques = 0
    for alerte in candidates:
        client = clients.get(alerte.client_id)
        if client is None:
            continue
        profil, partie = client, None
        if alerte.partie_liee_id is not None:
            ligne_partie = parties.get(alerte.partie_liee_id)
            # Partie que la source ne declare plus : le re-criblage ne l'a pas
            # confrontee, rien ne mesure qu'elle passe sous le seuil.
            if ligne_partie is None or ligne_partie.retiree_le is not None:
                continue
            partie = contexte(ligne_partie, alerte.client_name)
            profil = profil_de_criblage(ligne_partie)
        fiche = par_id[alerte.watchlist_entity_id]
        score = match_entities(profil, fiche, config)
        if score.get("status") == "ALERT":
            continue
        score["watchlist_entity"] = fiche
        score["client_fingerprint"] = empreinte_client(profil)
        ligne = log_compliance_decisions(db, profil, [(fiche, score)], wl_version, wl_hash,
                                         commit=False, partie=partie)[0]
        seuil = score.get("cut_off_applied")
        # « au re-criblage », pas « apres la mise a jour » : un lookback rend
        # aussi caduque une alerte dont la fiche n'a pas change (seuil releve).
        motif = (f"Correspondance sous le seuil au re-criblage de la fiche "
                 f"{alerte.watchlist_entity_id} (version {version}) : score "
                 f"{score.get('final_score', 0.0):.1f}"
                 + (f" pour un seuil de {float(seuil):.1f}" if seuil is not None else "")
                 + f" (audit #{ligne.id}).")
        # Deja caduque et au meme score : la ligne d'audit suffit, un second
        # evenement identique n'apprendrait rien a qui lit l'historique.
        nouvelle = alerte.lapsed_since is None or alerte.latest_score != score.get("final_score")
        alerte.latest_audit_id = ligne.id
        alerte.latest_score = score.get("final_score", 0.0)
        alerte.latest_at = now
        alerte.lapsed_since = alerte.lapsed_since or now
        alerte.lapsed_reason = motif
        if nouvelle:
            db.add(AlertEvent(alert_id=alerte.id, username=UTILISATEUR,
                              action="LAPSED",
                              detail=motif + " L'alerte reste ouverte : un humain la clôt "
                                             "(motif proposé)."))
        caduques += 1
    db.commit()
    return caduques

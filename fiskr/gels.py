"""
Le registre des mesures de gel : ce qui suit un vrai positif sur une liste de gel.

POURQUOI CE MODULE EXISTE
-------------------------
Releve en production (lecture seule, 19 octobre 2026) : deux alertes
confirmees sur une fiche OFAC, en priorite MEDIUM (echeance 120 h), closes
directement par la personne qui proposait et validait. Ensuite : aucune piece,
aucun projet de declaration, et un narratif redige dix-sept jours apres la
cloture. Le code ne prevoyait rien d'autre — `CLOSED_CONFIRMED` etait final, et
une alerte DGT se closait exactement comme une alerte PEP.

Or un vrai positif sur une liste de gel n'est pas la fin d'une instruction :
c'est le debut d'obligations datees (lignes directrices conjointes DGT/ACPR sur
le gel des avoirs, 18 mars 2026 — « LD » ci-dessous) :
  - geler sans delai, sans attendre ni autorisation ni confirmation de la DG
    Tresor (CMF L.562-4 ; LD §177) ;
  - DECLARER a la DG Tresor chaque action de mise en oeuvre (R.562-3 ; LD
    §180-181), « immediatement » (§182) ;
  - conserver les echanges avec la DG Tresor (§168) ;
  - controler les delais de declaration (§99) ;
  - lever la mesure selon une procedure ecrite (§87, §209-213).

Ce module tient ces obligations. Il ne transmet RIEN a la DG Tresor : il n'y a
pas d'API publique, et une transmission automatique serait une declaration que
personne n'aurait relue. Le produit prepare (projet imprimable), trace et
date ; l'humain transmet, puis joint la piece qui le prouve. Une mesure ne
passe « declaree » que sur cette piece : sans elle, le registre affirmerait
une transmission qu'il n'a pas constatee.

QUI DECIDE QU'UNE LISTE IMPOSE UN GEL
-------------------------------------
Le perimetre SANCTION (fiskr/perimeters.py), surcharges de l'exploitant
comprises — jamais une liste de types recopiee ici. Une liste rangee hors
sanction n'ouvre pas de mesure ; un type inconnu, si (meme regle que le
perimetre : le doute penche vers ce qui ne fait rien perdre).

UN CLIENT REPUTE DETENU OU CONTROLE
-----------------------------------
Un vrai positif sur une PARTIE LIEE du client (beneficiaire effectif,
mandataire, dirigeant, associe — fiskr/parties.py) n'ouvre pas de mesure a lui
seul (§144). Il en ouvre une quand les parties confirmees rendent le client
repute detenu (50 % cumules, §131 : gel) ou controle (une partie designee peut
engager ses fonds, §133 et §144 : suspension et interrogation de la DG
Tresor) — voir `ouvrir_si_detention`. Le cumul suit la chaine de detention
au-dela du premier niveau (fiskr/parties.py). La mesure porte son fondement
et le calcul qui l'etablit, chemins compris ; le registre la met en avant.

CE QUI N'EST PAS FAIT
---------------------
La declaration d'homonymie (LD §167 : alerte qu'on ne parvient pas a lever) a
son propre formulaire et son propre cycle de reponse de la DG Tresor. Elle
n'est pas couverte : le registre ne porte que les actions de mise en oeuvre
d'un gel CONFIRME.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fiskr.database import (
    Alert, AlertAttachment, AlertEvent, ClientEntity, MesureDeGel, Snapshot,
    WatchlistEntity, WATCHLIST_FILE_TYPES,
)

logger = logging.getLogger("fiskr.gels")

STATUT_A_DECLARER = "A_DECLARER"
STATUT_DECLAREE = "DECLAREE"
STATUT_LEVEE = "LEVEE"
STATUTS = (STATUT_A_DECLARER, STATUT_DECLAREE, STATUT_LEVEE)
STATUT_LABELS = {
    STATUT_A_DECLARER: "À déclarer",
    STATUT_DECLAREE: "Déclarée",
    STATUT_LEVEE: "Levée",
}

# Les actions de mise en oeuvre que la DG Tresor attend, une par une (LD §181).
# Chacune se declare : un credit recu sur un compte deja gele n'est pas couvert
# par la declaration du gel initial, c'est une mesure de plus.
ACTIONS = {
    "GEL": "Gel d'un compte, d'une opération ou d'un contrat",
    "CREDIT_COMPTE_GELE": "Opération portée au crédit d'un compte gelé",
    "SUSPENSION_MISE_A_DISPOSITION": "Suspension d'une mise à disposition de fonds",
    "REFUS_ENTREE_RELATION": "Refus d'entrée en relation ou d'opération occasionnelle",
    "CONTOURNEMENT": "Contournement ou tentative de contournement",
}
ACTION_PAR_DEFAUT = "GEL"
# Actions qui placent les avoirs DU CLIENT sous gel : elles seules passent sa
# fiche en « GELE ». Un refus d'entree en relation vise un prospect, une
# tentative de contournement peut venir d'un tiers.
ACTIONS_QUI_GELENT = ("GEL", "CREDIT_COMPTE_GELE")

# Ce qui met fin a une mesure (LD §209-212). La levee ne s'invente pas : elle
# suit un acte — abrogation, echeance d'un arrete national de six mois non
# renouvele, decision de justice, radiation de la liste.
MOTIFS_DE_LEVEE = {
    "ABROGATION": "Abrogation de la mesure (règlement, arrêté, résolution)",
    "ECHEANCE": "Échéance d'une mesure nationale non renouvelée",
    "DECISION_JURIDICTIONNELLE": "Annulation ou suspension par une juridiction",
    "RADIATION": "Radiation de la personne de la liste",
    "AUTRE": "Autre motif (à justifier)",
}

# Valeur ecrite dans `client_entities.client_status` : la forme que produit
# l'import du referentiel (il normalise en majuscules), pour qu'un re-import
# de la meme valeur se compare a l'identique.
STATUT_CLIENT_GELE = "GELE"

# Les deux boites de la DG Tresor, telles que son formulaire de declaration de
# gel les distingue. Le choix depend du regime ; Fiskr ne le devine pas.
ADRESSES_DGT = {
    "sanctions-gel-avoirs@dgtresor.gouv.fr":
        "Régimes « pays » ou thématiques (hors terrorisme) de l'UE et de l'ONU",
    "liste-nationale@dgtresor.gouv.fr":
        "Mesures de gel nationales et lutte contre le financement du terrorisme",
}

# Tolerance d'horloge pour une date saisie « maintenant » depuis un poste.
_AVANCE_TOLEREE = timedelta(minutes=5)
_MAX_LIGNES_DE_COMPTES = 50


class RefusDeGel(Exception):
    """Refus franc d'une operation du registre, avec le code HTTP a rendre."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ------------------------------------------------------------ ouverture

def est_due(alert: Alert, surcharges: Optional[Dict[str, str]] = None) -> bool:
    """Une mesure est due quand une alerte est CONFIRMEE sur une liste du
    perimetre SANCTION. C'est un du, pas une option : rien ne la desactive.

    Sauf sur une alerte de PARTIE LIEE (fiskr/parties.py) : confirmer que le
    beneficiaire effectif d'ACME est la personne designee ne gele pas ACME a
    lui seul — « dans les autres cas, l'organisme financier n'a pas a geler
    les fonds de la personne morale (par exemple, lorsque le beneficiaire
    effectif n'est qu'actionnaire minoritaire) » (LD §144). Ce qui est du se
    decide sur la detention ou le controle du client (`ouvrir_si_detention`)."""
    from fiskr.perimeters import est_sanction
    return (alert.partie_liee_id is None and alert.status == "CLOSED_CONFIRMED"
            and est_sanction(alert.list_type, surcharges))


def _snapshots_de_listes(db) -> List[str]:
    return [s.snapshot_id for s in db.query(Snapshot.snapshot_id).filter(
        Snapshot.status == "READY", Snapshot.file_type.in_(WATCHLIST_FILE_TYPES)).all()]


def fiche_en_production(db, entity_id: Optional[str]) -> Optional[WatchlistEntity]:
    """La fiche listee telle que les listes en production la portent encore.

    Les identifiants de lots en production d'abord, la fiche ensuite : la table
    des fiches compte des millions de lignes, et l'index composite
    (snapshot_id, entity_id) ne sert que si le lot est dans le filtre."""
    if not entity_id:
        return None
    lots = _snapshots_de_listes(db)
    if not lots:
        return None
    return db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_(lots),
        WatchlistEntity.entity_id == entity_id,
        WatchlistEntity.excluded.isnot(True),
    ).first()


def _ouvrir(db, alert: Alert, type_action: str, username: str,
            depuis: Optional[datetime], client_id: Optional[str] = None,
            client_name: Optional[str] = None) -> MesureDeGel:
    """`client_id` : le client dont les avoirs sont en cause, quand ce n'est
    pas celui de l'alerte — un client repute detenu par la chaine de detention
    d'un AUTRE client (fiskr/parties.py, `partie_client_id`)."""
    fiche = fiche_en_production(db, alert.watchlist_entity_id)
    autre = client_id is not None and client_id != alert.client_id
    mesure = MesureDeGel(
        alert_id=alert.id, type_action=type_action, statut=STATUT_A_DECLARER,
        client_id=client_id if autre else alert.client_id,
        client_name=(client_name or client_id) if autre else alert.client_name,
        watchlist_entity_id=alert.watchlist_entity_id,
        watchlist_name=alert.watchlist_name, list_type=alert.list_type,
        programmes=(fiche.sanction_programs if fiche is not None else None),
        reference_designation=(fiche.official_reference if fiche is not None else None),
        ouverte_le=depuis or datetime.utcnow(), ouverte_par=username,
    )
    db.add(mesure)
    db.flush()
    db.add(AlertEvent(
        alert_id=alert.id, username=username, action="GEL_OUVERT",
        detail=(f"Mesure de gel #{mesure.id} ouverte ({ACTIONS[type_action]})"
                + (f" pour le client {mesure.client_name}" if autre else "")
                + " : déclaration à la DG Trésor due immédiatement "
                  "(CMF L.562-4, R.562-3 ; LD DGT/ACPR §182).")))
    return mesure


def ouvrir_si_due(db, alert: Alert, username: str) -> Optional[MesureDeGel]:
    """
    Appele a CHAQUE confirmation d'alerte (avant le commit de la cloture) :
    ouvre la mesure « a declarer » si la liste impose un gel. Idempotent — une
    alerte deja pourvue n'en recoit pas une seconde.

    Le delai court depuis la confirmation (`decided_at`) : c'est le moment ou
    l'analyse « a permis de s'assurer » de l'identite (LD §182).
    """
    from fiskr.settings import perimeter_overrides
    if alert.partie_liee_id is not None:
        return ouvrir_si_detention(db, alert, username)
    surcharges = perimeter_overrides(db)
    if not est_due(alert, surcharges):
        return None
    if db.query(MesureDeGel.id).filter(MesureDeGel.alert_id == alert.id).first():
        return None
    mesure = _ouvrir(db, alert, ACTION_PAR_DEFAUT, username, alert.decided_at)
    # Le client lui-meme est designe : les clients dont une partie EST ce
    # client (fiskr/parties.py, `partie_client_id`) le comptent desormais dans
    # leur chaine de detention — meme decision, ici.
    db.flush()
    _suites_dans_les_chaines(db, alert, username, surcharges)
    return mesure


# ------------------------------------------------------------ detention reputee

def mesure_de_detention_en_vigueur(db, client_id: Optional[str]) -> Optional[MesureDeGel]:
    """La mesure ouverte parce que ce client est repute detenu ou controle, si
    elle n'est pas levee : une seule a la fois — un second vrai positif sur une
    autre partie la confirme, il n'en ouvre pas une autre."""
    if not client_id:
        return None
    return db.query(MesureDeGel).filter(
        MesureDeGel.client_id == client_id, MesureDeGel.fondement.isnot(None),
        MesureDeGel.statut != STATUT_LEVEE).order_by(MesureDeGel.id.asc()).first()


def ouvrir_si_detention(db, alert: Alert, username: str) -> Optional[MesureDeGel]:
    """
    Confirmation d'une alerte de PARTIE LIEE (fiskr/parties.py) : le client
    est-il desormais repute detenu ou controle par une personne designee ?
    L'historique de l'alerte le DIT dans les deux cas — un vrai positif sur une
    liste de gel sans mesure doit se lire comme une decision, pas comme un oubli.

    LE GESTE, SELON LE FONDEMENT
    - Detention reputee, 50 % ou plus cumules (LD §131) : « les fonds et
      ressources economiques de ces [entites] doivent etre geles » ; l'exemple
      des LD est celui-ci (30 % + 25 %, « devrait etre gelee »). Mesure GEL :
      le client passe GELE des que la date d'effet est posee, et la
      declaration a la DG Tresor est due immediatement (§180-182).
    - Controle par une partie designee qui peut engager les fonds (§133,
      §144) : les LD ne disent pas « gelez » mais « il s'abstient d'executer
      l'operation et interroge la DG Tresor sur son traitement ». Mesure
      SUSPENSION D'UNE MISE A DISPOSITION — une des actions a declarer (§181) :
      elle ne marque PAS le client gele, et sa declaration est l'interrogation
      de la DG Tresor. Si la DG Tresor repond « gelez », l'humain requalifie
      la mesure (action GEL) tant qu'elle n'est pas declaree, ou en ouvre une
      seconde.
    Ni l'un ni l'autre : rien n'est ouvert (§144, actionnaire minoritaire).
    En cas de doute, l'analyste ouvre lui-meme la suspension (`ouvrir`).

    AU-DELA DU PREMIER NIVEAU : le cumul est celui de la chaine de detention
    (fiskr/parties.py, « LA CHAINE DE DETENTION ») — directe et indirecte, au
    MEME point de decision. Et quand le client de l'alerte est lui-meme une
    partie d'autres clients (la holding est cliente), ceux-ci sont decides ici
    aussi (`_suites_dans_les_chaines`) : la mesure porte LEUR nom et CETTE
    alerte, celle qui a etabli la personne designee.
    """
    from fiskr.perimeters import est_sanction
    from fiskr.settings import perimeter_overrides
    surcharges = perimeter_overrides(db)
    if alert.status != "CLOSED_CONFIRMED" or not est_sanction(alert.list_type, surcharges):
        return None
    # La session de l'API ne vide pas d'elle-meme (autoflush coupe) : sans
    # cela, l'evaluation relirait CETTE alerte encore ouverte, et le 25 % qui
    # franchit le seuil ne serait jamais compte.
    db.flush()
    mesure = _decider_la_detention(db, alert, username, surcharges,
                                   alert.client_id, alert.client_name, dans_une_chaine=False)
    _suites_dans_les_chaines(db, alert, username, surcharges)
    return mesure


def _decider_la_detention(db, alert: Alert, username: str, surcharges: Optional[Dict[str, str]],
                          client_id: Optional[str], client_name: Optional[str], *,
                          dans_une_chaine: bool) -> Optional[MesureDeGel]:
    """La decision pour UN client, au vu de l'evaluation de sa chaine
    (fiskr/parties.py) : la mesure, ou pourquoi il n'y en a pas. Pour le
    client de l'alerte, l'historique le dit toujours ; pour un client dont la
    chaine passe par lui (`dans_une_chaine`), seulement ce qui demande une
    action — une mesure, ou un calcul qui n'a pas pu trancher."""
    from fiskr import parties as parties_liees
    evaluation = parties_liees.evaluer_la_detention(db, client_id, surcharges, client_name)
    phrase = parties_liees.phrase_de_detention(evaluation, client_name or client_id)
    if not evaluation["reputee"]:
        if dans_une_chaine and not evaluation["signalements"]:
            return None
        db.add(AlertEvent(
            alert_id=alert.id, username=username, action="DETENTION_NON_ETABLIE",
            detail=(f"{phrase} Pas de gel des avoirs du client à ce titre (LD §144). En cas "
                    f"de doute sur le contrôle, suspendez et interrogez la DG Trésor : "
                    f"mesure « suspension d'une mise à disposition ».")))
        return None
    deja = mesure_de_detention_en_vigueur(db, client_id)
    if deja is not None:
        db.add(AlertEvent(
            alert_id=alert.id, username=username, action="DETENTION_REPUTEE",
            detail=(f"{phrase} Mesure de gel #{deja.id} déjà ouverte à ce titre (alerte "
                    f"#{deja.alert_id}) : aucune autre n'est ouverte.")))
        return None
    detention = evaluation["fondement"] == parties_liees.FONDEMENT_DETENTION
    mesure = _ouvrir(db, alert, "GEL" if detention else "SUSPENSION_MISE_A_DISPOSITION",
                     username, alert.decided_at, client_id=client_id, client_name=client_name)
    mesure.fondement = evaluation["fondement"]
    mesure.detention = evaluation
    geste = ("geler les avoirs du client et déclarer le gel à la DG Trésor (LD §131, §180-182)"
             if detention else
             "s'abstenir d'exécuter toute opération du client et interroger la DG Trésor sur "
             "son traitement (LD §144) — la déclaration de cette mesure est cette interrogation")
    db.add(AlertEvent(
        alert_id=alert.id, username=username, action="DETENTION_REPUTEE",
        detail=f"{phrase} Mesure de gel #{mesure.id} ouverte : {geste}."))
    db.flush()
    return mesure


def _suites_dans_les_chaines(db, alert: Alert, username: str,
                             surcharges: Optional[Dict[str, str]]) -> List[MesureDeGel]:
    """
    Le client de l'alerte EST une partie d'autres clients (fiskr/parties.py,
    `partie_client_id` : la holding est cliente) : ce vrai positif peut les
    rendre reputes detenus ou controles. Meme decision que pour lui
    (`_decider_la_detention`), client par client, de proche en proche — la
    mesure porte le client dont les avoirs sont en cause et cette alerte.
    """
    from fiskr import parties as parties_liees
    ouvertes = []
    for client_id in parties_liees.clients_detenus_par(db, alert.client_id):
        mesure = _decider_la_detention(db, alert, username, surcharges, client_id,
                                       parties_liees.nom_du_client(db, client_id),
                                       dans_une_chaine=True)
        if mesure is not None:
            ouvertes.append(mesure)
    return ouvertes


def ouvrir(db, alert: Alert, type_action: str, username: str) -> MesureDeGel:
    """Mesure SUPPLEMENTAIRE sur une alerte confirmee : un credit arrive sur le
    compte gele, une tentative de contournement — chacune se declare (§181).

    Sur une alerte de partie liee confirmee, c'est aussi le geste du DOUTE
    (LD §144 : « en cas de doute, il s'abstient d'executer l'operation et
    interroge la DG Tresor ») : l'analyste ouvre la suspension que la
    detention reputee n'a pas ouverte d'elle-meme."""
    from fiskr.perimeters import est_sanction
    from fiskr.settings import perimeter_overrides
    code = (type_action or "").strip().upper()
    if code not in ACTIONS:
        raise RefusDeGel(f"Action inconnue ({', '.join(ACTIONS)}).")
    surcharges = perimeter_overrides(db)
    partie_confirmee = (alert.partie_liee_id is not None and alert.status == "CLOSED_CONFIRMED"
                        and est_sanction(alert.list_type, surcharges))
    if not (est_due(alert, surcharges) or partie_confirmee):
        if alert.status != "CLOSED_CONFIRMED":
            raise RefusDeGel("Seule une alerte confirmée (vrai positif) porte une mesure de gel.", 409)
        raise RefusDeGel("Cette liste est hors du périmètre SANCTION : elle n'impose aucun gel, "
                         "donc aucune déclaration à la DG Trésor.", 409)
    # La PREMIERE mesure d'une alerte est celle que la confirmation aurait du
    # ouvrir — cas des vrais positifs confirmes avant le registre (les deux
    # alertes OFAC de production). Son delai court depuis la confirmation, pas
    # depuis aujourd'hui : la declaration est due depuis ce jour-la, et le
    # registre ne doit pas rajeunir un retard. Une mesure suivante (un credit
    # recu plus tard) court depuis son ouverture.
    premiere = not db.query(MesureDeGel.id).filter(MesureDeGel.alert_id == alert.id).first()
    return _ouvrir(db, alert, code, username, alert.decided_at if premiere else None)


def alertes_confirmees_sans_mesure(db, plafond: int = 2000) -> List[Alert]:
    """Vrais positifs du perimetre SANCTION qui n'ont aucune mesure au
    registre — typiquement ceux confirmes avant qu'il n'existe. Le perimetre
    se juge en Python (surcharges, types inconnus) : la requete ne retient que
    les confirmations sans mesure, une population faible par nature."""
    from fiskr.settings import perimeter_overrides
    surcharges = perimeter_overrides(db)
    avec_mesure = db.query(MesureDeGel.alert_id)
    candidates = db.query(Alert).filter(
        Alert.status == "CLOSED_CONFIRMED", ~Alert.id.in_(avec_mesure)) \
        .order_by(Alert.decided_at.asc()).limit(plafond).all()
    return [a for a in candidates if est_due(a, surcharges)]


# ------------------------------------------------------------ saisie

def _date_saisie(valeur: Any, champ: str) -> Optional[datetime]:
    """ISO 8601 -> datetime UTC naif. Une date future est refusee : on ne
    trace pas ce qui n'a pas encore eu lieu."""
    if valeur in (None, ""):
        return None
    if isinstance(valeur, datetime):
        date = valeur
    else:
        try:
            date = datetime.fromisoformat(str(valeur).strip().replace("Z", "+00:00"))
        except ValueError:
            raise RefusDeGel(f"{champ} illisible (ISO 8601 attendu).")
    if date.tzinfo is not None:
        date = date.astimezone(timezone.utc).replace(tzinfo=None)
    if date > datetime.utcnow() + _AVANCE_TOLEREE:
        raise RefusDeGel(f"{champ} dans le futur : on ne trace que ce qui a eu lieu.")
    return date


def lire_comptes(texte: Optional[str]) -> List[Dict[str, str]]:
    """
    Les comptes et montants geles, en texte libre STRUCTURE : une ligne par
    compte, champs separes par « ; », dans l'ordre des rubriques du formulaire
    de la DG Tresor (« Identification des fonds geles ») :

        numéro ; nature ; devise ; montant à la date du gel ; montant à la date de la déclaration

    Les montants restent du texte, tels que saisis : « 1 234,56 » relu par un
    flottant deviendrait une autre somme, et c'est la somme declaree qui
    compte. Une ligne sans numero de compte est refusee — c'est, avec le nom
    et les montants, l'information minimale du formulaire.
    """
    lignes = [l.strip() for l in (texte or "").splitlines() if l.strip()]
    if len(lignes) > _MAX_LIGNES_DE_COMPTES:
        raise RefusDeGel(f"Au plus {_MAX_LIGNES_DE_COMPTES} comptes par mesure.")
    comptes = []
    for numero_de_ligne, ligne in enumerate(lignes, start=1):
        champs = [c.strip() for c in ligne.split(";")]
        if not champs[0]:
            raise RefusDeGel(f"Comptes, ligne {numero_de_ligne} : numéro de compte manquant.")
        champs += [""] * (5 - len(champs))
        comptes.append({
            "numero": champs[0][:100], "nature": champs[1][:100],
            "devise": champs[2][:10], "montant_gel": champs[3][:50],
            "montant_declaration": champs[4][:50],
        })
    return comptes


def _lignes_client(db, client_id: Optional[str]) -> List[ClientEntity]:
    """Fiches du client dans le referentiel EN PRODUCTION (la plus recente
    d'abord). Une partie de paiement (« TXN:… ») n'a pas de fiche."""
    if not client_id or str(client_id).startswith("TXN:"):
        return []
    lots = {s.snapshot_id: s.uploaded_at for s in db.query(Snapshot).filter(
        Snapshot.file_type == "CLIENT_BASE", Snapshot.status == "READY").all()}
    if not lots:
        return []
    lignes = db.query(ClientEntity).filter(
        ClientEntity.snapshot_id.in_(list(lots)), ClientEntity.client_id == client_id).all()
    return sorted(lignes, key=lambda l: lots.get(l.snapshot_id) or datetime.min, reverse=True)


def _est_gele(statut: Optional[str]) -> bool:
    return str(statut or "").strip().upper() == STATUT_CLIENT_GELE


def _tient_le_client():
    """Une mesure TIENT son client gele tant qu'elle n'est pas levee et qu'elle
    a inscrit le gel sur sa fiche. Une seule definition : la levee, l'import
    du referentiel et le webhook d'upsert la lisent."""
    return (MesureDeGel.statut != STATUT_LEVEE) & MesureDeGel.client_gele_ecrit.is_(True)


def _autres_gels_en_vigueur(db, mesure: MesureDeGel) -> List[MesureDeGel]:
    return db.query(MesureDeGel).filter(
        MesureDeGel.client_id == mesure.client_id, MesureDeGel.id != mesure.id,
        _tient_le_client()).all()


def mesures_qui_tiennent(db, client_id: Optional[str]) -> List[MesureDeGel]:
    """
    Les mesures en vigueur qui tiennent ce client gele.

    Le webhook `client-upsert` reecrivait `client_status` comme n'importe quel
    champ : un SI amont qui renvoie « actif » — ou qui n'envoie pas de statut
    du tout — degelait le client sans un mot, pendant que le registre portait
    une mesure en vigueur. Tant que cette liste n'est pas vide, aucune entree
    ne fait sortir le client de GELE ; seule la levee (`lever`) rend le statut.
    """
    if not client_id:
        return []
    return db.query(MesureDeGel).filter(
        MesureDeGel.client_id == client_id, _tient_le_client()).order_by(MesureDeGel.id).all()


def _geler_client(db, mesure: MesureDeGel) -> Optional[str]:
    """Passe la fiche du client en GELE si le gel est effectif. Rend un
    avertissement quand il n'y a rien a ecrire — jamais un silence."""
    if mesure.type_action not in ACTIONS_QUI_GELENT or mesure.date_effet is None:
        return None
    lignes = _lignes_client(db, mesure.client_id)
    if not lignes:
        return ("Aucune fiche de ce client dans le référentiel en production : "
                "son statut « gelé » n'a pas pu être inscrit.")
    if not mesure.client_gele_ecrit:
        non_geles = [l for l in lignes if not _est_gele(l.client_status)]
        if non_geles:
            mesure.statut_client_avant = non_geles[0].client_status
        else:
            # Deja gele par une autre mesure : son « avant » est le vrai avant
            autre = next((m for m in _autres_gels_en_vigueur(db, mesure)), None)
            mesure.statut_client_avant = autre.statut_client_avant if autre else None
    for ligne in lignes:
        ligne.client_status = STATUT_CLIENT_GELE
    mesure.client_gele_ecrit = True
    return None


def _degeler_client(db, mesure: MesureDeGel) -> Optional[str]:
    """Rend au client son statut d'avant le gel — sauf si une AUTRE mesure le
    tient encore gele, ce qui est dit plutot que tranche en silence."""
    if not mesure.client_gele_ecrit:
        return None
    encore = _autres_gels_en_vigueur(db, mesure)
    if encore:
        return (f"Le client reste gelé : mesure(s) "
                f"{', '.join('#' + str(m.id) for m in encore)} toujours en vigueur.")
    for ligne in _lignes_client(db, mesure.client_id):
        if _est_gele(ligne.client_status):
            ligne.client_status = mesure.statut_client_avant
    return None


def reappliquer_aux_clients(db, snapshot_id: str) -> int:
    """
    Un import du referentiel ne degele personne. Le SI amont ignore le
    registre : sa colonne `client_status` dirait « ACTIF » d'un client dont les
    avoirs sont geles, et le prochain import nocturne effacerait l'inscription.
    Appele apres chaque mise en production d'un CLIENT_BASE. Rend le nombre de
    fiches regelees (le commit est a l'appelant).
    """
    en_vigueur = db.query(MesureDeGel).filter(_tient_le_client()).all()
    ids = sorted({m.client_id for m in en_vigueur if m.client_id})
    if not ids:
        return 0
    lignes = db.query(ClientEntity).filter(
        ClientEntity.snapshot_id == snapshot_id, ClientEntity.client_id.in_(ids)).all()
    for ligne in lignes:
        ligne.client_status = STATUT_CLIENT_GELE
    return len(lignes)


def _journal(db, mesure: MesureDeGel, username: str, action: str, detail: str) -> None:
    db.add(AlertEvent(alert_id=mesure.alert_id, username=username, action=action, detail=detail))


def enregistrer_gel(db, mesure: MesureDeGel, *, username: str,
                    type_action: Optional[str] = None, date_effet: Any = None,
                    comptes: Optional[str] = None,
                    precisions: Optional[str] = None) -> List[str]:
    """
    Le gel EFFECTIF : action, date d'effet, comptes et montants. Le client
    passe « GELE » des que la date d'effet est posee sur une action qui gele.

    Une mesure declaree ne se modifie plus : ce qui a ete declare a la DG
    Tresor est ce qui a ete declare. Un fait nouveau est une mesure nouvelle.
    """
    if mesure.statut != STATUT_A_DECLARER:
        raise RefusDeGel("Mesure déjà déclarée (ou levée) : elle ne se modifie plus. "
                         "Un fait nouveau (crédit, contournement) s'enregistre comme une "
                         "nouvelle mesure.", 409)
    avertissements: List[str] = []
    changements = []
    if type_action is not None:
        code = type_action.strip().upper()
        if code not in ACTIONS:
            raise RefusDeGel(f"Action inconnue ({', '.join(ACTIONS)}).")
        if code != mesure.type_action:
            changements.append(f"action : {ACTIONS[mesure.type_action]} → {ACTIONS[code]}")
            mesure.type_action = code
    if date_effet is not None:
        date = _date_saisie(date_effet, "Date d'effet")
        if date != mesure.date_effet:
            changements.append(f"date d'effet : {date.strftime('%d/%m/%Y %H:%M') if date else '—'} UTC")
            mesure.date_effet = date
    if comptes is not None and (comptes.strip() or None) != mesure.comptes_texte:
        mesure.comptes = lire_comptes(comptes)
        mesure.comptes_texte = comptes.strip() or None
        changements.append(f"{len(mesure.comptes)} compte(s)")
    if precisions is not None and (precisions.strip()[:4000] or None) != mesure.precisions:
        mesure.precisions = precisions.strip()[:4000] or None
        changements.append("précisions")
    if mesure.client_gele_ecrit and (mesure.type_action not in ACTIONS_QUI_GELENT
                                     or mesure.date_effet is None):
        # L'action requalifiee ne gele plus (ex. refus d'entree en relation) :
        # l'inscription faite au nom de cette mesure ne tient plus.
        avertissement = _degeler_client(db, mesure)
        mesure.client_gele_ecrit = False
        if avertissement:
            avertissements.append(avertissement)
    else:
        avertissement = _geler_client(db, mesure)
        if avertissement:
            avertissements.append(avertissement)
    if changements:
        _journal(db, mesure, username, "GEL_MIS_A_JOUR",
                 f"Mesure de gel #{mesure.id} : " + " ; ".join(changements) + ".")
    return avertissements


def verifier_declaration(db, mesure: MesureDeGel, *, declaree_le: Any = None,
                         canal: Optional[str] = None) -> Dict[str, Any]:
    """Tout ce qui peut refuser une declaration, verifie AVANT que la piece ne
    soit ecrite sur le disque : un refus apres coup laisserait un fichier
    qu'aucune ligne ne reference."""
    if mesure.statut != STATUT_A_DECLARER:
        raise RefusDeGel(f"Mesure déjà {STATUT_LABELS[mesure.statut].lower()}.", 409)
    if mesure.date_effet is None:
        raise RefusDeGel("Renseignez d'abord la date d'effet du gel (ou de l'action) : "
                         "la DG Trésor la demande, et une déclaration sans elle "
                         "ne dit pas ce qui a été fait.", 409)
    date = _date_saisie(declaree_le, "Date de déclaration") or datetime.utcnow()
    alerte_creee = db.query(Alert.created_at).filter(Alert.id == mesure.alert_id).scalar()
    if alerte_creee and date < alerte_creee:
        raise RefusDeGel("Date de déclaration antérieure à l'alerte elle-même.")
    adresse = (canal or "").strip().lower() or None
    if adresse and adresse not in ADRESSES_DGT:
        raise RefusDeGel(f"Adresse de la DG Trésor inconnue ({', '.join(ADRESSES_DGT)}).")
    return {"declaree_le": date, "canal": adresse}


def enregistrer_declaration(db, mesure: MesureDeGel, *, username: str,
                            piece: Optional[AlertAttachment], verifiee: Dict[str, Any],
                            reference: Optional[str] = None) -> None:
    """
    La declaration a la DG Tresor, faite par un humain hors de Fiskr, et la
    piece qui la prouve (accuse de reception, ou a defaut copie du courriel
    envoye — LD §168 : conserver les echanges). La piece est exigee : sans
    elle, « declaree » serait une affirmation que le produit n'a pas verifiee.
    `verifiee` est le retour de `verifier_declaration`.
    """
    if piece is None:
        raise RefusDeGel("La pièce de transmission est obligatoire (accusé de réception "
                         "de la DG Trésor, ou copie du courriel envoyé).")
    date, adresse = verifiee["declaree_le"], verifiee["canal"]
    mesure.statut = STATUT_DECLAREE
    mesure.declaree_le = date
    mesure.declaree_par = username
    mesure.reference_dgt = (reference or "").strip()[:255] or None
    mesure.canal_dgt = adresse
    mesure.piece_declaration_id = piece.id
    from fiskr.kpi import delai_de_declaration_secondes
    delai = delai_de_declaration_secondes(mesure.ouverte_le, date)
    _journal(db, mesure, username, "GEL_DECLARE",
             f"Mesure de gel #{mesure.id} déclarée à la DG Trésor le "
             f"{date.strftime('%d/%m/%Y %H:%M')} UTC"
             + (f" ({adresse})" if adresse else "")
             + (f", réf. {mesure.reference_dgt}" if mesure.reference_dgt else "")
             + f" — pièce : {piece.file_name} — délai depuis la confirmation : "
             + _duree(delai) + ".")


def lever(db, mesure: MesureDeGel, *, username: str, motif: str, justification: str,
          levee_le: Any = None) -> List[str]:
    """
    Levee de la mesure (LD §209-213) : elle suit un acte (abrogation, echeance,
    decision de justice, radiation) et s'applique « sans attendre une
    confirmation de la DG Tresor » (§213).

    Une mesure NON declaree ne se leve pas : la levee eteindrait l'alarme de
    retard et enterrerait une declaration qui reste due (§181).
    """
    if mesure.statut == STATUT_LEVEE:
        raise RefusDeGel("Mesure déjà levée.", 409)
    if mesure.statut != STATUT_DECLAREE:
        raise RefusDeGel("Une mesure non déclarée ne se lève pas : la déclaration à la "
                         "DG Trésor reste due (LD §181). Déclarez-la d'abord.", 409)
    code = (motif or "").strip().upper()
    if code not in MOTIFS_DE_LEVEE:
        raise RefusDeGel(f"Motif de levée inconnu ({', '.join(MOTIFS_DE_LEVEE)}).")
    texte = (justification or "").strip()
    if not texte:
        raise RefusDeGel("La justification de la levée est obligatoire (référence de "
                         "l'acte : règlement, arrêté, décision).")
    date = _date_saisie(levee_le, "Date de levée") or datetime.utcnow()
    if mesure.date_effet and date < mesure.date_effet:
        raise RefusDeGel("Date de levée antérieure à la date d'effet du gel.")
    avertissements: List[str] = []
    # La fiche est-elle toujours dans les listes en production ? Une decision
    # de justice peut preceder la mise a jour des listes (§212) : on ne refuse
    # pas, on le DIT.
    if fiche_en_production(db, mesure.watchlist_entity_id) is not None:
        avertissements.append(
            "La fiche listée est toujours présente dans les listes en production : "
            "vérifiez que l'acte de levée est bien en vigueur.")
    mesure.statut = STATUT_LEVEE
    mesure.levee_le = date
    mesure.levee_par = username
    mesure.motif_levee = code
    mesure.justification_levee = texte[:4000]
    avertissement = _degeler_client(db, mesure)
    if avertissement:
        avertissements.append(avertissement)
    _journal(db, mesure, username, "GEL_LEVE",
             f"Mesure de gel #{mesure.id} levée le {date.strftime('%d/%m/%Y %H:%M')} UTC — "
             f"{MOTIFS_DE_LEVEE[code]} : {texte}"
             + (" — " + " ".join(avertissements) if avertissements else ""))
    return avertissements


# ------------------------------------------------------------ radiation

# Filtre du registre : mesures en vigueur dont la fiche a ete radiee
FILTRE_RADIATION = "RADIATION_A_EXAMINER"


def radiation_a_examiner(mesure: MesureDeGel) -> bool:
    return mesure.radiation_constatee_le is not None and mesure.statut != STATUT_LEVEE


def signaler_radiation(db, mesure: MesureDeGel, *, detail: str,
                       maintenant: Optional[datetime] = None) -> bool:
    """
    La fiche de la mesure n'est plus dans sa liste en production (constate par
    fiskr/caducite.py a la mise en production d'une version) : la mesure
    devient « radiation a examiner ».

    RIEN n'est leve. La levee suit un acte (LD §209-213), et une radiation
    d'une liste n'en est pas un a elle seule : la personne peut rester designee
    par un autre regime, ou la source avoir seulement change d'identifiant.
    Le registre le dit et date le constat — filtrer « sans delai » a
    l'abrogation (§123) commence par le savoir ; l'humain instruit la levee
    selon la procedure ecrite (§87). Signale une fois (rend False sinon).
    """
    if mesure.statut == STATUT_LEVEE or mesure.radiation_constatee_le is not None:
        return False
    mesure.radiation_constatee_le = maintenant or datetime.utcnow()
    mesure.radiation_detail = (detail or "")[:4000]
    _journal(db, mesure, "systeme", "GEL_RADIATION_A_EXAMINER",
             f"Mesure de gel #{mesure.id} : {detail} Levée à instruire (LD DGT/ACPR §87, "
             f"§123) — vérifiez l'acte d'abrogation et qu'aucun autre régime ne désigne "
             f"encore la personne. Rien n'est levé automatiquement.")
    return True


def notifier_radiation(db, mesure: MesureDeGel) -> None:
    """Notification IMMEDIATE (apres le commit de l'appelant) : un client gele
    sur une designation abrogee est une atteinte a ses droits qui court."""
    from fiskr.notifier import emit
    emit(db, "gel_radiation_a_examiner", {
        "Mesure": f"#{mesure.id}", "Alerte": mesure.alert_id,
        "Client": mesure.client_name, "Fiche listée": mesure.watchlist_name,
        "Liste": mesure.list_type or "—",
        "Statut": STATUT_LABELS.get(mesure.statut, mesure.statut),
        "Client gelé": "oui" if mesure.client_gele_ecrit else "non",
        "Constat": mesure.radiation_detail or "—",
        "À faire": ("Instruire la levée selon la procédure écrite (LD DGT/ACPR §87) — "
                    "rien n'est levé automatiquement."),
    })


# ------------------------------------------------------------ delai

def _duree(secondes: Optional[int]) -> str:
    if secondes is None:
        return "—"
    if secondes < 3600:
        return f"{max(1, secondes // 60)} min"
    if secondes < 48 * 3600:
        return f"{secondes / 3600:.1f} h".replace(".", ",")
    return f"{secondes / 86400:.1f} j".replace(".", ",")


def mesures_en_retard(db, maintenant: Optional[datetime] = None) -> List[MesureDeGel]:
    """Mesures encore a declarer au-dela du seuil — la definition du retard
    est celle de fiskr/kpi.py, lue ici telle quelle."""
    from fiskr.kpi import declaration_en_retard
    from fiskr.settings import gel_declaration_delai_heures
    now = maintenant or datetime.utcnow()
    seuil = gel_declaration_delai_heures(db)
    candidates = db.query(MesureDeGel).filter(
        MesureDeGel.statut == STATUT_A_DECLARER,
        MesureDeGel.ouverte_le < now - timedelta(hours=seuil)).all()
    return [m for m in candidates
            if declaration_en_retard(m.statut, m.ouverte_le, m.declaree_le, seuil, now)]


def detecter_retards(db, maintenant: Optional[datetime] = None) -> int:
    """
    Signale UNE fois chaque mesure non declaree au-dela du seuil : evenement
    immediat `gel_declaration_en_retard` et trace au journal de l'alerte.
    Rien n'est emis pour ce qui va bien — une mesure dans le delai ne dit rien.
    """
    from fiskr.notifier import emit
    from fiskr.kpi import delai_de_declaration_secondes
    from fiskr.settings import gel_declaration_delai_heures
    now = maintenant or datetime.utcnow()
    seuil = gel_declaration_delai_heures(db)
    signalees = 0
    for mesure in mesures_en_retard(db, now):
        if mesure.retard_signale_le is not None:
            continue
        delai = delai_de_declaration_secondes(mesure.ouverte_le, None, now)
        mesure.retard_signale_le = now
        _journal(db, mesure, "systeme", "GEL_DECLARATION_EN_RETARD",
                 f"Mesure de gel #{mesure.id} non déclarée à la DG Trésor "
                 f"{_duree(delai)} après la confirmation (seuil : {seuil} h).")
        db.commit()
        emit(db, "gel_declaration_en_retard", {
            "Mesure": f"#{mesure.id}", "Alerte": mesure.alert_id,
            "Client": mesure.client_name, "Fiche listée": mesure.watchlist_name,
            "Liste": mesure.list_type or "—", "Action": ACTIONS.get(mesure.type_action),
            "Confirmée le": mesure.ouverte_le.strftime("%d/%m/%Y %H:%M") + " UTC",
            "Délai écoulé": _duree(delai), "Seuil": f"{seuil} h",
            "Obligation": "Déclaration « immédiate » à la DG Trésor (CMF L.562-4 ; LD DGT/ACPR §182).",
        })
        signalees += 1
    return signalees


# ------------------------------------------------------------ lecture

def serialiser(db, mesure: MesureDeGel, seuil_heures: Optional[int] = None,
               maintenant: Optional[datetime] = None,
               pieces: Optional[Dict[int, AlertAttachment]] = None) -> Dict[str, Any]:
    from fiskr.kpi import declaration_en_retard, delai_de_declaration_secondes
    from fiskr.preuves import piece_presente
    from fiskr.settings import gel_declaration_delai_heures
    now = maintenant or datetime.utcnow()
    seuil = seuil_heures if seuil_heures is not None else gel_declaration_delai_heures(db)
    piece = None
    if mesure.piece_declaration_id:
        piece = (pieces or {}).get(mesure.piece_declaration_id) or db.query(AlertAttachment) \
            .filter(AlertAttachment.id == mesure.piece_declaration_id).first()

    def iso(d):
        return d.isoformat() if d else None

    return {
        "id": mesure.id, "alert_id": mesure.alert_id,
        "type_action": mesure.type_action,
        "type_action_label": ACTIONS.get(mesure.type_action, mesure.type_action),
        "statut": mesure.statut, "statut_label": STATUT_LABELS.get(mesure.statut, mesure.statut),
        "client_id": mesure.client_id, "client_name": mesure.client_name,
        "watchlist_entity_id": mesure.watchlist_entity_id,
        "watchlist_name": mesure.watchlist_name, "list_type": mesure.list_type,
        "programmes": mesure.programmes or [],
        "reference_designation": mesure.reference_designation,
        "ouverte_le": iso(mesure.ouverte_le), "ouverte_par": mesure.ouverte_par,
        "date_effet": iso(mesure.date_effet),
        "comptes": mesure.comptes or [], "comptes_texte": mesure.comptes_texte or "",
        "precisions": mesure.precisions,
        "client_gele": bool(mesure.client_gele_ecrit) and mesure.statut != STATUT_LEVEE,
        "declaree_le": iso(mesure.declaree_le), "declaree_par": mesure.declaree_par,
        "reference_dgt": mesure.reference_dgt, "canal_dgt": mesure.canal_dgt,
        "piece_declaration": ({
            "id": piece.id, "file_name": piece.file_name,
            "file_present": piece_presente(piece.file_path),
        } if piece is not None else None),
        "levee_le": iso(mesure.levee_le), "levee_par": mesure.levee_par,
        "motif_levee": mesure.motif_levee,
        "motif_levee_label": MOTIFS_DE_LEVEE.get(mesure.motif_levee or ""),
        "justification_levee": mesure.justification_levee,
        "delai_declaration_secondes": delai_de_declaration_secondes(
            mesure.ouverte_le, mesure.declaree_le, now),
        "en_retard": declaration_en_retard(mesure.statut, mesure.ouverte_le,
                                           mesure.declaree_le, seuil, now),
        "seuil_heures": seuil,
        # Fiche radiee de sa liste : levee a instruire (cf. signaler_radiation)
        "radiation_a_examiner": radiation_a_examiner(mesure),
        "radiation_constatee_le": iso(mesure.radiation_constatee_le),
        "radiation_detail": mesure.radiation_detail,
        # Client repute detenu ou controle par ses parties liees : le registre
        # le met en avant, le calcul fige a l'ouverture l'accompagne
        "fondement": mesure.fondement,
        "fondement_label": _libelle_de_fondement(mesure.fondement),
        "detention": mesure.detention,
    }


def _libelle_de_fondement(code: Optional[str]) -> Optional[str]:
    from fiskr.parties import FONDEMENTS
    return FONDEMENTS.get(code) if code else None


def vocabulaire() -> Dict[str, Any]:
    """Codes et libelles, pour l'ecran : une seule definition, ici."""
    return {
        "statuts": [{"code": c, "label": STATUT_LABELS[c]} for c in STATUTS],
        "actions": [{"code": c, "label": l} for c, l in ACTIONS.items()],
        "motifs_de_levee": [{"code": c, "label": l} for c, l in MOTIFS_DE_LEVEE.items()],
        "adresses_dgt": [{"adresse": a, "regimes": r} for a, r in ADRESSES_DGT.items()],
    }

"""
Prestataires de paiement defaillants : reglement (UE) 2023/1113, art. 8 §2.

POURQUOI CE MODULE EXISTE
-------------------------
Le filtrage rendait INCOMPLETE un virement auquel manquait une information
obligatoire (art. 4, 5 §1, 7) — transfert par transfert, ce que l'art. 8 §1
demande. Mais l'art. 8 §2 vise autre chose : « lorsqu'un prestataire de
services de paiement omet de maniere repetee de fournir les informations
requises », le prestataire du beneficiaire prend des dispositions
(avertissements et echeances, puis rejet, restriction ou fin de la relation)
ET « declare cette omission et les dispositions prises a l'autorite
competente ». Rien ne gardait la trace de QUI omettait : cent virements
incomplets du meme correspondant etaient cent incidents sans lien, et la
declaration que le texte exige ne pouvait pas meme etre ecrite.

A QUI L'OMISSION EST IMPUTEE
----------------------------
Au prestataire ANTERIEUR de la chaine — celui qui nous transmet le transfert —
c'est-a-dire l'agent instructeur (`InstgAgt`) du pacs.008, a defaut la banque
du donneur d'ordre (`DbtrAgt`). C'est lui que le texte et les orientations
visent : l'avertissement s'adresse au « prior PSP [...] in the transfer chain »
(EBA/GL/2024/11, §70), le rejet porte sur « tout nouveau transfert de fonds
provenant dudit prestataire » (art. 8 §2 b), la relation d'affaires qu'on
restreint est la sienne. Un intermediaire qui relaie un transfert incomplet
omet lui-meme (art. 10 a 12) ; le prestataire du donneur d'ordre est garde a
cote, dans la ligne, pour le signalement.
Ne sont imputes : ni un pain.001 (l'ordre d'un CLIENT : l'etablissement y est
le prestataire du donneur d'ordre, tenu par l'art. 4), ni un message dont
l'instructeur est l'etablissement lui-meme (message sortant, cf. le reglage
`bic_etablissement`), ni un transfert dont le prestataire n'a ni BIC ni LEI —
et la reponse le dit.

CE QUI COMPTE COMME OMISSION
----------------------------
Un transfert auquel manque une information obligatoire sous son regime
(`fiskr/transactions.manques_par_transaction`, la lecture qui fait
INCOMPLETE). Pas une partie NON CRIBLEE (un code de compensation n'est pas une
« information requise » du reglement). Et quel que soit le verdict : un HIT
qui est aussi incomplet reste une omission du prestataire.

Les criteres (periode, nombre, pourcentage) et la politique sont des reglages
(fiskr/settings.py, `prestataires_defaillants_criteres`, ou leur choix est
justifie). `est_defaillant` est la SEULE definition : le filtrage, l'ecran de
suivi et le signalement la lisent.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func

from fiskr.database import PrestataireDefaillant, ReceptionPrestataire

logger = logging.getLogger("fiskr.prestataires")

MOTIFS_NON_IMPUTE = {
    "MESSAGE_SORTANT": "Message émis par l'établissement lui-même (BIC de l'établissement) : son omission est la sienne (art. 4), elle n'est imputée à aucun autre prestataire.",
    "SANS_IDENTIFIANT": "Le prestataire qui transmet n'a ni BIC ni LEI : l'omission ne peut pas lui être imputée.",
    "AUCUN_PRESTATAIRE": "Ni agent instructeur (InstgAgt) ni banque du donneur d'ordre (DbtrAgt) dans le message : l'omission ne peut être imputée à personne.",
}
# Ordre de recherche du prestataire qui transmet le transfert
ROLES_IMPUTABLES = ("InstgAgt", "DbtrAgt")
# Identifiants de transferts gardes par ligne : au-dela, le compte suffit
MAX_TRANSACTIONS_PAR_LIGNE = 20
# Delai du signalement a l'autorite : « without undue delay, and no later than
# three months after identifying » (EBA/GL/2024/11, §74)
DELAI_SIGNALEMENT_MOIS = 3
REFERENCE_SIGNALEMENT = "Règlement (UE) 2023/1113, art. 8 §2 (et 12 §2) ; EBA/GL/2024/11, §74-75"


def _plus_mois(d: datetime, mois: int) -> datetime:
    """`d` avance de `mois` mois civils ; un 31 sans 31 recule au dernier jour."""
    annee, rang = divmod(d.month - 1 + mois, 12)
    for jour in (d.day, 30, 29, 28):
        try:
            return d.replace(year=d.year + annee, month=rang + 1, day=jour)
        except ValueError:
            continue
    return d


def a_signaler_avant(identifie_le: datetime) -> datetime:
    return _plus_mois(identifie_le, DELAI_SIGNALEMENT_MOIS)


def _identifiant(agent: Dict[str, Any]) -> Tuple[str, str]:
    """(identifiant du registre, BIC tel qu'ecrit) : le BIC sur 8 caracteres
    — l'etablissement, pas son agence —, a defaut le LEI."""
    from fiskr.transactions import bic_comparable, lei_comparable
    bic = bic_comparable(agent.get("bic"))
    if bic:
        return bic[:8], bic
    return lei_comparable(agent.get("lei")), ""


def prestataire_impute(tx: Dict[str, Any], bics_etablissement) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Le prestataire auquel une omission de `tx` est imputee, ou (None, motif)."""
    agents = {p["role_tag"]: p for p in tx.get("parties") or [] if p.get("is_agent")}
    for role in ROLES_IMPUTABLES:
        agent = agents.get(role)
        if agent is None:
            continue
        identifiant, bic = _identifiant(agent)
        if not identifiant:
            return None, "SANS_IDENTIFIANT"
        if identifiant in bics_etablissement:
            return None, "MESSAGE_SORTANT"
        donneur = agents.get("DbtrAgt")
        ident_donneur = _identifiant(donneur)[0] if (donneur and role != "DbtrAgt") else ""
        return {
            "identifiant": identifiant, "bic": bic, "role": role,
            "nom": agent.get("name") if agent.get("has_name") else "",
            "pays": (bic[4:6] if bic else agent.get("country") or "")[:2],
            "prestataire_du_donneur_d_ordre": ident_donneur if ident_donneur != identifiant else "",
        }, None
    return None, "AUCUN_PRESTATAIRE"


# ------------------------------------------------------------ la mesure

def statistiques(db, identifiant: str, criteres: Dict[str, Any],
                 maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """Transferts recus du prestataire sur la periode glissante, incomplets,
    pourcentage, et premiere/derniere omission constatee."""
    now = maintenant or datetime.utcnow()
    debut = now - timedelta(days=int(criteres["fenetre_jours"]))
    R = ReceptionPrestataire
    transferts, incomplets, messages = db.query(
        func.coalesce(func.sum(R.transferts), 0), func.coalesce(func.sum(R.incomplets), 0),
        func.count(R.id)).filter(R.identifiant == identifiant, R.recu_le >= debut).one()
    premiere, derniere, messages_incomplets = db.query(
        func.min(R.recu_le), func.max(R.recu_le), func.count(R.id)).filter(
        R.identifiant == identifiant, R.recu_le >= debut, R.incomplets > 0).one()
    transferts, incomplets = int(transferts or 0), int(incomplets or 0)
    return {
        "window_days": int(criteres["fenetre_jours"]),
        "window_start": debut.isoformat(), "window_end": now.isoformat(),
        "transfers": transferts, "incomplete": incomplets,
        "rate_pct": round(100.0 * incomplets / transferts, 1) if transferts else 0.0,
        "messages": int(messages or 0), "messages_incomplete": int(messages_incomplets or 0),
        "first_failure": premiere.isoformat() if premiere else None,
        "last_failure": derniere.isoformat() if derniere else None,
    }


def est_defaillant(stats: Dict[str, Any], criteres: Dict[str, Any]) -> bool:
    """LA definition : au moins `seuil_transferts` transferts incomplets sur la
    periode, ET au moins `seuil_taux_pct` % des transferts recus du
    prestataire (EBA/GL/2024/11, §68 a : un pourcentage par periode)."""
    return (stats["incomplete"] >= int(criteres["seuil_transferts"])
            and stats["rate_pct"] >= float(criteres["seuil_taux_pct"]))


def episode_ouvert(db, identifiant: str) -> Optional[PrestataireDefaillant]:
    return db.query(PrestataireDefaillant).filter(
        PrestataireDefaillant.identifiant == identifiant,
        PrestataireDefaillant.leve_le.is_(None),
    ).order_by(PrestataireDefaillant.identifie_le.desc()).first()


def _criteres_figes(criteres: Dict[str, Any]) -> Dict[str, Any]:
    return {k: criteres[k] for k in ("fenetre_jours", "seuil_transferts", "seuil_taux_pct", "politique")}


# ------------------------------------------------------------ a chaque message

def consigner(db, parsed: Dict[str, Any], manques: List[Tuple[Dict[str, Any], List[str]]],
              username: str, maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Impute les transferts du message a leurs prestataires, les consigne, et
    tient a jour le constat de defaillance. Rend ce que la reponse du filtrage
    en dit, et `retenir` : le message vient d'un prestataire DEJA defaillant
    et la politique renforcee est choisie — « ses messages suivants », pas
    celui qui le fait basculer.

    Un pain.001 n'est pas impute (ordre d'un client). La notification part
    une fois par identification, apres le commit.
    """
    from fiskr.settings import POLITIQUE_RETENIR, POLITIQUES_PRESTATAIRE_DEFAILLANT, prestataires_defaillants_criteres
    criteres = prestataires_defaillants_criteres(db)
    sortie: Dict[str, Any] = {
        "policy": criteres["politique"],
        "policy_label": POLITIQUES_PRESTATAIRE_DEFAILLANT[criteres["politique"]],
        "imputed": [], "not_imputed": [], "retain": False,
    }
    if parsed.get("message_type") != "pacs.008":
        return sortie
    now = maintenant or datetime.utcnow()
    bics = set(criteres.get("bic_etablissement") or [])
    par_prestataire: Dict[str, Dict[str, Any]] = {}
    non_imputes: Dict[str, List[str]] = {}
    for rang, (tx, codes) in enumerate(manques, start=1):
        ident_tx = tx.get("end_to_end_id") or f"#{rang}"
        prestataire, motif = prestataire_impute(tx, bics)
        if prestataire is None:
            # Ne se dit que pour ce qui manque : un transfert complet
            # non imputable n'appelle rien.
            if codes:
                non_imputes.setdefault(motif, []).append(ident_tx)
            continue
        entree = par_prestataire.setdefault(prestataire["identifiant"], dict(
            prestataire, transferts=0, incomplets=0, manques={}, transactions=[]))
        entree["transferts"] += 1
        if codes:
            entree["incomplets"] += 1
            for code in codes:
                entree["manques"][code] = entree["manques"].get(code, 0) + 1
            if len(entree["transactions"]) < MAX_TRANSACTIONS_PAR_LIGNE:
                entree["transactions"].append(ident_tx)

    a_notifier: List[Tuple[PrestataireDefaillant, Dict[str, Any]]] = []
    for identifiant, entree in par_prestataire.items():
        avant = episode_ouvert(db, identifiant)
        db.add(ReceptionPrestataire(
            recu_le=now, identifiant=identifiant, bic=entree["bic"] or None,
            nom=(entree["nom"] or None) and entree["nom"][:140], pays=entree["pays"] or None,
            role=entree["role"], prestataire_du_donneur_d_ordre=entree["prestataire_du_donneur_d_ordre"] or None,
            message_type=parsed["message_type"], msg_id=(parsed.get("msg_id") or "")[:100] or None,
            transferts=entree["transferts"], incomplets=entree["incomplets"],
            manques=entree["manques"] or None, transactions=entree["transactions"] or None,
            filtre_par=username,
        ))
        db.flush()
        stats = statistiques(db, identifiant, criteres, now)
        defaillant = est_defaillant(stats, criteres)
        episode = avant
        if defaillant and avant is None:
            episode = PrestataireDefaillant(
                identifiant=identifiant, nom=(entree["nom"] or None) and entree["nom"][:140],
                pays=entree["pays"] or None, identifie_le=now,
                constat=dict(stats, criteres=_criteres_figes(criteres)))
            db.add(episode)
            a_notifier.append((episode, stats))
        elif not defaillant and avant is not None:
            avant.leve_le = now
            avant.constat_levee = dict(stats, criteres=_criteres_figes(criteres))
            episode = None
        sortie["imputed"].append({
            "identifier": identifiant, "bic": entree["bic"], "name": entree["nom"],
            "country": entree["pays"], "role": entree["role"],
            "transfers": entree["transferts"], "incomplete": entree["incomplets"],
            "failing_before": avant is not None,
            "failing": defaillant,
            "newly_identified": defaillant and avant is None,
            "identified_on": (episode.identifie_le.isoformat() if episode is not None else None),
            "window": stats,
        })
        if avant is not None and criteres["politique"] == POLITIQUE_RETENIR:
            sortie["retain"] = True
    sortie["not_imputed"] = [{"code": code, "label": MOTIFS_NON_IMPUTE[code], "transactions": ids}
                             for code, ids in non_imputes.items()]
    db.commit()
    for episode, stats in a_notifier:
        _notifier(db, episode, stats, criteres)
    return sortie


def _notifier(db, episode: PrestataireDefaillant, stats: Dict[str, Any], criteres: Dict[str, Any]) -> None:
    from fiskr.notifier import emit
    from fiskr.settings import POLITIQUES_PRESTATAIRE_DEFAILLANT
    emit(db, "prestataire_defaillant", {
        "Prestataire": episode.nom or episode.identifiant,
        "BIC / LEI": episode.identifiant, "Pays": episode.pays or "—",
        "Période": f"{stats['window_days']} jours glissants",
        "Transferts reçus": stats["transfers"],
        "Transferts incomplets": f"{stats['incomplete']} ({stats['rate_pct']:g} %)",
        "Seuils": f"{criteres['seuil_transferts']} transferts incomplets et {criteres['seuil_taux_pct']:g} %",
        "Politique": POLITIQUES_PRESTATAIRE_DEFAILLANT[criteres["politique"]],
        "À signaler avant le": a_signaler_avant(episode.identifie_le).strftime("%d/%m/%Y"),
        "Obligation": "Dispositions à l'égard du prestataire et déclaration à l'autorité compétente (règlement (UE) 2023/1113, art. 8 §2 ; EBA/GL/2024/11 §74-75).",
    })
    episode.notifie_le = datetime.utcnow()
    db.commit()


# ------------------------------------------------------------ lecture

def _libelles_manques(manques: Optional[Dict[str, int]]) -> List[Dict[str, Any]]:
    from fiskr.transactions import MANQUES
    return [{"code": code, "label": MANQUES[code][0] if code in MANQUES else code, "count": n}
            for code, n in sorted((manques or {}).items(), key=lambda kv: -kv[1])]


def suivi(db, maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """
    L'ecran de suivi : les prestataires qui ont omis au moins une fois sur la
    periode, et ceux dont un episode de defaillance est ouvert. Ceux qui
    n'omettent rien n'y figurent pas — on nomme ce qui ne va pas.
    """
    from fiskr.settings import POLITIQUES_PRESTATAIRE_DEFAILLANT, prestataires_defaillants_criteres
    now = maintenant or datetime.utcnow()
    criteres = prestataires_defaillants_criteres(db)
    debut = now - timedelta(days=int(criteres["fenetre_jours"]))
    R = ReceptionPrestataire
    identifiants = {i for (i,) in db.query(R.identifiant).filter(
        R.recu_le >= debut, R.incomplets > 0).distinct().all()}
    ouverts = {e.identifiant: e for e in db.query(PrestataireDefaillant).filter(
        PrestataireDefaillant.leve_le.is_(None)).all()}
    identifiants |= set(ouverts)
    items = []
    for identifiant in identifiants:
        stats = statistiques(db, identifiant, criteres, now)
        derniere = db.query(R).filter(R.identifiant == identifiant).order_by(R.recu_le.desc()).first()
        episode = ouverts.get(identifiant)
        manques: Dict[str, int] = {}
        for (lot,) in db.query(R.manques).filter(R.identifiant == identifiant, R.recu_le >= debut,
                                                 R.incomplets > 0).all():
            for code, n in (lot or {}).items():
                manques[code] = manques.get(code, 0) + int(n)
        echeance = a_signaler_avant(episode.identifie_le) if episode else None
        items.append({
            "identifier": identifiant,
            "name": (episode.nom if episode and episode.nom else None) or (derniere.nom if derniere else None),
            "bic": derniere.bic if derniere else None,
            "country": (episode.pays if episode else None) or (derniere.pays if derniere else None),
            "window": stats,
            "missing": _libelles_manques(manques),
            "failing": est_defaillant(stats, criteres),
            "episode_open": episode is not None,
            "identified_on": episode.identifie_le.isoformat() if episode else None,
            "report_before": echeance.isoformat() if echeance else None,
            "report_overdue": bool(echeance and now > echeance),
        })
    items.sort(key=lambda x: (not x["episode_open"], -x["window"]["incomplete"], x["identifier"]))
    return {
        "criteria": {k: criteres[k] for k in ("fenetre_jours", "seuil_transferts", "seuil_taux_pct",
                                              "politique", "bic_etablissement")},
        "criteria_source": criteres["source"], "criteria_error": criteres["erreur"],
        "policy_label": POLITIQUES_PRESTATAIRE_DEFAILLANT[criteres["politique"]],
        "policies": dict(POLITIQUES_PRESTATAIRE_DEFAILLANT),
        "reference": REFERENCE_SIGNALEMENT,
        "items": items,
    }


def manquements(db, identifiant: str, maintenant: Optional[datetime] = None,
                limite: int = 200) -> List[Dict[str, Any]]:
    """Les messages incomplets recus du prestataire sur la periode, du plus
    recent au plus ancien : ce que l'on documente (EBA/GL/2024/11, §67)."""
    from fiskr.settings import prestataires_defaillants_criteres
    now = maintenant or datetime.utcnow()
    criteres = prestataires_defaillants_criteres(db)
    debut = now - timedelta(days=int(criteres["fenetre_jours"]))
    R = ReceptionPrestataire
    lignes = db.query(R).filter(R.identifiant == identifiant, R.recu_le >= debut, R.incomplets > 0) \
        .order_by(R.recu_le.desc(), R.id.desc()).limit(limite).all()
    return [{
        "received_at": l.recu_le.isoformat(), "msg_id": l.msg_id, "message_type": l.message_type,
        "role": l.role, "originator_psp": l.prestataire_du_donneur_d_ordre,
        "transfers": l.transferts, "incomplete": l.incomplets,
        "missing": _libelles_manques(l.manques), "transactions": l.transactions or [],
        "screened_by": l.filtre_par,
    } for l in lignes]


def signalement(db, maintenant: Optional[datetime] = None) -> Tuple[List[str], List[List[Any]]]:
    """
    Le signalement a l'autorite competente, une ligne par prestataire dont un
    episode est ouvert, dans l'ordre du contenu que fixent les orientations
    (EBA/GL/2024/11, §75 : a) nom, b) pays, c) frequence, periode, motifs
    donnes par le prestataire, d) dispositions prises). Fiskr remplit ce qu'il
    a mesure ; les motifs du prestataire et les dispositions (avertissements,
    echeances) ne sont pas dans les messages — colonnes A COMPLETER, dites
    comme telles. Aucun envoi : l'export est la piece, la transmission reste
    humaine.
    """
    from fiskr.settings import POLITIQUES_PRESTATAIRE_DEFAILLANT
    vue = suivi(db, maintenant)
    entete = ["prestataire", "bic_ou_lei", "pays_du_bic", "identifie_le", "a_signaler_avant",
              "periode_jours", "premiere_omission", "derniere_omission", "transferts_recus",
              "transferts_incomplets", "frequence_pct", "informations_manquantes",
              "messages_incomplets", "motifs_donnes_par_le_prestataire_a_completer",
              "dispositions_prises_a_completer", "politique_appliquee_par_fiskr", "reference"]
    lignes = []
    for item in vue["items"]:
        if not item["episode_open"]:
            continue
        w = item["window"]
        lignes.append([
            item["name"] or item["identifier"], item["identifier"], item["country"] or "",
            item["identified_on"][:10], item["report_before"][:10], w["window_days"],
            (w["first_failure"] or "")[:10], (w["last_failure"] or "")[:10],
            w["transfers"], w["incomplete"], f"{w['rate_pct']:g}",
            " ; ".join(f"{m['label']} : {m['count']}" for m in item["missing"]),
            w["messages_incomplete"], "", "",
            POLITIQUES_PRESTATAIRE_DEFAILLANT[vue["criteria"]["politique"]],
            REFERENCE_SIGNALEMENT,
        ])
    return entete, lignes


# ------------------------------------------------------------ conservation

def receptions_purgeables(db, limite: datetime):
    """Lignes consignees avant `limite` : la limite de la famille
    `audit_trail` (fiskr/retention.py), jamais plus recente que cinq annees
    civiles. Un prestataire n'est pas un client en relation : le delai court
    depuis la ligne, comme pour une operation occasionnelle."""
    return db.query(ReceptionPrestataire).filter(ReceptionPrestataire.recu_le < limite)

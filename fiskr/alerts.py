"""
Logique partagee des alertes de criblage : ouverture/re-detection dedupliquee
et consultation de la liste blanche client x liste. Module separe pour etre
utilisable a la fois par l'API temps reel (fiskr.api) et par le moteur de
re-criblage post-delta (fiskr.rescreen) sans import circulaire.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from fiskr.database import (Alert, AlertEvent, ALERT_OPEN_STATUSES, ALERT_PRIORITIES,
                            AuditTrail, WhitelistPair)
from fiskr.settings import alert_sla_hours, perimeter_overrides

logger = logging.getLogger("fiskr.alerts")

# Plancher de priorite d'une correspondance sur une liste du perimetre
# SANCTION (gel des avoirs). Releve en production : deux vrais positifs OFAC
# etaient en MEDIUM, echeance 120 h — la priorite ne dependait que du score, et
# un nom identique a 90 % sur la liste SDN pesait autant qu'un PEP local.
#
# HIGH, et pas CRITICAL. CRITICAL reste ce qu'il est deja : la concordance
# d'un IDENTIFIANT officiel (hard match), sur tout perimetre — un fait etabli.
# Une correspondance de NOM sur une liste de gel est le plus souvent une
# homonymie ; la mettre au rang d'un identifiant prouve ferait de CRITICAL le
# niveau de presque toutes les alertes sanctions, et il ne distinguerait plus
# rien. HIGH ramene l'echeance de 120 h a 72 h (SLA par defaut) sans faire
# crier au loup : les listes HORS_SANCTION (PEP, regulateurs, exclusions —
# 79 % des fiches en production) gardent leur priorite au score.
PLANCHER_SANCTION = "HIGH"


def plancher_de_priorite(list_type: Optional[str],
                         surcharges: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Priorite minimale d'une alerte sur ce type de liste, ou None. Derivee du
    perimetre (fiskr/perimeters.py) — un type inconnu est SANCTION, donc
    plancher : le doute penche vers ce qui ne fait rien perdre."""
    from fiskr.perimeters import est_sanction
    return PLANCHER_SANCTION if est_sanction(list_type, surcharges) else None


def sous_le_plancher(priorite: Optional[str], plancher: Optional[str]) -> bool:
    if plancher is None:
        return False
    rang = {p: i for i, p in enumerate(ALERT_PRIORITIES)}
    return rang.get(priorite or "MEDIUM", 1) < rang[plancher]


def compute_alert_priority(best_match: Dict[str, Any],
                           surcharges: Optional[Dict[str, str]] = None) -> str:
    """
    Priorite calculee a la creation (modifiable ensuite par l'analyste, jamais
    sous le plancher) : hard match (identifiant officiel identique) ->
    CRITICAL ; score tres eleve -> HIGH ; alerte standard -> MEDIUM ; proche
    du seuil -> LOW. Puis le plancher du perimetre SANCTION s'applique.
    """
    if best_match.get("hard_match_triggered"):
        return "CRITICAL"
    score = float(best_match.get("final_score") or 0.0)
    cut_off = float(best_match.get("cut_off_applied") or 75.0)
    if score >= 95.0:
        priorite = "HIGH"
    elif score >= cut_off + 5.0:
        priorite = "MEDIUM"
    else:
        priorite = "LOW"
    plancher = plancher_de_priorite(
        (best_match.get("watchlist_entity") or {}).get("_list_type"), surcharges)
    return plancher if sous_le_plancher(priorite, plancher) else priorite


def _echeance(sla: Dict[str, Any], priority: str,
              created_at: Optional[datetime] = None) -> Optional[datetime]:
    """Echeance SLA depuis un reglage DEJA lu (0 = aucune echeance)."""
    hours = (sla or {}).get(priority or "MEDIUM", 0)
    if not hours:
        return None
    return (created_at or datetime.utcnow()) + timedelta(hours=hours)


def compute_due_at(db, priority: str, created_at: Optional[datetime] = None) -> Optional[datetime]:
    """Echeance SLA de traitement selon la priorite (reglage a chaud ; 0 = aucune)."""
    return _echeance(alert_sla_hours(db), priority, created_at)


def is_whitelisted(db, client_id: Optional[str], entity_id: Optional[str]) -> Optional[WhitelistPair]:
    """
    Retourne la paire de liste blanche ACTIVE (non revoquee, non expiree)
    pour ce couple client x liste, ou None.
    """
    if not client_id or not entity_id:
        return None
    now = datetime.utcnow()
    return db.query(WhitelistPair).filter(
        WhitelistPair.client_id == client_id,
        WhitelistPair.watchlist_entity_id == entity_id,
        WhitelistPair.revoked_at.is_(None),
        (WhitelistPair.expires_at.is_(None)) | (WhitelistPair.expires_at > now)
    ).first()


def whitelisted_pairs(db, client_id: Optional[str],
                      entity_ids) -> Dict[str, WhitelistPair]:
    """
    Paires de liste blanche ACTIVES pour ce client, indexees par fiche listee.

    Version groupee de `is_whitelisted` : un criblage produit desormais AUTANT
    de correspondances qu'il en trouve au-dessus du seuil — 2 976 pour un nom
    tres courant, mesure en production. Une requete par correspondance aurait
    remis un N+1 sur le chemin le plus chaud de l'application.
    """
    ids = [e for e in dict.fromkeys(entity_ids) if e]
    if not client_id or not ids:
        return {}
    now = datetime.utcnow()
    lignes = db.query(WhitelistPair).filter(
        WhitelistPair.client_id == client_id,
        WhitelistPair.watchlist_entity_id.in_(ids),
        WhitelistPair.revoked_at.is_(None),
        (WhitelistPair.expires_at.is_(None)) | (WhitelistPair.expires_at > now)
    ).all()
    # Premiere paire gagnante par fiche (meme choix que `.first()`)
    trouvees: Dict[str, WhitelistPair] = {}
    for paire in lignes:
        trouvees.setdefault(paire.watchlist_entity_id, paire)
    return trouvees


# Au-dela de ce nombre d'alertes ouvertes par UN criblage, les notifications
# individuelles cedent la place a une seule notification de volumetrie : un
# nom tres courant sans contexte peut ouvrir des milliers d'alertes d'un coup,
# et autant de notifications serait un incident en soi.
NOTIFY_BURST_MAX = 10


def open_or_redetect_alerts(db, correspondances: Sequence[Dict[str, Any]], *,
                            client_id: Optional[str], username: str,
                            channel: str = "SCREENING",
                            detail_suffix: str = "") -> Dict[str, Any]:
    """
    Ouvre (ou re-detecte) une alerte pour CHAQUE correspondance au-dessus du
    seuil, en un seul aller-retour de base.

    Le criblage ne persistait que la MEILLEURE correspondance : un client
    homonyme de 2 976 fiches listees (mesure en production, « Mohammed Ali »
    sans pays) ne laissait qu'une seule trace, et 2 975 correspondances
    reglementaires disparaissaient sans laisser d'ecrit. Elles sont desormais
    toutes creees.

    `correspondances` : sequence de {"audit": AuditTrail, "match": dict,
    "rule": FpRule|None, "whitelist_stale": dict|None}. Une correspondance
    portant une regle est creee PUIS cloturee CLOSED_BY_RULE, avec le nom et la
    version de la regle en clair — jamais supprimee en silence. Une
    correspondance qu'une paire de liste blanche n'a pas absorbee parce que la
    fiche ou le client a change depuis sa decision (fiskr.empreintes) porte son
    reexamen : l'alerte recoit un evenement WHITELIST_STALE qui le dit.

    `alert_ids` est ALIGNE sur `correspondances` (meme rang, meme alerte). Il
    listait les alertes re-detectees d'abord et les nouvelles ensuite : un
    appelant qui les rapprochait par rang attribuait a la meilleure
    correspondance l'alerte d'une autre des que les deux sortes se melaient.

    Les notifications individuelles s'arretent a `NOTIFY_BURST_MAX` et cedent
    la place a une seule notification de volumetrie.
    """
    resultat = {"alert_ids": [], "opened": 0, "closed_by_rule": 0, "redetected": 0}
    if not correspondances:
        return resultat
    alertes_par_rang: List[Optional[Alert]] = [None] * len(correspondances)

    ids_fiches = [(c["match"].get("watchlist_entity") or {}).get("entity_id", "NONE")
                  for c in correspondances]
    # Les alertes deja cloturees par regle comptent aussi pour la dedup : un
    # re-criblage ne doit pas recreer ce qu'une regle a deja tranche.
    statuts = list(ALERT_OPEN_STATUSES) + ["CLOSED_BY_RULE"]
    existantes: Dict[str, Alert] = {}
    if client_id and ids_fiches:
        for alerte in db.query(Alert).filter(
                Alert.client_id == client_id,
                Alert.watchlist_entity_id.in_([i for i in dict.fromkeys(ids_fiches) if i]),
                Alert.status.in_(statuts)).all():
            existantes.setdefault(alerte.watchlist_entity_id, alerte)

    now = datetime.utcnow()
    # Reglage SLA lu UNE fois pour tout le lot : `compute_due_at` le relisait
    # par alerte, soit une requete de reglages par correspondance.
    sla = alert_sla_hours(db)
    # Classement des listes, lu une fois aussi : il porte le plancher de
    # priorite du perimetre SANCTION.
    surcharges = perimeter_overrides(db)
    evenements: List[AlertEvent] = []
    a_notifier: List[Alert] = []

    for rang, correspondance in enumerate(correspondances):
        audit = correspondance["audit"]
        match = correspondance["match"]
        regle = correspondance.get("rule")
        fiche = match.get("watchlist_entity") or {}
        wl_id = fiche.get("entity_id", "NONE")
        supprimee = regle is not None
        reexamen = correspondance.get("whitelist_stale")

        deja = existantes.get(wl_id)
        if deja is not None:
            alertes_par_rang[rang] = deja
            if match["final_score"] > deja.final_score:
                deja.final_score = match["final_score"]
            if deja.list_type is None and fiche.get("_list_type"):
                deja.list_type = fiche.get("_list_type")
            # Une alerte encore ouverte, creee AVANT le plancher, y remonte au
            # premier re-criblage : chaque mise en production de liste repasse
            # tout le referentiel, l'ecart se resorbe sans migration a la main.
            plancher = plancher_de_priorite(deja.list_type, surcharges)
            if deja.status in ALERT_OPEN_STATUSES and sous_le_plancher(deja.priority, plancher):
                evenements.append(AlertEvent(
                    alert_id=deja.id, username=username, action="PRIORITY_CHANGED",
                    detail=(f"Priorité {deja.priority or '—'} → {plancher} : plancher des "
                            f"listes de gel (périmètre SANCTION).")))
                deja.priority = plancher
                deja.due_at = _echeance(sla, plancher, deja.created_at)
            # Evenement de re-detection : ecrit pour les alertes que
            # quelqu'un doit encore traiter. Une alerte deja tranchee par une
            # regle n'en recoit pas a chaque passage — le re-criblage repasse
            # toute la base apres chaque mise en production, et un homonyme de
            # nom courant porte des milliers d'alertes cloturees par regle :
            # une ligne d'evenement par alerte et par passage ferait grossir
            # le journal sans rien apprendre. La ligne d'audit de CE criblage,
            # elle, est ecrite dans tous les cas.
            if deja.status != "CLOSED_BY_RULE":
                if supprimee:
                    detail = (f"Re-détectée puis à nouveau supprimée par la règle « {regle.name} » "
                              f"(v{regle.version}, audit #{audit.id}).{detail_suffix}")
                else:
                    detail = (f"Re-détectée lors d'un nouveau criblage "
                              f"(score {match['final_score']:.1f}, audit #{audit.id}).{detail_suffix}")
                evenements.append(AlertEvent(alert_id=deja.id, username=username,
                                             action="REDETECTED", detail=detail))
            # Une paire qui VIENT de basculer le dit aussi sur l'alerte deja
            # ouverte ; sur une paire deja signalee, ce serait une ligne par
            # criblage qui n'apprend rien. (Sans identifiant : alerte creee
            # plus haut dans ce meme lot, qui porte deja l'evenement.)
            if reexamen and reexamen.get("nouveau") and deja.id is not None:
                evenements.append(AlertEvent(alert_id=deja.id, username=username,
                                             action="WHITELIST_STALE",
                                             detail=reexamen["detail"]))
            resultat["redetected"] += 1
            continue

        priorite = compute_alert_priority(match, surcharges)
        alerte = Alert(
            audit_id=audit.id, channel=channel, client_id=client_id,
            client_name=audit.client_name, watchlist_entity_id=wl_id,
            watchlist_name=fiche.get("primary_name", "Inconnu"),
            final_score=match["final_score"], list_type=fiche.get("_list_type"),
            status="CLOSED_BY_RULE" if supprimee else "OPEN",
            priority=priorite,
            due_at=None if supprimee else _echeance(sla, priorite, now),
        )
        if supprimee:
            alerte.decided_by = "fp-rule"
            alerte.decided_at = now
            alerte.decision_comment = (
                f"Faux positif supprimé automatiquement par la règle « {regle.name} » "
                f"(#{regle.id} v{regle.version}). Conservée pour l'audit (ACPR/FED)."
            )
        db.add(alerte)
        alertes_par_rang[rang] = alerte
        # La dedup vaut AUSSI a l'interieur d'un meme criblage : deux fiches
        # portant le meme entity_id ne doivent pas ouvrir deux alertes.
        existantes[wl_id] = alerte
        correspondance["_alerte"] = alerte
        correspondance["_regle"] = regle
        if supprimee:
            resultat["closed_by_rule"] += 1
        else:
            resultat["opened"] += 1
            a_notifier.append(alerte)

    db.flush()   # attribue les identifiants avant d'ecrire les evenements

    for correspondance in correspondances:
        alerte = correspondance.get("_alerte")
        if alerte is None:
            continue
        regle = correspondance.get("_regle")
        match = correspondance["match"]
        evenements.append(AlertEvent(
            alert_id=alerte.id, username=username, action="CREATED",
            detail=f"Alerte créée par le criblage (score {match['final_score']:.1f}).{detail_suffix}"))
        # Toute alerte nee d'une paire qui n'a pas absorbe dit pourquoi, meme
        # si la paire etait deja signalee : sans cette ligne, l'analyste qui
        # a clos la precedente ne comprendrait pas qu'elle revienne.
        reexamen = correspondance.get("whitelist_stale")
        if reexamen:
            evenements.append(AlertEvent(
                alert_id=alerte.id, username=username, action="WHITELIST_STALE",
                detail=reexamen["detail"]))
        if regle is not None:
            evenements.append(AlertEvent(
                alert_id=alerte.id, username="fp-rule", action="RULE_SUPPRESSED",
                detail=(f"Auto-clôturée par la règle anti-faux positif « {regle.name} » "
                        f"(#{regle.id} v{regle.version}).")))
        correspondance.pop("_alerte", None)
        correspondance.pop("_regle", None)
    resultat["alert_ids"] = [a.id for a in alertes_par_rang]

    db.add_all(evenements)
    # Charges utiles de notification composees AVANT le commit : apres, la
    # session expire les objets et chaque attribut relu declenche un SELECT
    # par alerte — le N+1 rentrerait par la porte de derriere.
    charges = [{
        "_assignee": a.assigned_to,
        "Alerte": a.id, "Canal": channel, "Priorité": a.priority,
        "Client": a.client_name, "Fiche listée": a.watchlist_name,
        "Liste": a.list_type, "Score": f"{a.final_score:.1f}",
        "Échéance": a.due_at.isoformat() if a.due_at else "—",
    } for a in a_notifier[:NOTIFY_BURST_MAX]]
    resume = None
    if len(a_notifier) > NOTIFY_BURST_MAX:
        resume = {
            "Canal": channel,
            "Client": a_notifier[0].client_name,
            "Alertes ouvertes": len(a_notifier),
            "Notifiées individuellement": NOTIFY_BURST_MAX,
            "Score le plus élevé": f"{max(a.final_score for a in a_notifier):.1f}",
        }
    db.commit()

    _notifier_ouvertures(db, charges, resume)
    return resultat


def _notifier_ouvertures(db, charges: List[Dict[str, Any]],
                         resume: Optional[Dict[str, Any]]) -> None:
    """Notifie les alertes ouvertes — individuellement tant que le volume le
    permet, puis une seule fois pour le reste. Aucune notification pour les
    alertes auto-cloturees par regle (comportement d'origine)."""
    if not charges:
        return
    from fiskr.notifier import emit
    for charge in charges:
        emit(db, "alert_created", charge)
    if resume is not None:
        emit(db, "alert_volume", resume)

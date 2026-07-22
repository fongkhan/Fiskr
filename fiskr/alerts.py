"""
Logique partagee des alertes de criblage : ouverture/re-detection dedupliquee
et consultation de la liste blanche client x liste. Module separe pour etre
utilisable a la fois par l'API temps reel (fiskr.api) et par le moteur de
re-criblage post-delta (fiskr.rescreen) sans import circulaire.
"""
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from fiskr.database import Alert, AlertEvent, ALERT_OPEN_STATUSES, AuditTrail, WhitelistPair

logger = logging.getLogger("fiskr.alerts")


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


def open_or_redetect_alert(db, audit_record: AuditTrail, client_id: Optional[str],
                           best_match: Dict[str, Any], username: str,
                           detail_suffix: str = "", channel: str = "SCREENING",
                           suppressed_by_rule=None) -> int:
    """
    Ouvre une alerte de travail pour une decision ALERT, ou marque la
    re-detection si une alerte non close existe deja pour la meme paire
    client x liste (pas de doublons a chaque re-criblage).

    `channel` : SCREENING (criblage clients) ou FILTERING (transactions).
    `suppressed_by_rule` (FpRule) : si fourni, l'alerte est creee puis
    immediatement auto-cloturee CLOSED_BY_RULE (jamais silencieuse : la ligne
    d'audit porte deja fp_rule_applied). La dedup vaut aussi pour les alertes
    deja cloturees par regle (pas de doublon a chaque re-criblage).
    """
    wl_entity = best_match.get("watchlist_entity") or {}
    wl_id = wl_entity.get("entity_id", "NONE")

    # Une alerte deja cloturee par regle pour la meme paire est re-detectee au
    # lieu d'etre recreee (maitrise des volumes d'alertes auto-cloturees)
    dedup_statuses = list(ALERT_OPEN_STATUSES)
    if suppressed_by_rule is not None:
        dedup_statuses.append("CLOSED_BY_RULE")

    existing = db.query(Alert).filter(
        Alert.client_id == client_id,
        Alert.watchlist_entity_id == wl_id,
        Alert.status.in_(dedup_statuses)
    ).first()
    if existing:
        if best_match["final_score"] > existing.final_score:
            existing.final_score = best_match["final_score"]
        # Rattrapage progressif des alertes anterieures a la colonne list_type
        if existing.list_type is None and wl_entity.get("_list_type"):
            existing.list_type = wl_entity.get("_list_type")
        if suppressed_by_rule is not None:
            detail = (f"Re-détectée puis à nouveau supprimée par la règle « {suppressed_by_rule.name} » "
                      f"(v{suppressed_by_rule.version}, audit #{audit_record.id}).{detail_suffix}")
        else:
            detail = (f"Re-détectée lors d'un nouveau criblage "
                      f"(score {best_match['final_score']:.1f}, audit #{audit_record.id}).{detail_suffix}")
        db.add(AlertEvent(alert_id=existing.id, username=username, action="REDETECTED", detail=detail))
        db.commit()
        return existing.id

    now = datetime.utcnow()
    suppressed = suppressed_by_rule is not None
    alert = Alert(
        audit_id=audit_record.id,
        channel=channel,
        client_id=client_id,
        client_name=audit_record.client_name,
        watchlist_entity_id=wl_id,
        watchlist_name=wl_entity.get("primary_name", "Inconnu"),
        final_score=best_match["final_score"],
        list_type=wl_entity.get("_list_type"),
        status="CLOSED_BY_RULE" if suppressed else "OPEN",
    )
    if suppressed:
        alert.decided_by = "fp-rule"
        alert.decided_at = now
        alert.decision_comment = (
            f"Faux positif supprimé automatiquement par la règle « {suppressed_by_rule.name} » "
            f"(#{suppressed_by_rule.id} v{suppressed_by_rule.version}). Conservée pour l'audit (ACPR/FED)."
        )
    db.add(alert)
    db.flush()
    db.add(AlertEvent(
        alert_id=alert.id, username=username, action="CREATED",
        detail=f"Alerte créée par le criblage (score {best_match['final_score']:.1f}).{detail_suffix}"
    ))
    if suppressed:
        db.add(AlertEvent(
            alert_id=alert.id, username="fp-rule", action="RULE_SUPPRESSED",
            detail=(f"Auto-clôturée CLOSED_BY_RULE par la règle « {suppressed_by_rule.name} » "
                    f"(#{suppressed_by_rule.id} v{suppressed_by_rule.version}).")
        ))
    db.commit()
    return alert.id

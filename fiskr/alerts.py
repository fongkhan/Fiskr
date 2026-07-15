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
                           detail_suffix: str = "") -> int:
    """
    Ouvre une alerte de travail pour une decision ALERT, ou marque la
    re-detection si une alerte non close existe deja pour la meme paire
    client x liste (pas de doublons a chaque re-criblage).
    """
    wl_entity = best_match.get("watchlist_entity") or {}
    wl_id = wl_entity.get("entity_id", "NONE")

    existing = db.query(Alert).filter(
        Alert.client_id == client_id,
        Alert.watchlist_entity_id == wl_id,
        Alert.status.in_(ALERT_OPEN_STATUSES)
    ).first()
    if existing:
        if best_match["final_score"] > existing.final_score:
            existing.final_score = best_match["final_score"]
        db.add(AlertEvent(
            alert_id=existing.id, username=username, action="REDETECTED",
            detail=f"Re-détectée lors d'un nouveau criblage (score {best_match['final_score']:.1f}, audit #{audit_record.id}).{detail_suffix}"
        ))
        db.commit()
        return existing.id

    alert = Alert(
        audit_id=audit_record.id,
        client_id=client_id,
        client_name=audit_record.client_name,
        watchlist_entity_id=wl_id,
        watchlist_name=wl_entity.get("primary_name", "Inconnu"),
        final_score=best_match["final_score"],
        status="OPEN"
    )
    db.add(alert)
    db.flush()
    db.add(AlertEvent(
        alert_id=alert.id, username=username, action="CREATED",
        detail=f"Alerte créée par le criblage (score {best_match['final_score']:.1f}).{detail_suffix}"
    ))
    db.commit()
    return alert.id

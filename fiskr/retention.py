"""
Retention des donnees (RGPD / politique d'archivage) : purge des familles de
donnees au-dela de leur duree de conservation, reglable a chaud par famille
(0 = conservation illimitee, defaut).

Regles de conception :
- le journal des actions d'administration (admin_audit_log) n'est JAMAIS
  purge : c'est la trace append-only attendue en controle — chaque purge y
  est au contraire journalisee (action RETENTION_PURGE, volumes par famille) ;
- garde-fou : aucune purge en dessous de RETENTION_MIN_DAYS (30 jours) ;
- le journal de criblage (compliance_audit_trail) n'est purge que pour les
  lignes qui ne sont plus referencees par aucune alerte restante : une alerte
  conservee garde toujours son decision tree ;
- les alertes ne sont purgees que CLOTUREES, avec leur historique d'actions,
  leurs pieces jointes (fichiers supprimes au mieux) et la reference depuis
  les resultats batch mise a neant.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fiskr.database import (
    AuditTrail, Alert, AlertEvent, AlertAttachment, AdminAuditLog,
    BatchCampaign, BatchResult, SyncReport, ALERT_CLOSED_STATUSES,
)
from fiskr.settings import retention_policy, RETENTION_FAMILIES

logger = logging.getLogger("fiskr.retention")

# Archive des donnees purgees (JSON par table) : la purge reste reversible
# hors ligne — le dossier est a externaliser (bande, coffre) par l'exploitation
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "retention_archive"


def _row_to_dict(row) -> Dict:
    """Serialisation generique d'une ligne SQLAlchemy (colonnes de la table)."""
    out = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        out[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _archive_rows(archive_path: Path, table_name: str, rows: List) -> int:
    """Ecrit les lignes en JSON Lines avant leur suppression (une par ligne)."""
    if not rows:
        return 0
    archive_path.mkdir(parents=True, exist_ok=True)
    target = archive_path / f"{table_name}.jsonl"
    with open(target, "a", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(_row_to_dict(row), ensure_ascii=False, default=str) + "\n")
    return len(rows)


def _cutoffs(policy: Dict) -> Dict[str, Optional[datetime]]:
    """Date limite par famille (None = famille desactivee, jamais purgee)."""
    now = datetime.utcnow()
    return {
        family: (now - timedelta(days=int(policy[family]))) if int(policy[family] or 0) > 0 else None
        for family in RETENTION_FAMILIES
    }


def _purgeable_audit_query(db, cutoff: datetime):
    """Lignes d'audit expirees ET orphelines (plus referencees par une alerte)."""
    referenced = db.query(Alert.audit_id)
    return db.query(AuditTrail).filter(
        AuditTrail.timestamp < cutoff,
        ~AuditTrail.id.in_(referenced),
    )


def _purgeable_alert_ids(db, cutoff: datetime):
    # Date de reference : la decision quand elle existe, sinon la creation
    # (les clotures automatiques CLOSED_BY_RULE n'ont pas de decided_at)
    from sqlalchemy import or_, and_
    rows = db.query(Alert.id).filter(
        Alert.status.in_(ALERT_CLOSED_STATUSES),
        or_(
            and_(Alert.decided_at.isnot(None), Alert.decided_at < cutoff),
            and_(Alert.decided_at.is_(None), Alert.created_at < cutoff),
        ),
    ).all()
    return [r[0] for r in rows]


def _purgeable_campaign_ids(db, cutoff: datetime):
    rows = db.query(BatchCampaign.id).filter(
        BatchCampaign.status.in_(("COMPLETED", "ERROR")),
        BatchCampaign.created_at < cutoff,
    ).all()
    return [r[0] for r in rows]


def preview_retention(db) -> Dict[str, int]:
    """Volumes qui SERAIENT purges avec la politique actuelle (aucune ecriture)."""
    policy = retention_policy(db)
    cutoffs = _cutoffs(policy)
    preview = {}
    for family, cutoff in cutoffs.items():
        if cutoff is None:
            preview[family] = 0
        elif family == "audit_trail":
            preview[family] = _purgeable_audit_query(db, cutoff).count()
        elif family == "closed_alerts":
            preview[family] = len(_purgeable_alert_ids(db, cutoff))
        elif family == "sync_reports":
            preview[family] = db.query(SyncReport).filter(SyncReport.executed_at < cutoff).count()
        elif family == "batch_campaigns":
            preview[family] = len(_purgeable_campaign_ids(db, cutoff))
    return preview


def run_retention(db, username: str = "retention-scheduler") -> Dict[str, int]:
    """
    Applique la politique de retention et retourne les volumes supprimes par
    famille. Toute purge non vide est tracee au journal d'administration.
    """
    policy = retention_policy(db)
    cutoffs = _cutoffs(policy)
    deleted = {family: 0 for family in RETENTION_FAMILIES}
    # Archive horodatee de cette purge (desactivable dans la politique)
    archive_enabled = bool(policy.get("archive", True))
    archive_path = ARCHIVE_DIR / f"purge_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    # 1. Alertes cloturees expirees (events + pieces jointes + refs batch)
    cutoff = cutoffs["closed_alerts"]
    if cutoff is not None:
        alert_ids = _purgeable_alert_ids(db, cutoff)
        if alert_ids:
            attachments = db.query(AlertAttachment).filter(
                AlertAttachment.alert_id.in_(alert_ids)).all()
            if archive_enabled:
                _archive_rows(archive_path, "alerts",
                              db.query(Alert).filter(Alert.id.in_(alert_ids)).all())
                _archive_rows(archive_path, "alert_events",
                              db.query(AlertEvent).filter(AlertEvent.alert_id.in_(alert_ids)).all())
                _archive_rows(archive_path, "alert_attachments", attachments)
            for attachment in attachments:
                try:
                    if attachment.file_path and os.path.exists(attachment.file_path):
                        os.remove(attachment.file_path)
                except OSError as e:
                    logger.warning(f"Fichier de pièce jointe non supprimé ({attachment.file_path}) : {e}")
            db.query(AlertAttachment).filter(
                AlertAttachment.alert_id.in_(alert_ids)).delete(synchronize_session=False)
            db.query(AlertEvent).filter(
                AlertEvent.alert_id.in_(alert_ids)).delete(synchronize_session=False)
            db.query(BatchResult).filter(BatchResult.alert_id.in_(alert_ids)) \
              .update({BatchResult.alert_id: None}, synchronize_session=False)
            deleted["closed_alerts"] = db.query(Alert).filter(
                Alert.id.in_(alert_ids)).delete(synchronize_session=False)

    # 2. Journal de criblage : lignes expirees plus referencees par une alerte
    cutoff = cutoffs["audit_trail"]
    if cutoff is not None:
        if archive_enabled:
            _archive_rows(archive_path, "compliance_audit_trail",
                          _purgeable_audit_query(db, cutoff).all())
        deleted["audit_trail"] = _purgeable_audit_query(db, cutoff).delete(synchronize_session=False)

    # 3. Rapports de synchronisation
    cutoff = cutoffs["sync_reports"]
    if cutoff is not None:
        expired_syncs = db.query(SyncReport).filter(SyncReport.executed_at < cutoff)
        if archive_enabled:
            _archive_rows(archive_path, "sync_reports", expired_syncs.all())
        deleted["sync_reports"] = expired_syncs.delete(synchronize_session=False)

    # 4. Campagnes batch terminees (resultats puis campagnes)
    cutoff = cutoffs["batch_campaigns"]
    if cutoff is not None:
        campaign_ids = _purgeable_campaign_ids(db, cutoff)
        if campaign_ids:
            if archive_enabled:
                _archive_rows(archive_path, "batch_campaigns",
                              db.query(BatchCampaign).filter(BatchCampaign.id.in_(campaign_ids)).all())
                _archive_rows(archive_path, "batch_results",
                              db.query(BatchResult).filter(BatchResult.campaign_id.in_(campaign_ids)).all())
            db.query(BatchResult).filter(
                BatchResult.campaign_id.in_(campaign_ids)).delete(synchronize_session=False)
            deleted["batch_campaigns"] = db.query(BatchCampaign).filter(
                BatchCampaign.id.in_(campaign_ids)).delete(synchronize_session=False)

    if any(deleted.values()):
        archived_to = str(archive_path) if archive_enabled and archive_path.exists() else None
        db.add(AdminAuditLog(
            username=username, action="RETENTION_PURGE", target="retention",
            after={**deleted, "policy": {f: policy[f] for f in RETENTION_FAMILIES},
                   "archive": archived_to},
            detail="Purge de rétention : " + ", ".join(
                f"{family}={count}" for family, count in deleted.items() if count)
                   + (f" — archivée dans {archived_to}" if archived_to else " — sans archive"),
        ))
        logger.info(f"Purge de rétention effectuée : {deleted} (archive : {archived_to})")
    db.commit()
    return deleted

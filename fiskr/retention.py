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
  leurs pieces jointes et la reference depuis les resultats batch mise a
  neant. Les FICHIERS des pieces jointes sont COPIES dans l'archive avant
  d'etre supprimes, et une alerte dont une piece n'a pas pu etre copiee
  n'est pas purgee du tout (cf. _archiver_fichier).
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
from fiskr.settings import (retention_policy, RETENTION_FAMILIES,
                            retention_sous_la_duree_legale)

logger = logging.getLogger("fiskr.retention")

# Archive des donnees purgees (JSON par table) : la purge reste reversible
# hors ligne — le dossier est a externaliser (bande, coffre) par l'exploitation
ARCHIVE_DIR = Path(__file__).resolve().parent.parent / "retention_archive"

# Sous-dossier des FICHIERS de pieces jointes dans une archive de purge.
#
# POURQUOI IL EXISTE
# ------------------
# L'archive ecrivait les LIGNES des pieces jointes — identifiant, nom, chemin
# — et le fichier, lui, partait au `os.remove`. Restaurer cette archive rendait
# donc des references vers des fichiers detruits : la promesse « la purge reste
# reversible » etait fausse pour la seule chose qu'un auditeur demande, la
# piece elle-meme. Une preuve ne se reconstitue pas.
ARCHIVE_FICHIERS = "alert_attachments_fichiers"


def _row_to_dict(row) -> Dict:
    """Serialisation generique d'une ligne SQLAlchemy (colonnes de la table)."""
    out = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        out[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return out


def _archive_rows(archive_path: Path, table_name: str, rows: List, extra=None) -> int:
    """
    Ecrit les lignes en JSON Lines avant leur suppression (une par ligne).

    `extra(row)` ajoute des champs a la ligne archivee — c'est par la que la
    piece jointe emporte le nom du fichier copie a cote d'elle : sans ce
    renvoi, l'archive contiendrait un chemin d'origine qui ne designe plus
    rien, et retrouver la piece demanderait de deviner.
    """
    if not rows:
        return 0
    archive_path.mkdir(parents=True, exist_ok=True)
    target = archive_path / f"{table_name}.jsonl"
    with open(target, "a", encoding="utf-8") as out:
        for row in rows:
            ligne = _row_to_dict(row)
            if extra is not None:
                ligne.update(extra(row) or {})
            out.write(json.dumps(ligne, ensure_ascii=False, default=str) + "\n")
    return len(rows)


def _archiver_fichier(archive_path: Path, attachment) -> Optional[str]:
    """
    Copie le fichier d'une piece jointe dans l'archive de la purge.

    Retourne le nom du fichier archive, ou None si la copie n'a pas eu lieu.
    Le nom porte l'identifiant de la piece : deux alertes peuvent avoir
    televerse « scan.pdf », et l'archive ne doit pas en perdre une.
    """
    import shutil
    source = attachment.file_path
    if not source:
        return None
    try:
        if not os.path.isfile(source):
            return None
        cible_dir = archive_path / ARCHIVE_FICHIERS
        cible_dir.mkdir(parents=True, exist_ok=True)
        nom = f"{attachment.id}_{os.path.basename(source)}"
        shutil.copy2(source, cible_dir / nom)
        return nom
    except OSError as e:
        logger.warning(f"Pièce jointe non archivée ({source}) : {e}")
        return None


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
    # Sort des FICHIERS de pieces jointes, suivi separement des lignes : ce
    # sont eux la preuve, et ce sont eux qui ne reviennent pas.
    pieces = {"archivees": 0, "supprimees": 0, "non_archivees": [], "orphelines": []}
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

            # Les FICHIERS d'abord, et le sort des lignes en depend. Une piece
            # dont le fichier existe mais n'a pas pu etre copie fait RETIRER
            # son alerte de la purge : detruire une preuve sans en garder de
            # copie ne se rattrape pas, alors qu'une alerte gardee un mois de
            # plus se repurge au prochain passage.
            a_epargner = set()
            copies = {}
            if archive_enabled:
                for attachment in attachments:
                    if not attachment.file_path or not os.path.isfile(attachment.file_path):
                        continue  # rien a copier : la ligne partira seule
                    nom = _archiver_fichier(archive_path, attachment)
                    if nom is None:
                        a_epargner.add(attachment.alert_id)
                        pieces["non_archivees"].append(
                            {"alerte": attachment.alert_id, "fichier": attachment.file_name})
                    else:
                        copies[attachment.id] = nom
                        pieces["archivees"] += 1
            if a_epargner:
                alert_ids = [i for i in alert_ids if i not in a_epargner]
                attachments = [a for a in attachments if a.alert_id not in a_epargner]
                logger.warning("Purge de rétention : %d alerte(s) épargnée(s), "
                               "leur pièce probante n'a pas pu être archivée.",
                               len(a_epargner))
            if alert_ids:
                if archive_enabled:
                    _archive_rows(archive_path, "alerts",
                                  db.query(Alert).filter(Alert.id.in_(alert_ids)).all())
                    _archive_rows(archive_path, "alert_events",
                                  db.query(AlertEvent).filter(AlertEvent.alert_id.in_(alert_ids)).all())
                    _archive_rows(archive_path, "alert_attachments", attachments,
                                  extra=lambda row: {"archive_fichier": copies.get(row.id)})
                for attachment in attachments:
                    if not attachment.file_path or not os.path.exists(attachment.file_path):
                        continue
                    try:
                        os.remove(attachment.file_path)
                        pieces["supprimees"] += 1
                    except OSError as e:
                        # La base va dire « purgé » alors que la donnée est encore
                        # sur le disque : c'est exactement l'inverse de ce que la
                        # politique de rétention promet, et personne ne le voyait.
                        logger.warning(f"Fichier de pièce jointe non supprimé ({attachment.file_path}) : {e}")
                        pieces["orphelines"].append(
                            {"alerte": attachment.alert_id, "chemin": attachment.file_path,
                             "erreur": str(e)[:200]})
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

    # Dernière ligne : une purge qui emporte de la preuve sous le plancher légal
    # le DIT, dans le journal que le contrôle lira. Elle ne s'interrompt pas —
    # ce serait décider à la place de l'exploitant, et sans lui laisser de
    # moyen d'agir — mais elle ne passe plus inaperçue.
    ecarts = retention_sous_la_duree_legale(policy)
    if any(deleted.values()):
        archived_to = str(archive_path) if archive_enabled and archive_path.exists() else None
        avertissement = ""
        if ecarts and any(deleted.get(e["famille"]) for e in ecarts):
            avertissement = " — CONSERVATION SOUS LA DURÉE LÉGALE : " + " ; ".join(
                e["message"] for e in ecarts if deleted.get(e["famille"]))
            logger.warning("Purge de rétention sous la durée légale :%s", avertissement)
        db.add(AdminAuditLog(
            username=username, action="RETENTION_PURGE", target="retention",
            after={**deleted, "policy": {f: policy[f] for f in RETENTION_FAMILIES},
                   "archive": archived_to, "pieces": pieces,
                   "sous_la_duree_legale": [e["famille"] for e in ecarts
                                            if deleted.get(e["famille"])]},
            detail="Purge de rétention : " + ", ".join(
                f"{family}={count}" for family, count in deleted.items() if count)
                   + (f" — archivée dans {archived_to}" if archived_to else " — sans archive")
                   + (f" — {pieces['archivees']} pièce(s) jointe(s) copiée(s) dans l'archive"
                      if pieces["archivees"] else "")
                   + avertissement,
        ))
        logger.info(f"Purge de rétention effectuée : {deleted} (archive : {archived_to})")

    # Le sort des fichiers se dit MEME quand rien n'a ete supprime en base :
    # une alerte epargnee parce que sa preuve n'a pas pu etre copiee produit
    # justement zero ligne supprimee, et c'est le cas ou il faut parler.
    if pieces["non_archivees"] or pieces["orphelines"]:
        _signaler_pieces_en_souffrance(db, pieces, archive_path if archive_enabled else None)

    db.commit()
    return deleted


def _signaler_pieces_en_souffrance(db, pieces: Dict, archive_path: Optional[Path]) -> None:
    """
    Deux situations, deux consequences opposees, et aucune ne doit se taire.

    - `non_archivees` : la piece est TOUJOURS la, l'alerte n'a pas ete purgee,
      et il reste donc une fenetre pour agir. C'est reparable.
    - `orphelines` : la ligne est partie, le fichier est reste. La base
      affirme que la donnee est purgee alors qu'elle est encore sur le
      disque — l'inverse exact de ce que la politique de retention promet, et
      cette fois personne ne peut le deviner en lisant l'application.
    """
    from fiskr.notifier import emit
    charge = {
        "Pièces non archivées (alertes épargnées)": len(pieces["non_archivees"]),
        "Fichiers restés sur le disque (lignes purgées)": len(pieces["orphelines"]),
        "Archive": str(archive_path) if archive_path else "désactivée",
    }
    if pieces["non_archivees"]:
        charge["Détail non archivées"] = " ; ".join(
            f"alerte #{p['alerte']} · {p['fichier']}" for p in pieces["non_archivees"][:10])
    if pieces["orphelines"]:
        charge["Détail restés sur le disque"] = " ; ".join(
            f"alerte #{p['alerte']} · {p['chemin']}" for p in pieces["orphelines"][:10])
    try:
        emit(db, "retention_pieces_en_souffrance", charge)
    except Exception as e:  # une notification en echec n'annule pas une purge
        logger.error(f"Signalement des pièces en souffrance impossible : {e}")

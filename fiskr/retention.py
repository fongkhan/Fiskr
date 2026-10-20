"""
Retention des donnees (RGPD / politique d'archivage) : purge des familles de
donnees au-dela de leur duree de conservation, reglable a chaud par famille
(0 = conservation illimitee, defaut).

Regles de conception :
- le journal des actions d'administration (admin_audit_log) n'est JAMAIS
  purge : c'est la trace append-only attendue en controle — chaque purge y
  est au contraire journalisee (action RETENTION_PURGE, volumes par famille) ;
- garde-fou : aucune purge en dessous de RETENTION_MIN_DAYS (30 jours) ;
- les familles PROBANTES (journal de criblage, alertes cloturees avec leurs
  pieces) comptent leur duree depuis la FIN DE LA RELATION, jamais en dessous
  de cinq annees civiles (CMF L.561-12, cf. fiskr/conservation.py) : un
  client en relation, ou sans date de fin, n'est jamais purge ;
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

from fiskr import conservation
from fiskr.database import (
    AuditTrail, Alert, AlertEvent, AlertAttachment, AdminAuditLog,
    BatchCampaign, BatchResult, SyncReport, ALERT_CLOSED_STATUSES,
)
from fiskr.settings import (retention_policy, RETENTION_FAMILIES,
                            RETENTION_FAMILLES_PROBANTES)

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


def _dates_de_la_politique(policy: Dict, now: datetime) -> Dict[str, Optional[datetime]]:
    """Ce que la politique, seule, purgerait : la date de la ligne contre la
    duree reglee (None = famille desactivee)."""
    return {
        family: (now - timedelta(days=int(policy[family]))) if int(policy[family] or 0) > 0 else None
        for family in RETENTION_FAMILIES
    }


def _cutoffs(policy: Dict, maintenant: Optional[datetime] = None) -> Dict[str, Optional[datetime]]:
    """
    Date limite par famille (None = famille desactivee, jamais purgee).

    Pour une famille probante, c'est la date avant laquelle le DELAI doit avoir
    commence a courir — fin de la relation, ou operation occasionnelle — et
    jamais plus recente que cinq annees civiles : la purge comptait depuis la
    date de la ligne, et une politique a 1 825 jours detruisait le dossier
    d'un client toujours en portefeuille (fiskr/conservation.py).
    """
    now = maintenant or datetime.utcnow()
    plancher = conservation.limite_legale(now)
    limites = _dates_de_la_politique(policy, now)
    for family in RETENTION_FAMILLES_PROBANTES:
        if limites.get(family) is not None:
            limites[family] = min(limites[family], plancher)
    return limites


def _audit_orphelin(db, depuis: datetime):
    """Lignes d'audit plus anciennes que `depuis` et plus referencees par
    aucune alerte."""
    referenced = db.query(Alert.audit_id)
    # Le DERNIER criblage d'une alerte est l'arbre que son valideur lit
    # (fiskr/alerts.py, `latest_audit_id`) : il est reference au meme titre.
    derniers = db.query(Alert.latest_audit_id).filter(Alert.latest_audit_id.isnot(None))
    return db.query(AuditTrail).filter(
        AuditTrail.timestamp < depuis,
        ~AuditTrail.id.in_(referenced),
        ~AuditTrail.id.in_(derniers),
    )


def _motif_audit(cutoff: datetime):
    return conservation.motif(AuditTrail.client_id, AuditTrail.timestamp, cutoff)


def _purgeable_audit_query(db, cutoff: datetime):
    """Lignes d'audit orphelines dont le delai de conservation a couru avant
    `cutoff` (fin de relation, ou ligne d'un client sans fiche)."""
    return _audit_orphelin(db, cutoff).filter(_motif_audit(cutoff) == conservation.PURGEABLE)


def _date_de_l_alerte():
    # Date de reference : la decision quand elle existe, sinon la creation
    # (les clotures automatiques CLOSED_BY_RULE n'ont pas de decided_at)
    from sqlalchemy import func
    return func.coalesce(Alert.decided_at, Alert.created_at)


def _alertes_cloturees(db, depuis: datetime, *colonnes):
    from fiskr.database import DossierPPE, MesureDeGel
    # Une alerte qui porte une mesure de gel est le DOSSIER de cette mesure :
    # la piece de declaration a la DG Tresor est une piece jointe de l'alerte,
    # et le registre la reference (cle etrangere). La purger effacerait la
    # preuve d'une declaration — pendant que le gel court, ou apres sa levee —
    # et casserait la cle sur PostgreSQL. Le registre des gels n'est pas une
    # famille de retention (comme le journal d'administration, il n'est jamais
    # purge) ; les alertes qu'il porte ne le sont donc pas non plus.
    # Meme raison pour le dossier PPE (fiskr/ppe.py) : l'origine des fonds et la
    # decision de la direction sont des pieces de l'alerte, et le dossier la
    # reference par cle etrangere.
    tenues_par_un_gel = db.query(MesureDeGel.alert_id)
    tenues_par_un_dossier_ppe = db.query(DossierPPE.alert_id)
    return db.query(*colonnes).filter(
        Alert.status.in_(ALERT_CLOSED_STATUSES),
        _date_de_l_alerte() < depuis,
        ~Alert.id.in_(tenues_par_un_gel),
        ~Alert.id.in_(tenues_par_un_dossier_ppe),
    )


def _motif_alerte(cutoff: datetime):
    return conservation.motif(Alert.client_id, _date_de_l_alerte(), cutoff)


def _purgeable_alert_ids(db, cutoff: datetime):
    rows = _alertes_cloturees(db, cutoff, Alert.id).filter(
        _motif_alerte(cutoff) == conservation.PURGEABLE).all()
    return [r[0] for r in rows]


def conservees_au_titre_de_la_relation(db, policy: Optional[Dict] = None,
                                       maintenant: Optional[datetime] = None) -> Dict[str, Dict[str, int]]:
    """
    Par famille probante : combien de pieces la politique aurait purgees (leur
    date est plus ancienne que la duree reglee) et que la loi garde, et
    pourquoi (cf. conservation.MOTIFS). Lu par l'ecran de retention et ecrit
    au journal de chaque purge : ce qui est annonce comme conserve est ce que
    la purge epargne, par la MEME expression.
    """
    from sqlalchemy import func
    policy = policy or retention_policy(db)
    now = maintenant or datetime.utcnow()
    politique = _dates_de_la_politique(policy, now)
    limites = _cutoffs(policy, now)
    sources = {
        "audit_trail": lambda depuis, limite: _audit_orphelin(db, depuis).with_entities(
            _motif_audit(limite).label("motif")),
        "closed_alerts": lambda depuis, limite: _alertes_cloturees(
            db, depuis, _motif_alerte(limite).label("motif")),
    }
    sortie: Dict[str, Dict[str, int]] = {}
    for family in RETENTION_FAMILLES_PROBANTES:
        depuis, limite = politique.get(family), limites.get(family)
        if depuis is None or family not in sources:
            continue
        # Regroupement sur la colonne d'une sous-requete, pas sur l'expression :
        # PostgreSQL refuse un GROUP BY dont l'expression ne se reconnait pas
        # a l'identique dans le SELECT.
        sous = sources[family](depuis, limite).subquery()
        comptes = {m: int(n) for m, n in db.query(sous.c.motif, func.count())
                   .group_by(sous.c.motif).all()}
        sortie[family] = {m: comptes.get(m, 0) for m in conservation.MOTIFS}
    return sortie


def _purgeable_campaign_ids(db, cutoff: datetime):
    rows = db.query(BatchCampaign.id).filter(
        BatchCampaign.status.in_(("COMPLETED", "ERROR")),
        BatchCampaign.created_at < cutoff,
    ).all()
    return [r[0] for r in rows]


def preview_retention(db, maintenant: Optional[datetime] = None) -> Dict[str, int]:
    """Volumes qui SERAIENT purges avec la politique actuelle (aucune ecriture)."""
    policy = retention_policy(db)
    cutoffs = _cutoffs(policy, maintenant)
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

    # Le journal dit sous quel plancher la purge a compte, et ce qu'il a garde.
    # Il disait « CONSERVATION SOUS LA DURÉE LÉGALE » quand la politique etait
    # reglee sous cinq ans : ce n'est plus vrai — les familles probantes ne
    # purgent que des pieces dont les cinq ans depuis la fin de la relation
    # sont ecoules (cf. _cutoffs) — et le journal ne l'affirmera donc plus.
    if any(deleted.values()):
        archived_to = str(archive_path) if archive_enabled and archive_path.exists() else None
        gardees = conservees_au_titre_de_la_relation(db, policy)
        db.add(AdminAuditLog(
            username=username, action="RETENTION_PURGE", target="retention",
            after={**deleted, "policy": {f: policy[f] for f in RETENTION_FAMILIES},
                   "archive": archived_to, "pieces": pieces,
                   "plancher_legal": {
                       "article": "CMF L.561-12",
                       "delai_ecoule_avant": conservation.limite_legale().date().isoformat()},
                   "conservees_au_titre_de_la_relation": gardees},
            detail="Purge de rétention : " + ", ".join(
                f"{family}={count}" for family, count in deleted.items() if count)
                   + (f" — archivée dans {archived_to}" if archived_to else " — sans archive")
                   + (f" — {pieces['archivees']} pièce(s) jointe(s) copiée(s) dans l'archive"
                      if pieces["archivees"] else ""),
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

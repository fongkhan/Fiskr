"""
Un import est-il alle au bout ? Ce que la reparation des instantanes ne
savait pas, et affirmait quand meme.

`_repair_stuck_snapshots` remet d'aplomb les instantanes restes en
PROCESSING plus d'une heure. Elle est nee d'un defaut precis — un import
TERMINE dont le statut n'etait pas persiste — et en a tire une regle trop
large : « les fiches sont la, l'import avait bien abouti ». Compter les
fiches ne dit pas si l'import est alle au bout. Releve en production (journal
d'administration, 25 novembre 2026) : les cinq instantanes PEP « repares »
etaient tous des imports INTERROMPUS, promus avec leur compte du moment —
625 000, 548 000, 61 000, 0 et 297 000 fiches, quand une liste PEP complete en
compte environ 713 000. Des multiples ronds de 1 000 : la taille d'un lot de
la boucle de persistance, la ou elle s'etait arretee. Celui de 548 000 a ete
homologue : il est la liste PEP en production depuis le 1er septembre.

Les deux preuves qu'un import est alle au bout, et la seule qui compte :
- le travail qui l'ecrivait (synchronisation ou import de fichier) s'est
  TERMINE sans erreur ;
- ou son rapport de synchronisation existe, avec un statut d'aboutissement.
Sans l'une ou l'autre, un instantane en PROCESSING est un import interrompu :
il ne se promeut pas.

Et tant que ce travail est vivant (en file ou en cours), l'instantane n'est
pas « bloque » : il est en train de s'ecrire. Un travail qui ne progresse
plus est arrete par `jobs.declarer_figes` ; c'est ensuite seulement que
l'instantane se juge.
"""
from typing import Any, Dict, List, Optional

# Travaux qui ECRIVENT un instantane de liste ou de referentiel. Un travail
# d'homologation (`approve`) porte lui aussi l'identifiant de l'instantane,
# mais il le met en production : il ne dit rien de son import.
TRAVAUX_QUI_ECRIVENT = ("sync", "ingest")

# Statuts d'un rapport de synchronisation qui attestent un import abouti.
RAPPORTS_ABOUTIS = ("SUCCESS", "PENDING_REVIEW")

ACTION_REPARATION = "SNAPSHOT_REPAIRED"
STATUTS_PROMUS = ("READY", "PENDING_REVIEW")


def milliers(n) -> str:
    """« 548 000 » : un compte de fiches lisible d'un coup d'oeil."""
    return f"{int(n or 0):,}".replace(",", " ")


def travail_vivant(db, snapshot_id: str):
    """Le travail en file ou en cours qui ecrit cet instantane, ou None."""
    from fiskr.database import Job
    return db.query(Job).filter(
        Job.snapshot_id == snapshot_id,
        Job.kind.in_(TRAVAUX_QUI_ECRIVENT),
        Job.status.in_(("QUEUED", "RUNNING")),
    ).first()


def achevement_prouve(db, snapshot_id: str) -> bool:
    """Vrai si l'import de cet instantane est ATTESTE termine."""
    from fiskr.database import Job, SyncReport
    if db.query(Job.id).filter(
        Job.snapshot_id == snapshot_id,
        Job.kind.in_(TRAVAUX_QUI_ECRIVENT),
        Job.status == "DONE",
    ).first() is not None:
        return True
    return db.query(SyncReport.id).filter(
        SyncReport.snapshot_id == snapshot_id,
        SyncReport.status.in_(RAPPORTS_ABOUTIS),
    ).first() is not None


def promotions_sans_preuve(db, snapshot_ids) -> Dict[str, Dict[str, Any]]:
    """
    Les reparations qui ont promu ces instantanes SANS preuve d'achevement,
    par instantane, en une lecture groupee du journal (la file d'homologation
    en affiche des dizaines).

    Les instantanes repares avant cette correction n'ont, pour la plupart,
    aucune preuve : leur travail ne s'est jamais termine. Leur ligne du
    journal d'administration est la seule trace de ce qui s'est passe — on la
    lit telle quelle, sans rien reecrire.
    """
    from fiskr.database import AdminAuditLog
    ids = [s for s in dict.fromkeys(snapshot_ids or []) if s]
    if not ids:
        return {}
    dernieres: Dict[str, Any] = {}
    for ligne in db.query(AdminAuditLog).filter(
            AdminAuditLog.action == ACTION_REPARATION,
            AdminAuditLog.target.in_(ids),
    ).order_by(AdminAuditLog.at.asc(), AdminAuditLog.id.asc()).all():
        dernieres[ligne.target] = ligne  # la plus recente l'emporte
    constats = {}
    for snapshot_id, ligne in dernieres.items():
        apres = ligne.after or {}
        if apres.get("status") not in STATUTS_PROMUS:
            continue
        if achevement_prouve(db, snapshot_id):
            continue
        constats[snapshot_id] = {
            "fiches": apres.get("record_count"),
            "reparee_le": ligne.at.isoformat() if ligne.at else None,
        }
    return constats


def promotion_sans_preuve(db, snapshot_id: str) -> Optional[Dict[str, Any]]:
    """La reparation qui a promu cet instantane SANS preuve d'achevement, ou None."""
    return promotions_sans_preuve(db, [snapshot_id]).get(snapshot_id)


def derniere_version_complete(db, file_type: str, sauf: Optional[str] = None):
    """La version la plus recente de cette liste dont l'import est atteste,
    pour donner l'ordre de grandeur d'une liste complete. None sinon."""
    from fiskr.database import Snapshot
    candidats = db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.status.in_(("READY", "PENDING_REVIEW", "SUPERSEDED")),
    ).order_by(Snapshot.uploaded_at.desc()).limit(40).all()
    for snap in candidats:
        if snap.snapshot_id == sauf:
            continue
        if promotion_sans_preuve(db, snap.snapshot_id) is None:
            return snap
    return None


def productions_sans_preuve(db) -> List[Dict[str, Any]]:
    """Les listes en production promues par une reparation sans preuve.
    Deux lectures groupees, plus une par liste trouvee : la sonde de
    conformite la rejoue chaque minute."""
    from fiskr.database import Snapshot, WATCHLIST_FILE_TYPES
    en_production = db.query(Snapshot).filter(
        Snapshot.status == "READY",
        Snapshot.file_type.in_(tuple(WATCHLIST_FILE_TYPES))).all()
    promotions = promotions_sans_preuve(db, [s.snapshot_id for s in en_production])
    constats = []
    for snap in en_production:
        promotion = promotions.get(snap.snapshot_id)
        if promotion is None:
            continue
        complete = derniere_version_complete(db, snap.file_type, sauf=snap.snapshot_id)
        constats.append({
            "file_type": snap.file_type,
            "snapshot_id": snap.snapshot_id,
            "fiches": snap.record_count,
            "reparee_le": promotion["reparee_le"],
            "version_complete": complete.snapshot_id if complete else None,
            "fiches_version_complete": complete.record_count if complete else None,
        })
    return sorted(constats, key=lambda c: c["file_type"])

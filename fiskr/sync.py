"""
Synchronisation automatique des sources de sanctions.

Deux collecteurs sont fournis, executables manuellement (dashboard / API) ou
chaque matin par le planificateur :

1. OFAC  : telechargement du fichier SDN_ADVANCED.XML officiel, ingestion en
           snapshot, delta par rapport a la liste active, puis remplacement
           (les anciens snapshots OFAC passent en SUPERSEDED : les ajouts,
           modifications et suppressions du delta sont appliques au cache).
2. EURLEX: lecture du Journal Officiel de l'UE du jour, detection des actes
           mentionnant "mesures restrictives", scraping heuristique des listes
           (Individus, Entites, Navires, Aeronefs), puis fusion incrementale
           avec la liste EU active (le JO du jour amende la liste, il ne la
           remplace pas) et delta.

Chaque execution produit un rapport de suivi (table sync_reports) affiche dans
l'application et envoye par email si un serveur SMTP est configure (.env).
"""
import os
import re
import uuid
import hashlib
import logging
import smtplib
import unicodedata
from datetime import datetime, date
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from fiskr.config import config, PROJECT_ROOT
from fiskr.quality import evaluate_and_clean
from fiskr.delta import calculate_delta
from fiskr.ingest import (
    parse_ofac_advanced_xml, parse_dgt_gels_json, parse_eu_fsf_xml, parse_un_consolidated_xml,
    parse_pep_targets_csv, parse_ofsi_conlist_csv
)
from fiskr.names import parse_individual_name, ensure_parsed_name
from fiskr.database import Snapshot, WatchlistEntity, SyncReport, compute_checksum
from fiskr.settings import require_approval_enabled

logger = logging.getLogger("fiskr.sync")

DEFAULT_OFAC_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML"
# Version anglaise du Journal Officiel : c'est la reference reglementaire retenue
DEFAULT_EURLEX_DAILY_URL = "https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?ojDate={date}&locale=en"
DEFAULT_EURLEX_KEYWORD = "restrictive measures"

# Registre national des gels des avoirs (Direction generale du Tresor, API
# publique ENGEL sans authentification) : criblage obligatoire pour les
# etablissements assujettis francais (lignes directrices ACPR/DGT).
DEFAULT_DGT_URL = "https://gels-avoirs.dgtresor.gouv.fr/ApiPublic/api/v1/publication/derniere-publication-fichier-json"

# Liste consolidee officielle des sanctions financieres de l'UE (fichiers FSF,
# webgate FSD). {token} est le nom d'utilisateur cree lors de l'inscription
# gratuite sur le webgate de la Commission — a renseigner dans config.yaml.
DEFAULT_EU_FSF_URL = "https://webgate.ec.europa.eu/fsd/fsf/public/files/xmlFullSanctionsList_1_1/content?token={token}"

# Liste consolidee du Conseil de securite de l'ONU (publique, sans token)
DEFAULT_UN_URL = "https://scsanctions.un.org/resources/xml/en/consolidated.xml"

# Dataset PEP OpenSanctions (usage non commercial libre ; licence requise pour
# un usage commercial — opensanctions.org/licensing)
DEFAULT_PEP_URL = "https://data.opensanctions.org/datasets/latest/peps/targets.simple.csv"

# Liste consolidee UK OFSI (HM Treasury, publique, format 2022)
DEFAULT_OFSI_URL = "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"

# Archivage probant : les PDF officiels des actes EUR-Lex font foi en audit
EURLEX_ARCHIVE_DIR = PROJECT_ROOT / "eurlex_archives"

# Le snapshot des ajouts manuels reste toujours actif : il n'est ni fusionne
# ni remplace par les synchronisations automatiques.
MANUAL_SNAPSHOT_ID = "manual-watchlist"

# Taille maximale des listes de details conservees dans le rapport stocke
MAX_REPORT_DETAILS = 100


def get_sync_config() -> Dict[str, Any]:
    """Configuration de synchronisation (config.yaml, section sync) avec defauts."""
    sync_cfg = config.get("sync", {}) or {}
    return {
        "auto_enabled": bool(sync_cfg.get("auto_enabled", False)),
        "schedule_time": sync_cfg.get("schedule_time", "06:00"),
        "ofac": {
            "enabled": bool((sync_cfg.get("ofac") or {}).get("enabled", True)),
            "url": (sync_cfg.get("ofac") or {}).get("url", DEFAULT_OFAC_URL),
        },
        "eurlex": {
            "enabled": bool((sync_cfg.get("eurlex") or {}).get("enabled", True)),
            "daily_journal_url": (sync_cfg.get("eurlex") or {}).get("daily_journal_url", DEFAULT_EURLEX_DAILY_URL),
            "keyword": (sync_cfg.get("eurlex") or {}).get("keyword", DEFAULT_EURLEX_KEYWORD),
        },
        "dgt": {
            "enabled": bool((sync_cfg.get("dgt") or {}).get("enabled", True)),
            "url": (sync_cfg.get("dgt") or {}).get("url", DEFAULT_DGT_URL),
        },
        # Desactive par defaut : necessite un token (inscription gratuite au webgate FSD)
        "eu_fsf": {
            "enabled": bool((sync_cfg.get("eu_fsf") or {}).get("enabled", False)),
            "url": (sync_cfg.get("eu_fsf") or {}).get("url", DEFAULT_EU_FSF_URL),
            "token": str((sync_cfg.get("eu_fsf") or {}).get("token", "") or ""),
        },
        "un": {
            "enabled": bool((sync_cfg.get("un") or {}).get("enabled", True)),
            "url": (sync_cfg.get("un") or {}).get("url", DEFAULT_UN_URL),
        },
        # Desactives par defaut : PEP (volumetrie + licence commerciale
        # OpenSanctions) et OFSI (liste UK, opt-in selon l'exposition)
        "pep": {
            "enabled": bool((sync_cfg.get("pep") or {}).get("enabled", False)),
            "url": (sync_cfg.get("pep") or {}).get("url", DEFAULT_PEP_URL),
        },
        "ofsi": {
            "enabled": bool((sync_cfg.get("ofsi") or {}).get("enabled", False)),
            "url": (sync_cfg.get("ofsi") or {}).get("url", DEFAULT_OFSI_URL),
        },
    }


# ------------------ RECUPERATION HTTP ------------------

def download_to_file(url: str, dest_path: Path, timeout: float = 300.0) -> None:
    """Telecharge un fichier volumineux en streaming vers dest_path."""
    import httpx
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as response:
        response.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in response.iter_bytes(chunk_size=1024 * 256):
                f.write(chunk)


def http_get_text(url: str, timeout: float = 60.0, retries: int = 2) -> str:
    """
    Recupere le contenu textuel d'une page web avec reprises.
    EUR-Lex repond parfois HTTP 202 avec un corps vide (anti-robot / backoff) :
    on reessaie apres un delai, et on echoue franchement plutot que de traiter
    une page vide comme un Journal Officiel sans publication.
    """
    import time
    import httpx
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Fiskr-Compliance/2.4; +https://github.com/fongkhan/Fiskr)",
        "Accept-Language": "fr",
    }
    last_status, last_length = None, 0
    for attempt in range(retries + 1):
        response = httpx.get(url, timeout=timeout, follow_redirects=True, headers=headers)
        if response.status_code == 200 and response.text.strip():
            return response.text
        last_status, last_length = response.status_code, len(response.text)
        logger.warning(f"Reponse HTTP {last_status} ({last_length} octets) de {url}, tentative {attempt + 1}/{retries + 1}")
        if attempt < retries:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Reponse invalide de {url} (HTTP {last_status}, {last_length} octets apres {retries + 1} tentatives)")


# ------------------ PERSISTANCE DES SNAPSHOTS ------------------

def _clamp_to_column_lengths(values: Dict[str, Any]) -> Dict[str, Any]:
    """
    Tronque les valeurs textuelles aux longueurs maximales des colonnes
    (VARCHAR(n)) : les donnees scrapees (titres d'actes EUR-Lex, adresses...)
    peuvent depasser les capacites du schema et faire echouer l'INSERT
    (StringDataRightTruncation sous PostgreSQL).
    """
    for column in WatchlistEntity.__table__.columns:
        max_length = getattr(column.type, "length", None)
        value = values.get(column.name)
        if max_length and isinstance(value, str) and len(value) > max_length:
            values[column.name] = value[:max_length]
    return values


def build_watchlist_entity(snap_id: str, item: Dict[str, Any], report: Dict[str, Any]) -> WatchlistEntity:
    """Construit une ligne WatchlistEntity depuis un enregistrement au schema pivot."""
    parsed_name = item.get("individual_name_parsed") or {}
    alt_addrs = item.get("alternative_addresses")
    if isinstance(alt_addrs, str):
        alt_addrs = [a.strip() for a in alt_addrs.split(";")]
    return WatchlistEntity(**_clamp_to_column_lengths(dict(
        snapshot_id=snap_id,
        entity_id=item.get("entity_id"),
        entity_type=item.get("entity_type"),
        primary_name=report["cleansed_name"],
        individual_name_parsed={
            "first_name": parsed_name.get("first_name", ""),
            "last_name": parsed_name.get("last_name", ""),
            "maiden_name": report["cleansed_maiden_name"]
        },
        aliases=report["cleansed_aliases"],
        dates_of_birth=item.get("dates_of_birth", []),
        date_of_death=item.get("date_of_death"),
        is_deceased=item.get("is_deceased", False),
        gender=report["resolved_gender"],
        countries=item.get("countries", {}),
        place_of_birth=item.get("place_of_birth"),
        address=item.get("address") or item.get("adress"),
        city=item.get("city"),
        state=item.get("state"),
        country=item.get("country"),
        origin=item.get("origin"),
        designation=item.get("designation"),
        designation_reasons=item.get("designation_reasons"),
        additional_informations=item.get("additional_informations") or item.get("additional_info"),
        alternative_addresses=alt_addrs or [],
        imo_number=item.get("imo_number"),
        aircraft_tail_number=item.get("aircraft_tail_number"),
        lei_number=item.get("lei_number"),
        national_registry_ids=item.get("national_registry_ids"),
        other_registration_ids=item.get("other_registration_ids"),
        passport_documents=item.get("passport_documents"),
        national_id_documents=item.get("national_id_documents"),
        other_id_documents=item.get("other_id_documents"),
        entity_checksum=item.get("entity_checksum") or compute_checksum(item)
    )))


def persist_pivot_items(db, snap_id: str, items) -> int:
    """Valide (Quality Gate) et persiste des enregistrements pivots. Retourne le nombre insere."""
    count = 0
    for item in items:
        # Complete le decoupage prenoms / nom de famille des individus quand la
        # source ne le fournit pas (moteur de detection fiskr.names)
        item = ensure_parsed_name(item)
        report = evaluate_and_clean(item)
        if not report["is_valid"]:
            continue
        # Un nom qui ne survit pas au nettoyage (ex: uniquement des caracteres
        # speciaux ou cyrilliques) ne peut pas etre crible : fiche ecartee
        if len([c for c in report["cleansed_name"] if c.isalnum()]) < 2:
            continue
        db.add(build_watchlist_entity(snap_id, item, report))
        count += 1
    return count


def _clone_entity_row(snap_id: str, ent: WatchlistEntity) -> WatchlistEntity:
    """Copie une ligne d'entite existante vers un nouveau snapshot (checksum conserve)."""
    values = {c.name: getattr(ent, c.name) for c in ent.__table__.columns if c.name != "id"}
    values["snapshot_id"] = snap_id
    return WatchlistEntity(**values)


def _latest_ready_snapshot(db, file_type: str) -> Optional[Snapshot]:
    return db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.status == "READY",
        Snapshot.snapshot_id != MANUAL_SNAPSHOT_ID
    ).order_by(Snapshot.uploaded_at.desc()).first()


def _latest_reviewable_snapshot(db, file_type: str) -> Optional[Snapshot]:
    """
    Base de fusion incrementale en mode homologation : le snapshot le plus
    recent encore vivant (en production OU en attente de pointage). Sans cela,
    un JO du jour 2 fusionne sur la production perdrait les entites du pending
    du jour 1 lors de son approbation.
    """
    return db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.status.in_(["READY", "PENDING_REVIEW"]),
        Snapshot.snapshot_id != MANUAL_SNAPSHOT_ID
    ).order_by(Snapshot.uploaded_at.desc()).first()


def _existing_snapshot_with_hash(db, file_type: str, fhash: str) -> Optional[Snapshot]:
    """
    Deduplication par hash etendue aux snapshots en attente d'homologation :
    sans cela, la sync quotidienne recreerait chaque matin un doublon pending
    du meme fichier tant que le pointage n'a pas eu lieu.
    """
    return db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.file_hash == fhash,
        Snapshot.status.in_(["READY", "PENDING_REVIEW"])
    ).first()


def _snapshot_entity_dicts(db, snapshot_id: str) -> List[Dict[str, Any]]:
    ents = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == snapshot_id).all()
    return [{c.name: getattr(e, c.name) for c in e.__table__.columns} for e in ents]


def _supersede_previous_snapshots(db, file_type: str, keep_snapshot_id: str) -> None:
    """
    Applique le delta au referentiel actif : les anciens snapshots READY du meme
    type passent en SUPERSEDED, seul le snapshot le plus recent reste charge en
    cache (les ajouts manuels a la volee sont preserves).
    """
    db.query(Snapshot).filter(
        Snapshot.file_type == file_type,
        Snapshot.status == "READY",
        Snapshot.snapshot_id != keep_snapshot_id,
        Snapshot.snapshot_id != MANUAL_SNAPSHOT_ID
    ).update({"status": "SUPERSEDED"}, synchronize_session=False)


def _truncate_delta_details(delta: Dict[str, Any]) -> Dict[str, Any]:
    """Limite la taille des details stockes dans le rapport (les compteurs restent exacts)."""
    details = delta.get("details", {})
    truncated = {}
    for key in ("added", "removed", "modified"):
        rows = details.get(key, [])
        truncated[key] = rows[:MAX_REPORT_DETAILS]
        if len(rows) > MAX_REPORT_DETAILS:
            truncated[f"{key}_truncated"] = len(rows) - MAX_REPORT_DETAILS
    return {"summary": delta.get("summary", {}), "details": truncated}


def _save_report(db, **kwargs) -> SyncReport:
    report = SyncReport(**kwargs)
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


# ------------------ NOTIFICATION EMAIL ------------------

def send_report_email(report: SyncReport) -> bool:
    """
    Envoie le rapport de synchronisation par email si un serveur SMTP est
    configure (variables SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD /
    SMTP_FROM / SYNC_EMAIL_TO). Retourne False sans erreur si non configure.
    """
    host = os.getenv("SMTP_HOST")
    recipients = [r.strip() for r in os.getenv("SYNC_EMAIL_TO", "").split(",") if r.strip()]
    if not host or not recipients:
        return False

    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", user or "fiskr@localhost")

    body = (
        f"Rapport de synchronisation Fiskr\n"
        f"--------------------------------\n"
        f"Source        : {report.source}\n"
        f"Execution     : {report.executed_at} ({report.trigger})\n"
        f"Statut        : {report.status}\n"
        f"Message       : {report.message or '-'}\n"
        f"Snapshot      : {report.snapshot_id or '-'}\n"
        f"Ajouts        : {report.added_count}\n"
        f"Modifications : {report.modified_count}\n"
        f"Suppressions  : {report.removed_count}\n"
    )
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = f"[Fiskr] Sync {report.source} - {report.status} (+{report.added_count} ~{report.modified_count} -{report.removed_count})"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPNotSupportedError:
                pass
            if user and password:
                server.login(user, password)
            server.sendmail(sender, recipients, msg.as_string())
        return True
    except Exception as e:
        logger.error(f"Echec de l'envoi de l'email de rapport: {e}")
        return False


def _finalize_report(db, **kwargs) -> SyncReport:
    """Persiste le rapport, tente l'envoi email et memorise le resultat."""
    report = _save_report(db, **kwargs)
    if send_report_email(report):
        report.email_sent = True
        db.commit()
    return report


# ------------------ SYNCHRONISATION OFAC ------------------

def run_ofac_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge le fichier OFAC SDN_ADVANCED.XML, l'ingere en snapshot, calcule
    le delta par rapport a la liste OFAC active et applique le remplacement.
    """
    cfg = get_sync_config()["ofac"]
    url = cfg["url"]
    fetch = fetcher or download_to_file

    temp_dir = PROJECT_ROOT / "temp_ingestion"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"ofac_sync_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.xml"

    previous = _latest_ready_snapshot(db, "WATCHLIST_OFAC")
    snap_id = None
    try:
        logger.info(f"Sync OFAC: telechargement de {url}")
        fetch(url, temp_file)

        with open(temp_file, "rb") as f:
            fhash = hashlib.sha256(f.read()).hexdigest()

        duplicate = _existing_snapshot_with_hash(db, "WATCHLIST_OFAC", fhash)
        if duplicate:
            if duplicate.status == "PENDING_REVIEW":
                message = "Le fichier OFAC est identique a un snapshot deja en attente d'homologation."
            else:
                message = "Le fichier OFAC est identique a la version active (hash inchange)."
            return _finalize_report(
                db, source="OFAC", trigger=trigger, status="NO_CHANGE",
                message=message,
                previous_snapshot_id=duplicate.snapshot_id
            )

        # Ingestion du nouveau snapshot
        snap_id = f"ofac-sync-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type="WATCHLIST_OFAC",
            file_name=f"SDN_ADVANCED_{datetime.utcnow().strftime('%Y-%m-%d')}.xml",
            file_hash=fhash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()

        record_count = persist_pivot_items(db, snap_id, parse_ofac_advanced_xml(str(temp_file)))
        # Mode homologation : le snapshot attend un pointage humain, l'ancienne
        # liste READY reste en production jusqu'a l'approbation.
        staging = require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count
        db.commit()

        # Delta par rapport a la liste active (= production, non supersedee)
        old_entities = _snapshot_entity_dicts(db, previous.snapshot_id) if previous else []
        new_entities = _snapshot_entity_dicts(db, snap_id)
        delta = calculate_delta(old_entities, new_entities, "entity_id")

        if not staging:
            # Application immediate (remplacement de la liste OFAC active)
            _supersede_previous_snapshots(db, "WATCHLIST_OFAC", snap_id)
            db.commit()
            if reload_cache:
                reload_cache()

        summary = delta["summary"]
        if staging:
            message = (
                f"{record_count} fiches importees depuis le fichier OFAC officiel, "
                "snapshot en attente d'homologation (pointage humain requis)."
            )
        else:
            message = f"{record_count} fiches importees depuis le fichier OFAC officiel."
        return _finalize_report(
            db, source="OFAC", trigger=trigger, status="PENDING_REVIEW" if staging else "SUCCESS",
            message=message,
            snapshot_id=snap_id,
            previous_snapshot_id=previous.snapshot_id if previous else None,
            added_count=summary["added_count"],
            modified_count=summary["modified_count"],
            removed_count=summary["removed_count"],
            delta_report=_truncate_delta_details(delta)
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Echec de la synchronisation OFAC: {e}")
        if snap_id:
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        return _finalize_report(
            db, source="OFAC", trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )
    finally:
        if temp_file.exists():
            os.remove(temp_file)


def run_dgt_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge le registre national des gels des avoirs (DGT, JSON officiel),
    l'ingere en snapshot, calcule le delta par rapport a la liste active et
    applique le remplacement (ou attend l'homologation si le mode est actif).
    """
    cfg = get_sync_config()["dgt"]
    url = cfg["url"]
    fetch = fetcher or download_to_file

    temp_dir = PROJECT_ROOT / "temp_ingestion"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"dgt_sync_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.json"

    previous = _latest_ready_snapshot(db, "WATCHLIST_DGT")
    snap_id = None
    try:
        logger.info(f"Sync DGT: telechargement de {url}")
        fetch(url, temp_file)

        with open(temp_file, "rb") as f:
            fhash = hashlib.sha256(f.read()).hexdigest()

        duplicate = _existing_snapshot_with_hash(db, "WATCHLIST_DGT", fhash)
        if duplicate:
            if duplicate.status == "PENDING_REVIEW":
                message = "Le registre DGT est identique a un snapshot deja en attente d'homologation."
            else:
                message = "Le registre DGT est identique a la version active (hash inchange)."
            return _finalize_report(
                db, source="DGT", trigger=trigger, status="NO_CHANGE",
                message=message,
                previous_snapshot_id=duplicate.snapshot_id
            )

        snap_id = f"dgt-sync-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type="WATCHLIST_DGT",
            file_name=f"Registre_gels_DGT_{datetime.utcnow().strftime('%Y-%m-%d')}.json",
            file_hash=fhash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()

        record_count = persist_pivot_items(db, snap_id, parse_dgt_gels_json(str(temp_file)))
        staging = require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count
        db.commit()

        # Delta par rapport a la liste active (= production, non supersedee)
        old_entities = _snapshot_entity_dicts(db, previous.snapshot_id) if previous else []
        new_entities = _snapshot_entity_dicts(db, snap_id)
        delta = calculate_delta(old_entities, new_entities, "entity_id")

        if not staging:
            _supersede_previous_snapshots(db, "WATCHLIST_DGT", snap_id)
            db.commit()
            if reload_cache:
                reload_cache()

        summary = delta["summary"]
        if staging:
            message = (
                f"{record_count} fiches importees depuis le registre national des gels (DGT), "
                "snapshot en attente d'homologation (pointage humain requis)."
            )
        else:
            message = f"{record_count} fiches importees depuis le registre national des gels (DGT)."
        return _finalize_report(
            db, source="DGT", trigger=trigger, status="PENDING_REVIEW" if staging else "SUCCESS",
            message=message,
            snapshot_id=snap_id,
            previous_snapshot_id=previous.snapshot_id if previous else None,
            added_count=summary["added_count"],
            modified_count=summary["modified_count"],
            removed_count=summary["removed_count"],
            delta_report=_truncate_delta_details(delta)
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Echec de la synchronisation DGT: {e}")
        if snap_id:
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        return _finalize_report(
            db, source="DGT", trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )
    finally:
        if temp_file.exists():
            os.remove(temp_file)


def _run_list_replacement_sync(
    db,
    source: str,
    file_type: str,
    url: str,
    parser: Callable[[str], Any],
    file_label: str,
    temp_suffix: str,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Cycle generique de synchronisation d'une liste officielle a remplacement
    complet : telechargement, deduplication par hash (y compris snapshots en
    attente d'homologation), ingestion, delta par rapport a la liste active,
    puis application (supersede + rechargement du cache) ou attente de
    pointage humain si le mode homologation est actif.
    """
    fetch = fetcher or download_to_file
    temp_dir = PROJECT_ROOT / "temp_ingestion"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file = temp_dir / f"{source.lower()}_sync_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}{temp_suffix}"

    previous = _latest_ready_snapshot(db, file_type)
    snap_id = None
    try:
        logger.info(f"Sync {source}: telechargement de {url}")
        fetch(url, temp_file)

        with open(temp_file, "rb") as f:
            fhash = hashlib.sha256(f.read()).hexdigest()

        duplicate = _existing_snapshot_with_hash(db, file_type, fhash)
        if duplicate:
            if duplicate.status == "PENDING_REVIEW":
                message = f"Le fichier {source} est identique a un snapshot deja en attente d'homologation."
            else:
                message = f"Le fichier {source} est identique a la version active (hash inchange)."
            return _finalize_report(
                db, source=source, trigger=trigger, status="NO_CHANGE",
                message=message,
                previous_snapshot_id=duplicate.snapshot_id
            )

        snap_id = f"{source.lower()}-sync-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type=file_type,
            file_name=f"{file_label}_{datetime.utcnow().strftime('%Y-%m-%d')}{temp_suffix}",
            file_hash=fhash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()

        record_count = persist_pivot_items(db, snap_id, parser(str(temp_file)))
        staging = require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count
        db.commit()

        old_entities = _snapshot_entity_dicts(db, previous.snapshot_id) if previous else []
        new_entities = _snapshot_entity_dicts(db, snap_id)
        delta = calculate_delta(old_entities, new_entities, "entity_id")

        if not staging:
            _supersede_previous_snapshots(db, file_type, snap_id)
            db.commit()
            if reload_cache:
                reload_cache()

        summary = delta["summary"]
        message = f"{record_count} fiches importees depuis la source {source}."
        if staging:
            message += " Snapshot en attente d'homologation (pointage humain requis)."
        return _finalize_report(
            db, source=source, trigger=trigger, status="PENDING_REVIEW" if staging else "SUCCESS",
            message=message,
            snapshot_id=snap_id,
            previous_snapshot_id=previous.snapshot_id if previous else None,
            added_count=summary["added_count"],
            modified_count=summary["modified_count"],
            removed_count=summary["removed_count"],
            delta_report=_truncate_delta_details(delta)
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Echec de la synchronisation {source}: {e}")
        if snap_id:
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        return _finalize_report(
            db, source=source, trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )
    finally:
        if temp_file.exists():
            os.remove(temp_file)


def run_eu_fsf_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee officielle des sanctions financieres de
    l'UE (fichiers FSF du webgate FSD) et remplace la liste EU active. Fait
    autorite sur le scraping du Journal Officiel : les radiations y sont
    fiables. Necessite un token (inscription gratuite au webgate).
    """
    cfg = get_sync_config()["eu_fsf"]
    url = cfg["url"]
    if "{token}" in url:
        if not cfg["token"]:
            return _finalize_report(
                db, source="EUFSF", trigger=trigger, status="ERROR",
                message=(
                    "Token FSF non configure : creez un compte gratuit sur le webgate FSD "
                    "de la Commission europeenne puis renseignez sync.eu_fsf.token dans config.yaml."
                )
            )
        url = url.replace("{token}", cfg["token"])
    return _run_list_replacement_sync(
        db, source="EUFSF", file_type="WATCHLIST_EU", url=url,
        parser=parse_eu_fsf_xml, file_label="EU_FSF_Consolidated",
        temp_suffix=".xml", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_un_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee du Conseil de securite de l'ONU (XML
    public officiel) et remplace la liste ONU active.
    """
    cfg = get_sync_config()["un"]
    return _run_list_replacement_sync(
        db, source="UN", file_type="WATCHLIST_UN", url=cfg["url"],
        parser=parse_un_consolidated_xml, file_label="UN_Consolidated",
        temp_suffix=".xml", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_pep_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge le dataset PEP OpenSanctions (targets.simple.csv) et remplace
    la liste PEP active. Usage non commercial libre ; licence OpenSanctions
    requise pour un usage commercial.
    """
    cfg = get_sync_config()["pep"]
    return _run_list_replacement_sync(
        db, source="PEP", file_type="WATCHLIST_PEP", url=cfg["url"],
        parser=parse_pep_targets_csv, file_label="OpenSanctions_PEP",
        temp_suffix=".csv", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


def run_ofsi_sync(
    db,
    trigger: str = "MANUAL",
    fetcher: Optional[Callable[[str, Path], None]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
) -> SyncReport:
    """
    Telecharge la liste consolidee UK OFSI (ConList.csv, format 2022) et
    remplace la liste OFSI active.
    """
    cfg = get_sync_config()["ofsi"]
    return _run_list_replacement_sync(
        db, source="OFSI", file_type="WATCHLIST_OFSI", url=cfg["url"],
        parser=parse_ofsi_conlist_csv, file_label="UK_OFSI_ConList",
        temp_suffix=".csv", trigger=trigger, fetcher=fetcher, reload_cache=reload_cache
    )


# ------------------ SCRAPING EUR-LEX ------------------

class _HTMLDocumentExtractor(HTMLParser):
    """Extrait les liens (href, texte) et les tableaux (lignes de cellules) d'une page HTML."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: List[Tuple[str, str]] = []
        self.tables: List[List[List[str]]] = []
        self._link_href = None
        self._link_text: List[str] = []
        self._table_stack: List[List[List[str]]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._link_href = dict(attrs).get("href")
            self._link_text = []
        elif tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "a" and self._link_href is not None:
            text = re.sub(r"\s+", " ", " ".join(self._link_text)).strip()
            if text:
                self.links.append((self._link_href, text))
            self._link_href = None
            self._link_text = []
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", " ".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table_stack:
            if any(c for c in self._row):
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            table = self._table_stack.pop()
            if table:
                self.tables.append(table)

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._link_href is not None:
            self._link_text.append(data)


def _strip_accents_lower(text: str) -> str:
    normalized = unicodedata.normalize("NFD", (text or "").replace("’", "'"))
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn").lower()


def extract_daily_acts(html: str, base_url: str, keyword: str = DEFAULT_EURLEX_KEYWORD) -> List[Dict[str, str]]:
    """
    Extrait de la page du Journal Officiel du jour les actes dont le titre
    mentionne le mot-cle (par defaut "mesures restrictives").
    """
    parser = _HTMLDocumentExtractor()
    parser.feed(html)

    keyword_norm = _strip_accents_lower(keyword)
    acts = []
    seen = set()
    for href, text in parser.links:
        if keyword_norm not in _strip_accents_lower(text):
            continue
        url = urljoin(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        acts.append({"title": text, "url": url})
    return acts


_TYPE_KEYWORDS = [
    ("V", ["navire", "vessel", "ship", "imo", "tanker", "petrolier", "pétrolier",
           "flotte fantome", "shadow fleet", "mmsi", "pavillon", "flag of"]),
    ("O", ["aeronef", "aéronef", "aircraft", "immatriculation de l'aeronef", "tail number"]),
    ("E", ["entite", "entité", "entity", "societe", "société", "organisation", "organisme",
           "company", "corporation", "enterprise", "subsidiary", "filiale", "holding",
           "incorporated", "registered in", "immatriculee", "enregistree", "state-owned",
           "joint stock", "sarl", "llc", "ltd", "gmbh", "fze"]),
]


def _detect_entity_type(context_text: str) -> str:
    """
    Determine le type du liste (I/E/V/O) a partir de toute la ligne d'annexe :
    informations d'identification ET motifs de la designation. Les indices
    personnels (date/lieu de naissance, pronoms, fonctions) priment sur les
    mots-cles d'entites ou de navires cites dans les motifs.
    """
    ctx = _strip_accents_lower(context_text)
    if _PERSONAL_INDICATORS.search(ctx):
        return "I"
    for etype, keywords in _TYPE_KEYWORDS:
        for kw in keywords:
            # Correspondance sur mot entier ("ship" ne doit pas matcher "SHIPPING")
            if re.search(rf"\b{re.escape(_strip_accents_lower(kw))}\b", ctx):
                return etype
    return "I"


def _stable_eu_entity_id(name: str) -> str:
    digest = hashlib.sha1(_strip_accents_lower(name).encode("utf-8")).hexdigest()
    return f"EU-{digest[:12].upper()}"


_DOB_PATTERN = re.compile(r"(\d{1,2})[./](\d{1,2})[./](\d{4})|(\d{4})-(\d{2})-(\d{2})")
_IMO_PATTERN = re.compile(r"IMO\D{0,3}(\d{7})", re.IGNORECASE)
_TAIL_PATTERN = re.compile(r"immatriculation\D{0,10}([A-Z0-9\-]{4,10})", re.IGNORECASE)


def _extract_dob(text: str) -> Optional[str]:
    m = _DOB_PATTERN.search(text)
    if not m:
        return None
    if m.group(4):
        return f"{m.group(4)}-{m.group(5)}-{m.group(6)}"
    return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"


# Tournures editoriales des actes (considerants, references, mise en page)
# a ne pas confondre avec des noms
_NON_NAME_PATTERNS = (
    # Francais
    "journal officiel", "union europeenne", "il y a ", "vu le ", "vu la ", "considerant",
    "le conseil ", "la commission ", "conformement ", "annex", "serie l",
    "informations d'identification", "translitteration", "caracteres latins",
    # Anglais (version de reference du JO)
    "official journal", "european union", "having regard", "whereas",
    "the council", "the commission", "in accordance", "identifying information",
    "transliteration", "latin characters", "latin script", "l series",
    "should therefore", "as follows",
)

# En-tete de la colonne des motifs dans les annexes (FR/EN)
_MOTIFS_HEADER = re.compile(r"motif|reasons|grounds", re.IGNORECASE)

# Instructions d'amendement citant du texte de liste ("la mention suivante est
# remplacee par...") : leurs lignes ne decrivent pas un liste
_AMENDMENT_CONTEXT = re.compile(
    r"(est|sont) (remplace|ajoute|supprime|modifie)|texte suivant|mention suivante|"
    r"rubrique suivante|entree suivante|(is|are) (replaced|added|deleted|amended)",
    re.IGNORECASE
)

# Indices d'attributs personnels, y compris dans les motifs de la designation
# (pronoms, fonctions, professions) : ils priment sur les mots-cles d'entites
# presents dans la meme ligne (ex: "dirigeant d'une entite")
_PERSONAL_INDICATORS = re.compile(
    r"date de naissance|lieu de naissance|date of birth|place of birth|"
    r"\bn[ée]e? le\b|\bborn\b|nationalit[ey]|sexe\s*:|gender\s*:|"
    r"\b(he|she) (is|was|has)\b|\bil est\b|\belle est\b|"
    r"minist(re|er)|oligar(que|ch)|homme d'affaires|femme d'affaires|"
    r"business(man|woman)|propagandist|ressortissant",
    re.IGNORECASE
)


def _looks_like_name(value: str) -> bool:
    v = value.strip()
    if len(v) < 3 or len(v) > 120:
        return False
    if not re.search(r"[A-Za-zÀ-ÿ]{2}", v):
        return False
    # Exclut les numeros d'ordre, dates et libelles de colonnes
    if re.fullmatch(r"[\d\s./-]+", v):
        return False
    normalized = _strip_accents_lower(v)
    if normalized in ("nom", "noms", "name", "names", "nom complet", "full name", "designation",
                      "type", "identite", "identity", "reasons", "grounds",
                      "limited liability company"):
        return False
    # Libelles de colonnes, references d'actes et formules juridiques (FR + EN)
    if normalized.startswith(("motifs", "date d", "date de", "lieu d", "informations",
                              "sont ", "tous les", "les fonds", "en russe", "en anglais",
                              "reasons", "grounds", "date of", "place of", "identifying",
                              "statement of", "in russian", "in english", "all funds",
                              "funds and", "name of", "regulation", "decision", "directive",
                              "reglement", "implementing")):
        return False
    # Les phrases longues sont du texte editorial, pas des identites
    if len(v.split()) > 8:
        return False
    if any(p in normalized for p in _NON_NAME_PATTERNS):
        return False
    return True


def scrape_act_entities(html: str, act_title: str = "", act_url: str = "") -> List[Dict[str, Any]]:
    """
    Analyse heuristique d'un acte EUR-Lex (annexes de reglements de mesures
    restrictives) pour en extraire les listes au schema pivot Fiskr.
    Couvre les annexes tabulaires (Nom / Informations / Motifs / Date) et les
    listes numerotees en texte.
    """
    parser = _HTMLDocumentExtractor()
    parser.feed(html)

    entities: Dict[str, Dict[str, Any]] = {}

    def register(name: str, context: str, reasons: Optional[str] = None):
        # Les lignes d'instructions d'amendement citent du texte de liste
        # entre guillemets : ce ne sont pas des listes
        if _AMENDMENT_CONTEXT.search(_strip_accents_lower(context)):
            return
        # Retire les guillemets (typographiques inclus) avant analyse
        name = re.sub(r"[«»“”\"]", " ", name)
        # Ne conserve que le segment latin du nom (les translitterations
        # cyrilliques/arabes accolees dans la meme cellule sont ecartees)
        name = name.strip()
        latin = re.match(r"^[A-Za-zÀ-ÿ0-9\s'’.,()\-/]+", name)
        if latin:
            name = latin.group(0)
        # Retire les mentions de langue tronquees par la coupe ci-dessus, avec ou
        # sans parenthese et dans les deux syntaxes : "Anton USOV en russe : ..."
        # (FR) et "Maria DUDKO (Russian: ...)" (EN)
        name = re.sub(
            r"\s*[\(«\"]?\s*\b((en|in)\s+)?(russe|anglais|ukrainien|bielorusse|arabe|persan|farsi|"
            r"russian|english|ukrainian|belarusian|arabic|persian)\b(\s*[:)].*)?$",
            "", name, flags=re.IGNORECASE
        )
        name = re.sub(
            r"\s*[\(«\"]?\s*\b(en|in)\s+(russe|anglais|ukrainien|bielorusse|arabe|persan|farsi|"
            r"russian|english|ukrainian|belarusian|arabic|persian)\b.*$",
            "", name, flags=re.IGNORECASE
        )
        name = re.sub(r"\s*\(\s*(en|in)\s+[^)]*\)?\s*$", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\s+", " ", name).strip().strip("«»\"").rstrip(".;,(")
        if not _looks_like_name(name):
            return
        etype = _detect_entity_type(context)
        dob = _extract_dob(context) if etype == "I" else None
        imo = None
        tail = None
        if etype == "V":
            imo_match = _IMO_PATTERN.search(context)
            imo = imo_match.group(1) if imo_match else None
        if etype == "O":
            tail_match = _TAIL_PATTERN.search(context)
            tail = tail_match.group(1) if tail_match else None

        entity_id = _stable_eu_entity_id(name)
        # Une fiche deja enrichie (DOB connue) n'est pas ecrasee par une occurrence plus pauvre
        existing = entities.get(entity_id)
        if existing and existing.get("dates_of_birth") and not dob:
            return
        item = {
            "entity_id": entity_id,
            "entity_type": etype,
            "primary_name": name,
            "individual_name_parsed": parse_individual_name(name) if etype == "I" else {"first_name": "", "last_name": "", "maiden_name": ""},
            "aliases": {"high_priority": [], "low_priority": []},
            "dates_of_birth": [dob] if dob else [],
            "date_of_death": None,
            "is_deceased": False,
            "gender": "U",
            "countries": {"citizenship": [], "residence": [], "birth_country": [], "jurisdiction_country": []},
            "imo_number": imo,
            "aircraft_tail_number": tail,
            "origin": f"EUR-Lex - {act_title}" if act_title else "EUR-Lex",
            "designation_reasons": reasons,
            "additional_informations": act_url or None,
        }
        entities[entity_id] = item

    # 1. Annexes tabulaires : la premiere colonne plausible porte le nom,
    #    le reste de la ligne sert de contexte (type, DOB, IMO...).
    #    La ligne d'en-tete sert a localiser la colonne "Motifs de la designation".
    for table in parser.tables:
        motifs_idx = None
        for row in table:
            norm_cells = [_strip_accents_lower(c) for c in row]
            if any(_MOTIFS_HEADER.search(c) for c in norm_cells) and \
               any(c in ("nom", "name", "identite", "identity") for c in norm_cells):
                motifs_idx = next(i for i, c in enumerate(norm_cells) if _MOTIFS_HEADER.search(c))
                break

        for row in table:
            if not row:
                continue
            row_context = " | ".join(row)
            # Ignore les lignes d'en-tete
            header_like = all(not _looks_like_name(c) or _strip_accents_lower(c) in
                              ("nom", "name", "informations d'identification", "motifs", "type")
                              for c in row)
            if header_like:
                continue
            name_cell = next((c for c in row if _looks_like_name(c)), None)
            if not name_cell:
                continue
            reasons = None
            if motifs_idx is not None and motifs_idx < len(row):
                candidate = row[motifs_idx].strip()
                if candidate and candidate != name_cell and not _MOTIFS_HEADER.search(_strip_accents_lower(candidate)):
                    reasons = candidate
            register(name_cell, row_context, reasons)

    # 2. Repli sur les listes numerotees en texte brut (ex: "12. DUPONT Jean (alias ...)"),
    #    uniquement si les annexes tabulaires n'ont rien donne : le corps des actes
    #    (considerants numerotes) genererait sinon des faux positifs.
    if not entities:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text)
        for m in re.finditer(r"\b\d{1,3}\.\s+([A-ZÀ-Ý][A-Za-zÀ-ÿ'\-\. ]{2,80}?)(?:\s*\(([^)]{0,200})\)|[,;])", text):
            name, extra = m.group(1), m.group(2) or ""
            register(name, f"{name} {extra}")

    return list(entities.values())


def _act_pdf_url(act_url: str) -> str:
    """URL du PDF officiel d'un acte EUR-Lex (…/legal-content/EN/TXT/PDF/?uri=…)."""
    if "/TXT/PDF/" in act_url:
        return act_url
    return re.sub(r"/TXT(/HTML)?/", "/TXT/PDF/", act_url)


def _archive_act_pdf(act: Dict[str, str], pdf_fetcher: Callable[[str, Path], None],
                     archive_dir: Path) -> None:
    """
    Telecharge et archive le PDF officiel de l'acte (version qui fait foi lors
    des audits), avec empreinte SHA-256 pour garantir son integrite probante.
    Un echec de telechargement n'interrompt pas la synchronisation.
    """
    match = re.search(r"uri=([^&]+)", act["url"])
    base_name = match.group(1) if match else hashlib.sha1(act["url"].encode()).hexdigest()[:12]
    filename = re.sub(r"[^A-Za-z0-9_.\-]", "_", base_name) + ".pdf"
    dest = archive_dir / filename
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            pdf_fetcher(_act_pdf_url(act["url"]), dest)
        with open(dest, "rb") as f:
            act["pdf_sha256"] = hashlib.sha256(f.read()).hexdigest()
        act["pdf_file"] = filename
    except Exception as e:
        logger.warning(f"Echec de l'archivage du PDF officiel {act['url']}: {e}")
        act["pdf_file"] = None
        if dest.exists():
            os.remove(dest)


def fetch_eurlex_entities(
    for_date: date,
    http_get: Callable[[str], str],
    daily_url_template: str,
    keyword: str,
) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    """
    Recupere le JO du jour, filtre les actes "mesures restrictives" et scrape
    chacun d'eux. Retourne (actes retenus, entites extraites).
    """
    daily_url = daily_url_template.format(date=for_date.strftime("%d%m%Y"))
    logger.info(f"Sync EUR-Lex: lecture du Journal Officiel {daily_url}")
    daily_html = http_get(daily_url)
    acts = extract_daily_acts(daily_html, daily_url, keyword)

    all_entities: Dict[str, Dict[str, Any]] = {}
    for act in acts:
        try:
            act_html = http_get(act["url"])
        except Exception as e:
            logger.warning(f"Sync EUR-Lex: echec du chargement de l'acte {act['url']}: {e}")
            continue
        for ent in scrape_act_entities(act_html, act["title"], act["url"]):
            all_entities[ent["entity_id"]] = ent
    return acts, list(all_entities.values())


def run_eurlex_sync(
    db,
    for_date: Optional[date] = None,
    trigger: str = "MANUAL",
    http_get: Optional[Callable[[str], str]] = None,
    reload_cache: Optional[Callable[[], None]] = None,
    pdf_fetcher: Optional[Callable[[str, Path], None]] = None,
    archive_dir: Optional[Path] = None,
) -> SyncReport:
    """
    Scrape le Journal Officiel de l'UE du jour (version anglaise, qui fait
    reference), archive les PDF officiels des actes retenus (valeur probante
    en audit) et fusionne les listes trouves avec la liste EU active (mode
    incremental : le JO amende la liste, les suppressions explicites ne sont
    pas encore detectees automatiquement).
    """
    cfg = get_sync_config()["eurlex"]
    for_date = for_date or date.today()
    getter = http_get or http_get_text
    pdf_getter = pdf_fetcher or download_to_file
    archive_dir = archive_dir or EURLEX_ARCHIVE_DIR

    # Base de fusion : inclut un eventuel snapshot en attente d'homologation pour
    # que les amendements de jours successifs s'enchainent sans perte.
    previous = _latest_reviewable_snapshot(db, "WATCHLIST_EU")
    snap_id = None
    try:
        acts, scraped = fetch_eurlex_entities(for_date, getter, cfg["daily_journal_url"], cfg["keyword"])

        # Archivage probant : le PDF officiel de chaque acte retenu fait foi
        for act in acts:
            _archive_act_pdf(act, pdf_getter, archive_dir)

        if not acts:
            return _finalize_report(
                db, source="EURLEX", trigger=trigger, status="NO_PUBLICATION",
                message=f"Aucun acte mentionnant \"{cfg['keyword']}\" au JO du {for_date.strftime('%d/%m/%Y')}.",
                previous_snapshot_id=previous.snapshot_id if previous else None
            )
        if not scraped:
            return _finalize_report(
                db, source="EURLEX", trigger=trigger, status="NO_CHANGE",
                message=f"{len(acts)} acte(s) trouve(s) au JO du {for_date.strftime('%d/%m/%Y')} mais aucun liste extrait.",
                previous_snapshot_id=previous.snapshot_id if previous else None,
                delta_report={"acts": acts}
            )

        # Fusion incrementale : les fiches actives sont reconduites, les fiches
        # scrapees du jour ajoutent ou remplacent (cle = entity_id stable)
        scraped_ids = {e["entity_id"] for e in scraped}
        snap_id = f"eurlex-sync-{for_date.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"
        content_hash = hashlib.sha256(
            "|".join(sorted(e["entity_id"] + (compute_checksum(e)) for e in scraped)).encode("utf-8")
        ).hexdigest()
        duplicate = _existing_snapshot_with_hash(db, "WATCHLIST_EU", content_hash)
        if duplicate:
            if duplicate.status == "PENDING_REVIEW":
                message = "Contenu identique a un snapshot EU deja en attente d'homologation."
            else:
                message = "Contenu identique a la liste EU active (hash inchange)."
            return _finalize_report(
                db, source="EURLEX", trigger=trigger, status="NO_CHANGE",
                message=message,
                previous_snapshot_id=duplicate.snapshot_id,
                delta_report={"acts": acts}
            )
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type="WATCHLIST_EU",
            file_name=f"EUR-Lex JO {for_date.strftime('%Y-%m-%d')} ({len(acts)} acte(s))",
            file_hash=content_hash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()

        record_count = persist_pivot_items(db, snap_id, scraped)
        carried = 0
        if previous:
            # Les entites exclues lors d'une revue ne sont pas reconduites
            prev_rows = db.query(WatchlistEntity).filter(
                WatchlistEntity.snapshot_id == previous.snapshot_id,
                WatchlistEntity.excluded.isnot(True)
            ).all()
            for row in prev_rows:
                if row.entity_id not in scraped_ids:
                    db.add(_clone_entity_row(snap_id, row))
                    carried += 1
        staging = require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count + carried
        db.commit()

        old_entities = _snapshot_entity_dicts(db, previous.snapshot_id) if previous else []
        new_entities = _snapshot_entity_dicts(db, snap_id)
        delta = calculate_delta(old_entities, new_entities, "entity_id")

        if not staging:
            _supersede_previous_snapshots(db, "WATCHLIST_EU", snap_id)
            db.commit()
            if reload_cache:
                reload_cache()

        summary = delta["summary"]
        delta_stored = _truncate_delta_details(delta)
        delta_stored["acts"] = acts
        message = f"{len(acts)} acte(s) \"{cfg['keyword']}\" au JO du {for_date.strftime('%d/%m/%Y')} ; {len(scraped)} liste(s) extrait(s), {carried} fiche(s) reconduite(s)."
        if staging:
            message += " Snapshot en attente d'homologation (pointage humain requis)."
        return _finalize_report(
            db, source="EURLEX", trigger=trigger, status="PENDING_REVIEW" if staging else "SUCCESS",
            message=message,
            snapshot_id=snap_id,
            previous_snapshot_id=previous.snapshot_id if previous else None,
            added_count=summary["added_count"],
            modified_count=summary["modified_count"],
            removed_count=summary["removed_count"],
            delta_report=delta_stored
        )
    except Exception as e:
        db.rollback()
        logger.error(f"Echec de la synchronisation EUR-Lex: {e}")
        if snap_id:
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        return _finalize_report(
            db, source="EURLEX", trigger=trigger, status="ERROR",
            message=f"Echec: {e}"
        )

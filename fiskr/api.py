import os
import uuid
import json
import re
import asyncio
import hashlib
import logging
import shutil
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from fastapi import FastAPI, Depends, HTTPException, Query, status, UploadFile, File, Form, Response, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fiskr.config import config, PROJECT_ROOT
from fiskr.quality import evaluate_and_clean
from fiskr.blocking import generate_blocking_keys
from fiskr.scoring import match_entities, jaro_wink_similarity
from fiskr.delta import calculate_delta
from fiskr.ingest import (
    parse_ofac_advanced_xml, parse_csv_file, parse_pdf_watchlist, parse_dgt_gels_json,
    parse_eu_fsf_xml, parse_un_consolidated_xml, parse_pep_targets_csv, parse_ofsi_conlist_csv
)
from fiskr.ssie import parse_ssie_xml, merge_ssie_selectors, DEFAULT_SOURCE_FORMAT
from fiskr.database import (
    get_db, init_db, log_compliance_decision, AuditTrail, Snapshot,
    WatchlistEntity, ClientEntity, compute_checksum, User, verify_password, hash_password,
    SyncReport, Alert, AlertEvent, ALERT_OPEN_STATUSES, ALERT_CLOSED_STATUSES, WhitelistPair,
    WatchlistEntityChange, FpRule, FpRuleChange, FpRuleTest,
    AlertAttachment, AdminAuditLog, ALERT_PRIORITIES,
    EntityRelationship, RELATION_TYPES, refresh_source_relationships,
    BatchCampaign, BatchResult, ApiKey, SavedView, AppSetting, HookDelivery
)
from fiskr.alerts import open_or_redetect_alert, is_whitelisted, compute_due_at
from fiskr.notify import notify_event
from fiskr.fprules import (
    evaluate_fp_rules, build_screening_ctx, annotate_suppression, compile_rule,
    run_rule, FP_RULE_CHANNELS, RULE_TEMPLATE, validate_rule_code,
    generate_rule_code, get_fprules_llm_config,
    RuleGenerationUnavailable, RuleGenerationFailed
)
from fiskr.rescreen import rescreen_after_snapshot_change, rescreen_lookback
from fiskr.backtest import (
    run_backtest, generate_test_panel, TEST_PANEL_FILE_TYPE, PANEL_FILE_TYPES
)
from fiskr.sync import (
    run_ofac_sync, run_eurlex_sync, run_dgt_sync, run_eu_fsf_sync, run_un_sync,
    run_pep_sync, run_ofsi_sync,
    get_sync_config, EURLEX_ARCHIVE_DIR,
    _supersede_previous_snapshots, _snapshot_entity_dicts, _latest_ready_snapshot,
    _truncate_delta_details
)
from fiskr.names import ensure_parsed_name
from fiskr.transactions import parse_iso20022_payment, screen_payment_message
from fiskr.adverse_media import search_adverse_media
from fiskr.narrative import generate_alert_narrative
from fiskr.auth import (
    get_current_user, require_admin, require_reviewer, require_blocking, require_fprules,
    create_access_token, decode_access_token, parse_roles, normalize_roles,
    validate_password, security_config, hash_api_key, API_KEY_PREFIX_LEN,
    generate_totp_secret, verify_totp, totp_provisioning_uri
)
from fiskr.settings import (
    require_approval_enabled, exclusion_requirements, alert_four_eyes_required,
    whitelist_requirements, auto_rescreen_enabled,
    backtest_max_gap_pct, backtest_required,
    get_setting_with_source, set_setting,
    SETTING_REQUIRE_APPROVAL, SETTING_EXCLUSION_JUSTIFICATION_REQUIRED,
    SETTING_EXCLUSION_FILE_REQUIRED, SETTING_ALERT_FOUR_EYES,
    SETTING_WHITELIST_JUSTIFICATION_REQUIRED, SETTING_WHITELIST_FILE_REQUIRED,
    SETTING_AUTO_RESCREEN, SETTING_BACKTEST_REQUIRED, SETTING_BACKTEST_MAX_GAP_PCT,
    SETTING_BLOCKING_SCREENING, SETTING_BLOCKING_FILTERING,
    BLOCKING_COMPONENTS, blocking_layout, blocking_layout_with_source, blocking_config_for,
    alert_sla_hours, notification_events,
    SETTING_ALERT_SLA_HOURS, SETTING_NOTIFICATIONS, DEFAULT_NOTIFICATION_EVENTS,
    sync_schedules, SETTING_SYNC_SCHEDULES, SYNC_SOURCES,
    digest_settings, SETTING_DIGEST,
    retention_policy, SETTING_RETENTION, RETENTION_FAMILIES, RETENTION_MIN_DAYS,
    score_thresholds, scoring_config_with_thresholds, SETTING_SCORE_THRESHOLDS,
    investigation_checklist, SETTING_CHECKLIST, DEFAULT_CHECKLIST
)
from fiskr.retention import preview_retention, run_retention
from fiskr.apimessages import resolve_lang as resolve_api_lang, translate_payload
from fiskr import progress as progress_registry



logger = logging.getLogger("fiskr.api")

# Snapshot file types persisted as WatchlistEntity records
WATCHLIST_FILE_TYPES = [
    "WATCHLIST_OFAC", "WATCHLIST_EU", "WATCHLIST_SSIE", "WATCHLIST_DGT",
    "WATCHLIST_UN", "WATCHLIST_PEP", "WATCHLIST_OFSI"
]

# Champs etendus des listes (schema pivot -> colonnes WatchlistEntity).
# Reutilise par les 3 chemins d'ingestion (parseurs officiels, PDF, CSV).
EXTENDED_ENTITY_FIELDS = (
    "crypto_wallets", "bic_swift", "tax_id", "duns_number",
    "vessel_call_sign", "vessel_mmsi", "vessel_flag", "vessel_type",
    "vessel_tonnage", "vessel_owner",
    "aircraft_model", "aircraft_operator", "aircraft_construction_number",
    "sanction_programs", "listed_on", "delisted_on", "name_original_script",
    "title", "pep_role", "secondary_sanctions_risk", "designating_state",
    "organization_established_date", "organization_type",
    "phone_numbers", "email_addresses", "websites",
)

_EXTENDED_LIST_FIELDS = ("sanction_programs", "phone_numbers", "email_addresses", "websites")

def _extended_entity_kwargs(item: Dict[str, Any]) -> Dict[str, Any]:
    """Champs etendus normalises : les colonnes CSV texte des champs liste
    sont decoupees sur « ; » (parite avec les parseurs officiels)."""
    out: Dict[str, Any] = {}
    for field in EXTENDED_ENTITY_FIELDS:
        value = item.get(field)
        if isinstance(value, str) and field in _EXTENDED_LIST_FIELDS:
            value = [v.strip() for v in value.split(";") if v.strip()] or None
        elif isinstance(value, str) and field == "crypto_wallets":
            value = [{"currency": "", "address": v.strip()} for v in value.split(";") if v.strip()] or None
        elif isinstance(value, str):
            value = value.strip() or None
        out[field] = value
    return out

# In-memory index cache
watchlist_store: List[Dict[str, Any]] = []
watchlist_index: Dict[str, List[Dict[str, Any]]] = {}
watchlist_version: str = "Database Active Snapshot"
watchlist_hash: str = "N/A"
# Layout de blocking utilise pour CONSTRUIRE l'index en memoire : les sondes
# du criblage doivent utiliser le meme (coherence index/sonde garantie)
watchlist_index_layout: List[str] = ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]

def load_watchlist_cache(db: Session):
    """Loads the active READY watchlist entities from the database into the in-memory cache."""
    global watchlist_store, watchlist_index, watchlist_hash, watchlist_index_layout
    
    # 1. Look for latest READY snapshots in DB of watchlist types (OFAC / EU / SSIE)
    snapshots = db.query(Snapshot).filter(
        Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
        Snapshot.status == "READY"
    ).order_by(Snapshot.uploaded_at.desc()).all()

    if not snapshots:
        # Fallback: Ingest watchlist.json if it exists to seed the database
        seed_watchlist_json(db)
        # Re-fetch
        snapshots = db.query(Snapshot).filter(
            Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
            Snapshot.status == "READY"
        ).order_by(Snapshot.uploaded_at.desc()).all()
        
    if not snapshots:
        logger.warning("No watchlist snapshots found in database to load cache.")
        return
        
    # Get active watchlist hash
    active_hash = snapshots[0].file_hash
    watchlist_hash = active_hash
    
    # Load all entities for these active snapshots (excluded entities stay out
    # of production but are kept in DB for audit; NULL = legacy rows, not excluded)
    snapshot_ids = [s.snapshot_id for s in snapshots]
    snapshot_types = {s.snapshot_id: s.file_type for s in snapshots}
    entities = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_(snapshot_ids),
        WatchlistEntity.excluded.isnot(True)
    ).yield_per(2000)

    temp_store = []
    temp_index = {}

    # Layout de blocking du canal criblage (parametrable a chaud) : l'index
    # est construit avec, et les sondes reutilisent le layout memorise
    screening_layout = blocking_layout(db, "SCREENING")
    screening_cfg = blocking_config_for(screening_layout)

    for ent in entities:
        # Convert SQLAlchemy object to dictionary for cache
        ent_dict = {c.name: getattr(ent, c.name) for c in ent.__table__.columns}
        # Type de liste d'origine : permet les seuils de cut-off par liste
        ent_dict["_list_type"] = snapshot_types.get(ent.snapshot_id)
        temp_store.append(ent_dict)

        # Index by blocking key
        keys = generate_blocking_keys(ent_dict, screening_cfg)
        for k in keys:
            if k not in temp_index:
                temp_index[k] = []
            temp_index[k].append(ent_dict)

    watchlist_store = temp_store
    watchlist_index = temp_index
    watchlist_index_layout = screening_layout
    logger.info(f"Loaded {len(watchlist_store)} active database entities into memory across {len(watchlist_index)} blocking blocks.")

def seed_watchlist_json(db: Session):
    """Seeds the DB watchlist from watchlist.json if DB is empty."""
    watchlist_path = PROJECT_ROOT / "watchlist.json"
    if not watchlist_path.exists():
        return
        
    logger.info("Seeding database watchlist from watchlist.json...")
    try:
        with open(watchlist_path, "rb") as f:
            content = f.read()
            fhash = hashlib.sha256(content).hexdigest()
            data = json.loads(content)
            
        snap_id = f"seed-snap-{str(uuid.uuid4())[:8]}"
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type="WATCHLIST_OFAC",
            file_name="watchlist.json",
            file_hash=fhash,
            record_count=len(data),
            status="READY"
        )
        db.add(snap)
        
        for idx, item in enumerate(data):
            entity_id = item.get("entity_id") or f"WL-SEED-{idx}"
            
            # Map parsed fields
            parsed = item.get("individual_name_parsed") or {}
            aliases = item.get("aliases") or []
            if not isinstance(aliases, dict):
                # Classify aliases dynamically
                from fiskr.ingest import categorize_aliases
                raw_aliases = [{"name": a, "type": "Strong"} for a in aliases if a]
                aliases = categorize_aliases(raw_aliases)
                
            countries = item.get("countries") or {}
            
            # Create checksum
            ent_checksum = compute_checksum(item)
            
            raw_etype = item.get("entity_type", "I")
            etype = "I" if raw_etype == "PP" else ("E" if raw_etype == "PM" else raw_etype)
            
            alt_addrs = [a.strip() for a in item.get("alternative_addresses", "").split(";")] if isinstance(item.get("alternative_addresses"), str) else (item.get("alternative_addresses") or [])
            
            db_ent = WatchlistEntity(
                snapshot_id=snap_id,
                entity_id=entity_id,
                entity_type=etype,
                primary_name=item.get("primary_name", ""),
                individual_name_parsed=parsed,
                aliases=aliases,
                dates_of_birth=item.get("dates_of_birth", []),
                date_of_death=item.get("date_of_death"),
                is_deceased=str(item.get("is_deceased", "False")).lower() == "true",
                gender=item.get("gender") or (item.get("genders", ["U"])[0] if item.get("genders") else "U"),
                countries=countries,
                # New fields
                place_of_birth=item.get("place_of_birth"),
                address=item.get("address") or item.get("adress"),
                city=item.get("city"),
                state=item.get("state"),
                country=item.get("country"),
                origin=item.get("origin"),
                designation=item.get("designation"),
                designation_reasons=item.get("designation_reasons"),
                additional_informations=item.get("additional_informations") or item.get("additional_info"),
                alternative_addresses=alt_addrs,
                imo_number=item.get("imo_number"),
                aircraft_tail_number=item.get("aircraft_tail_number"),
                lei_number=item.get("lei_number"),
                national_registry_ids=item.get("national_registry_ids"),
                other_registration_ids=item.get("other_registration_ids"),
                passport_documents=item.get("passport_documents"),
                national_id_documents=item.get("national_id_documents"),
                other_id_documents=item.get("other_id_documents"),
                entity_checksum=ent_checksum
            )
            db.add(db_ent)
            
        db.commit()
        logger.info("Successfully seeded database watchlist snapshot.")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to seed watchlist from JSON: {e}")

def _run_scheduled_syncs():
    """Execute les synchronisations de sources activees (appel planifie quotidien)."""
    sync_cfg = get_sync_config()
    db = next(get_db())

    def _apply_rescreen(report):
        # Surveillance continue : re-criblage post-delta apres chaque application
        if report.status == "SUCCESS" and report.snapshot_id and auto_rescreen_enabled(db):
            snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
            if snap:
                rescreen_after_snapshot_change(db, snap.file_type, report.snapshot_id, report.previous_snapshot_id)

    try:
        if sync_cfg["ofac"]["enabled"]:
            _apply_rescreen(run_ofac_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db)))
        # FSF (liste consolidee, fait autorite) avant le scraping du JO du jour
        if sync_cfg["eu_fsf"]["enabled"]:
            _apply_rescreen(run_eu_fsf_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db)))
        if sync_cfg["eurlex"]["enabled"]:
            _apply_rescreen(run_eurlex_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db)))
        if sync_cfg["dgt"]["enabled"]:
            _apply_rescreen(run_dgt_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db)))
        if sync_cfg["un"]["enabled"]:
            _apply_rescreen(run_un_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db)))
        if sync_cfg["pep"]["enabled"]:
            _apply_rescreen(run_pep_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db)))
        if sync_cfg["ofsi"]["enabled"]:
            _apply_rescreen(run_ofsi_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db)))
    finally:
        db.close()

# Executeurs de sync par source (planification cron individuelle)
_SYNC_RUNNERS = {
    "ofac": run_ofac_sync, "eurlex": run_eurlex_sync, "dgt": run_dgt_sync,
    "eu_fsf": run_eu_fsf_sync, "un": run_un_sync, "pep": run_pep_sync, "ofsi": run_ofsi_sync,
}
# Sources en cours d'execution : une meme source ne se chevauche jamais
_running_syncs: set = set()

def _run_source_sync(source: str) -> None:
    """Synchronise UNE source (declenchement cron), avec re-criblage post-delta."""
    db = next(get_db())
    try:
        report = _SYNC_RUNNERS[source](db, trigger="SCHEDULED",
                                       reload_cache=lambda: load_watchlist_cache(db))
        if report.status == "SUCCESS" and report.snapshot_id and auto_rescreen_enabled(db):
            snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
            if snap:
                rescreen_after_snapshot_change(db, snap.file_type, report.snapshot_id,
                                               report.previous_snapshot_id)
    finally:
        db.close()

async def _cron_sync_scheduler():
    """
    Planificateur cron par source : chaque minute, les sources activees dont
    l'expression cron effective (reglage a chaud > config > horaire global)
    matche sont synchronisees, chacune dans son thread, sans chevauchement
    d'une meme source.
    """
    from fiskr.cron import cron_matches, CronError

    async def _launch(source: str):
        _running_syncs.add(source)
        try:
            await asyncio.to_thread(_run_source_sync, source)
        except Exception as e:
            logger.error(f"Echec de la synchronisation planifiee de {source}: {e}")
        finally:
            _running_syncs.discard(source)

    while True:
        now = datetime.now()
        # Reveil a la prochaine minute pleine (evaluation une fois par minute)
        await asyncio.sleep(60 - now.second - now.microsecond / 1_000_000 + 0.05)
        tick = datetime.now()
        try:
            sync_cfg = get_sync_config()
            db = next(get_db())
            try:
                schedules = sync_schedules(db)
            finally:
                db.close()
            for source, expr in schedules.items():
                if not sync_cfg.get(source, {}).get("enabled"):
                    continue
                if source in _running_syncs:
                    continue  # la precedente execution n'est pas terminee
                try:
                    if cron_matches(expr, tick):
                        logger.info(f"Cron {source} ({expr}) : synchronisation declenchee.")
                        asyncio.create_task(_launch(source))
                except CronError as bad:
                    logger.error(f"Cron invalide pour {source} ({expr}) : {bad}")
        except Exception as e:
            logger.error(f"Planificateur cron en echec sur ce tick : {e}")

def build_kpi_digest(db) -> Dict[str, Any]:
    """
    Synthese conformite du digest periodique : etat de la file de travail,
    retards SLA, homologations en attente, volumetrie 24 h et sante des
    dernieres synchronisations — le tour de table du matin, sans se connecter.
    """
    now = datetime.utcnow()
    day_ago = now - timedelta(hours=24)
    open_q = db.query(Alert).filter(Alert.status.in_(ALERT_OPEN_STATUSES))
    last_sync_by_source: Dict[str, str] = {}
    for row in db.query(SyncReport).order_by(SyncReport.executed_at.desc()).limit(60).all():
        key = (row.source or "?").upper()
        if key not in last_sync_by_source:
            when = row.executed_at.strftime("%d/%m %H:%M") if row.executed_at else "?"
            last_sync_by_source[key] = f"{row.status} ({when})"
    return {
        "Alertes ouvertes — criblage": open_q.filter(
            (Alert.channel == "SCREENING") | (Alert.channel.is_(None))).count(),
        "Alertes ouvertes — filtrage": open_q.filter(Alert.channel == "FILTERING").count(),
        "Alertes en retard SLA": db.query(Alert).filter(
            Alert.status.in_(ALERT_OPEN_STATUSES), Alert.due_at.isnot(None),
            Alert.due_at < now).count(),
        "Décisions en attente 4-yeux": db.query(Alert).filter(
            Alert.status == "PENDING_VALIDATION").count(),
        "Snapshots à homologuer": db.query(Snapshot).filter(
            Snapshot.status == "PENDING_REVIEW").count(),
        "Alertes créées (24 h)": db.query(Alert).filter(Alert.created_at >= day_ago).count(),
        "Alertes clôturées (24 h)": db.query(Alert).filter(
            Alert.decided_at.isnot(None), Alert.decided_at >= day_ago).count(),
        "Dernières synchronisations": "; ".join(
            f"{s} : {st}" for s, st in sorted(last_sync_by_source.items())) or "aucune",
    }

async def _digest_scheduler():
    """
    Digest KPI periodique : chaque minute, si le reglage est actif et que son
    expression cron matche, la synthese part par email/webhooks (notify.py,
    fire-and-forget). Independant du planificateur de synchronisation : il
    tourne meme quand les syncs automatiques sont desactivees.
    """
    from fiskr.cron import cron_matches, CronError

    while True:
        now = datetime.now()
        await asyncio.sleep(60 - now.second - now.microsecond / 1_000_000 + 0.05)
        tick = datetime.now()
        try:
            db = next(get_db())
            try:
                digest_cfg = digest_settings(db)
                if not digest_cfg["enabled"]:
                    continue
                try:
                    if not cron_matches(digest_cfg["cron"], tick):
                        continue
                except CronError as bad:
                    logger.error(f"Cron du digest invalide ({digest_cfg['cron']}) : {bad}")
                    continue
                payload = build_kpi_digest(db)
            finally:
                db.close()
            logger.info("Digest KPI périodique : envoi de la synthèse conformité.")
            notify_event("kpi_digest", payload)
        except Exception as e:
            logger.error(f"Digest KPI en échec sur ce tick : {e}")

async def _retention_scheduler():
    """
    Purge de retention quotidienne : chaque minute, si au moins une famille a
    une duree de conservation non nulle et que l'expression cron de la
    politique matche, la purge s'execute (tracee RETENTION_PURGE au journal).
    """
    from fiskr.cron import cron_matches, CronError

    while True:
        now = datetime.now()
        await asyncio.sleep(60 - now.second - now.microsecond / 1_000_000 + 0.05)
        tick = datetime.now()
        try:
            db = next(get_db())
            try:
                policy = retention_policy(db)
                if not any(int(policy[f] or 0) > 0 for f in RETENTION_FAMILIES):
                    continue
                try:
                    if not cron_matches(policy["cron"], tick):
                        continue
                except CronError as bad:
                    logger.error(f"Cron de rétention invalide ({policy['cron']}) : {bad}")
                    continue
                deleted = await asyncio.to_thread(run_retention, db)
                if any(deleted.values()):
                    logger.info(f"Purge de rétention planifiée : {deleted}")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"Purge de rétention en échec sur ce tick : {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Fiskr application...")
    init_db()
    # Populate the cache from database
    db = next(get_db())
    load_watchlist_cache(db)
    # Start the per-source cron synchronization scheduler if enabled
    scheduler_task = None
    if get_sync_config()["auto_enabled"]:
        scheduler_task = asyncio.create_task(_cron_sync_scheduler())
    # Inbox CFT surveillee (auto-desactivee si batch.inbox_dir est vide)
    inbox_task = asyncio.create_task(_inbox_poller())
    # Digest KPI periodique (reglage a chaud notifications.digest)
    digest_task = asyncio.create_task(_digest_scheduler())
    # Purge de retention quotidienne (reglage a chaud retention.policy)
    retention_task = asyncio.create_task(_retention_scheduler())
    yield
    # Shutdown
    if scheduler_task:
        scheduler_task.cancel()
    inbox_task.cancel()
    digest_task.cancel()
    retention_task.cancel()
    logger.info("Stopping Fiskr application...")

app = FastAPI(
    title="Fiskr API Server",
    description="Compliance PEP/Sanctions Engine with Snapshots and Versioning Delta Engine",
    version="2.0.0",
    lifespan=lifespan
)

# En-tetes de securite HTTP sur toutes les reponses. CSP : le dashboard repose
# sur des gestionnaires inline et Google Fonts, d'ou 'unsafe-inline' cible ;
# tout le reste est restreint a l'origine.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "frame-ancestors 'none'"
    ),
}

@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    return response

@app.middleware("http")
async def translate_api_messages(request: Request, call_next):
    """
    Messages d'API multilingues : quand le client prefere une langue
    supportee (Accept-Language), les champs detail/message des reponses
    JSON sont traduits depuis le catalogue (fiskr/apimessages.py).
    Les messages hors catalogue restent en francais.
    """
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        return response
    lang = resolve_api_lang(request.headers.get("accept-language"))
    if lang == "fr":
        return response
    content_type = response.headers.get("content-type", "")
    if not content_type.startswith("application/json"):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    try:
        data = json.loads(body)
        if translate_payload(data, lang):
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeDecodeError):
        pass
    headers = dict(response.headers)
    headers.pop("content-length", None)
    return Response(content=body, status_code=response.status_code,
                    headers=headers, media_type="application/json")

@app.get("/api/health")
async def healthcheck():
    """
    Sonde de supervision (SANS authentification, volontairement minimale) :
    base de donnees joignable et cache de criblage charge. A brancher sur le
    monitoring d'exploitation (liveness/readiness).
    """
    db_ok = True
    try:
        db = next(get_db())
        try:
            from sqlalchemy import text as _sql_text
            db.execute(_sql_text("SELECT 1"))
        finally:
            db.close()
    except Exception:
        db_ok = False
    cache_ok = len(watchlist_store) > 0
    return {
        "status": "ok" if (db_ok and cache_ok) else "degraded",
        "database": db_ok,
        "watchlist_cache_loaded": cache_ok,
    }

# ------------------ PYDANTIC MODELS ------------------

class ScreenCountries(BaseModel):
    nationality: List[str] = []
    residence: List[str] = []
    birth_country: List[str] = []
    registration_country: List[str] = []

class ScreenClientRequest(BaseModel):
    client_id: Optional[str] = Field(None, json_schema_extra={"example": "CUST-0091"})
    client_type: str = Field(..., description="PP (Individu) ou PM (Entreprise)", json_schema_extra={"example": "PP"})
    client_first_name: Optional[str] = Field(None, json_schema_extra={"example": "Vladimir"})
    client_last_name: Optional[str] = Field(None, json_schema_extra={"example": "Putin"})
    client_maiden_name: Optional[str] = Field(None, json_schema_extra={"example": ""})
    client_company_name: Optional[str] = Field(None, json_schema_extra={"example": ""})
    client_dob: Optional[str] = Field(None, json_schema_extra={"example": "1952-10-07"})
    client_gender: Optional[str] = Field("U", json_schema_extra={"example": "M"})
    client_is_deceased: Optional[bool] = Field(False)
    client_countries: ScreenCountries = ScreenCountries()
    
    # New fields requested
    client_place_of_birth: Optional[str] = None
    client_address: Optional[str] = None
    client_city: Optional[str] = None
    client_state: Optional[str] = None
    client_country: Optional[str] = None
    client_origin: Optional[str] = None
    client_designation: Optional[str] = None
    client_additional_informations: Optional[str] = None
    client_alternative_addresses: List[str] = []
    client_date_of_death: Optional[str] = None
    
    # Identifiers
    transaction_vessel_imo: Optional[str] = None
    transaction_aircraft_registration: Optional[str] = None
    transaction_vessel_mmsi: Optional[str] = None
    transaction_vessel_call_sign: Optional[str] = None
    client_lei_number: Optional[str] = None

    # Champs etendus KYC (miroirs de matching)
    client_bic: Optional[str] = None
    client_tax_id: Optional[str] = None
    client_iban: Optional[str] = None
    client_crypto_wallets: List[str] = []

    client_national_registry_ids: List[Dict[str, Any]] = []
    client_other_registration_ids: List[Dict[str, Any]] = []
    client_passport_documents: List[Dict[str, Any]] = []
    client_national_id_documents: List[Dict[str, Any]] = []
    client_other_id_documents: List[Dict[str, Any]] = []

    # Restriction du criblage a un sous-ensemble de listes (WATCHLIST_*).
    # Absent ou vide = TOUTES les listes (valeur par defaut conformite).
    # Toute restriction est tracee dans le journal d'audit.
    screening_lists: Optional[List[str]] = None

class DeltaRequest(BaseModel):
    snapshot_old_id: str
    snapshot_new_id: str

class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: Optional[str] = None


# ------------------ AUTHENTICATION ENDPOINTS ------------------

@app.post("/api/auth/login")
async def login(
    request: Request,
    response: Response,
    request_data: LoginRequest,
    db: Session = Depends(get_db)
):
    """
    Authentifie l'utilisateur et pose le cookie de session HttpOnly.
    Anti-brute-force : verrouillage temporaire apres N echecs consecutifs ;
    chaque connexion, echec et verrouillage est trace au journal
    d'administration (exigence de tracabilite des acces).
    """
    sec = security_config()
    client_ip = request.client.host if request.client else "?"
    if not request_data.username or not request_data.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nom d'utilisateur et mot de passe requis."
        )

    user = db.query(User).filter(User.username == request_data.username).first()

    # Compte temporairement verrouille (anti-brute-force)
    if user and user.locked_until and user.locked_until > datetime.utcnow():
        remaining = int((user.locked_until - datetime.utcnow()).total_seconds() // 60) + 1
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail=f"Compte temporairement verrouillé après trop d'échecs. Réessayez dans {remaining} minute(s)."
        )

    def _register_failure(reason: str):
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= sec["max_login_failures"]:
                user.locked_until = datetime.utcnow() + timedelta(minutes=sec["lockout_minutes"])
                user.failed_login_count = 0
                log_admin_action(db, user.username, "ACCOUNT_LOCKED", target=user.username,
                                 detail=f"Verrouillé {sec['lockout_minutes']} min après "
                                        f"{sec['max_login_failures']} échecs consécutifs (IP {client_ip}).")
        log_admin_action(db, request_data.username, "LOGIN_FAILED",
                         target=request_data.username, detail=f"{reason} (IP {client_ip})")
        db.commit()

    if not user or not verify_password(request_data.password, user.hashed_password, user.salt):
        _register_failure("Mot de passe incorrect")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identifiants incorrects. Veuillez réessayer."
        )

    # MFA TOTP : deuxieme facteur exige quand il est active sur le compte.
    # Le mot de passe seul ne suffit plus ; un code absent redemande le champ
    # (totp_required) sans compter d'echec, un code faux compte comme un echec.
    if user.totp_enabled and user.totp_secret:
        if not (request_data.totp_code or "").strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"totp_required": True,
                        "message": "Code de vérification requis (MFA activée sur ce compte)."}
            )
        if not verify_totp(user.totp_secret, request_data.totp_code):
            _register_failure("Code MFA incorrect")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Code de vérification incorrect. Veuillez réessayer."
            )

    # Succes : remise a zero du compteur d'echecs + traçage de la session
    user.failed_login_count = 0
    user.locked_until = None
    log_admin_action(db, user.username, "LOGIN", target=user.username, detail=f"IP {client_ip}")
    db.commit()

    session_hours = max(1, sec["session_hours"])
    token = create_access_token({"sub": user.username, "role": user.role},
                                expires_delta=timedelta(hours=session_hours))
    response.set_cookie(
        key="fiskr_access_token",
        value=token,
        httponly=True,
        secure=sec["secure_cookies"],
        samesite=sec["cookie_samesite"],
        max_age=session_hours * 3600,
    )

    return {
        "access_token": token,
        "token_type": "bearer",
        "user": {
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
            "roles": parse_roles(user.role)
        }
    }

@app.post("/api/auth/logout")
async def logout(
    response: Response,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Deconnecte l'utilisateur (cookie efface, session tracee au journal)."""
    log_admin_action(db, current_user["username"], "LOGOUT", target=current_user["username"])
    db.commit()
    response.delete_cookie("fiskr_access_token")
    return {"message": "Déconnexion réussie."}

# ------------------ MFA TOTP (double authentification) ------------------

class TotpConfirmRequest(BaseModel):
    code: str

class TotpDisableRequest(BaseModel):
    password: str

def _current_human_user(db: Session, current_user: Dict[str, Any]) -> User:
    """Charge l'utilisateur humain courant ; les cles d'API n'ont pas de MFA."""
    if current_user.get("is_api_key"):
        raise HTTPException(status_code=400, detail="La MFA ne s'applique pas aux clés d'API.")
    user = db.query(User).filter(User.username == current_user["username"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    return user

@app.post("/api/auth/totp/setup")
async def totp_setup(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Demarre l'enrolement MFA : genere un secret TOTP (montre une seule fois,
    a saisir/scanner dans l'application d'authentification). La MFA ne
    devient active qu'apres confirmation d'un premier code valide.
    """
    user = _current_human_user(db, current_user)
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="La MFA est déjà activée sur ce compte.")
    secret = generate_totp_secret()
    user.totp_secret = secret
    user.totp_enabled = False
    db.commit()
    return {
        "secret": secret,
        "otpauth_uri": totp_provisioning_uri(secret, user.username),
        "message": "Saisissez le secret dans votre application d'authentification puis confirmez avec un code.",
    }

@app.post("/api/auth/totp/confirm")
async def totp_confirm(
    payload: TotpConfirmRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Active la MFA apres verification d'un premier code (preuve d'enrolement)."""
    user = _current_human_user(db, current_user)
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="La MFA est déjà activée sur ce compte.")
    if not user.totp_secret:
        raise HTTPException(status_code=400, detail="Aucun enrôlement en cours : lancez d'abord la configuration.")
    if not verify_totp(user.totp_secret, payload.code):
        raise HTTPException(status_code=400, detail="Code incorrect : vérifiez l'application d'authentification.")
    user.totp_enabled = True
    log_admin_action(db, user.username, "MFA_ENABLED", target=user.username)
    db.commit()
    return {"message": "MFA activée : un code sera demandé à chaque connexion."}

@app.post("/api/auth/totp/disable")
async def totp_disable(
    payload: TotpDisableRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Desactive la MFA (mot de passe exige : un poste laisse ouvert ne suffit pas)."""
    user = _current_human_user(db, current_user)
    if not user.totp_enabled and not user.totp_secret:
        raise HTTPException(status_code=409, detail="La MFA n'est pas activée sur ce compte.")
    if not verify_password(payload.password, user.hashed_password, user.salt):
        raise HTTPException(status_code=401, detail="Mot de passe incorrect.")
    user.totp_enabled = False
    user.totp_secret = None
    log_admin_action(db, user.username, "MFA_DISABLED", target=user.username)
    db.commit()
    return {"message": "MFA désactivée."}

@app.post("/api/users/{user_id}/totp/reset")
async def totp_admin_reset(
    user_id: int,
    db: Session = Depends(get_db),
    admin: Dict[str, Any] = Depends(require_admin)
):
    """
    Reinitialisation MFA par un admin (telephone perdu) : supprime le secret,
    l'utilisateur se reconnecte au mot de passe seul et peut re-enroler.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    if not user.totp_enabled and not user.totp_secret:
        raise HTTPException(status_code=409, detail="La MFA n'est pas activée sur ce compte.")
    user.totp_enabled = False
    user.totp_secret = None
    log_admin_action(db, admin["username"], "MFA_RESET", target=user.username,
                     detail="Réinitialisation MFA par un administrateur.")
    db.commit()
    return {"message": f"MFA réinitialisée pour {user.username}."}

class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str

class UpdateSelfProfileRequest(BaseModel):
    username: Optional[str] = None
    full_name: Optional[str] = None

class CreateUserRequest(BaseModel):
    username: str
    password: str
    full_name: Optional[str] = None
    role: str = "user"

class UpdateUserAdminRequest(BaseModel):
    username: Optional[str] = None
    password: Optional[str] = None
    full_name: Optional[str] = None
    role: Optional[str] = None

@app.get("/api/auth/me")
async def get_me(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Returns profile info of the currently logged-in user."""
    return {"user": current_user}

# ------------------ USER MANAGEMENT ENDPOINTS ------------------

@app.put("/api/users/me/password")
async def change_own_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Allows any logged-in user to change their own password."""
    if not payload.old_password or not payload.new_password:
        raise HTTPException(status_code=400, detail="L'ancien et le nouveau mot de passe sont requis.")
    try:
        validate_password(payload.new_password)
    except ValueError as weak:
        raise HTTPException(status_code=400, detail=str(weak))
        
    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user or not verify_password(payload.old_password, user.hashed_password, user.salt):
        raise HTTPException(status_code=400, detail="L'ancien mot de passe est incorrect.")
        
    h_pass, salt_str = hash_password(payload.new_password)
    user.hashed_password = h_pass
    user.salt = salt_str
    db.commit()
    return {"message": "Mot de passe modifié avec succès."}

@app.put("/api/users/me/profile")
async def update_own_profile(
    payload: UpdateSelfProfileRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Allows any logged-in user to update their profile information."""
    user = db.query(User).filter(User.id == current_user["id"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
        
    if payload.username and payload.username.strip() != user.username:
        new_uname = payload.username.strip()
        existing = db.query(User).filter(User.username == new_uname, User.id != user.id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ce nom d'utilisateur est déjà utilisé par un autre compte.")
        user.username = new_uname
        
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip()
        
    db.commit()
    db.refresh(user)
    return {
        "message": "Profil mis à jour avec succès.",
        "user": {
            "id": user.id,
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role
        }
    }

@app.get("/api/users")
async def list_users(
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Lists all user accounts (Admin only)."""
    users = db.query(User).order_by(User.id.asc()).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "full_name": u.full_name,
            "role": u.role,
            "roles": parse_roles(u.role),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "totp_enabled": bool(u.totp_enabled),
            "absent_until": u.absent_until.isoformat()
                if (u.absent_until and u.absent_until > datetime.utcnow()) else None,
            "delegate_to": u.delegate_to
                if (u.absent_until and u.absent_until > datetime.utcnow()) else None,
        }
        for u in users
    ]

# ------------------ JOURNAL DES ACTIONS D'ADMINISTRATION ------------------

def log_admin_action(db: Session, username: str, action: str, target: Optional[str] = None,
                     before: Optional[Dict[str, Any]] = None, after: Optional[Dict[str, Any]] = None,
                     detail: Optional[str] = None) -> None:
    """Trace append-only d'une action d'administration (commit par l'appelant)."""
    db.add(AdminAuditLog(username=username, action=action, target=target,
                         before=before, after=after, detail=detail))

@app.get("/api/admin-log")
async def get_admin_log(
    action: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Journal des actions d'administration (utilisateurs, reglages, purges,
    revocations) — append-only, admin uniquement."""
    query = db.query(AdminAuditLog)
    if action:
        query = query.filter(AdminAuditLog.action == action.strip().upper())
    total = query.count()
    rows = query.order_by(AdminAuditLog.at.desc(), AdminAuditLog.id.desc()) \
                .offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total, "page": page, "page_size": page_size,
        "items": [
            {
                "id": r.id, "at": r.at.isoformat() if r.at else None, "username": r.username,
                "action": r.action, "target": r.target, "before": r.before, "after": r.after,
                "detail": r.detail,
            }
            for r in rows
        ],
    }

# ------------------ RETENTION DES DONNEES (RGPD / ARCHIVAGE) ------------------

class RetentionSettingsUpdate(BaseModel):
    audit_trail: Optional[int] = None
    closed_alerts: Optional[int] = None
    sync_reports: Optional[int] = None
    batch_campaigns: Optional[int] = None
    cron: Optional[str] = None
    archive: Optional[bool] = None

@app.get("/api/admin/retention")
async def get_retention(
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Politique de retention effective + volumes qui seraient purges
    aujourd'hui (previsualisation sans aucune ecriture). Le journal des
    actions d'administration n'est jamais purge."""
    return {"policy": retention_policy(db), "preview": preview_retention(db),
            "min_days": RETENTION_MIN_DAYS}

@app.put("/api/settings/retention")
async def update_retention_settings(
    payload: RetentionSettingsUpdate,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """
    Regle a chaud les durees de conservation (jours, 0 = illimite) et l'heure
    de purge quotidienne. Garde-fou : jamais moins de RETENTION_MIN_DAYS
    quand une purge est activee.
    """
    before = retention_policy(db)
    merged = dict(before)
    provided = False
    for family in RETENTION_FAMILIES:
        value = getattr(payload, family)
        if value is None:
            continue
        provided = True
        if value != 0 and value < RETENTION_MIN_DAYS:
            raise HTTPException(
                status_code=400,
                detail=f"Durée trop courte pour {family} : minimum {RETENTION_MIN_DAYS} jours (0 = conservation illimitée).")
        merged[family] = int(value)
    if payload.cron is not None:
        cron_expr = payload.cron.strip()
        if cron_expr:
            from fiskr.cron import parse_cron, CronError
            try:
                parse_cron(cron_expr)
            except CronError as bad:
                raise HTTPException(status_code=400, detail=f"Expression cron de purge invalide : {bad}")
            merged["cron"] = cron_expr
            provided = True
    if payload.archive is not None:
        merged["archive"] = bool(payload.archive)
        provided = True
    if not provided:
        raise HTTPException(status_code=400, detail="Aucun réglage fourni.")
    set_setting(db, SETTING_RETENTION, merged, updated_by=admin_user["username"])
    delta = {k: v for k, v in merged.items() if before.get(k) != v}
    if delta:
        log_admin_action(db, admin_user["username"], "SETTINGS_UPDATED", target="retention",
                         before={k: before.get(k) for k in delta}, after=delta)
        db.commit()
    return {"message": "Politique de rétention mise à jour.",
            "policy": retention_policy(db), "preview": preview_retention(db)}

# ------------------ DELEGATION D'ABSENCE ------------------

class AbsenceRequest(BaseModel):
    absent_until: Optional[str] = None   # ISO AAAA-MM-JJ[THH:MM] ; vide/None = fin d'absence
    delegate_to: Optional[str] = None
    reassign_open: bool = True

def resolve_delegate(db: Session, assignee: str):
    """Redirige une assignation vers le delegue si l'assigne est absent.
    Retourne (assigne_effectif, assigne_initial_si_redirige). Un seul saut :
    pas de chaine de delegations."""
    user = db.query(User).filter(User.username == assignee).first()
    if (user and user.absent_until and user.absent_until > datetime.utcnow()
            and (user.delegate_to or "").strip()):
        return user.delegate_to.strip(), assignee
    return assignee, None

def _apply_absence(db: Session, user: User, payload: AbsenceRequest, actor: str) -> Dict[str, Any]:
    if not payload.absent_until:
        # Fin d'absence
        user.absent_until = None
        user.delegate_to = None
        log_admin_action(db, actor, "ABSENCE_CLEARED", target=user.username)
        db.commit()
        return {"message": f"Absence de @{user.username} terminée.", "reassigned": 0}
    try:
        until = datetime.fromisoformat(payload.absent_until)
    except ValueError:
        raise HTTPException(status_code=400, detail="Date de fin d'absence invalide (format ISO attendu).")
    if until <= datetime.utcnow():
        raise HTTPException(status_code=400, detail="La fin d'absence doit être dans le futur.")
    delegate_name = (payload.delegate_to or "").strip()
    if not delegate_name:
        raise HTTPException(status_code=400, detail="Un délégué est requis pendant l'absence.")
    if delegate_name == user.username:
        raise HTTPException(status_code=400, detail="Le délégué doit être un autre utilisateur.")
    delegate = db.query(User).filter(User.username == delegate_name).first()
    if not delegate:
        raise HTTPException(status_code=404, detail=f"Délégué introuvable : {delegate_name}.")
    if "auditor" in parse_roles(delegate.role):
        raise HTTPException(status_code=400, detail="Un auditeur (lecture seule) ne peut pas être délégué.")

    user.absent_until = until
    user.delegate_to = delegate_name
    reassigned = 0
    if payload.reassign_open:
        open_alerts = db.query(Alert).filter(
            Alert.assigned_to == user.username,
            Alert.status.in_(ALERT_OPEN_STATUSES)).all()
        for alert in open_alerts:
            alert.assigned_to = delegate_name
            _log_alert_event(db, alert.id, actor, "ASSIGNED",
                             f"Réassignée à {delegate_name} (délégation d'absence de @{user.username}).")
            reassigned += 1
    log_admin_action(db, actor, "ABSENCE_SET", target=user.username,
                     after={"absent_until": until.isoformat(), "delegate_to": delegate_name,
                            "reassigned_open_alerts": reassigned})
    db.commit()
    return {"message": f"Absence de @{user.username} enregistrée jusqu'au "
                       f"{until.strftime('%d/%m/%Y %H:%M')} — délégué : @{delegate_name}"
                       + (f", {reassigned} alerte(s) réassignée(s)." if reassigned else "."),
            "reassigned": reassigned}

@app.put("/api/users/me/absence")
async def set_own_absence(
    payload: AbsenceRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Declare sa propre absence (delegue obligatoire) ou y met fin."""
    if current_user.get("is_api_key"):
        raise HTTPException(status_code=400, detail="Les clés d'API n'ont pas d'absence.")
    user = db.query(User).filter(User.username == current_user["username"]).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    return _apply_absence(db, user, payload, current_user["username"])

@app.put("/api/users/{user_id}/absence")
async def set_user_absence(
    user_id: int,
    payload: AbsenceRequest,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Declare ou termine l'absence d'un collaborateur (Admin)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    return _apply_absence(db, user, payload, admin_user["username"])

@app.get("/api/users/directory")
async def get_users_directory(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Annuaire minimal (nom d'utilisateur + nom complet + absence) accessible
    a tout utilisateur connecte : choix d'un delegue, assignations."""
    now = datetime.utcnow()
    users = db.query(User).order_by(User.username.asc()).all()
    return {"items": [
        {"username": u.username, "full_name": u.full_name,
         "roles": parse_roles(u.role),
         "absent": bool(u.absent_until and u.absent_until > now),
         "delegate_to": u.delegate_to if (u.absent_until and u.absent_until > now) else None}
        for u in users
    ]}

# ------------------ SEUILS DE SCORE (a chaud) ------------------

class ScoringSettingsUpdate(BaseModel):
    cut_off_threshold: Optional[float] = None
    cut_off_overrides: Optional[Dict[str, Optional[float]]] = None

@app.get("/api/settings/scoring")
async def get_scoring_settings(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Seuils de cut-off effectifs (global + surcharges par type de liste)."""
    return score_thresholds(db)

@app.put("/api/settings/scoring")
async def update_scoring_settings(
    payload: ScoringSettingsUpdate,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """
    Regle a chaud le seuil global et les surcharges par liste (0-100 ;
    surcharge a None/vide = retour au seuil global). Effet immediat sur le
    criblage ET le filtrage transactionnel, sans redemarrage.
    """
    before = score_thresholds(db)
    merged = {"cut_off_threshold": before["cut_off_threshold"],
              "cut_off_overrides": dict(before["cut_off_overrides"])}
    if payload.cut_off_threshold is not None:
        if not (0 <= payload.cut_off_threshold <= 100):
            raise HTTPException(status_code=400, detail="Le seuil global doit être entre 0 et 100.")
        merged["cut_off_threshold"] = float(payload.cut_off_threshold)
    if payload.cut_off_overrides is not None:
        for list_type, threshold in payload.cut_off_overrides.items():
            key = str(list_type).strip().upper()
            if threshold is None:
                merged["cut_off_overrides"].pop(key, None)
                continue
            if not (0 <= float(threshold) <= 100):
                raise HTTPException(status_code=400, detail=f"Seuil invalide pour {key} (0-100).")
            merged["cut_off_overrides"][key] = float(threshold)
    if payload.cut_off_threshold is None and payload.cut_off_overrides is None:
        raise HTTPException(status_code=400, detail="Aucun réglage fourni.")
    set_setting(db, SETTING_SCORE_THRESHOLDS, merged, updated_by=admin_user["username"])
    after = score_thresholds(db)
    delta = {k: after[k] for k in ("cut_off_threshold", "cut_off_overrides") if before.get(k) != after.get(k)}
    if delta:
        log_admin_action(db, admin_user["username"], "SETTINGS_UPDATED", target="scoring",
                         before={k: before.get(k) for k in delta}, after=delta)
        db.commit()
    return {"message": "Seuils de score mis à jour (effet immédiat).", **after}

class ScoringSimulateRequest(BaseModel):
    cut_off_threshold: Optional[float] = None
    cut_off_overrides: Optional[Dict[str, Optional[float]]] = None
    days: int = 30

_SIMULATE_MAX_ROWS = 50000

@app.post("/api/settings/scoring/simulate")
async def simulate_scoring_thresholds(
    payload: ScoringSimulateRequest,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """
    Simulation d'impact AVANT de changer les seuils : rejoue les decisions
    de criblage des N derniers jours (journal d'audit immuable) avec les
    seuils candidats et compte, par liste, les alertes en plus / en moins.
    Aucune ecriture : le reglage reel reste inchange.
    """
    if not (1 <= payload.days <= 365):
        raise HTTPException(status_code=400, detail="La période doit être entre 1 et 365 jours.")
    current = score_thresholds(db)
    candidate_global = current["cut_off_threshold"]
    candidate_overrides = dict(current["cut_off_overrides"])
    if payload.cut_off_threshold is not None:
        if not (0 <= payload.cut_off_threshold <= 100):
            raise HTTPException(status_code=400, detail="Le seuil global doit être entre 0 et 100.")
        candidate_global = float(payload.cut_off_threshold)
    if payload.cut_off_overrides is not None:
        for list_type, threshold in payload.cut_off_overrides.items():
            key = str(list_type).strip().upper()
            if threshold is None:
                candidate_overrides.pop(key, None)
            else:
                if not (0 <= float(threshold) <= 100):
                    raise HTTPException(status_code=400, detail=f"Seuil invalide pour {key} (0-100).")
                candidate_overrides[key] = float(threshold)

    def candidate_cutoff(list_type: Optional[str]) -> float:
        if list_type and list_type in candidate_overrides:
            return candidate_overrides[list_type]
        return candidate_global

    since = datetime.utcnow() - timedelta(days=payload.days)
    # Seules les decisions avec un vrai candidat sont rejouables ; les
    # suppressions gouvernees (liste blanche, regles) restent supprimees
    rows = (db.query(AuditTrail.list_type, AuditTrail.status, AuditTrail.final_score)
              .filter(AuditTrail.timestamp >= since,
                      AuditTrail.watchlist_id != "NONE",
                      ~AuditTrail.status.in_(("WHITELISTED", "CLOSED_BY_RULE")))
              .limit(_SIMULATE_MAX_ROWS).all())

    by_list: Dict[str, Dict[str, int]] = {}
    for list_type, status_val, final_score in rows:
        key = list_type or "UNKNOWN"
        bucket = by_list.setdefault(key, {"replayed": 0, "alerts_now": 0, "alerts_candidate": 0})
        bucket["replayed"] += 1
        if status_val == "ALERT":
            bucket["alerts_now"] += 1
        if final_score is not None and final_score >= candidate_cutoff(key):
            bucket["alerts_candidate"] += 1
    for bucket in by_list.values():
        bucket["delta"] = bucket["alerts_candidate"] - bucket["alerts_now"]

    totals = {
        "replayed": sum(b["replayed"] for b in by_list.values()),
        "alerts_now": sum(b["alerts_now"] for b in by_list.values()),
        "alerts_candidate": sum(b["alerts_candidate"] for b in by_list.values()),
    }
    totals["delta"] = totals["alerts_candidate"] - totals["alerts_now"]
    return {
        "period_days": payload.days,
        "current": {"cut_off_threshold": current["cut_off_threshold"],
                    "cut_off_overrides": current["cut_off_overrides"]},
        "candidate": {"cut_off_threshold": candidate_global,
                      "cut_off_overrides": candidate_overrides},
        "by_list": by_list,
        "totals": totals,
        "truncated": len(rows) >= _SIMULATE_MAX_ROWS,
    }

# ------------------ IMPORT / EXPORT DE LA CONFIGURATION ------------------

# Seuls les reglages a chaud connus sont portables : jamais de secrets
# (les cles d'API, comptes et mots de passe ne transitent pas par ici)
_PORTABLE_SETTINGS = {
    SETTING_REQUIRE_APPROVAL, SETTING_EXCLUSION_JUSTIFICATION_REQUIRED,
    SETTING_EXCLUSION_FILE_REQUIRED, SETTING_ALERT_FOUR_EYES,
    SETTING_WHITELIST_JUSTIFICATION_REQUIRED, SETTING_WHITELIST_FILE_REQUIRED,
    SETTING_AUTO_RESCREEN, SETTING_BACKTEST_REQUIRED, SETTING_BACKTEST_MAX_GAP_PCT,
    SETTING_BLOCKING_SCREENING, SETTING_BLOCKING_FILTERING,
    SETTING_SYNC_SCHEDULES, SETTING_ALERT_SLA_HOURS, SETTING_NOTIFICATIONS,
    SETTING_DIGEST, SETTING_RETENTION, SETTING_SCORE_THRESHOLDS,
}

class ConfigImportRequest(BaseModel):
    settings: Dict[str, Any]

@app.get("/api/admin/config/export")
async def export_app_config(
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """
    Export JSON des reglages a chaud (portabilite entre environnements :
    recette -> production). Uniquement les reglages connus, aucun secret.
    """
    rows = db.query(AppSetting).filter(AppSetting.key.in_(_PORTABLE_SETTINGS)).all()
    payload = {
        "application": "fiskr",
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "exported_by": admin_user["username"],
        "settings": {row.key: row.value for row in rows},
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="fiskr_config_{datetime.utcnow().strftime("%Y%m%d_%H%M")}.json"'},
    )

@app.post("/api/admin/config/import")
async def import_app_config(
    payload: ConfigImportRequest,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """
    Import des reglages exportes : seules les cles connues sont appliquees
    (les autres sont restituees dans `skipped`), le delta est journalise
    SETTINGS_IMPORTED. Prend effet immediatement, sans redemarrage.
    """
    if not payload.settings:
        raise HTTPException(status_code=400, detail="Aucun réglage dans le fichier importé.")
    applied, skipped, before, after = [], [], {}, {}
    for key, value in payload.settings.items():
        if key not in _PORTABLE_SETTINGS:
            skipped.append(key)
            continue
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        old_value = row.value if row else None
        if old_value != value:
            before[key] = old_value
            after[key] = value
        set_setting(db, key, value, updated_by=admin_user["username"])
        applied.append(key)
    if not applied:
        raise HTTPException(status_code=400,
                            detail="Aucune clé reconnue dans le fichier (clés attendues : "
                                   + ", ".join(sorted(_PORTABLE_SETTINGS)) + ").")
    if after:
        log_admin_action(db, admin_user["username"], "SETTINGS_IMPORTED", target="config",
                         before=before, after=after,
                         detail=f"{len(applied)} réglage(s) importé(s), {len(skipped)} ignoré(s).")
        db.commit()
    return {"applied": sorted(applied), "skipped": sorted(skipped),
            "changed": sorted(after.keys()),
            "message": f"{len(applied)} réglage(s) appliqué(s)"
                       + (f", {len(skipped)} clé(s) inconnue(s) ignorée(s)" if skipped else "")
                       + "."}

@app.post("/api/admin/retention/run")
async def run_retention_now(
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Execute la purge de retention immediatement (tracee RETENTION_PURGE)."""
    deleted = run_retention(db, username=admin_user["username"])
    total = sum(deleted.values())
    return {"deleted": deleted,
            "message": f"Purge effectuée : {total} enregistrement(s) supprimé(s)."
                       if total else "Rien à purger avec la politique actuelle."}

@app.post("/api/users")
async def create_user(
    payload: CreateUserRequest,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Creates a new user account (Admin only)."""
    username = payload.username.strip()
    if not username or not payload.password:
        raise HTTPException(status_code=400, detail="Nom d'utilisateur et mot de passe requis.")
    try:
        validate_password(payload.password)
    except ValueError as weak:
        raise HTTPException(status_code=400, detail=str(weak))

    try:
        canonical_role = normalize_roles(payload.role)
    except ValueError as role_err:
        raise HTTPException(status_code=400, detail=str(role_err))

    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"L'utilisateur '{username}' existe déjà.")
        
    h_pass, salt_str = hash_password(payload.password)
    new_user = User(
        username=username,
        hashed_password=h_pass,
        salt=salt_str,
        full_name=(payload.full_name or "").strip(),
        role=canonical_role
    )
    db.add(new_user)
    log_admin_action(db, admin_user["username"], "USER_CREATED", target=username,
                     after={"username": username, "full_name": new_user.full_name, "role": canonical_role})
    db.commit()
    db.refresh(new_user)

    return {
        "message": f"Utilisateur '{username}' créé avec succès.",
        "user": {
            "id": new_user.id,
            "username": new_user.username,
            "full_name": new_user.full_name,
            "role": new_user.role
        }
    }

@app.put("/api/users/{user_id}")
async def update_user_admin(
    user_id: int,
    payload: UpdateUserAdminRequest,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Updates any user account details or resets password (Admin only)."""
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
    before_state = {"username": target_user.username, "full_name": target_user.full_name,
                    "role": target_user.role}

    if payload.username and payload.username.strip() != target_user.username:
        new_uname = payload.username.strip()
        existing = db.query(User).filter(User.username == new_uname, User.id != user_id).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ce nom d'utilisateur est déjà attribué à un autre compte.")
        target_user.username = new_uname
        
    if payload.full_name is not None:
        target_user.full_name = payload.full_name.strip()
        
    if payload.role:
        try:
            target_user.role = normalize_roles(payload.role)
        except ValueError as role_err:
            raise HTTPException(status_code=400, detail=str(role_err))
        
    if payload.password and payload.password.strip():
        try:
            validate_password(payload.password.strip())
        except ValueError as weak:
            raise HTTPException(status_code=400, detail=str(weak))
        h_pass, salt_str = hash_password(payload.password.strip())
        target_user.hashed_password = h_pass
        target_user.salt = salt_str

    log_admin_action(
        db, admin_user["username"], "USER_UPDATED", target=target_user.username,
        before=before_state,
        after={"username": target_user.username, "full_name": target_user.full_name,
               "role": target_user.role},
        detail="Mot de passe réinitialisé." if (payload.password and payload.password.strip()) else None,
    )
    db.commit()
    db.refresh(target_user)
    
    return {
        "message": "Compte utilisateur mis à jour.",
        "user": {
            "id": target_user.id,
            "username": target_user.username,
            "full_name": target_user.full_name,
            "role": target_user.role
        }
    }

@app.delete("/api/users/{user_id}")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Deletes a user account (Admin only). Cannot delete active self user."""
    if user_id == admin_user["id"]:
        raise HTTPException(status_code=400, detail="Impossible de supprimer votre propre compte actif.")
        
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="Utilisateur introuvable.")
        
    db.delete(target_user)
    log_admin_action(db, admin_user["username"], "USER_DELETED", target=target_user.username,
                     before={"username": target_user.username, "full_name": target_user.full_name,
                             "role": target_user.role})
    db.commit()
    return {"message": f"Utilisateur '{target_user.username}' supprimé avec succès."}


# ------------------ CLES D'API TECHNIQUES (comptes de service) ------------------

class ApiKeyCreateRequest(BaseModel):
    name: str
    role: str = "user"

def _api_key_summary(key: ApiKey) -> Dict[str, Any]:
    return {
        "id": key.id, "name": key.name, "prefix": key.prefix, "roles": key.roles,
        "created_by": key.created_by,
        "created_at": key.created_at.isoformat() if key.created_at else None,
        "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None,
        "revoked_by": key.revoked_by,
        "revoked_at": key.revoked_at.isoformat() if key.revoked_at else None,
        "active": key.revoked_at is None,
    }

@app.post("/api/apikeys")
async def create_api_key(
    payload: ApiKeyCreateRequest,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """
    Cree une cle d'API technique (integrations systemes : CFT, ordonnanceur,
    SI amont). La cle complete « fsk_... » n'est retournee QU'ICI, une seule
    fois — seuls le prefixe et le hash sont conserves. Moindre privilege :
    le role admin est interdit aux comptes de service.
    """
    import secrets as _secrets
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Un nom de clé est requis (ex. « CFT production »).")
    try:
        canonical_role = normalize_roles(payload.role)
    except ValueError as role_err:
        raise HTTPException(status_code=400, detail=str(role_err))
    if "admin" in parse_roles(canonical_role):
        raise HTTPException(status_code=400,
                            detail="Le rôle admin est interdit pour une clé d'API (moindre privilège).")
    full_key = "fsk_" + _secrets.token_urlsafe(33)
    key = ApiKey(
        name=name, prefix=full_key[:API_KEY_PREFIX_LEN], key_hash=hash_api_key(full_key),
        roles=canonical_role, created_by=admin_user["username"],
    )
    db.add(key)
    log_admin_action(db, admin_user["username"], "APIKEY_CREATED", target=name,
                     after={"prefix": full_key[:API_KEY_PREFIX_LEN], "roles": canonical_role})
    db.commit()
    db.refresh(key)
    return {
        "message": "Clé créée. Copiez-la maintenant : elle ne sera plus jamais affichée.",
        "api_key": full_key,
        **_api_key_summary(key),
    }

@app.get("/api/apikeys")
async def list_api_keys(
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Liste des cles d'API (prefixes seulement, jamais les cles completes)."""
    rows = db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()
    return {"items": [_api_key_summary(k) for k in rows]}

@app.post("/api/apikeys/{key_id}/revoke")
async def revoke_api_key(
    key_id: int,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Revocation douce d'une cle d'API (effet immediat, jamais supprimee)."""
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="Clé introuvable.")
    if key.revoked_at:
        raise HTTPException(status_code=409, detail="Clé déjà révoquée.")
    key.revoked_at = datetime.utcnow()
    key.revoked_by = admin_user["username"]
    log_admin_action(db, admin_user["username"], "APIKEY_REVOKED", target=key.name,
                     before={"prefix": key.prefix, "roles": key.roles})
    db.commit()
    return {"message": f"Clé « {key.name} » révoquée.", **_api_key_summary(key)}

# ------------------ DATA ENDPOINTS ------------------

def screen_client_profile(db: Session, client_dict: Dict[str, Any], username: str,
                          requested_lists: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Cœur du criblage unitaire, PARTAGE entre l'endpoint temps réel /api/screen
    et les campagnes batch : quality gate -> blocking -> scoring -> liste
    blanche -> règles anti-faux positifs -> journal d'audit -> alerte.
    Lève HTTPException 400 si le quality gate rejette le profil.
    """
    client_dict = dict(client_dict)
    # Normalize client_type to PP/PM for internal validation and scoring engine
    if client_dict.get("client_type") in ["I", "PP"]:
        client_dict["client_type"] = "PP"
    else:
        client_dict["client_type"] = "PM"
        
    # Evaluate Data Quality
    report = evaluate_and_clean(client_dict)
    if not report["is_valid"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"errors": report["errors"]}
        )
        
    cleansed_client = client_dict.copy()
    
    # Override client fields with cleansed variables
    if client_dict["client_type"] == "PP":
        cleansed_client["client_first_name"] = report["cleansed_name"].split()[0] if report["cleansed_name"].split() else ""
        cleansed_client["client_last_name"] = " ".join(report["cleansed_name"].split()[1:]) if len(report["cleansed_name"].split()) > 1 else report["cleansed_name"]
        cleansed_client["client_maiden_name"] = report["cleansed_maiden_name"]
    else:
        cleansed_client["client_company_name"] = report["cleansed_name"]
        
    cleansed_client["client_gender"] = report["resolved_gender"]
    
    # Generate blocking keys — meme layout que celui de l'index en memoire
    client_keys = generate_blocking_keys(cleansed_client, blocking_config_for(watchlist_index_layout))

    # Retrieve candidates matching blocking keys
    candidates = {}
    for key in client_keys:
        for item in watchlist_index.get(key, []):
            if requested_lists and item.get("_list_type") not in requested_lists:
                continue
            candidates[item["entity_id"]] = item

    # Scoring — seuils de cut-off a chaud (reglage > config.yaml)
    scoring_config = scoring_config_with_thresholds(db)
    matches = []
    best_match = None
    best_score = -1.0

    for item_id, candidate in candidates.items():
        score_res = match_entities(cleansed_client, candidate, scoring_config)
        score_res["watchlist_entity"] = candidate
        
        matches.append(score_res)
        if score_res["final_score"] > best_score:
            best_score = score_res["final_score"]
            best_match = score_res
            
    # Audit trail persistence
    audit_id = None
    alert_id = None
    whitelist_pair_id = None
    if best_match:
        # Liste blanche client x liste : la suppression n'est JAMAIS silencieuse,
        # le journal d'audit trace la decision avec le statut WHITELISTED
        if best_match.get("status") == "ALERT":
            wl_pair = is_whitelisted(
                db, client_dict.get("client_id"),
                (best_match.get("watchlist_entity") or {}).get("entity_id")
            )
            if wl_pair:
                best_match["status"] = "WHITELISTED"
                best_match["whitelist_pair_id"] = wl_pair.id
                whitelist_pair_id = wl_pair.id
        # Tracabilite : toute restriction du perimetre de criblage est
        # persistee dans le decision_tree du journal immuable
        best_match["screening_lists_restriction"] = requested_lists or "ALL"
        # Regle des 50 % : risque herite par detention majoritaire du liste
        # matche (informatif, trace dans le decision_tree)
        matched_entity_id = (best_match.get("watchlist_entity") or {}).get("entity_id")
        if matched_entity_id:
            inherited = compute_inherited_risk(db, matched_entity_id, max_depth=2)
            if inherited:
                best_match["ownership_inherited_risk"] = inherited
        # Regles anti-faux positifs du canal SCREENING : appliquees avant de
        # tracer, pour marquer la decision dans le journal immuable
        suppressed_by_rule = None
        if best_match.get("status") == "ALERT":
            ctx = build_screening_ctx(client_dict, best_match["watchlist_entity"], best_match)
            suppressed_by_rule = evaluate_fp_rules(db, "SCREENING", ctx)
            if suppressed_by_rule is not None:
                annotate_suppression(best_match, suppressed_by_rule)
        audit_record = log_compliance_decision(
            db,
            client_dict,
            best_match["watchlist_entity"],
            best_match,
            watchlist_version,
            watchlist_hash
        )
        audit_id = audit_record.id
        # Une decision ALERT ouvre (ou re-detecte) une alerte de travail
        if best_match.get("status") == "ALERT":
            alert_id = open_or_redetect_alert(
                db, audit_record, client_dict.get("client_id"), best_match, username,
                channel="SCREENING",
                suppressed_by_rule=suppressed_by_rule,
                detail_suffix=(
                    f" [Criblage restreint aux listes : {', '.join(requested_lists)}]"
                    if requested_lists else ""
                )
            )
    else:
        # Log dummy NO_MATCH result
        no_match_result = {
            "status": "NO_MATCH",
            "base_score": 0.0,
            "final_score": 0.0,
            "hard_match_triggered": False,
            "best_client_name": report["cleansed_name"],
            "best_watchlist_name": "Aucun candidat trouvé (Bloqué)",
            "adjustments": {
                "dob": {"score": 0.0, "description": "N/A"},
                "gender": {"score": 0.0, "description": "N/A"},
                "geography": {"score": 0.0, "description": "N/A"}
            },
            "cut_off_applied": scoring_config.get("scoring", {}).get("cut_off_threshold", 75.0),
            "screening_lists_restriction": requested_lists or "ALL"
        }
        dummy_wl = {"entity_id": "NONE", "primary_name": "Aucun match"}
        audit_record = log_compliance_decision(
            db,
            client_dict,
            dummy_wl,
            no_match_result,
            watchlist_version,
            watchlist_hash
        )
        audit_id = audit_record.id
        
    return {
        "client_quality_report": report,
        "blocking_keys_generated": list(client_keys),
        "candidates_count": len(candidates),
        "best_match": best_match,
        "all_matches": sorted(matches, key=lambda x: x["final_score"], reverse=True),
        "audit_trail_id": audit_id,
        "alert_id": alert_id,
        "whitelisted": whitelist_pair_id is not None,
        "whitelist_pair_id": whitelist_pair_id,
        "screening_lists": requested_lists or "ALL"
    }

def _validate_screening_lists(raw_lists) -> Optional[List[str]]:
    """Valide et normalise une restriction de perimetre (None = toutes listes)."""
    if not raw_lists:
        return None
    requested = sorted({v.strip().upper() for v in raw_lists if v and v.strip()})
    invalid = [v for v in requested if v not in WATCHLIST_FILE_TYPES]
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Type(s) de liste inconnu(s) : {', '.join(invalid)} (valeurs possibles : {', '.join(WATCHLIST_FILE_TYPES)})."
        )
    if set(requested) == set(WATCHLIST_FILE_TYPES):
        return None  # toutes les listes = aucune restriction
    return requested

@app.post("/api/screen")
async def screen_client(
    request: ScreenClientRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Screens a client profile against active watchlists in-memory cache.
    1. Runs Data Quality Gate evaluation.
    2. Runs exact Hard Match priority sequences.
    3. Runs fuzzy matching and contextual adjustment calculations.
    """
    client_dict = request.model_dump()
    # Restriction eventuelle du perimetre (retiree du profil : elle n'en fait pas partie)
    client_dict.pop("screening_lists", None)
    requested_lists = _validate_screening_lists(request.screening_lists)
    return screen_client_profile(db, client_dict, current_user["username"], requested_lists)

@app.post("/api/transactions/screen")
async def screen_transaction_message(
    file: UploadFile = File(...),
    screening_lists: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Filtrage transactionnel ISO 20022 : parse un message de paiement pain.001
    ou pacs.008, crible chaque partie (donneur d'ordre, bénéficiaire, ultimes,
    agents bancaires) contre les listes en production — ou le sous-ensemble
    `screening_lists` (CSV de types WATCHLIST_*, restriction tracée dans
    l'audit) — et rend un verdict global PASS / HIT. Chaque partie criblée
    est tracée dans le journal d'audit ; chaque hit ouvre une alerte.
    """
    requested_lists = None
    if screening_lists and screening_lists.strip():
        requested_lists = sorted({v.strip().upper() for v in screening_lists.split(",") if v.strip()})
        invalid = [v for v in requested_lists if v not in WATCHLIST_FILE_TYPES]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Type(s) de liste inconnu(s) : {', '.join(invalid)} (valeurs possibles : {', '.join(WATCHLIST_FILE_TYPES)})."
            )
        if set(requested_lists) == set(WATCHLIST_FILE_TYPES):
            requested_lists = None

    content = await file.read()
    try:
        parsed = parse_iso20022_payment(content)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    result = screen_payment_message(
        db, parsed, watchlist_index, watchlist_version, watchlist_hash,
        current_user["username"], screening_lists=requested_lists
    )
    return result


@app.get("/api/adverse-media")
def adverse_media_lookup(
    name: str = Query(..., min_length=2),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Revue de presse négative (adverse media) sur un nom, via le fournisseur
    configuré (Google News RSS par défaut). Purement informatif : ne modifie
    jamais un score ni un statut de criblage.
    Fonction synchrone à dessein : l'appel HTTP sortant est bloquant, FastAPI
    l'exécute donc dans son threadpool sans geler l'event loop.
    """
    try:
        return search_adverse_media(name)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Le fournisseur adverse media est injoignable : {e}"
        )


_INGEST_COMMIT_EVERY = 1000

def _ingest_progress_tick(db: Session, snap: Snapshot, count: int,
                          progress_id: Optional[str]) -> Snapshot:
    """
    Point de progression periodique d'un import : persiste le compteur
    (visible par polling meme depuis une autre session), vide l'identity
    map SQLAlchemy (un import de 750 000 fiches n'accumule plus les objets
    en RAM) et actualise le registre memoire.
    """
    snap.processed_count = count
    snap.phase = "PERSIST"
    db.commit()
    db.expunge_all()
    snap = db.merge(snap)
    progress_registry.update(progress_id, phase="PERSIST", processed=count,
                             snapshot_id=snap.snapshot_id)
    return snap

@app.post("/api/snapshots/ingest")
@app.post("/api/ingest")
def ingest_snapshot(
    file_type: str = Form(...),
    file: UploadFile = File(...),
    delimiter: str = Form(","),
    ssie_selectors: Optional[str] = Form(None),
    ssie_source_format: Optional[str] = Form(None),
    progress_id: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Ingest XML, CSV or PDF files into the database.
    Performs data quality validation and saves snapshot.
    WATCHLIST_SSIE runs the Smart Sanctions Ingestion Engine pipeline
    (Discovery -> Resolution -> Restitution) with configurable tag selectors.
    Fonction synchrone volontairement (`def`) : FastAPI l'execute dans le
    threadpool, l'event loop reste disponible pour servir GET /api/progress
    pendant toute la duree de l'import (suivi en direct des gros fichiers).
    """
    # Validate SSIE selectors overrides upfront (before any snapshot record is created)
    ssie_selector_overrides = None
    if file_type == "WATCHLIST_SSIE" and ssie_selectors:
        try:
            ssie_selector_overrides = json.loads(ssie_selectors)
            if not isinstance(ssie_selector_overrides, dict):
                raise ValueError("selectors must be a JSON object")
        except (json.JSONDecodeError, ValueError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid ssie_selectors JSON: {e}"
            )
    # 1. Create a temporary path
    temp_dir = PROJECT_ROOT / "temp_ingestion"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_file_path = temp_dir / file.filename
    
    try:
        # 1+2. Copie du televersement ET empreinte SHA-256 en une seule passe
        # streamee (jamais le fichier entier en memoire — 750k fiches PEP
        # representent plusieurs centaines de Mo)
        progress_registry.update(progress_id, phase="UPLOAD")
        hasher = hashlib.sha256()
        bytes_received = 0
        with open(temp_file_path, "wb") as buffer:
            while chunk := file.file.read(1024 * 1024):
                buffer.write(chunk)
                hasher.update(chunk)
                bytes_received += len(chunk)
                progress_registry.update(progress_id, phase="UPLOAD", processed=bytes_received)
        fhash = hasher.hexdigest()
            
        # Clean up any existing failed snapshots with the same hash
        failed_snapshots = db.query(Snapshot).filter(Snapshot.file_hash == fhash, Snapshot.status == "ERROR").all()
        if failed_snapshots:
            failed_ids = [fs.snapshot_id for fs in failed_snapshots]
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(failed_ids)).delete(synchronize_session=False)
            db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(failed_ids)).delete(synchronize_session=False)
            for fs in failed_snapshots:
                db.delete(fs)
            db.commit()
            
        # Validate that snapshot hash doesn't already exist to prevent redundant work
        exists = db.query(Snapshot).filter(Snapshot.file_hash == fhash).first()
        if exists:
            # Snapshot already loaded (possibly pending review or rejected), reuse it.
            return {
                "message": "Snapshot with this hash already uploaded.",
                "snapshot_id": exists.snapshot_id,
                "record_count": exists.record_count,
                "status": exists.status
            }
            
        # Create Snapshot record
        snap_id = str(uuid.uuid4())
        snap = Snapshot(
            snapshot_id=snap_id,
            file_type=file_type,
            file_name=file.filename,
            file_hash=fhash,
            record_count=0,
            status="PROCESSING"
        )
        db.add(snap)
        db.commit()
        
        record_count = 0
        ofac_relations = None  # liens entre profils (OFAC uniquement)

        # 3. Parse contents based on File Type
        eu_fsf_upload = file_type == "WATCHLIST_EU" and file.filename.lower().endswith(".xml")
        if file_type in ("WATCHLIST_OFAC", "WATCHLIST_SSIE", "WATCHLIST_DGT", "WATCHLIST_UN", "WATCHLIST_PEP", "WATCHLIST_OFSI") or eu_fsf_upload:
            if eu_fsf_upload:
                # Liste consolidee UE au format FSF XML officiel
                parser_stream = parse_eu_fsf_xml(str(temp_file_path))
            elif file_type == "WATCHLIST_PEP":
                # Dataset PEP OpenSanctions (targets.simple.csv)
                parser_stream = parse_pep_targets_csv(str(temp_file_path))
            elif file_type == "WATCHLIST_OFSI":
                # Liste consolidee UK OFSI (ConList.csv format 2022)
                parser_stream = parse_ofsi_conlist_csv(str(temp_file_path))
            elif file_type == "WATCHLIST_UN":
                # Liste consolidee du Conseil de securite de l'ONU (XML officiel)
                parser_stream = parse_un_consolidated_xml(str(temp_file_path))
            elif file_type == "WATCHLIST_DGT":
                # Registre national des gels (DGT) : fichier JSON officiel
                parser_stream = parse_dgt_gels_json(str(temp_file_path))
            elif file_type == "WATCHLIST_SSIE":
                # Smart Sanctions Ingestion Engine: config-driven agnostic XML pipeline
                ssie_config = config.get("ssie", {}) or {}
                selectors = merge_ssie_selectors(ssie_config.get("selectors"))
                if ssie_selector_overrides:
                    selectors = merge_ssie_selectors({**selectors, **ssie_selector_overrides})
                source_format = ssie_source_format or ssie_config.get("source_format") or DEFAULT_SOURCE_FORMAT
                parser_stream = parse_ssie_xml(str(temp_file_path), selectors=selectors, source_format=source_format)
            else:
                # OFAC Advanced XML parsing (iterparse) — les liens entre
                # profils (ownership) sont recoltes au passage
                ofac_relations = []
                parser_stream = parse_ofac_advanced_xml(str(temp_file_path), relations_out=ofac_relations)

            for item in parser_stream:
                # Complete le decoupage prenoms / nom des individus si absent
                item = ensure_parsed_name(item)
                # Validate quality gate
                report = evaluate_and_clean(item)
                if not report["is_valid"]:
                    continue
                    
                # Create checksum
                ent_checksum = compute_checksum(item)
                
                parsed_name = item.get("individual_name_parsed") or {}
                
                alt_addrs_ofac = [a.strip() for a in item.get("alternative_addresses", "").split(";")] if isinstance(item.get("alternative_addresses"), str) else (item.get("alternative_addresses") or [])
                db_ent = WatchlistEntity(
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
                    # New fields
                    place_of_birth=item.get("place_of_birth"),
                    address=item.get("address") or item.get("adress"),
                    city=item.get("city"),
                    state=item.get("state"),
                    country=item.get("country"),
                    origin=item.get("origin"),
                    designation=item.get("designation"),
                    designation_reasons=item.get("designation_reasons"),
                    additional_informations=item.get("additional_informations") or item.get("additional_info"),
                    official_reference=item.get("official_reference"),
                    alternative_addresses=alt_addrs_ofac,
                    imo_number=item.get("imo_number"),
                    aircraft_tail_number=item.get("aircraft_tail_number"),
                    lei_number=item.get("lei_number"),
                    national_registry_ids=item.get("national_registry_ids"),
                    other_registration_ids=item.get("other_registration_ids"),
                    passport_documents=item.get("passport_documents"),
                    national_id_documents=item.get("national_id_documents"),
                    other_id_documents=item.get("other_id_documents"),
                    entity_checksum=ent_checksum,
                    **_extended_entity_kwargs(item)
                )
                db.add(db_ent)
                record_count += 1
                if record_count % _INGEST_COMMIT_EVERY == 0:
                    snap = _ingest_progress_tick(db, snap, record_count, progress_id)

            # Rafraichissement idempotent du graphe de relations OFAC
            # (les relations MANUAL ne sont jamais touchees)
            if ofac_relations:
                rel_count = refresh_source_relationships(db, "OFAC", ofac_relations)
                logger.info(f"OFAC : {rel_count} relation(s) entre profils rafraîchie(s).")

        elif file_type == "WATCHLIST_EU":
            # PDF or CSV
            if file.filename.endswith(".pdf"):
                extracted = parse_pdf_watchlist(str(temp_file_path))
                for item in extracted:
                    item = ensure_parsed_name(item)
                    report = evaluate_and_clean(item)
                    if not report["is_valid"]:
                        continue
                    ent_checksum = compute_checksum(item)

                    parsed_pdf = item.get("individual_name_parsed") or {"first_name": "", "last_name": "", "maiden_name": ""}
                    alt_addrs_pdf = [a.strip() for a in item.get("alternative_addresses", "").split(";")] if isinstance(item.get("alternative_addresses"), str) else (item.get("alternative_addresses") or [])
                    db_ent = WatchlistEntity(
                        snapshot_id=snap_id,
                        entity_id=item.get("entity_id"),
                        entity_type=item.get("entity_type"),
                        primary_name=report["cleansed_name"],
                        individual_name_parsed=parsed_pdf,
                        aliases={"high_priority": [], "low_priority": []},
                        dates_of_birth=[],
                        is_deceased=False,
                        gender="U",
                        countries=item.get("countries", {}),
                        # New fields
                        place_of_birth=item.get("place_of_birth"),
                        address=item.get("address") or item.get("adress"),
                        city=item.get("city"),
                        state=item.get("state"),
                        country=item.get("country"),
                        origin=item.get("origin"),
                        designation=item.get("designation"),
                        designation_reasons=item.get("designation_reasons"),
                        additional_informations=item.get("additional_informations") or item.get("additional_info"),
                        official_reference=item.get("official_reference"),
                        alternative_addresses=alt_addrs_pdf,
                        imo_number=item.get("imo_number"),
                        entity_checksum=ent_checksum,
                        **_extended_entity_kwargs(item)
                    )
                    db.add(db_ent)
                    record_count += 1
                    if record_count % _INGEST_COMMIT_EVERY == 0:
                        snap = _ingest_progress_tick(db, snap, record_count, progress_id)
            else:
                for item in parse_csv_file(str(temp_file_path), delimiter=delimiter):
                    # Moteur de detection des noms : colonnes explicites ou
                    # decoupage du nom principal pour les individus (PP/I)
                    item = ensure_parsed_name(item)
                    report = evaluate_and_clean(item)
                    if not report["is_valid"]:
                        continue
                    ent_checksum = compute_checksum(item)
                    
                    # Convert CSV record to Watchlist Schema
                    aliases = item.get("aliases", [])
                    if isinstance(aliases, str) and aliases:
                        aliases = [a.strip() for a in aliases.split(",") if a]
                        from fiskr.ingest import categorize_aliases
                        raw = [{"name": a, "type": "Strong"} for a in aliases]
                        aliases = categorize_aliases(raw)
                    elif not isinstance(aliases, dict):
                        aliases = {"high_priority": [], "low_priority": []}
                        
                    dob = item.get("dates_of_birth") or item.get("dob")
                    dob_arr = [dob] if dob else []
                    
                    countries = {
                        "citizenship": [c.strip() for c in (item.get("nationality") or "").split(",") if c],
                        "residence": [c.strip() for c in (item.get("residence") or "").split(",") if c]
                    }
                    
                    raw_etype = item.get("entity_type", "E")
                    etype = "I" if raw_etype == "PP" else ("E" if raw_etype == "PM" else raw_etype)
                    
                    parsed_csv = item.get("individual_name_parsed") or {"first_name": "", "last_name": "", "maiden_name": ""}
                    alt_addrs_csv = [a.strip() for a in item.get("alternative_addresses", "").split(";")] if isinstance(item.get("alternative_addresses"), str) else (item.get("alternative_addresses") or [])
                    db_ent = WatchlistEntity(
                        snapshot_id=snap_id,
                        entity_id=item.get("entity_id") or item.get("id") or str(uuid.uuid4())[:8],
                        entity_type=etype,
                        primary_name=report["cleansed_name"],
                        individual_name_parsed=parsed_csv,
                        aliases=aliases,
                        dates_of_birth=dob_arr,
                        is_deceased=False,
                        gender=report["resolved_gender"],
                        countries=countries,
                        # New fields
                        place_of_birth=item.get("place_of_birth"),
                        address=item.get("address") or item.get("adress"),
                        city=item.get("city"),
                        state=item.get("state"),
                        country=item.get("country"),
                        origin=item.get("origin"),
                        designation=item.get("designation"),
                        designation_reasons=item.get("designation_reasons"),
                        additional_informations=item.get("additional_informations") or item.get("additional_info"),
                        official_reference=item.get("official_reference"),
                        alternative_addresses=alt_addrs_csv,
                        lei_number=item.get("lei_number"),
                        entity_checksum=ent_checksum,
                        **_extended_entity_kwargs(item)
                    )
                    db.add(db_ent)
                    record_count += 1
                    if record_count % _INGEST_COMMIT_EVERY == 0:
                        snap = _ingest_progress_tick(db, snap, record_count, progress_id)

        elif file_type == "CLIENT_BASE":
            # Client base CSV
            for item in parse_csv_file(str(temp_file_path), delimiter=delimiter):
                report = evaluate_and_clean(item)
                if not report["is_valid"]:
                    continue
                    
                ent_checksum = compute_checksum(item)
                
                # Split countries
                countries_obj = {
                    "nationality": [c.strip() for c in (item.get("nationality") or "").split(",") if c],
                    "residence": [c.strip() for c in (item.get("residence") or "").split(",") if c],
                    "birth_country": [c.strip() for c in (item.get("birth_country") or "").split(",") if c],
                    "registration_country": [c.strip() for c in (item.get("registration_country") or "").split(",") if c]
                }
                
                alt_addrs_client = [a.strip() for a in item.get("alternative_addresses", "").split(";")] if isinstance(item.get("alternative_addresses"), str) else (item.get("alternative_addresses") or [])
                db_ent = ClientEntity(
                    snapshot_id=snap_id,
                    client_id=item.get("client_id"),
                    client_type=item.get("client_type"),
                    client_first_name=item.get("client_first_name"),
                    client_last_name=item.get("client_last_name"),
                    client_maiden_name=item.get("client_maiden_name"),
                    client_company_name=item.get("client_company_name"),
                    client_dob=item.get("client_dob"),
                    client_gender=report["resolved_gender"],
                    client_is_deceased=str(item.get("client_is_deceased", "False")).lower() == "true",
                    client_countries=countries_obj,
                    # New fields
                    client_place_of_birth=item.get("client_place_of_birth") or item.get("place_of_birth"),
                    client_address=item.get("client_address") or item.get("address") or item.get("adress"),
                    client_city=item.get("client_city") or item.get("city"),
                    client_state=item.get("client_state") or item.get("state"),
                    client_country=item.get("client_country") or item.get("country"),
                    client_origin=item.get("client_origin") or item.get("origin"),
                    client_designation=item.get("client_designation") or item.get("designation"),
                    client_additional_informations=item.get("client_additional_informations") or item.get("additional_informations") or item.get("additional_info"),
                    client_alternative_addresses=alt_addrs_client,
                    client_date_of_death=item.get("client_date_of_death") or item.get("date_of_death"),
                    client_lei_number=item.get("client_lei_number"),
                    client_national_registry_ids=json.loads(item.get("client_national_registry_ids", "[]")) if item.get("client_national_registry_ids") else [],
                    client_other_registration_ids=json.loads(item.get("client_other_registration_ids", "[]")) if item.get("client_other_registration_ids") else [],
                    client_passport_documents=json.loads(item.get("client_passport_documents", "[]")) if item.get("client_passport_documents") else [],
                    client_national_id_documents=json.loads(item.get("client_national_id_documents", "[]")) if item.get("client_national_id_documents") else [],
                    client_other_id_documents=json.loads(item.get("client_other_id_documents", "[]")) if item.get("client_other_id_documents") else [],
                    # Champs etendus KYC
                    client_iban=item.get("client_iban") or item.get("iban") or None,
                    client_bic=item.get("client_bic") or item.get("bic") or None,
                    client_tax_id=item.get("client_tax_id") or item.get("tax_id") or None,
                    client_phone=item.get("client_phone") or item.get("phone") or None,
                    client_email=item.get("client_email") or item.get("email") or None,
                    client_website=item.get("client_website") or item.get("website") or None,
                    client_crypto_wallets=[w.strip() for w in (item.get("client_crypto_wallets") or "").split(";") if w.strip()],
                    client_risk_rating=(item.get("client_risk_rating") or "").strip().upper() or None,
                    client_pep_flag=str(item.get("client_pep_flag", "")).strip().lower() in ("true", "1", "oui", "yes"),
                    client_segment=item.get("client_segment") or None,
                    client_activity_sector=item.get("client_activity_sector") or None,
                    client_activity_countries=[c.strip().upper() for c in (item.get("client_activity_countries") or "").split(",") if c.strip()],
                    client_relationship_start=item.get("client_relationship_start") or None,
                    client_status=(item.get("client_status") or "").strip().upper() or None,
                    entity_checksum=ent_checksum
                )
                db.add(db_ent)
                record_count += 1
                if record_count % _INGEST_COMMIT_EVERY == 0:
                    snap = _ingest_progress_tick(db, snap, record_count, progress_id)

        # Update Snapshot status. En mode homologation, les watchlists attendent
        # un pointage humain (PENDING_REVIEW) et restent hors du cache de criblage.
        staging = file_type in WATCHLIST_FILE_TYPES and require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count
        snap.processed_count = record_count
        snap.phase = "RELOAD" if (file_type in WATCHLIST_FILE_TYPES and not staging) else "DONE"
        db.commit()

        # Reload cache to integrate newly loaded watchlists
        rescreen_result = None
        if file_type in WATCHLIST_FILE_TYPES and not staging:
            progress_registry.update(progress_id, phase="RELOAD", processed=record_count,
                                     snapshot_id=snap_id)
            load_watchlist_cache(db)
            snap.phase = "DONE"
            db.commit()
            # Surveillance continue : re-criblage du referentiel clients
            # contre les entites du nouveau snapshot
            if auto_rescreen_enabled(db):
                rescreen_result = rescreen_after_snapshot_change(db, file_type, snap_id, None)

        if staging:
            message = (
                f"{record_count} fiches importées, snapshot en attente d'homologation "
                "(pointage humain requis avant mise en production)."
            )
            if notification_events(db).get("snapshot_pending_review"):
                notify_event("snapshot_pending_review", {
                    "snapshot_id": snap_id, "liste": file_type, "fichier": file.filename,
                    "fiches": record_count,
                })
        else:
            message = f"Successfully imported {record_count} items."
        progress_registry.finish(progress_id)
        return {
            "message": message,
            "snapshot_id": snap_id,
            "record_count": record_count,
            "status": snap.status,
            "rescreen": rescreen_result
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to ingest file: {e}")
        progress_registry.finish(progress_id, status="ERROR", error=str(e))
        # Mark snapshot as ERROR
        if 'snap_id' in locals():
            error_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
            if error_snap:
                error_snap.status = "ERROR"
                db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed: {str(e)}"
        )
    finally:
        # Delete temp file
        if temp_file_path.exists():
            os.remove(temp_file_path)

@app.get("/api/snapshots")
async def get_snapshots(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Lists loaded snapshots."""
    return db.query(Snapshot).order_by(Snapshot.uploaded_at.desc()).all()

@app.post("/api/snapshots/compare")
async def compare_snapshots(
    request: DeltaRequest, 
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Compares two database snapshots of the same file_type.
    Returns ADDED, REMOVED, and MODIFIED records delta report.
    """
    snap_old = db.query(Snapshot).filter(Snapshot.snapshot_id == request.snapshot_old_id).first()
    snap_new = db.query(Snapshot).filter(Snapshot.snapshot_id == request.snapshot_new_id).first()
    
    if not snap_old or not snap_new:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One of the snapshot IDs was not found."
        )
        
    if snap_old.file_type != snap_new.file_type:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot compare snapshots of different file types."
        )
        
    # Query all entities for both snapshots
    if snap_old.file_type in WATCHLIST_FILE_TYPES:
        old_ents = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == request.snapshot_old_id).all()
        new_ents = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == request.snapshot_new_id).all()
        key_column = "entity_id"
    else:
        old_ents = db.query(ClientEntity).filter(ClientEntity.snapshot_id == request.snapshot_old_id).all()
        new_ents = db.query(ClientEntity).filter(ClientEntity.snapshot_id == request.snapshot_new_id).all()
        key_column = "client_id"
        
    # Serialize to dictionary for Delta Engine
    old_list = [{c.name: getattr(ent, c.name) for c in ent.__table__.columns} for ent in old_ents]
    new_list = [{c.name: getattr(ent, c.name) for c in ent.__table__.columns} for ent in new_ents]
    
    # Calculate delta
    report = calculate_delta(old_list, new_list, key_column)
    
    # Add metadata to report matching Section 8.4 spec
    report["comparison_metadata"] = {
        "file_type": snap_old.file_type,
        "old_snapshot_id": snap_old.snapshot_id,
        "new_snapshot_id": snap_new.snapshot_id,
        "execution_timestamp": datetime.utcnow().isoformat() + "Z"
    }
    return report

class WatchlistEntityCreate(BaseModel):
    entity_type: str
    primary_name: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    maiden_name: Optional[str] = None
    aliases: Optional[str] = None
    dates_of_birth: Optional[str] = None
    nationality: Optional[str] = None
    residence: Optional[str] = None
    lei_number: Optional[str] = None
    imo_number: Optional[str] = None
    gender: Optional[str] = "U"
    aircraft_tail_number: Optional[str] = None
    passport_documents: Optional[str] = None
    national_id_documents: Optional[str] = None
    
    # New fields requested
    place_of_birth: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    origin: Optional[str] = None
    designation: Optional[str] = None
    designation_reasons: Optional[str] = None
    additional_informations: Optional[str] = None
    official_reference: Optional[str] = None
    alternative_addresses: Optional[str] = None
    date_of_death: Optional[str] = None
    # Champs etendus (scalaires principaux, reglables a l'ajout manuel par API)
    bic_swift: Optional[str] = None
    tax_id: Optional[str] = None
    duns_number: Optional[str] = None
    title: Optional[str] = None
    listed_on: Optional[str] = None
    name_original_script: Optional[str] = None

@app.post("/api/watchlist/entity")
async def create_watchlist_entity(
    payload: WatchlistEntityCreate, 
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Manually adds a new entity to the active watchlist and updates the engine cache."""
    # 1. Ensure the manual snapshot exists
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == "manual-watchlist").first()
    if not snap:
        snap = Snapshot(
            snapshot_id="manual-watchlist",
            file_type="WATCHLIST_EU",
            file_name="Manuel / Entités à la volée",
            file_hash="manual-watchlist-hash",
            record_count=0,
            uploaded_at=datetime.utcnow(),
            status="READY"
        )
        db.add(snap)
        db.commit()
        db.refresh(snap)
        
    # 2. Parse fields
    aliases_list = [a.strip() for a in payload.aliases.split(",") if a.strip()] if payload.aliases else []
    dob_list = [d.strip() for d in payload.dates_of_birth.split(",") if d.strip()] if payload.dates_of_birth else []
    nationality_list = [c.strip().upper() for c in payload.nationality.split(",") if c.strip()] if payload.nationality else []
    residence_list = [c.strip().upper() for c in payload.residence.split(",") if c.strip()] if payload.residence else []
    alt_addrs = [a.strip() for a in payload.alternative_addresses.split(";") if a.strip()] if payload.alternative_addresses else []
    
    passport_list = [{"number": num.strip(), "issuing_country": "XX"} for num in payload.passport_documents.split(",") if num.strip()] if payload.passport_documents else []
    national_id_list = [{"number": num.strip(), "issuing_country": "XX"} for num in payload.national_id_documents.split(",") if num.strip()] if payload.national_id_documents else []
    
    from fiskr.ingest import categorize_aliases
    raw_aliases = [{"name": name, "type": "Strong"} for name in aliases_list]
    parsed_aliases = categorize_aliases(raw_aliases)
    
    ent_dict = {
        "entity_id": f"MANUAL-{str(uuid.uuid4())[:8].upper()}",
        "entity_type": payload.entity_type,
        "primary_name": payload.primary_name,
        "individual_name_parsed": {
            "first_name": payload.first_name or "",
            "last_name": payload.last_name or "",
            "maiden_name": payload.maiden_name or ""
        },
        "aliases": parsed_aliases,
        "dates_of_birth": dob_list,
        "date_of_death": payload.date_of_death or None,
        "is_deceased": bool(payload.date_of_death),
        "gender": payload.gender or "U",
        "countries": {
            "citizenship": nationality_list,
            "residence": residence_list
        },
        "lei_number": payload.lei_number or None,
        "imo_number": payload.imo_number or None,
        "aircraft_tail_number": payload.aircraft_tail_number or None,
        "passport_documents": passport_list,
        "national_id_documents": national_id_list,
        # New fields
        "place_of_birth": payload.place_of_birth or None,
        "address": payload.address or None,
        "city": payload.city or None,
        "state": payload.state or None,
        "country": payload.country or None,
        "origin": payload.origin or None,
        "designation": payload.designation or None,
        "designation_reasons": payload.designation_reasons or None,
        "additional_informations": payload.additional_informations or None,
        "official_reference": payload.official_reference or None,
        "alternative_addresses": alt_addrs,
        # Champs etendus (scalaires principaux)
        "bic_swift": payload.bic_swift or None,
        "tax_id": payload.tax_id or None,
        "duns_number": payload.duns_number or None,
        "title": payload.title or None,
        "listed_on": payload.listed_on or None,
        "name_original_script": payload.name_original_script or None,
    }

    # Moteur de detection des noms : decoupe le nom principal si prenom/nom absents
    ent_dict = ensure_parsed_name(ent_dict)

    # 3. Quality Gate check
    report = evaluate_and_clean(ent_dict)
    if not report["is_valid"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Quality Gate rejected the entity.", "errors": report["errors"]}
        )
        
    # 4. Save to Database
    ent_checksum = compute_checksum(ent_dict)
    db_ent = WatchlistEntity(
        snapshot_id=snap.snapshot_id,
        entity_id=ent_dict["entity_id"],
        entity_type=payload.entity_type,
        primary_name=report["cleansed_name"],
        individual_name_parsed=ent_dict["individual_name_parsed"],
        aliases=report["cleansed_aliases"],
        dates_of_birth=ent_dict["dates_of_birth"],
        date_of_death=ent_dict["date_of_death"],
        is_deceased=ent_dict["is_deceased"],
        gender=report["resolved_gender"],
        countries=ent_dict["countries"],
        lei_number=payload.lei_number or None,
        imo_number=payload.imo_number or None,
        aircraft_tail_number=ent_dict["aircraft_tail_number"],
        passport_documents=ent_dict["passport_documents"],
        national_id_documents=ent_dict["national_id_documents"],
        # New fields
        place_of_birth=ent_dict["place_of_birth"],
        address=ent_dict["address"],
        city=ent_dict["city"],
        state=ent_dict["state"],
        country=ent_dict["country"],
        origin=ent_dict["origin"],
        designation=ent_dict["designation"],
        designation_reasons=ent_dict["designation_reasons"],
        additional_informations=ent_dict["additional_informations"],
        official_reference=ent_dict["official_reference"],
        alternative_addresses=ent_dict["alternative_addresses"],
        bic_swift=ent_dict["bic_swift"],
        tax_id=ent_dict["tax_id"],
        duns_number=ent_dict["duns_number"],
        title=ent_dict["title"],
        listed_on=ent_dict["listed_on"],
        name_original_script=ent_dict["name_original_script"],
        entity_checksum=ent_checksum
    )
    db.add(db_ent)
    
    # Update Snapshot record count
    snap.record_count += 1
    db.commit()
    
    # 5. Reload Cache
    load_watchlist_cache(db)
    
    return {
        "message": "Entité ajoutée avec succès.",
        "entity_id": ent_dict["entity_id"],
        "primary_name": report["cleansed_name"]
    }

# ------------------ PATCH DE VALEURS D'UNE FICHE LISTEE ------------------

def _serialize_watchlist_entity(entity: WatchlistEntity, snap: Snapshot) -> Dict[str, Any]:
    """Memes cles que le cache moteur (+ metadonnees snapshot) : la modale de
    details du dashboard fonctionne a l'identique sur les deux sources."""
    d = {c.name: getattr(entity, c.name) for c in entity.__table__.columns}
    d["_list_type"] = snap.file_type
    d["snapshot_status"] = snap.status
    d["snapshot_uploaded_at"] = snap.uploaded_at.isoformat() if snap.uploaded_at else None
    d["snapshot_file_name"] = snap.file_name
    return d


# Dates reconnues dans la reference officielle : ISO (YYYY-MM-DD) ou JJ/MM/AAAA
OFFICIAL_REF_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})")


def _touch_official_reference_date(reference: Optional[str]):
    """
    Remplace la date de mise a jour contenue dans la reference officielle par
    la date du jour, en conservant son format d'origine. La date de mise a
    jour est la DERNIERE date du texte (les references de reglement peuvent
    contenir d'autres dates en amont). Retourne (nouvelle_valeur, date_trouvee).
    """
    if not reference:
        return reference, False
    matches = list(OFFICIAL_REF_DATE_RE.finditer(reference))
    if not matches:
        return reference, False
    match = matches[-1]
    today = datetime.utcnow().date()
    new_date = today.strftime("%d/%m/%Y") if "/" in match.group(1) else today.isoformat()
    return reference[:match.start(1)] + new_date + reference[match.end(1):], True


# Colonnes exclues du recalcul de checksum (traces de gouvernance, pas des
# donnees de la fiche) — s'ajoutent aux cles deja filtrees par compute_checksum
_CHECKSUM_EXCLUDED_COLS = {
    "modified_by", "modified_at", "excluded", "exclusion_justification",
    "exclusion_file_name", "exclusion_file_path", "excluded_by", "excluded_at",
}


class WatchlistEntityPatch(BaseModel):
    """Patch partiel d'une fiche listee : seuls les champs fournis sont modifies
    (un champ fourni a null est efface)."""
    primary_name: Optional[str] = None
    entity_type: Optional[str] = None
    gender: Optional[str] = None
    place_of_birth: Optional[str] = None
    address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    date_of_death: Optional[str] = None
    is_deceased: Optional[bool] = None
    origin: Optional[str] = None
    designation: Optional[str] = None
    designation_reasons: Optional[str] = None
    additional_informations: Optional[str] = None
    official_reference: Optional[str] = None
    lei_number: Optional[str] = None
    imo_number: Optional[str] = None
    aircraft_tail_number: Optional[str] = None
    individual_name_parsed: Optional[Dict[str, Any]] = None
    dates_of_birth: Optional[List[str]] = None
    countries: Optional[Dict[str, Any]] = None
    aliases: Optional[Dict[str, Any]] = None
    alternative_addresses: Optional[List[str]] = None
    # Champs etendus scalaires
    bic_swift: Optional[str] = None
    tax_id: Optional[str] = None
    duns_number: Optional[str] = None
    title: Optional[str] = None
    name_original_script: Optional[str] = None
    listed_on: Optional[str] = None
    delisted_on: Optional[str] = None
    pep_role: Optional[str] = None
    secondary_sanctions_risk: Optional[str] = None
    designating_state: Optional[str] = None
    vessel_call_sign: Optional[str] = None
    vessel_mmsi: Optional[str] = None
    vessel_flag: Optional[str] = None
    vessel_type: Optional[str] = None
    vessel_tonnage: Optional[str] = None
    vessel_owner: Optional[str] = None
    aircraft_model: Optional[str] = None
    aircraft_operator: Optional[str] = None
    aircraft_construction_number: Optional[str] = None
    organization_established_date: Optional[str] = None
    organization_type: Optional[str] = None
    # Champs etendus JSON
    crypto_wallets: Optional[List[Dict[str, Any]]] = None
    sanction_programs: Optional[List[str]] = None
    phone_numbers: Optional[List[str]] = None
    email_addresses: Optional[List[str]] = None
    websites: Optional[List[str]] = None
    # Si vrai, la date contenue dans la reference officielle (s'il y en a une)
    # est remplacee par la date du jour, dans son format d'origine
    touch_official_reference_date: bool = False


@app.patch("/api/watchlist/entity/{entity_pk}")
async def patch_watchlist_entity(
    entity_pk: int,
    payload: WatchlistEntityPatch,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Modifie les valeurs d'une fiche listee en production (snapshot READY, non
    exclue). Chaque champ modifie est journalise dans watchlist_entity_changes
    (qui, quand, ancienne -> nouvelle valeur), le checksum de version est
    recalcule et le cache de criblage est recharge immediatement.
    """
    row = db.query(WatchlistEntity).filter(WatchlistEntity.id == entity_pk).first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Fiche introuvable.")
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == row.snapshot_id).first()
    if not snap or snap.file_type not in WATCHLIST_FILE_TYPES or snap.status != "READY" or row.excluded is True:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Seules les fiches en production (snapshot homologué, non exclues) sont modifiables."
        )

    fields = payload.model_dump(exclude_unset=True)
    touch_date = fields.pop("touch_official_reference_date", False)

    if "primary_name" in fields and not (fields["primary_name"] or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le nom principal ne peut pas être vide.")
    if "entity_type" in fields and fields["entity_type"] not in ("I", "E", "V", "O"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="entity_type doit être I, E, V ou O.")

    def _journal_value(value):
        if value is None or isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    changed_fields = []

    def _apply_change(field, new_value):
        old_value = getattr(row, field)
        if old_value == new_value:
            return
        db.add(WatchlistEntityChange(
            entity_pk=row.id,
            entity_id=row.entity_id,
            snapshot_id=row.snapshot_id,
            field=field,
            old_value=_journal_value(old_value),
            new_value=_journal_value(new_value),
            changed_by=reviewer["username"],
        ))
        setattr(row, field, new_value)
        changed_fields.append(field)

    for field, new_value in fields.items():
        if isinstance(new_value, str):
            new_value = new_value.strip() or None
        _apply_change(field, new_value)

    # Reference officielle : ramener sa date de mise a jour a la date du jour
    date_touched = False
    if touch_date:
        new_ref, date_touched = _touch_official_reference_date(row.official_reference)
        if date_touched:
            _apply_change("official_reference", new_ref)

    if not changed_fields:
        return {
            "message": "Aucune modification (valeurs identiques).",
            "changed_fields": [],
            "official_reference_date_touched": date_touched,
            "entity": _serialize_watchlist_entity(row, snap),
        }

    # Recalcul du checksum de version (donnees de la fiche uniquement)
    ent_dict = {
        c.name: getattr(row, c.name)
        for c in row.__table__.columns if c.name not in _CHECKSUM_EXCLUDED_COLS
    }
    row.entity_checksum = compute_checksum(ent_dict)
    row.modified_by = reviewer["username"]
    row.modified_at = datetime.utcnow()
    db.commit()

    # Les nouvelles valeurs criblent immediatement
    load_watchlist_cache(db)
    db.refresh(row)

    return {
        "message": f"{len(changed_fields)} champ(s) modifié(s).",
        "changed_fields": changed_fields,
        "official_reference_date_touched": date_touched,
        "entity": _serialize_watchlist_entity(row, snap),
    }


@app.get("/api/watchlist/entity/{entity_pk}/changes")
async def get_watchlist_entity_changes(
    entity_pk: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Journal des modifications manuelles d'une fiche listee (antichronologique)."""
    rows = db.query(WatchlistEntityChange).filter(
        WatchlistEntityChange.entity_pk == entity_pk
    ).order_by(WatchlistEntityChange.changed_at.desc(), WatchlistEntityChange.id.desc()).all()
    return {
        "items": [
            {
                "field": r.field,
                "old_value": r.old_value,
                "new_value": r.new_value,
                "changed_by": r.changed_by,
                "changed_at": r.changed_at.isoformat() if r.changed_at else None,
            }
            for r in rows
        ]
    }

# ------------------ RELATIONS ENTRE ENTITES (OWNERSHIP, REGLE DES 50 %) ------------------

RELATION_TYPE_LABELS = {
    "OWNED_BY": "Détenu / contrôlé par",
    "ACTING_FOR": "Agit pour le compte de",
    "ASSOCIATE_OF": "Associé de",
    "FAMILY_OF": "Membre de la famille de",
    "LEADER_OF": "Dirigeant de / rôle de direction dans",
    "PROVIDING_SUPPORT": "Apporte un soutien à",
    "OTHER": "Autre relation",
}

def _entity_names_map(db: Session, entity_ids) -> Dict[str, str]:
    """Resout entity_id -> nom principal (fiches en production d'abord)."""
    ids = [i for i in set(entity_ids) if i]
    if not ids:
        return {}
    rows = (
        db.query(WatchlistEntity.entity_id, WatchlistEntity.primary_name, Snapshot.status)
          .join(Snapshot, WatchlistEntity.snapshot_id == Snapshot.snapshot_id)
          .filter(WatchlistEntity.entity_id.in_(ids)).all()
    )
    names: Dict[str, str] = {}
    for entity_id, name, snap_status in rows:
        if entity_id not in names or snap_status == "READY":
            names[entity_id] = name
    return names

def _relation_view(rel: EntityRelationship, names: Dict[str, str]) -> Dict[str, Any]:
    return {
        "id": rel.id,
        "from_entity_id": rel.from_entity_id,
        "from_name": names.get(rel.from_entity_id),
        "to_entity_id": rel.to_entity_id,
        "to_name": names.get(rel.to_entity_id),
        "relation_type": rel.relation_type,
        "relation_type_label": RELATION_TYPE_LABELS.get(rel.relation_type, rel.relation_type),
        "relation_label": rel.relation_label,
        "ownership_pct": rel.ownership_pct,
        "source": rel.source,
        "comment": rel.comment,
        "created_by": rel.created_by,
        "created_at": rel.created_at.isoformat() if rel.created_at else None,
    }

def compute_inherited_risk(db: Session, entity_id: str, max_depth: int = 3) -> List[Dict[str, Any]]:
    """
    Regle des 50 % (OFAC) : remonte les liens OWNED_BY dont la detention est
    majoritaire (>= 50 %) ou presumee (relation OFAC sans pourcentage — figurer
    au SDN comme « Owned or Controlled By » vaut presomption de controle).
    Transitive avec garde de profondeur et de cycles.
    """
    chains: List[Dict[str, Any]] = []
    visited = {entity_id}

    def walk(current_id: str, path: List[str], depth: int):
        if depth >= max_depth:
            return
        edges = db.query(EntityRelationship).filter(
            EntityRelationship.from_entity_id == current_id,
            EntityRelationship.relation_type == "OWNED_BY",
        ).all()
        for edge in edges:
            owner = edge.to_entity_id
            majority = (edge.ownership_pct is not None and edge.ownership_pct >= 50.0)
            presumed = (edge.ownership_pct is None and edge.source == "OFAC")
            if not (majority or presumed) or owner in visited:
                continue
            visited.add(owner)
            chains.append({
                "owner_entity_id": owner,
                "ownership_pct": edge.ownership_pct,
                "presumed": presumed,
                "via": list(path),
            })
            walk(owner, path + [owner], depth + 1)

    walk(entity_id, [], 0)
    names = _entity_names_map(db, [c["owner_entity_id"] for c in chains])
    for chain in chains:
        chain["owner_name"] = names.get(chain["owner_entity_id"])
    return chains

class RelationshipCreate(BaseModel):
    from_entity_id: str
    to_entity_id: str
    relation_type: str
    ownership_pct: Optional[float] = None
    comment: Optional[str] = None

@app.get("/api/relationships/{entity_id}")
async def get_entity_relationships(
    entity_id: str,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Relations d'une entite listee (deux sens, noms resolus) + risque
    herite par detention majoritaire (regle des 50 %)."""
    from sqlalchemy import or_
    rels = db.query(EntityRelationship).filter(or_(
        EntityRelationship.from_entity_id == entity_id,
        EntityRelationship.to_entity_id == entity_id,
    )).order_by(EntityRelationship.relation_type.asc(), EntityRelationship.id.asc()).all()
    names = _entity_names_map(
        db, [r.from_entity_id for r in rels] + [r.to_entity_id for r in rels] + [entity_id]
    )
    return {
        "entity_id": entity_id,
        "entity_name": names.get(entity_id),
        "relations": [_relation_view(r, names) for r in rels],
        "relation_types": [
            {"code": code, "label": RELATION_TYPE_LABELS[code]} for code in RELATION_TYPES
        ],
        "inherited_risk": compute_inherited_risk(db, entity_id),
    }

GRAPH_MAX_NODES = 60

@app.get("/api/relationships/graph/{entity_id}")
async def get_relationship_graph(
    entity_id: str,
    depth: int = Query(2, ge=1, le=3),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Sous-graphe de relations autour d'une entite (BFS dans les deux sens,
    profondeur 1-3, borne a 60 nœuds) pour la visualisation reseau.
    Chaque arete porte son type, son % de detention et un drapeau
    « majority » (detention >= 50 % ou presomption OFAC : regle des 50 %).
    """
    from sqlalchemy import or_
    node_depths: Dict[str, int] = {entity_id: 0}
    edges: Dict[int, EntityRelationship] = {}
    frontier = [entity_id]
    truncated = False
    for level in range(1, depth + 1):
        if not frontier:
            break
        rels = db.query(EntityRelationship).filter(or_(
            EntityRelationship.from_entity_id.in_(frontier),
            EntityRelationship.to_entity_id.in_(frontier),
        )).all()
        next_frontier: List[str] = []
        for rel in rels:
            edges[rel.id] = rel
            for node in (rel.from_entity_id, rel.to_entity_id):
                if node not in node_depths:
                    if len(node_depths) >= GRAPH_MAX_NODES:
                        truncated = True
                        continue
                    node_depths[node] = level
                    next_frontier.append(node)
        frontier = next_frontier

    names = _entity_names_map(db, list(node_depths.keys()))
    # Types d'entites pour l'affichage (I/E/V/A)
    type_rows = db.query(WatchlistEntity.entity_id, WatchlistEntity.entity_type) \
                  .filter(WatchlistEntity.entity_id.in_(list(node_depths.keys()))).all()
    entity_types = {eid: etype for eid, etype in type_rows}

    def _edge_view(rel: EntityRelationship) -> Dict[str, Any]:
        majority = rel.relation_type == "OWNED_BY" and (
            (rel.ownership_pct is not None and rel.ownership_pct >= 50.0)
            or (rel.ownership_pct is None and rel.source == "OFAC")
        )
        return {
            "id": rel.id,
            "from": rel.from_entity_id, "to": rel.to_entity_id,
            "relation_type": rel.relation_type,
            "label": RELATION_TYPE_LABELS.get(rel.relation_type, rel.relation_type),
            "ownership_pct": rel.ownership_pct,
            "source": rel.source,
            "majority": majority,
        }

    return {
        "center": entity_id,
        "depth": depth,
        "truncated": truncated,
        "nodes": [
            {
                "id": node, "name": names.get(node, node),
                "entity_type": entity_types.get(node), "depth": node_depth,
            }
            for node, node_depth in sorted(node_depths.items(), key=lambda kv: (kv[1], kv[0]))
        ],
        "edges": [
            _edge_view(rel) for rel in edges.values()
            if rel.from_entity_id in node_depths and rel.to_entity_id in node_depths
        ],
    }

@app.post("/api/relationships")
async def create_relationship(
    payload: RelationshipCreate,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """Ajout manuel d'une relation entre entites (reviewer/admin) — support de
    la regle des 50 % via ownership_pct sur les liens OWNED_BY."""
    from_id = (payload.from_entity_id or "").strip()
    to_id = (payload.to_entity_id or "").strip()
    rel_type = (payload.relation_type or "").strip().upper()
    if not from_id or not to_id or from_id == to_id:
        raise HTTPException(status_code=400, detail="Deux identifiants d'entités distincts sont requis.")
    if rel_type not in RELATION_TYPES:
        raise HTTPException(status_code=400, detail=f"Type de relation inconnu ({', '.join(RELATION_TYPES)}).")
    if payload.ownership_pct is not None and not (0 < payload.ownership_pct <= 100):
        raise HTTPException(status_code=400, detail="ownership_pct doit être entre 0 et 100.")
    # Les deux entites doivent exister en base (n'importe quel snapshot)
    known = _entity_names_map(db, [from_id, to_id])
    missing = [i for i in (from_id, to_id) if i not in known]
    if missing:
        raise HTTPException(status_code=404, detail=f"Entité(s) introuvable(s) en base : {', '.join(missing)}.")
    duplicate = db.query(EntityRelationship).filter(
        EntityRelationship.from_entity_id == from_id,
        EntityRelationship.to_entity_id == to_id,
        EntityRelationship.relation_type == rel_type,
    ).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="Cette relation existe déjà.")
    rel = EntityRelationship(
        from_entity_id=from_id, to_entity_id=to_id, relation_type=rel_type,
        ownership_pct=payload.ownership_pct, source="MANUAL",
        comment=(payload.comment or "").strip() or None, created_by=reviewer["username"],
    )
    db.add(rel)
    log_admin_action(db, reviewer["username"], "RELATION_CREATED",
                     target=f"{from_id} --{rel_type}--> {to_id}",
                     after={"ownership_pct": payload.ownership_pct, "comment": payload.comment})
    db.commit()
    names = _entity_names_map(db, [from_id, to_id])
    return {"message": "Relation créée.", "relation": _relation_view(rel, names)}

@app.delete("/api/relationships/{rel_id}")
async def delete_relationship(
    rel_id: int,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """Suppression d'une relation MANUELLE uniquement (les relations OFAC sont
    rafraichies par les synchronisations et repousseraient)."""
    rel = db.query(EntityRelationship).filter(EntityRelationship.id == rel_id).first()
    if not rel:
        raise HTTPException(status_code=404, detail="Relation introuvable.")
    if rel.source != "MANUAL":
        raise HTTPException(status_code=409, detail="Seules les relations manuelles peuvent être supprimées (les relations de source officielle sont gérées par les synchronisations).")
    log_admin_action(db, reviewer["username"], "RELATION_DELETED",
                     target=f"{rel.from_entity_id} --{rel.relation_type}--> {rel.to_entity_id}",
                     before={"ownership_pct": rel.ownership_pct, "comment": rel.comment})
    db.delete(rel)
    db.commit()
    return {"message": "Relation supprimée."}

@app.get("/api/watchlist")
async def get_watchlist(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Returns the active loaded in-memory watchlist."""
    return {
        "version": watchlist_version,
        "hash": watchlist_hash,
        "items": watchlist_store
    }

# ------------------ CAMPAGNES DE CRIBLAGE BATCH (upload manuel / inbox CFT) ------------------
# Un fichier de clients ad hoc est crible cote serveur en tache de fond avec
# les MEMES garanties que le criblage unitaire (quality gate, liste blanche,
# regles anti-faux positifs, journal d'audit immuable, alertes). L'inbox
# surveillee est le point d'integration CFT : le moniteur de transfert depose
# un fichier, Fiskr en fait une campagne.

BATCH_MAX_ROWS = 20000

def _batch_row_to_profile(row: Dict[str, str]) -> Dict[str, Any]:
    """Ligne CSV -> profil de criblage (memes colonnes que CLIENT_BASE)."""
    def val(*keys):
        for key in keys:
            v = row.get(key)
            if v is not None and str(v).strip():
                return str(v).strip()
        return None

    def csv_list(key, sep=","):
        return [c.strip() for c in (row.get(key) or "").split(sep) if c.strip()]

    ctype = (val("client_type", "type") or "PP").upper()
    return {
        "client_id": val("client_id", "id"),
        "client_type": "PP" if ctype in ("PP", "I") else "PM",
        "client_first_name": val("client_first_name", "first_name") or "",
        "client_last_name": val("client_last_name", "last_name") or "",
        "client_maiden_name": val("client_maiden_name", "maiden_name") or "",
        "client_company_name": val("client_company_name", "company_name", "name") or "",
        "client_dob": val("client_dob", "dob", "birth_date"),
        "client_gender": val("client_gender", "gender") or "U",
        "client_is_deceased": False,
        "client_countries": {
            "nationality": csv_list("nationality"),
            "residence": csv_list("residence"),
            "birth_country": csv_list("birth_country"),
            "registration_country": csv_list("registration_country"),
        },
        "client_place_of_birth": val("client_place_of_birth", "place_of_birth"),
        "client_lei_number": val("client_lei_number", "lei_number"),
        "client_bic": val("client_bic", "bic"),
        "client_tax_id": val("client_tax_id", "tax_id"),
        "client_iban": val("client_iban", "iban"),
        "client_crypto_wallets": csv_list("client_crypto_wallets", ";"),
        "client_alternative_addresses": [],
        "client_national_registry_ids": [],
        "client_other_registration_ids": [],
        "client_passport_documents": json.loads(row["client_passport_documents"]) if row.get("client_passport_documents") else [],
        "client_national_id_documents": json.loads(row["client_national_id_documents"]) if row.get("client_national_id_documents") else [],
        "client_other_id_documents": [],
    }

def _parse_batch_csv(path: Path) -> List[Dict[str, Any]]:
    """Parse un fichier clients CSV (separateur , ou ; auto-detecte)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        head = f.readline()
        delimiter = ";" if head.count(";") > head.count(",") else ","
        f.seek(0)
        reader = __import__("csv").DictReader(f, delimiter=delimiter)
        profiles = []
        for row in reader:
            row = {(k or "").strip().lower(): v for k, v in row.items()}
            profiles.append(_batch_row_to_profile(row))
            if len(profiles) > BATCH_MAX_ROWS:
                raise HTTPException(
                    status_code=400,
                    detail=f"Fichier trop volumineux (> {BATCH_MAX_ROWS} lignes) : découpez la campagne."
                )
    if not profiles:
        raise HTTPException(status_code=400, detail="Aucune ligne client exploitable dans le fichier.")
    return profiles

def _campaign_summary(c: BatchCampaign) -> Dict[str, Any]:
    return {
        "id": c.id, "name": c.name, "file_name": c.file_name, "trigger": c.trigger,
        "status": c.status, "error_message": c.error_message,
        "screening_lists": c.screening_lists or "ALL",
        "total_clients": c.total_clients, "processed_clients": c.processed_clients,
        "alert_count": c.alert_count, "no_match_count": c.no_match_count,
        "rejected_count": c.rejected_count,
        "created_by": c.created_by,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "finished_at": c.finished_at.isoformat() if c.finished_at else None,
    }

def _run_batch_campaign(campaign_id: int, profiles: List[Dict[str, Any]],
                        username: str, requested_lists: Optional[List[str]]) -> None:
    """Corps de la campagne (thread dedie, session DB propre)."""
    from fiskr.database import SessionLocal
    db = SessionLocal()
    try:
        campaign = db.query(BatchCampaign).filter(BatchCampaign.id == campaign_id).first()
        if campaign is None:
            return
        for profile in profiles:
            try:
                result = screen_client_profile(db, profile, username, requested_lists)
                best = result.get("best_match") or {}
                wl_entity = best.get("watchlist_entity") or {}
                result_status = best.get("status") or "NO_MATCH"
                db.add(BatchResult(
                    campaign_id=campaign_id,
                    client_id=profile.get("client_id"),
                    client_name=(result.get("client_quality_report") or {}).get("cleansed_name")
                                or profile.get("client_company_name") or profile.get("client_last_name"),
                    status=result_status,
                    final_score=best.get("final_score"),
                    watchlist_entity_id=wl_entity.get("entity_id"),
                    watchlist_name=wl_entity.get("primary_name"),
                    list_type=wl_entity.get("_list_type"),
                    audit_id=result.get("audit_trail_id"),
                    alert_id=result.get("alert_id"),
                ))
                if result_status == "ALERT":
                    campaign.alert_count += 1
                else:
                    campaign.no_match_count += 1
            except HTTPException as gate_error:
                # Quality gate : ligne refusee, motif conserve (jamais silencieux)
                db.add(BatchResult(
                    campaign_id=campaign_id,
                    client_id=profile.get("client_id"),
                    client_name=profile.get("client_company_name") or profile.get("client_last_name"),
                    status="REJECTED",
                    error=json.dumps(gate_error.detail, ensure_ascii=False, default=str),
                ))
                campaign.rejected_count += 1
            campaign.processed_clients += 1
            if campaign.processed_clients % 25 == 0:
                db.commit()
        campaign.status = "DONE"
        campaign.finished_at = datetime.utcnow()
        db.commit()
        logger.info(f"Campagne batch #{campaign_id} terminée : {campaign.alert_count} alerte(s) "
                    f"sur {campaign.processed_clients} client(s).")
    except Exception as e:
        db.rollback()
        campaign = db.query(BatchCampaign).filter(BatchCampaign.id == campaign_id).first()
        if campaign is not None:
            campaign.status = "ERROR"
            campaign.error_message = str(e)
            campaign.finished_at = datetime.utcnow()
            db.commit()
        logger.error(f"Campagne batch #{campaign_id} en erreur : {e}")
    finally:
        db.close()

def _launch_batch_campaign(db: Session, name: str, file_name: Optional[str],
                           profiles: List[Dict[str, Any]], username: str,
                           requested_lists: Optional[List[str]],
                           trigger: str = "manual") -> BatchCampaign:
    import threading
    campaign = BatchCampaign(
        name=name, file_name=file_name, trigger=trigger, status="RUNNING",
        screening_lists=requested_lists, total_clients=len(profiles),
        created_by=username,
    )
    db.add(campaign)
    db.commit()
    db.refresh(campaign)
    threading.Thread(
        target=_run_batch_campaign,
        args=(campaign.id, profiles, username, requested_lists),
        daemon=True,
    ).start()
    return campaign

@app.post("/api/batch/campaigns")
async def create_batch_campaign(
    file: UploadFile = File(...),
    name: Optional[str] = Form(None),
    screening_lists: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Lance une campagne de criblage batch depuis un fichier CSV de clients
    (colonnes CLIENT_BASE). Execution en tache de fond, progression consultable."""
    requested_lists = _validate_screening_lists(
        [v for v in (screening_lists or "").split(",") if v.strip()]
    )
    safe_upload_name = re.sub(r"[^\w.\-]", "_", file.filename or "clients.csv")
    temp_path = PROJECT_ROOT / "temp_ingestion" / f"batch_{uuid.uuid4().hex[:8]}_{safe_upload_name}"
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    with open(temp_path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    try:
        profiles = _parse_batch_csv(temp_path)
    finally:
        temp_path.unlink(missing_ok=True)
    campaign = _launch_batch_campaign(
        db, (name or "").strip() or f"Campagne {datetime.utcnow().strftime('%d/%m/%Y %H:%M')}",
        file.filename, profiles, current_user["username"], requested_lists,
    )
    return {"message": f"Campagne lancée sur {len(profiles)} client(s).", **_campaign_summary(campaign)}

@app.get("/api/batch/campaigns")
async def list_batch_campaigns(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Dernieres campagnes batch (progression et statut inclus)."""
    rows = db.query(BatchCampaign).order_by(BatchCampaign.created_at.desc()).limit(50).all()
    return {"items": [_campaign_summary(c) for c in rows]}

@app.get("/api/batch/campaigns/{campaign_id}")
async def get_batch_campaign(
    campaign_id: int,
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Detail d'une campagne : progression + resultats unitaires pagines."""
    campaign = db.query(BatchCampaign).filter(BatchCampaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne introuvable.")
    query = db.query(BatchResult).filter(BatchResult.campaign_id == campaign_id)
    if status_filter:
        statuses = [s.strip().upper() for s in status_filter.split(",") if s.strip()]
        query = query.filter(BatchResult.status.in_(statuses))
    total = query.count()
    rows = query.order_by(BatchResult.final_score.desc().nullslast(), BatchResult.id.asc()) \
                .offset((page - 1) * page_size).limit(page_size).all()
    return {
        **_campaign_summary(campaign),
        "results_total": total, "page": page, "page_size": page_size,
        "results": [
            {
                "id": r.id, "client_id": r.client_id, "client_name": r.client_name,
                "status": r.status, "final_score": r.final_score,
                "watchlist_entity_id": r.watchlist_entity_id, "watchlist_name": r.watchlist_name,
                "list_type": r.list_type, "audit_id": r.audit_id, "alert_id": r.alert_id,
                "error": r.error,
            }
            for r in rows
        ],
    }

@app.get("/api/export/batch/{campaign_id}.csv")
async def export_batch_campaign_csv(
    campaign_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Export CSV des resultats d'une campagne batch."""
    campaign = db.query(BatchCampaign).filter(BatchCampaign.id == campaign_id).first()
    if not campaign:
        raise HTTPException(status_code=404, detail="Campagne introuvable.")
    rows = db.query(BatchResult).filter(BatchResult.campaign_id == campaign_id) \
             .order_by(BatchResult.id.asc()).limit(_EXPORT_MAX_ROWS).all()
    header = ["client_id", "client", "statut", "score", "fiche_listee_id", "fiche_listee",
              "liste", "audit_id", "alerte_id", "motif_rejet"]
    data = [
        [r.client_id or "", r.client_name or "", r.status,
         f"{r.final_score:.1f}" if r.final_score is not None else "",
         r.watchlist_entity_id or "", r.watchlist_name or "", r.list_type or "",
         r.audit_id or "", r.alert_id or "", r.error or ""]
        for r in rows
    ]
    return _csv_response(f"fiskr_campagne_{campaign_id}.csv", header, data)

# ------------------ INBOX CFT SURVEILLEE (depot de fichiers clients) ------------------

def _process_inbox_once() -> int:
    """
    Scrute le repertoire de depot (batch.inbox_dir, la ou CFT pose ses
    fichiers) : chaque *.csv stable est archive puis crible en campagne.
    Retourne le nombre de campagnes lancees.
    """
    from fiskr.database import SessionLocal
    batch_cfg = config.get("batch", {}) or {}
    inbox_raw = (batch_cfg.get("inbox_dir") or "").strip()
    if not inbox_raw:
        return 0
    inbox = Path(inbox_raw)
    if not inbox.is_dir():
        return 0
    archive = Path((batch_cfg.get("archive_dir") or "").strip() or (inbox / "archive"))
    archive.mkdir(parents=True, exist_ok=True)
    launched = 0
    import time as _time
    for candidate in sorted(inbox.glob("*.csv")):
        try:
            # Fichier encore en cours de transfert : attendre la prochaine passe
            if _time.time() - candidate.stat().st_mtime < 5:
                continue
            archived = archive / f"{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{candidate.name}"
            shutil.move(str(candidate), str(archived))
            profiles = _parse_batch_csv(archived)
            db = SessionLocal()
            try:
                _launch_batch_campaign(
                    db, f"Dépôt CFT — {candidate.name}", candidate.name,
                    profiles, "cft-inbox", None, trigger="inbox",
                )
            finally:
                db.close()
            launched += 1
            logger.info(f"Inbox CFT : campagne lancée pour {candidate.name} ({len(profiles)} client(s)).")
        except HTTPException as bad_file:
            logger.error(f"Inbox CFT : fichier {candidate.name} refusé — {bad_file.detail}")
        except Exception as e:
            logger.error(f"Inbox CFT : échec sur {candidate.name} — {e}")
    return launched

async def _inbox_poller():
    """Boucle de scrutation de l'inbox CFT (desactivee si batch.inbox_dir vide)."""
    while True:
        batch_cfg = config.get("batch", {}) or {}
        poll_seconds = max(10, int(batch_cfg.get("inbox_poll_seconds", 60) or 60))
        if not (batch_cfg.get("inbox_dir") or "").strip():
            await asyncio.sleep(300)
            continue
        try:
            await asyncio.to_thread(_process_inbox_once)
        except Exception as e:
            logger.error(f"Scrutation de l'inbox CFT en échec : {e}")
        await asyncio.sleep(poll_seconds)

WATCHLIST_DB_SCOPES = ("production", "all", "PENDING_REVIEW", "SUPERSEDED", "REJECTED", "EXCLUDED")

# Champs cherchables de la vue base de donnees. Les colonnes JSON (alias,
# dates, pays, documents...) sont cherchees via CAST(col AS TEXT) — valide en
# SQLite (JSON stocke en TEXT) comme en PostgreSQL (cast json -> text).
_WL_TEXT_SEARCH_COLS = {
    "primary_name": WatchlistEntity.primary_name,
    "entity_id": WatchlistEntity.entity_id,
    "entity_type": WatchlistEntity.entity_type,
    "gender": WatchlistEntity.gender,
    "place_of_birth": WatchlistEntity.place_of_birth,
    "address": WatchlistEntity.address,
    "city": WatchlistEntity.city,
    "state": WatchlistEntity.state,
    "country": WatchlistEntity.country,
    "origin": WatchlistEntity.origin,
    "designation": WatchlistEntity.designation,
    "designation_reasons": WatchlistEntity.designation_reasons,
    "additional_informations": WatchlistEntity.additional_informations,
    "official_reference": WatchlistEntity.official_reference,
    "lei_number": WatchlistEntity.lei_number,
    "imo_number": WatchlistEntity.imo_number,
    "aircraft_tail_number": WatchlistEntity.aircraft_tail_number,
    "date_of_death": WatchlistEntity.date_of_death,
    # Champs etendus scalaires
    "bic_swift": WatchlistEntity.bic_swift,
    "tax_id": WatchlistEntity.tax_id,
    "duns_number": WatchlistEntity.duns_number,
    "title": WatchlistEntity.title,
    "name_original_script": WatchlistEntity.name_original_script,
    "listed_on": WatchlistEntity.listed_on,
    "delisted_on": WatchlistEntity.delisted_on,
    "pep_role": WatchlistEntity.pep_role,
    "secondary_sanctions_risk": WatchlistEntity.secondary_sanctions_risk,
    "designating_state": WatchlistEntity.designating_state,
    "vessel_call_sign": WatchlistEntity.vessel_call_sign,
    "vessel_mmsi": WatchlistEntity.vessel_mmsi,
    "vessel_flag": WatchlistEntity.vessel_flag,
    "vessel_type": WatchlistEntity.vessel_type,
    "vessel_owner": WatchlistEntity.vessel_owner,
    "aircraft_model": WatchlistEntity.aircraft_model,
    "aircraft_operator": WatchlistEntity.aircraft_operator,
    "organization_type": WatchlistEntity.organization_type,
}
_WL_JSON_SEARCH_COLS = {
    "aliases": WatchlistEntity.aliases,
    "dates_of_birth": WatchlistEntity.dates_of_birth,
    "countries": WatchlistEntity.countries,
    "individual_name_parsed": WatchlistEntity.individual_name_parsed,
    "alternative_addresses": WatchlistEntity.alternative_addresses,
    "national_registry_ids": WatchlistEntity.national_registry_ids,
    "other_registration_ids": WatchlistEntity.other_registration_ids,
    "passport_documents": WatchlistEntity.passport_documents,
    "national_id_documents": WatchlistEntity.national_id_documents,
    "other_id_documents": WatchlistEntity.other_id_documents,
    # Champs etendus JSON
    "crypto_wallets": WatchlistEntity.crypto_wallets,
    "sanction_programs": WatchlistEntity.sanction_programs,
    "phone_numbers": WatchlistEntity.phone_numbers,
    "email_addresses": WatchlistEntity.email_addresses,
    "websites": WatchlistEntity.websites,
}
WATCHLIST_SEARCH_FIELDS = ("default", "any") + tuple(_WL_TEXT_SEARCH_COLS) + tuple(_WL_JSON_SEARCH_COLS)


# Seuil de similarite du repli fuzzy de la vue base de donnees (0-100)
WATCHLIST_FUZZY_MIN_SCORE = 80.0


def _flatten_json_strings(value) -> List[str]:
    """Valeurs textuelles d'une structure JSON (dict/list imbriques)."""
    out: List[str] = []
    if isinstance(value, dict):
        for v in value.values():
            out.extend(_flatten_json_strings(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            out.extend(_flatten_json_strings(v))
    elif value not in (None, ""):
        out.append(str(value))
    return out


def _fuzzy_candidate_texts(entity: WatchlistEntity, field: str) -> List[str]:
    """Textes de la fiche a comparer en fuzzy, selon le champ de recherche choisi."""
    if field in _WL_TEXT_SEARCH_COLS:
        text_cols, json_cols = [field], []
    elif field in _WL_JSON_SEARCH_COLS:
        text_cols, json_cols = [], [field]
    elif field == "any":
        text_cols, json_cols = list(_WL_TEXT_SEARCH_COLS), list(_WL_JSON_SEARCH_COLS)
    else:  # default : memes champs que la recherche exacte indexee
        text_cols, json_cols = ["primary_name", "entity_id", "lei_number", "imo_number"], []
    texts = [str(v) for c in text_cols if (v := getattr(entity, c, None))]
    for c in json_cols:
        texts.extend(_flatten_json_strings(getattr(entity, c, None)))
    return texts


def _fuzzy_best_score(needle_norm: str, texts: List[str]) -> float:
    """
    Meilleure similarite Jaro-Winkler (0-100) entre la recherche et les textes
    de la fiche — texte entier ET mot a mot, pour tolerer une faute de frappe
    dans un nom au sein d'un champ long.
    """
    from fiskr.quality import strip_accents
    best = 0.0
    for text in texts:
        text_norm = strip_accents(str(text).upper().strip())
        if not text_norm:
            continue
        candidates = [text_norm]
        if " " in text_norm:
            candidates.extend(text_norm.split())
        for cand in candidates:
            # Ecart de longueur trop grand : similarite forcement insuffisante
            if abs(len(cand) - len(needle_norm)) > max(3, len(needle_norm) // 2):
                continue
            score = jaro_wink_similarity(needle_norm, cand)
            if score > best:
                best = score
                if best >= 99.9:
                    return best
    return best


def _wl_search_clauses(field: str, needle: str):
    """Clauses ilike pour un champ cherchable (liste a combiner en OR)."""
    from sqlalchemy import cast, Text
    if field in _WL_TEXT_SEARCH_COLS:
        return [_WL_TEXT_SEARCH_COLS[field].ilike(needle)]
    if field in _WL_JSON_SEARCH_COLS:
        return [cast(_WL_JSON_SEARCH_COLS[field], Text).ilike(needle)]
    if field == "any":
        return (
            [col.ilike(needle) for col in _WL_TEXT_SEARCH_COLS.values()]
            + [cast(col, Text).ilike(needle) for col in _WL_JSON_SEARCH_COLS.values()]
        )
    # default : champs indexes rapides (comportement historique)
    return [
        WatchlistEntity.primary_name.ilike(needle),
        WatchlistEntity.entity_id.ilike(needle),
        WatchlistEntity.lei_number.ilike(needle),
        WatchlistEntity.imo_number.ilike(needle),
    ]


@app.get("/api/watchlist/db")
async def browse_watchlist_db(
    scope: str = Query("production"),
    list_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    search_field: str = Query("default"),
    sort_by: Optional[str] = Query(None),
    sort_dir: str = Query("asc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Consultation EN DIRECT de la base de donnees des listes (pagination et
    recherche SQL), independante du cache memoire du moteur. Le scope par
    defaut « production » reflete exactement l'univers crible (snapshots
    READY, entites non exclues) ; les autres scopes exposent les entites en
    attente d'homologation, remplacees, rejetees ou exclues.
    """
    scope = (scope or "production").strip()
    if scope not in WATCHLIST_DB_SCOPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Scope inconnu ({', '.join(WATCHLIST_DB_SCOPES)})."
        )
    search_field = (search_field or "default").strip()
    if search_field not in WATCHLIST_SEARCH_FIELDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Champ de recherche inconnu ({', '.join(WATCHLIST_SEARCH_FIELDS)})."
        )
    # Tri serveur : colonnes texte du referentiel + identifiants, valide strictement
    sort_by = (sort_by or "").strip() or None
    sort_dir = "desc" if (sort_dir or "").strip().lower() == "desc" else "asc"
    _WL_SORTABLE = {"entity_id", "entity_type", "primary_name", "origin", "country",
                    "listed_on", "official_reference", "bic_swift"}
    if sort_by is not None and sort_by not in _WL_SORTABLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Colonne de tri inconnue ({', '.join(sorted(_WL_SORTABLE))})."
        )

    query = db.query(WatchlistEntity, Snapshot).join(
        Snapshot, WatchlistEntity.snapshot_id == Snapshot.snapshot_id
    ).filter(Snapshot.file_type.in_(WATCHLIST_FILE_TYPES))

    if scope == "production":
        query = query.filter(Snapshot.status == "READY", WatchlistEntity.excluded.isnot(True))
    elif scope == "EXCLUDED":
        query = query.filter(WatchlistEntity.excluded.is_(True))
    elif scope != "all":
        query = query.filter(Snapshot.status == scope)

    if list_type:
        values = [v.strip().upper() for v in list_type.split(",") if v.strip()]
        if values:
            query = query.filter(Snapshot.file_type.in_(values))

    match_mode = None
    search_term = (search or "").strip()
    if search_term:
        from sqlalchemy import or_
        needle = f"%{search_term}%"
        exact_query = query.filter(or_(*_wl_search_clauses(search_field, needle)))
        exact_total = exact_query.count()

        if exact_total > 0:
            # Des resultats exacts existent : on ne montre QU'eux (pas de fuzzy)
            match_mode = "exact"
            query = exact_query
        else:
            # Repli fuzzy : tolerance aux fautes de frappe, classement par
            # similarite (Jaro-Winkler, normalisation accents/casse du moteur)
            from fiskr.quality import strip_accents
            match_mode = "fuzzy"
            needle_norm = strip_accents(search_term.upper())
            scored = []
            for entity, snap in query.yield_per(500):
                score_value = _fuzzy_best_score(needle_norm, _fuzzy_candidate_texts(entity, search_field))
                if score_value >= WATCHLIST_FUZZY_MIN_SCORE:
                    scored.append((score_value, entity, snap))
            scored.sort(key=lambda t: (-t[0], t[1].id))
            total = len(scored)
            page_rows = scored[(page - 1) * page_size: (page - 1) * page_size + page_size]
            items = []
            for score_value, entity, snap in page_rows:
                d = _serialize_watchlist_entity(entity, snap)
                d["_fuzzy_score"] = round(score_value, 1)
                items.append(d)
            return {"total": total, "page": page, "page_size": page_size, "scope": scope,
                    "match_mode": match_mode, "items": items}

    total = query.count()
    if sort_by:
        col = getattr(WatchlistEntity, sort_by)
        order_clause = col.desc() if sort_dir == "desc" else col.asc()
        query = query.order_by(order_clause, WatchlistEntity.id.asc())
    else:
        query = query.order_by(Snapshot.uploaded_at.desc(), WatchlistEntity.id.asc())
    rows = query.offset((page - 1) * page_size).limit(page_size).all()

    items = [_serialize_watchlist_entity(entity, snap) for entity, snap in rows]

    return {"total": total, "page": page, "page_size": page_size, "scope": scope,
            "match_mode": match_mode, "items": items}

@app.get("/api/history")
async def get_audit_history(
    list_type: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Journal d'audit pagine, filtrable par statut de decision et type de liste.
    Les enregistrements anterieurs a la colonne list_type sont restitues via
    le decision_tree quand il porte le type (fallback lecture, le journal
    immuable n'est jamais reecrit) ; le filtre SQL UNKNOWN les cible.
    """
    query = db.query(AuditTrail)
    if status_filter:
        statuses = [s.strip().upper() for s in status_filter.split(",") if s.strip()]
        query = query.filter(AuditTrail.status.in_(statuses))
    query = _apply_list_type_filter(query, AuditTrail.list_type, list_type)
    total = query.count()
    rows = query.order_by(AuditTrail.timestamp.desc()) \
                .offset((page - 1) * page_size).limit(page_size).all()

    def _row(r: AuditTrail) -> Dict[str, Any]:
        tree = r.decision_tree or {}
        fallback = ((tree.get("watchlist_entity") or {}).get("_list_type")
                    if isinstance(tree, dict) else None)
        return {
            "id": r.id,
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
            "client_id": r.client_id,
            "client_name": r.client_name,
            "client_type": r.client_type,
            "watchlist_id": r.watchlist_id,
            "watchlist_name": r.watchlist_name,
            "base_score": r.base_score,
            "final_score": r.final_score,
            "status": r.status,
            "list_type": r.list_type or fallback,
            "decision_tree": r.decision_tree,
            "config_state": r.config_state,
            "watchlist_version": r.watchlist_version,
            "watchlist_hash": r.watchlist_hash,
        }

    return {"total": total, "page": page, "page_size": page_size,
            "items": [_row(r) for r in rows]}

# ------------------ VUE CLIENT 360° ------------------

@app.get("/api/clients/{client_id}/overview")
async def get_client_overview(
    client_id: str,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Vue 360° d'un client : fiche KYC (dernier referentiel en production),
    historique de criblage, alertes (tous statuts) et paires de liste blanche
    — tout ce qu'un analyste doit voir au meme endroit pendant une instruction.
    """
    kyc_row = (
        db.query(ClientEntity, Snapshot)
          .join(Snapshot, ClientEntity.snapshot_id == Snapshot.snapshot_id)
          .filter(ClientEntity.client_id == client_id, Snapshot.status == "READY")
          .order_by(Snapshot.uploaded_at.desc()).first()
    )
    kyc = None
    if kyc_row:
        entity, snap = kyc_row
        kyc = {
            "client_type": entity.client_type,
            "first_name": entity.client_first_name, "last_name": entity.client_last_name,
            "company_name": entity.client_company_name, "dob": entity.client_dob,
            "gender": entity.client_gender, "countries": entity.client_countries,
            "address": entity.client_address, "city": entity.client_city,
            "country": entity.client_country,
            "iban": entity.client_iban, "bic": entity.client_bic, "tax_id": entity.client_tax_id,
            "phone": entity.client_phone, "email": entity.client_email,
            "risk_rating": entity.client_risk_rating, "pep_flag": bool(entity.client_pep_flag),
            "segment": entity.client_segment, "activity_sector": entity.client_activity_sector,
            "relationship_start": entity.client_relationship_start, "status": entity.client_status,
            "snapshot_uploaded_at": snap.uploaded_at.isoformat() if snap.uploaded_at else None,
        }

    audits = db.query(AuditTrail).filter(AuditTrail.client_id == client_id) \
               .order_by(AuditTrail.timestamp.desc()).limit(50).all()
    alerts = db.query(Alert).filter(Alert.client_id == client_id) \
               .order_by(Alert.created_at.desc()).limit(50).all()
    pairs = db.query(WhitelistPair).filter(WhitelistPair.client_id == client_id) \
              .order_by(WhitelistPair.created_at.desc()).all()

    return {
        "client_id": client_id,
        "kyc": kyc,
        "screenings": [
            {
                "id": r.id, "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                "watchlist_id": r.watchlist_id, "watchlist_name": r.watchlist_name,
                "final_score": r.final_score, "status": r.status, "list_type": r.list_type,
            }
            for r in audits
        ],
        "alerts": [_alert_summary(a) for a in alerts],
        "whitelist_pairs": [_whitelist_summary(p) for p in pairs],
        "counts": {
            "screenings": db.query(AuditTrail).filter(AuditTrail.client_id == client_id).count(),
            "alerts": db.query(Alert).filter(Alert.client_id == client_id).count(),
            "whitelist_pairs": len(pairs),
        },
    }

# ------------------ EXPORTS CSV (alertes, journal d'audit, listes) ------------------

def _csv_response(filename: str, header: List[str], rows) -> Response:
    """CSV « ; » avec BOM UTF-8 : ouverture directe dans Excel FR."""
    import csv as _csv
    import io
    buffer = io.StringIO()
    writer = _csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

_EXPORT_MAX_ROWS = 50000

@app.get("/api/export/alerts.csv")
async def export_alerts_csv(
    status_filter: Optional[str] = Query(None, alias="status"),
    channel: Optional[str] = Query(None),
    list_type: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    assigned_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Export CSV de la file d'alertes (les filtres de l'ecran s'appliquent)."""
    query = db.query(Alert)
    if channel:
        ch = channel.strip().upper()
        if ch == "SCREENING":
            query = query.filter((Alert.channel == "SCREENING") | (Alert.channel.is_(None)))
        else:
            query = query.filter(Alert.channel == ch)
    if status_filter:
        statuses = [s.strip().upper() for s in status_filter.split(",") if s.strip()]
        query = query.filter(Alert.status.in_(statuses))
    if priority:
        prios = [p.strip().upper() for p in priority.split(",") if p.strip()]
        query = query.filter(Alert.priority.in_(prios))
    if assigned_to:
        query = query.filter(Alert.assigned_to == assigned_to)
    query = _apply_list_type_filter(query, Alert.list_type, list_type)
    rows = query.order_by(Alert.created_at.desc()).limit(_EXPORT_MAX_ROWS).all()
    header = ["id", "cree_le", "canal", "priorite", "echeance_sla", "client_id", "client",
              "fiche_listee_id", "fiche_listee", "liste", "score", "statut", "assignee_a",
              "decide_par", "decide_le", "commentaire_decision"]
    data = [
        [a.id, a.created_at.isoformat() if a.created_at else "", a.channel or "SCREENING",
         a.priority or "", a.due_at.isoformat() if a.due_at else "", a.client_id or "",
         a.client_name, a.watchlist_entity_id, a.watchlist_name, a.list_type or "",
         f"{a.final_score:.1f}", a.status, a.assigned_to or "", a.decided_by or "",
         a.decided_at.isoformat() if a.decided_at else "", a.decision_comment or ""]
        for a in rows
    ]
    return _csv_response(f"fiskr_alertes_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv", header, data)

@app.get("/api/export/history.csv")
async def export_history_csv(
    list_type: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Export CSV du journal d'audit de criblage (filtres de l'ecran appliques)."""
    query = db.query(AuditTrail)
    if status_filter:
        statuses = [s.strip().upper() for s in status_filter.split(",") if s.strip()]
        query = query.filter(AuditTrail.status.in_(statuses))
    query = _apply_list_type_filter(query, AuditTrail.list_type, list_type)
    rows = query.order_by(AuditTrail.timestamp.desc()).limit(_EXPORT_MAX_ROWS).all()
    header = ["id", "horodatage", "client_id", "client", "type_client", "fiche_listee_id",
              "fiche_listee", "liste", "score_base", "score_final", "statut", "version_watchlist"]
    data = [
        [r.id, r.timestamp.isoformat() if r.timestamp else "", r.client_id or "", r.client_name,
         r.client_type or "", r.watchlist_id or "", r.watchlist_name or "", r.list_type or "",
         f"{r.base_score:.1f}" if r.base_score is not None else "",
         f"{r.final_score:.1f}" if r.final_score is not None else "",
         r.status, r.watchlist_version or ""]
        for r in rows
    ]
    return _csv_response(f"fiskr_audit_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv", header, data)

@app.get("/api/export/watchlist.csv")
async def export_watchlist_csv(
    scope: str = Query("production"),
    list_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    search_field: str = Query("default"),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Export CSV de la vue base des listes (memes filtres que l'ecran ;
    recherche exacte uniquement, pas de repli fuzzy sur un export)."""
    scope = (scope or "production").strip()
    if scope not in WATCHLIST_DB_SCOPES:
        raise HTTPException(status_code=400, detail=f"Scope inconnu ({', '.join(WATCHLIST_DB_SCOPES)}).")
    if search_field not in WATCHLIST_SEARCH_FIELDS:
        raise HTTPException(status_code=400, detail="Champ de recherche inconnu.")
    query = db.query(WatchlistEntity, Snapshot).join(
        Snapshot, WatchlistEntity.snapshot_id == Snapshot.snapshot_id
    ).filter(Snapshot.file_type.in_(WATCHLIST_FILE_TYPES))
    if scope == "production":
        query = query.filter(Snapshot.status == "READY", WatchlistEntity.excluded.isnot(True))
    elif scope == "EXCLUDED":
        query = query.filter(WatchlistEntity.excluded.is_(True))
    elif scope != "all":
        query = query.filter(Snapshot.status == scope)
    if list_type:
        values = [v.strip().upper() for v in list_type.split(",") if v.strip()]
        if values:
            query = query.filter(Snapshot.file_type.in_(values))
    if search and search.strip():
        from sqlalchemy import or_
        query = query.filter(or_(*_wl_search_clauses(search_field, f"%{search.strip()}%")))
    rows = query.order_by(WatchlistEntity.primary_name.asc()).limit(_EXPORT_MAX_ROWS).all()
    header = ["entity_id", "liste", "statut_snapshot", "type", "nom_principal", "pays",
              "dates_naissance", "bic_swift", "tax_id", "programmes", "inscrit_le",
              "reference_officielle"]
    data = []
    for entity, snap in rows:
        countries = entity.countries or {}
        all_countries = sorted({c for values in countries.values() if isinstance(values, list) for c in values})
        data.append([
            entity.entity_id, snap.file_type, snap.status, entity.entity_type,
            entity.primary_name, ", ".join(all_countries),
            ", ".join(entity.dates_of_birth or []), entity.bic_swift or "",
            entity.tax_id or "", ", ".join(entity.sanction_programs or []),
            entity.listed_on or "", entity.official_reference or "",
        ])
    return _csv_response(f"fiskr_listes_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv", header, data)

# ------------------ RAPPORT D'ACTIVITE REGLEMENTAIRE (periode) ------------------

def _parse_report_period(date_from: Optional[str], date_to: Optional[str]):
    """Bornes de periode [debut, fin) : dates ISO AAAA-MM-JJ, fin incluse.
    Defaut : les 30 derniers jours."""
    try:
        end_day = datetime.strptime(date_to, "%Y-%m-%d") if date_to else datetime.utcnow()
        start = datetime.strptime(date_from, "%Y-%m-%d") if date_from else end_day - timedelta(days=30)
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates invalides (format attendu : AAAA-MM-JJ).")
    end = end_day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1) \
        if date_to else end_day
    if start >= end:
        raise HTTPException(status_code=400, detail="La date de début doit précéder la date de fin.")
    return start, end

def _count_by(rows) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for key, count in rows:
        out[str(key) if key else "—"] = count
    return out

def build_activity_report(db, start: datetime, end: datetime) -> Dict[str, Any]:
    """
    Synthese d'activite de la periode pour le reporting reglementaire
    (ACPR/FED) : volumetrie de criblage, alertes et decisions, delais,
    liste blanche, synchronisations et campagnes batch.
    """
    from sqlalchemy import func

    screenings_q = db.query(AuditTrail).filter(
        AuditTrail.timestamp >= start, AuditTrail.timestamp < end)
    alerts_created_q = db.query(Alert).filter(
        Alert.created_at >= start, Alert.created_at < end)
    alerts_decided_q = db.query(Alert).filter(
        Alert.decided_at.isnot(None), Alert.decided_at >= start, Alert.decided_at < end)

    decided = alerts_decided_q.all()
    delays = [
        (a.decided_at - a.created_at).total_seconds() / 3600.0
        for a in decided if a.created_at and a.decided_at and a.decided_at >= a.created_at
    ]

    return {
        "period": {"from": start.strftime("%Y-%m-%d"),
                   "to": (end - timedelta(seconds=1)).strftime("%Y-%m-%d")},
        "screenings": {
            "total": screenings_q.count(),
            "by_status": _count_by(
                screenings_q.with_entities(AuditTrail.status, func.count(AuditTrail.id))
                            .group_by(AuditTrail.status).all()),
            "by_list_type": _count_by(
                screenings_q.with_entities(AuditTrail.list_type, func.count(AuditTrail.id))
                            .group_by(AuditTrail.list_type).all()),
        },
        "alerts": {
            "created": alerts_created_q.count(),
            "created_by_channel": _count_by(
                alerts_created_q.with_entities(Alert.channel, func.count(Alert.id))
                                .group_by(Alert.channel).all()),
            "created_by_priority": _count_by(
                alerts_created_q.with_entities(Alert.priority, func.count(Alert.id))
                                .group_by(Alert.priority).all()),
            "decided": len(decided),
            "decided_by_status": _count_by(
                alerts_decided_q.with_entities(Alert.status, func.count(Alert.id))
                                .group_by(Alert.status).all()),
            "avg_decision_hours": round(sum(delays) / len(delays), 1) if delays else None,
            "escalations": db.query(AlertEvent).filter(
                AlertEvent.action == "ESCALATED",
                AlertEvent.timestamp >= start, AlertEvent.timestamp < end).count(),
            "still_open": db.query(Alert).filter(Alert.status.in_(ALERT_OPEN_STATUSES)).count(),
        },
        "whitelist": {
            "created": db.query(WhitelistPair).filter(
                WhitelistPair.created_at >= start, WhitelistPair.created_at < end).count(),
            "revoked": db.query(WhitelistPair).filter(
                WhitelistPair.revoked_at.isnot(None),
                WhitelistPair.revoked_at >= start, WhitelistPair.revoked_at < end).count(),
        },
        "syncs": {
            "total": db.query(SyncReport).filter(
                SyncReport.executed_at >= start, SyncReport.executed_at < end).count(),
            "by_status": _count_by(
                db.query(SyncReport.status, func.count(SyncReport.id))
                  .filter(SyncReport.executed_at >= start, SyncReport.executed_at < end)
                  .group_by(SyncReport.status).all()),
            "by_source": _count_by(
                db.query(SyncReport.source, func.count(SyncReport.id))
                  .filter(SyncReport.executed_at >= start, SyncReport.executed_at < end)
                  .group_by(SyncReport.source).all()),
        },
        "batch": {
            "campaigns": db.query(BatchCampaign).filter(
                BatchCampaign.created_at >= start, BatchCampaign.created_at < end).count(),
            "clients_screened": int(db.query(func.coalesce(func.sum(BatchCampaign.processed_clients), 0))
                                     .filter(BatchCampaign.created_at >= start,
                                             BatchCampaign.created_at < end).scalar() or 0),
        },
    }

@app.get("/api/reports/activity")
async def get_activity_report(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Rapport d'activite sur la periode (defaut : 30 derniers jours)."""
    start, end = _parse_report_period(date_from, date_to)
    return build_activity_report(db, start, end)

def _activity_report_rows(report: Dict[str, Any]):
    """Aplatis le rapport en lignes (section ; indicateur ; valeur) pour le CSV."""
    rows = []
    def emit(section, label, value):
        rows.append([section, label, value if value is not None else ""])
    emit("Période", "Du", report["period"]["from"])
    emit("Période", "Au", report["period"]["to"])
    emit("Criblage", "Décisions totales", report["screenings"]["total"])
    for status, count in sorted(report["screenings"]["by_status"].items()):
        emit("Criblage", f"Décisions {status}", count)
    for lt, count in sorted(report["screenings"]["by_list_type"].items()):
        emit("Criblage", f"Décisions liste {lt}", count)
    alerts = report["alerts"]
    emit("Alertes", "Créées", alerts["created"])
    for channel, count in sorted(alerts["created_by_channel"].items()):
        emit("Alertes", f"Créées canal {channel}", count)
    for prio, count in sorted(alerts["created_by_priority"].items()):
        emit("Alertes", f"Créées priorité {prio}", count)
    emit("Alertes", "Décidées", alerts["decided"])
    for status, count in sorted(alerts["decided_by_status"].items()):
        emit("Alertes", f"Décidées {status}", count)
    emit("Alertes", "Délai moyen de décision (h)", alerts["avg_decision_hours"])
    emit("Alertes", "Escalades", alerts["escalations"])
    emit("Alertes", "Encore ouvertes (fin de période)", alerts["still_open"])
    emit("Liste blanche", "Paires créées", report["whitelist"]["created"])
    emit("Liste blanche", "Paires révoquées", report["whitelist"]["revoked"])
    emit("Synchronisations", "Total", report["syncs"]["total"])
    for status, count in sorted(report["syncs"]["by_status"].items()):
        emit("Synchronisations", f"Statut {status}", count)
    for source, count in sorted(report["syncs"]["by_source"].items()):
        emit("Synchronisations", f"Source {source}", count)
    emit("Batch", "Campagnes", report["batch"]["campaigns"])
    emit("Batch", "Clients criblés", report["batch"]["clients_screened"])
    return rows

@app.get("/api/reports/activity.csv")
async def export_activity_report_csv(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Export CSV du rapport d'activite (Excel FR : « ; » + BOM)."""
    start, end = _parse_report_period(date_from, date_to)
    report = build_activity_report(db, start, end)
    return _csv_response(
        f"fiskr_activite_{report['period']['from']}_{report['period']['to']}.csv",
        ["Section", "Indicateur", "Valeur"], _activity_report_rows(report))

@app.get("/api/reports/activity/print")
async def print_activity_report(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Rapport d'activite HTML autonome imprimable (impression -> PDF)."""
    from html import escape
    start, end = _parse_report_period(date_from, date_to)
    report = build_activity_report(db, start, end)
    rows_html = "\n".join(
        f"<tr><td>{escape(str(section))}</td><td>{escape(str(label))}</td>"
        f"<td style='text-align:right'>{escape(str(value))}</td></tr>"
        for section, label, value in _activity_report_rows(report)
    )
    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Fiskr — Rapport d'activité {escape(report['period']['from'])} → {escape(report['period']['to'])}</title>
<style>
body {{ font-family: Arial, sans-serif; color: #111; margin: 2rem auto; max-width: 860px; }}
h1 {{ font-size: 1.4rem; }} .sub {{ color: #555; margin-bottom: 1.5rem; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.9rem; }}
th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
th {{ background: #f0f0f0; }}
@media print {{ .no-print {{ display: none; }} body {{ margin: 0; }} }}
</style></head><body>
<button class="no-print" onclick="window.print()">🖨 Imprimer / PDF</button>
<h1>Fiskr — Rapport d'activité conformité</h1>
<p class="sub">Période du {escape(report['period']['from'])} au {escape(report['period']['to'])}
 — généré le {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC.</p>
<table><thead><tr><th>Section</th><th>Indicateur</th><th>Valeur</th></tr></thead>
<tbody>{rows_html}</tbody></table>
</body></html>"""
    return HTMLResponse(content=html)

@app.get("/api/counters")
async def get_sidebar_counters(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Compteurs legers pour les badges de la barre laterale (polling) :
    alertes ouvertes et snapshots en attente d'homologation.
    """
    open_q = db.query(Alert).filter(Alert.status.in_(ALERT_OPEN_STATUSES))
    return {
        "open_alerts": open_q.count(),
        "open_alerts_screening": open_q.filter(
            (Alert.channel == "SCREENING") | (Alert.channel.is_(None))
        ).count(),
        "open_alerts_filtering": open_q.filter(Alert.channel == "FILTERING").count(),
        "pending_validation": db.query(Alert).filter(Alert.status == "PENDING_VALIDATION").count(),
        "overdue_alerts": db.query(Alert).filter(
            Alert.status.in_(ALERT_OPEN_STATUSES), Alert.due_at.isnot(None),
            Alert.due_at < datetime.utcnow()
        ).count(),
        "pending_reviews": db.query(Snapshot).filter(Snapshot.status == "PENDING_REVIEW").count(),
    }

@app.get("/api/config")
async def get_active_config(current_user: Dict[str, Any] = Depends(get_current_user)):
    # Create a deep copy to sanitize sensitive database credentials before returning to client
    sanitized_config = json.loads(json.dumps(config))
    if "database" in sanitized_config and "url" in sanitized_config["database"]:
        url = sanitized_config["database"]["url"]
        if "@" in url and "://" in url:
            prefix, rest = url.split("://", 1)
            creds, target = rest.split("@", 1)
            if ":" in creds:
                db_user, _ = creds.split(":", 1)
                sanitized_config["database"]["url"] = f"{prefix}://{db_user}:*****@{target}"
            else:
                sanitized_config["database"]["url"] = f"{prefix}://*****@{target}"
    return sanitized_config

@app.post("/api/snapshots/purge")
async def purge_failed_snapshots(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Purges (deletes) all snapshots and their associated entities (watchlist and clients)
    that are in status 'ERROR', 'PROCESSING' (aborted/failed) or 'REJECTED'
    (refused during homologation review; purging frees the file hash for re-upload).
    """
    try:
        # Find failed / processing / rejected snapshots
        failed_snapshots = db.query(Snapshot).filter(Snapshot.status.in_(["ERROR", "PROCESSING", "REJECTED"])).all()
        if not failed_snapshots:
            return {"message": "Aucun snapshot erroné ou en cours à purger.", "purged_snapshots_count": 0}
            
        purged_ids = [s.snapshot_id for s in failed_snapshots]
        
        # 1. Delete associated WatchlistEntity records
        deleted_watchlist = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(purged_ids)).delete(synchronize_session=False)
        
        # 2. Delete associated ClientEntity records
        deleted_client = db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(purged_ids)).delete(synchronize_session=False)
        
        # 3. Delete the Snapshots themselves
        deleted_snapshots = db.query(Snapshot).filter(Snapshot.snapshot_id.in_(purged_ids)).delete(synchronize_session=False)

        log_admin_action(db, current_user["username"], "SNAPSHOTS_PURGED",
                         target=f"{deleted_snapshots} snapshot(s)",
                         before={"snapshot_ids": purged_ids},
                         detail=f"{deleted_watchlist} fiches watchlist et {deleted_client} fiches client supprimées.")
        db.commit()
        
        # Reload cache to ensure in-memory items are in sync
        load_watchlist_cache(db)
        
        return {
            "message": f"Purge réussie : {deleted_snapshots} snapshot(s), {deleted_watchlist} fiches watchlist, et {deleted_client} fiches client supprimées.",
            "purged_snapshots_count": deleted_snapshots,
            "purged_watchlist_entities": deleted_watchlist,
            "purged_client_entities": deleted_client
        }
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to purge snapshots: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Purge failed: {str(e)}"
        )

# ------------------ SOURCE SYNCHRONIZATION (OFAC download / EUR-Lex scraping) ------------------

class SyncRunRequest(BaseModel):
    source: str                      # OFAC | EURLEX
    date: Optional[str] = None       # YYYY-MM-DD (EURLEX uniquement, defaut: aujourd'hui)

def _serialize_sync_report(report: SyncReport) -> Dict[str, Any]:
    return {c.name: getattr(report, c.name) for c in report.__table__.columns}

@app.post("/api/sync/run")
def run_source_sync(
    request: SyncRunRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(require_admin)
):
    """
    Declenche manuellement la synchronisation d'une source officielle :
    telechargement du fichier OFAC ou scraping du Journal Officiel EUR-Lex,
    delta par rapport a la liste active, application et rapport de suivi.
    """
    source = (request.source or "").strip().upper()
    reload_cache = lambda: load_watchlist_cache(db)

    if source == "OFAC":
        report = run_ofac_sync(db, trigger="MANUAL", reload_cache=reload_cache)
    elif source == "EURLEX":
        for_date = None
        if request.date:
            try:
                for_date = datetime.strptime(request.date, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Format de date invalide (attendu: YYYY-MM-DD)."
                )
        report = run_eurlex_sync(db, for_date=for_date, trigger="MANUAL", reload_cache=reload_cache)
    elif source == "DGT":
        report = run_dgt_sync(db, trigger="MANUAL", reload_cache=reload_cache)
    elif source in ("EUFSF", "EU_FSF", "FSF"):
        report = run_eu_fsf_sync(db, trigger="MANUAL", reload_cache=reload_cache)
    elif source in ("UN", "ONU"):
        report = run_un_sync(db, trigger="MANUAL", reload_cache=reload_cache)
    elif source == "PEP":
        report = run_pep_sync(db, trigger="MANUAL", reload_cache=reload_cache)
    elif source == "OFSI":
        report = run_ofsi_sync(db, trigger="MANUAL", reload_cache=reload_cache)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source inconnue (valeurs possibles: OFAC, EURLEX, EUFSF, DGT, UN, PEP, OFSI)."
        )

    response = _serialize_sync_report(report)
    # Surveillance continue : re-criblage du referentiel clients contre les
    # entites nouvelles/modifiees du snapshot applique
    if report.status == "SUCCESS" and report.snapshot_id and auto_rescreen_enabled(db):
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
        if snap:
            response["rescreen"] = rescreen_after_snapshot_change(
                db, snap.file_type, report.snapshot_id, report.previous_snapshot_id
            )
    return response

@app.get("/api/sync/reports")
async def get_sync_reports(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Historique des rapports de synchronisation des sources (suivi in-app)."""
    reports = db.query(SyncReport).order_by(SyncReport.executed_at.desc()).limit(limit).all()
    return [_serialize_sync_report(r) for r in reports]

@app.get("/api/sync/config")
async def get_sync_configuration(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Configuration active de la synchronisation automatique des sources,
    avec la planification cron effective et la prochaine occurrence par source."""
    from fiskr.cron import next_run as cron_next_run, CronError
    cfg = get_sync_config()
    cfg["email_configured"] = bool(os.getenv("SMTP_HOST") and os.getenv("SYNC_EMAIL_TO"))
    schedules = sync_schedules(db)
    cfg["schedules"] = schedules
    next_runs = {}
    for source, expr in schedules.items():
        try:
            occurrence = cron_next_run(expr)
            next_runs[source] = occurrence.isoformat() if occurrence else None
        except CronError:
            next_runs[source] = None
    cfg["next_runs"] = next_runs
    # Synchronisations en cours d'execution (le front peut alors interroger
    # GET /api/progress?id=sync:<source> pour afficher la progression)
    cfg["running"] = sorted(_running_syncs)
    return cfg

@app.get("/api/progress")
async def get_operation_progress(
    id: str = Query(..., description="Jeton de progression (UUID d'ingestion, sync:<source> ou snapshot_id)"),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Etat d'avancement d'une operation longue (import de liste, synchronisation).

    Source primaire : le registre memoire (fiskr.progress), alimente pendant que
    la requete d'origine est encore en vol. Repli : si le jeton correspond a un
    snapshot_id connu (ex. apres redemarrage du processus), l'etat est reconstruit
    depuis les colonnes persistees Snapshot.processed_count/total_hint/phase."""
    state = progress_registry.get(id)
    if state is not None:
        return {"id": id, **state}
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == id).first()
    if snap is not None:
        processed = snap.processed_count or 0
        total = snap.total_hint
        pct = round(100.0 * processed / total, 1) if total and processed <= total else None
        status = "RUNNING" if snap.status == "PROCESSING" else ("ERROR" if snap.status == "ERROR" else "DONE")
        return {
            "id": id,
            "phase": snap.phase or ("DONE" if status == "DONE" else "PERSIST"),
            "processed": processed,
            "total": total,
            "pct": pct,
            "snapshot_id": snap.snapshot_id,
            "status": status,
            "error": "Import en erreur (voir la table des snapshots)" if status == "ERROR" else None,
            "updated_at": None,
        }
    raise HTTPException(status_code=404, detail="Operation inconnue ou expiree")

class SyncSchedulesUpdate(BaseModel):
    # source -> expression cron 5 champs ; chaine vide = retour au defaut
    schedules: Dict[str, str]

@app.put("/api/settings/sync")
async def update_sync_schedules(
    payload: SyncSchedulesUpdate,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Planification cron par source, modifiable a chaud (admin). Une valeur
    vide retire la surcharge (retour au defaut config/horaire global)."""
    from fiskr.cron import parse_cron, CronError
    unknown = [s for s in payload.schedules if s not in SYNC_SOURCES]
    if unknown:
        raise HTTPException(status_code=400,
                            detail=f"Source(s) inconnue(s) : {', '.join(unknown)} ({', '.join(SYNC_SOURCES)}).")
    current = get_setting_with_source(db, SETTING_SYNC_SCHEDULES, {})["value"] or {}
    merged = dict(current) if isinstance(current, dict) else {}
    for source, expr in payload.schedules.items():
        expr = (expr or "").strip()
        if expr:
            try:
                parse_cron(expr)
            except CronError as bad:
                raise HTTPException(status_code=400, detail=f"{source} : {bad}")
            merged[source] = expr
        else:
            merged.pop(source, None)
    before = dict(current) if isinstance(current, dict) else {}
    set_setting(db, SETTING_SYNC_SCHEDULES, merged, updated_by=admin_user["username"])
    log_admin_action(db, admin_user["username"], "SETTINGS_UPDATED", target="sync.schedules",
                     before=before, after=merged)
    db.commit()
    schedules = sync_schedules(db)
    return {"message": "Planification des synchronisations mise à jour.", "schedules": schedules}

@app.get("/api/sync/evidence")
async def list_sync_evidence(
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Liste les PDF officiels EUR-Lex archives (pieces probantes d'audit)."""
    if not EURLEX_ARCHIVE_DIR.exists():
        return {"files": []}
    return {"files": sorted(p.name for p in EURLEX_ARCHIVE_DIR.glob("*.pdf"))}

@app.get("/api/sync/evidence/{filename}")
async def download_sync_evidence(
    filename: str,
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Telecharge un PDF officiel EUR-Lex archive (version faisant foi en audit)."""
    import re as _re
    if not _re.fullmatch(r"[A-Za-z0-9_.\-]+\.pdf", filename):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nom de fichier invalide.")
    file_path = EURLEX_ARCHIVE_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Piece probante introuvable.")
    return FileResponse(str(file_path), media_type="application/pdf", filename=filename)

# ------------------ HOMOLOGATION (REVUE AVANT PRODUCTION) ------------------

# Pieces justificatives des exclusions d'entites (valeur probante en audit)
EXCLUSION_EVIDENCE_DIR = PROJECT_ROOT / "exclusion_evidence"

class IngestionSettingsUpdate(BaseModel):
    require_approval: Optional[bool] = None
    exclusion_justification_required: Optional[bool] = None
    exclusion_file_required: Optional[bool] = None
    alert_four_eyes_required: Optional[bool] = None
    whitelist_justification_required: Optional[bool] = None
    whitelist_file_required: Optional[bool] = None
    auto_rescreen: Optional[bool] = None
    backtest_required: Optional[bool] = None
    backtest_max_gap_pct: Optional[float] = None
    # SLA d'alertes (heures par priorite, 0 = pas d'echeance) et notifications
    alert_sla_hours: Optional[Dict[str, int]] = None
    notification_events: Optional[Dict[str, bool]] = None
    # Digest KPI periodique : {"enabled": bool, "cron": "0 8 * * 1-5"}
    digest: Optional[Dict[str, Any]] = None

class ReviewDecisionRequest(BaseModel):
    comment: Optional[str] = None

class ExclusionRemoveRequest(BaseModel):
    entity_ids: List[int]

def _settings_payload(db: Session) -> Dict[str, Any]:
    approval = get_setting_with_source(db, SETTING_REQUIRE_APPROVAL, False)
    justif = get_setting_with_source(db, SETTING_EXCLUSION_JUSTIFICATION_REQUIRED, True)
    evidence = get_setting_with_source(db, SETTING_EXCLUSION_FILE_REQUIRED, False)
    four_eyes = get_setting_with_source(db, SETTING_ALERT_FOUR_EYES, True)
    wl_justif = get_setting_with_source(db, SETTING_WHITELIST_JUSTIFICATION_REQUIRED, True)
    wl_file = get_setting_with_source(db, SETTING_WHITELIST_FILE_REQUIRED, False)
    rescreen = get_setting_with_source(db, SETTING_AUTO_RESCREEN, True)
    bt_required = get_setting_with_source(db, SETTING_BACKTEST_REQUIRED, False)
    bt_gap = get_setting_with_source(db, SETTING_BACKTEST_MAX_GAP_PCT, 20.0)
    try:
        bt_gap_value = float(bt_gap["value"])
    except (TypeError, ValueError):
        bt_gap_value = 20.0
    return {
        "require_approval": bool(approval["value"]),
        "exclusion_justification_required": bool(justif["value"]),
        "exclusion_file_required": bool(evidence["value"]),
        "alert_four_eyes_required": bool(four_eyes["value"]),
        "whitelist_justification_required": bool(wl_justif["value"]),
        "whitelist_file_required": bool(wl_file["value"]),
        "auto_rescreen": bool(rescreen["value"]),
        "backtest_required": bool(bt_required["value"]),
        "backtest_max_gap_pct": bt_gap_value,
        "alert_sla_hours": alert_sla_hours(db),
        "notification_events": notification_events(db),
        "digest": digest_settings(db),
        "sources": {
            "require_approval": approval["source"],
            "exclusion_justification_required": justif["source"],
            "exclusion_file_required": evidence["source"],
            "alert_four_eyes_required": four_eyes["source"],
            "whitelist_justification_required": wl_justif["source"],
            "whitelist_file_required": wl_file["source"],
            "auto_rescreen": rescreen["source"],
            "backtest_required": bt_required["source"],
            "backtest_max_gap_pct": bt_gap["source"],
        },
    }

@app.get("/api/settings/ingestion")
async def get_ingestion_settings(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Etat effectif du mode homologation et des exigences de justification d'exclusion."""
    return _settings_payload(db)

@app.put("/api/settings/ingestion")
async def update_ingestion_settings(
    payload: IngestionSettingsUpdate,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Modifie a chaud les reglages d'homologation (Admin). Effet immediat, sans redemarrage."""
    updates = {
        SETTING_REQUIRE_APPROVAL: payload.require_approval,
        SETTING_EXCLUSION_JUSTIFICATION_REQUIRED: payload.exclusion_justification_required,
        SETTING_EXCLUSION_FILE_REQUIRED: payload.exclusion_file_required,
        SETTING_ALERT_FOUR_EYES: payload.alert_four_eyes_required,
        SETTING_WHITELIST_JUSTIFICATION_REQUIRED: payload.whitelist_justification_required,
        SETTING_WHITELIST_FILE_REQUIRED: payload.whitelist_file_required,
        SETTING_AUTO_RESCREEN: payload.auto_rescreen,
        SETTING_BACKTEST_REQUIRED: payload.backtest_required,
    }
    changed = {k: v for k, v in updates.items() if v is not None}
    if (not changed and payload.backtest_max_gap_pct is None
            and payload.alert_sla_hours is None and payload.notification_events is None
            and payload.digest is None):
        raise HTTPException(status_code=400, detail="Aucun réglage fourni.")
    before_state = _settings_payload(db)
    before_state.pop("sources", None)
    for key, value in changed.items():
        set_setting(db, key, bool(value), updated_by=admin_user["username"])
    if payload.backtest_max_gap_pct is not None:
        if not (0 <= payload.backtest_max_gap_pct <= 1000):
            raise HTTPException(status_code=400, detail="backtest_max_gap_pct doit être entre 0 et 1000.")
        set_setting(db, SETTING_BACKTEST_MAX_GAP_PCT, float(payload.backtest_max_gap_pct),
                    updated_by=admin_user["username"])
    if payload.alert_sla_hours is not None:
        sla = {}
        for prio, hours in payload.alert_sla_hours.items():
            key = str(prio).strip().upper()
            if key not in ALERT_PRIORITIES:
                raise HTTPException(status_code=400, detail=f"Priorité SLA inconnue : {prio}.")
            try:
                sla[key] = max(0, int(hours))
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail=f"Délai SLA invalide pour {key}.")
        merged = dict(alert_sla_hours(db))
        merged.update(sla)
        set_setting(db, SETTING_ALERT_SLA_HOURS, merged, updated_by=admin_user["username"])
    if payload.notification_events is not None:
        unknown = [e for e in payload.notification_events if e not in DEFAULT_NOTIFICATION_EVENTS]
        if unknown:
            raise HTTPException(status_code=400, detail=f"Événement de notification inconnu : {', '.join(unknown)}.")
        merged_events = dict(notification_events(db))
        merged_events.update({e: bool(v) for e, v in payload.notification_events.items()})
        set_setting(db, SETTING_NOTIFICATIONS, merged_events, updated_by=admin_user["username"])
    if payload.digest is not None:
        from fiskr.cron import parse_cron, CronError
        merged_digest = dict(digest_settings(db))
        if "enabled" in payload.digest:
            merged_digest["enabled"] = bool(payload.digest["enabled"])
        if "cron" in payload.digest:
            cron_expr = str(payload.digest.get("cron") or "").strip()
            if cron_expr:
                try:
                    parse_cron(cron_expr)
                except CronError as bad:
                    raise HTTPException(status_code=400, detail=f"Expression cron du digest invalide : {bad}")
                merged_digest["cron"] = cron_expr
        set_setting(db, SETTING_DIGEST, merged_digest, updated_by=admin_user["username"])
    after_state = _settings_payload(db)
    after_state.pop("sources", None)
    delta = {k: v for k, v in after_state.items() if before_state.get(k) != v}
    log_admin_action(db, admin_user["username"], "SETTINGS_UPDATED", target="ingestion",
                     before={k: before_state.get(k) for k in delta}, after=delta)
    db.commit()
    return {"message": "Réglages d'homologation mis à jour.", **_settings_payload(db)}

# ------------------ BLOCKING KEYS PAR CANAL ------------------

def _blocking_payload(db: Session) -> Dict[str, Any]:
    screening = blocking_layout_with_source(db, "SCREENING")
    filtering = blocking_layout_with_source(db, "FILTERING")
    return {
        "components": list(BLOCKING_COMPONENTS),
        "component_labels": {
            "COUNTRY_ISO": "Pays (ISO)",
            "ENTITY_TYPE": "Type d'entité (PP/PM)",
            "PHONETIC_FIRST": "Phonétique du nom",
        },
        "screening": {"layout": screening["layout"], "source": screening["source"]},
        "filtering": {"layout": filtering["layout"], "source": filtering["source"]},
        "active_screening_layout": watchlist_index_layout,
    }

class BlockingSettingsUpdate(BaseModel):
    screening_layout: Optional[List[str]] = None
    filtering_layout: Optional[List[str]] = None

@app.get("/api/settings/blocking")
async def get_blocking_settings(
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_blocking)
):
    """Layouts de blocking effectifs par canal (rôle blocking ou admin)."""
    return _blocking_payload(db)

@app.put("/api/settings/blocking")
async def update_blocking_settings(
    payload: BlockingSettingsUpdate,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_blocking)
):
    """
    Modifie a chaud les blocking keys d'un canal. Le changement du canal
    criblage RECHARGE immediatement le cache de production (cohérence
    index/sonde garantie).
    """
    def _validate(layout, label):
        if not isinstance(layout, list) or not layout:
            raise HTTPException(status_code=400, detail=f"{label} : liste de composantes non vide requise.")
        invalid = [c for c in layout if c not in BLOCKING_COMPONENTS]
        if invalid:
            raise HTTPException(
                status_code=400,
                detail=f"{label} : composante(s) inconnue(s) {', '.join(invalid)} "
                       f"(valeurs possibles : {', '.join(BLOCKING_COMPONENTS)})."
            )
        if len(set(layout)) != len(layout):
            raise HTTPException(status_code=400, detail=f"{label} : composantes en double.")

    reloaded = False
    if payload.screening_layout is not None:
        _validate(payload.screening_layout, "Layout criblage")
        set_setting(db, SETTING_BLOCKING_SCREENING, list(payload.screening_layout),
                    updated_by=param_user["username"])
        load_watchlist_cache(db)  # coherence index/sonde immediate
        reloaded = True
    if payload.filtering_layout is not None:
        _validate(payload.filtering_layout, "Layout filtrage")
        set_setting(db, SETTING_BLOCKING_FILTERING, list(payload.filtering_layout),
                    updated_by=param_user["username"])
    if payload.screening_layout is None and payload.filtering_layout is None:
        raise HTTPException(status_code=400, detail="Aucun layout fourni.")
    log_admin_action(
        db, param_user["username"], "BLOCKING_UPDATED", target="blocking",
        after={"screening_layout": payload.screening_layout, "filtering_layout": payload.filtering_layout},
    )
    db.commit()
    return {
        "message": "Blocking keys mises à jour." + (" Cache de criblage rechargé." if reloaded else ""),
        "cache_reloaded": reloaded,
        **_blocking_payload(db),
    }

# ------------------ REGLES ANTI-FAUX POSITIFS (Python, mode DEV) ------------------

def _fp_rule_summary(r: FpRule, with_code: bool = False) -> Dict[str, Any]:
    data = {
        "id": r.id,
        "channel": r.channel,
        "name": r.name,
        "description": r.description,
        "status": r.status,
        "enabled": bool(r.enabled),
        "run_order": r.run_order,
        "hit_count": r.hit_count or 0,
        "version": r.version,
        "replaces_rule_id": r.replaces_rule_id,
        "created_by": r.created_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_by": r.updated_by,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        "submitted_by": r.submitted_by,
        "validated_by": r.validated_by,
        "validation_comment": r.validation_comment,
    }
    if with_code:
        data["code"] = r.code
    return data


def _log_rule_change(db, rule: FpRule, action: str, username: str,
                     old_code: Optional[str] = None, comment: Optional[str] = None):
    db.add(FpRuleChange(
        rule_id=rule.id, rule_name=rule.name, channel=rule.channel, action=action,
        old_code=old_code, new_code=rule.code, comment=comment, changed_by=username,
    ))


def _run_rule_tests(db, rule: FpRule) -> Dict[str, Any]:
    """Rejoue les tests unitaires enregistres d'une regle. Met a jour leur etat."""
    tests = db.query(FpRuleTest).filter(FpRuleTest.rule_id == rule.id).all()
    passed = 0
    results = []
    now = datetime.utcnow()
    for t in tests:
        result, error = run_rule(rule.code, t.ctx or {})
        ok = (error is None and result == t.expected)
        t.last_result = result
        t.last_error = error
        t.last_run_at = now
        if ok:
            passed += 1
        results.append({
            "id": t.id, "name": t.name, "expected": t.expected,
            "result": result, "error": error, "passed": ok,
        })
    return {"total": len(tests), "passed": passed,
            "all_green": len(tests) > 0 and passed == len(tests), "results": results}


class FpRuleCreate(BaseModel):
    channel: str
    name: str
    description: Optional[str] = None
    code: Optional[str] = None
    run_order: int = 100

class FpRuleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None
    run_order: Optional[int] = None

class FpRuleDecision(BaseModel):
    comment: Optional[str] = None

class FpRuleTestCreate(BaseModel):
    name: str
    ctx: Dict[str, Any]
    expected: bool

class FpRuleBenchRequest(BaseModel):
    source: str = "history"      # history | panel
    panel_snapshot_id: Optional[str] = None
    sample_size: int = 200

class FpRuleValidateRequest(BaseModel):
    code: str

class FpRuleGenerateRequest(BaseModel):
    instruction: str
    channel: str = "SCREENING"


def _ctx_from_alert(db: Session, alert: Alert) -> Dict[str, Any]:
    """Reconstruit le contexte rule(ctx) d'une alerte reelle depuis son journal
    d'audit — utilise par le banc d'essai (rejeu historique) et par l'aide
    « creer un test depuis une alerte » de l'editeur de regles."""
    channel = alert.channel or "SCREENING"
    audit = db.query(AuditTrail).filter(AuditTrail.id == alert.audit_id).first()
    tree = (audit.decision_tree if audit else {}) or {}
    return {
        "channel": channel,
        "client_id": alert.client_id, "client_name": alert.client_name,
        "entity_id": alert.watchlist_entity_id, "entity_name": alert.watchlist_name,
        "list_type": alert.list_type, "final_score": float(alert.final_score or 0.0),
        "base_score": float(tree.get("base_score", 0.0)),
        "hard_match": bool(tree.get("hard_match_triggered", False)),
        "adjustments": tree.get("adjustments") or {},
        "client": None, "entity": (tree.get("watchlist_entity") or {}),
        "party": None, "message": None,
    }


@app.get("/api/fprules")
async def list_fp_rules(
    channel: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Regles anti-faux positifs (rôle rules ou admin)."""
    query = db.query(FpRule)
    if channel:
        query = query.filter(FpRule.channel == channel.strip().upper())
    if status_filter:
        query = query.filter(FpRule.status == status_filter.strip().upper())
    rows = query.order_by(FpRule.channel.asc(), FpRule.run_order.asc(), FpRule.id.asc()).all()
    return {"items": [_fp_rule_summary(r, with_code=True) for r in rows]}


@app.post("/api/fprules")
async def create_fp_rule(
    payload: FpRuleCreate,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Cree une regle en BROUILLON (jamais appliquee en production avant validation)."""
    channel = payload.channel.strip().upper()
    if channel not in FP_RULE_CHANNELS:
        raise HTTPException(status_code=400, detail=f"Canal inconnu ({', '.join(FP_RULE_CHANNELS)}).")
    code = payload.code if (payload.code and payload.code.strip()) else RULE_TEMPLATE
    try:
        compile_rule(code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    rule = FpRule(
        channel=channel, name=payload.name.strip() or "Règle sans nom",
        description=(payload.description or "").strip() or None,
        code=code, status="DRAFT", run_order=payload.run_order,
        created_by=param_user["username"],
    )
    db.add(rule)
    db.flush()
    _log_rule_change(db, rule, "CREATED", param_user["username"])
    db.commit()
    db.refresh(rule)
    return _fp_rule_summary(rule, with_code=True)


# NB : routes a segment fixe declarees AVANT les routes /{rule_id}
@app.post("/api/fprules/validate")
async def validate_fp_rule_code(
    payload: FpRuleValidateRequest,
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Validation syntaxique detaillee du code d'une regle (aide a l'edition) :
    retourne la ligne/colonne de l'erreur pour positionner le curseur."""
    return validate_rule_code(payload.code)


@app.get("/api/fprules/context-from-alert/{alert_id}")
async def fp_rule_context_from_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Contexte rule(ctx) reconstruit depuis une alerte reelle : pre-remplit
    un cas de test de regle sans saisir le JSON a la main."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    return {"alert_id": alert.id, "channel": alert.channel or "SCREENING",
            "ctx": _ctx_from_alert(db, alert)}


@app.post("/api/fprules/generate")
async def generate_fp_rule(
    payload: FpRuleGenerateRequest,
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """
    Genere le code d'une regle depuis une instruction en langage naturel
    (API Claude, si configuree). Le resultat n'est qu'un BROUILLON depose dans
    l'editeur : tests unitaires, soumission et validation 4-yeux s'appliquent
    inchanges. Erreurs explicites : 503 si l'IA n'est pas configuree (le front
    propose alors le formulaire structure), 422 si le code genere reste
    invalide (le code brut est restitue pour correction manuelle).
    """
    channel = payload.channel.strip().upper()
    if channel not in FP_RULE_CHANNELS:
        raise HTTPException(status_code=400, detail=f"Canal inconnu ({', '.join(FP_RULE_CHANNELS)}).")
    if not (payload.instruction or "").strip():
        raise HTTPException(status_code=400, detail="Décrivez la règle souhaitée en langage naturel.")
    try:
        result = generate_rule_code(payload.instruction, channel)
    except RuleGenerationUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except RuleGenerationFailed as e:
        raise HTTPException(status_code=422, detail={"message": str(e), "raw_code": e.raw_code})
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Appel au modèle impossible : {e}")
    return result


@app.put("/api/fprules/{rule_id}")
async def update_fp_rule(
    rule_id: int,
    payload: FpRuleUpdate,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """
    Modifie une regle BROUILLON. Sur une regle ACTIVE, cree une NOUVELLE
    version brouillon (branche de la production) sans toucher la version en
    service — elle prendra effet apres validation 4-yeux.
    """
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    if payload.code is not None:
        try:
            compile_rule(payload.code)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    if rule.status == "ACTIVE":
        # Branche : nouvelle version DRAFT rattachee a la version active
        draft = FpRule(
            channel=rule.channel,
            name=(payload.name or rule.name).strip(),
            description=(payload.description if payload.description is not None else rule.description),
            code=payload.code if payload.code is not None else rule.code,
            status="DRAFT",
            run_order=payload.run_order if payload.run_order is not None else rule.run_order,
            version=(rule.version or 1) + 1,
            replaces_rule_id=rule.id,
            created_by=param_user["username"],
        )
        db.add(draft)
        db.flush()
        _log_rule_change(db, draft, "CREATED", param_user["username"],
                         comment=f"Nouvelle version (branche de #{rule.id} v{rule.version} en production).")
        db.commit()
        db.refresh(draft)
        return _fp_rule_summary(draft, with_code=True)

    if rule.status != "DRAFT":
        raise HTTPException(
            status_code=409,
            detail="Seules les règles en brouillon sont modifiables (une règle en validation doit être renvoyée en brouillon)."
        )
    old_code = rule.code
    if payload.name is not None:
        rule.name = payload.name.strip() or rule.name
    if payload.description is not None:
        rule.description = payload.description.strip() or None
    if payload.code is not None:
        rule.code = payload.code
    if payload.run_order is not None:
        rule.run_order = payload.run_order
    rule.updated_by = param_user["username"]
    rule.updated_at = datetime.utcnow()
    _log_rule_change(db, rule, "UPDATED", param_user["username"], old_code=old_code)
    db.commit()
    db.refresh(rule)
    return _fp_rule_summary(rule, with_code=True)


@app.delete("/api/fprules/{rule_id}")
async def delete_fp_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Supprime une regle BROUILLON (les versions en production/validation ne se suppriment pas)."""
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    if rule.status != "DRAFT":
        raise HTTPException(status_code=409, detail="Seules les règles en brouillon peuvent être supprimées.")
    _log_rule_change(db, rule, "DELETED", param_user["username"])
    db.query(FpRuleTest).filter(FpRuleTest.rule_id == rule_id).delete(synchronize_session=False)
    db.delete(rule)
    db.commit()
    return {"message": "Règle supprimée."}


@app.get("/api/fprules/{rule_id}/changes")
async def get_fp_rule_changes(
    rule_id: int,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Journal immuable des modifications d'une regle (antichronologique)."""
    rows = db.query(FpRuleChange).filter(FpRuleChange.rule_id == rule_id) \
             .order_by(FpRuleChange.changed_at.desc(), FpRuleChange.id.desc()).all()
    return {"items": [
        {
            "action": c.action, "comment": c.comment,
            "changed_by": c.changed_by,
            "changed_at": c.changed_at.isoformat() if c.changed_at else None,
        } for c in rows
    ]}


@app.post("/api/fprules/{rule_id}/submit")
async def submit_fp_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Soumet une regle BROUILLON en validation. Exige des tests unitaires 100 % verts."""
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    if rule.status != "DRAFT":
        raise HTTPException(status_code=409, detail="Seule une règle en brouillon peut être soumise.")
    test_report = _run_rule_tests(db, rule)
    if not test_report["all_green"]:
        db.commit()  # persiste l'etat des tests rejoues
        raise HTTPException(
            status_code=400,
            detail=(f"Soumission refusée : {test_report['passed']}/{test_report['total']} test(s) unitaire(s) au vert. "
                    "Au moins un test enregistré et 100 % de tests verts sont exigés.")
        )
    rule.status = "PENDING_VALIDATION"
    rule.submitted_by = param_user["username"]
    rule.submitted_at = datetime.utcnow()
    _log_rule_change(db, rule, "SUBMITTED", param_user["username"])
    db.commit()
    db.refresh(rule)
    return {"message": "Règle soumise en validation.", "test_report": test_report,
            **_fp_rule_summary(rule, with_code=True)}


@app.post("/api/fprules/{rule_id}/validate")
async def validate_fp_rule(
    rule_id: int,
    payload: FpRuleDecision,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """
    Valide (4-yeux) une regle EN VALIDATION : elle devient ACTIVE et remplace
    (SUPERSEDED) la version qu'elle branche. Le valideur doit differer du
    soumetteur. Les tests unitaires sont rejoues (garde-fou).
    """
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    if rule.status != "PENDING_VALIDATION":
        raise HTTPException(status_code=409, detail="Cette règle n'est pas en attente de validation.")
    if rule.submitted_by and rule.submitted_by == param_user["username"]:
        raise HTTPException(
            status_code=403,
            detail="Contrôle 4-yeux : le validateur doit être différent du soumetteur."
        )
    test_report = _run_rule_tests(db, rule)
    if not test_report["all_green"]:
        db.commit()
        raise HTTPException(status_code=400, detail="Validation refusée : les tests unitaires ne sont plus tous au vert.")
    # Merge : supersede la version remplacee, le cas echeant
    if rule.replaces_rule_id:
        old = db.query(FpRule).filter(FpRule.id == rule.replaces_rule_id).first()
        if old and old.status == "ACTIVE":
            old.status = "SUPERSEDED"
            _log_rule_change(db, old, "SUPERSEDED" if False else "DISABLED", param_user["username"],
                             comment=f"Remplacée par #{rule.id} v{rule.version}.")
    rule.status = "ACTIVE"
    rule.enabled = True
    rule.validated_by = param_user["username"]
    rule.validated_at = datetime.utcnow()
    rule.validation_comment = (payload.comment or "").strip() or None
    _log_rule_change(db, rule, "VALIDATED", param_user["username"], comment=payload.comment)
    db.commit()
    db.refresh(rule)
    return {"message": "Règle validée et mise en production.", **_fp_rule_summary(rule, with_code=True)}


@app.post("/api/fprules/{rule_id}/reject")
async def reject_fp_rule(
    rule_id: int,
    payload: FpRuleDecision,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Renvoie une regle EN VALIDATION vers le brouillon (commentaire obligatoire)."""
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    if rule.status != "PENDING_VALIDATION":
        raise HTTPException(status_code=409, detail="Cette règle n'est pas en attente de validation.")
    if not (payload.comment or "").strip():
        raise HTTPException(status_code=400, detail="Un commentaire est obligatoire pour renvoyer une règle en brouillon.")
    rule.status = "DRAFT"
    rule.submitted_by = None
    rule.submitted_at = None
    _log_rule_change(db, rule, "REJECTED", param_user["username"], comment=payload.comment)
    db.commit()
    db.refresh(rule)
    return {"message": "Règle renvoyée en brouillon.", **_fp_rule_summary(rule, with_code=True)}


@app.post("/api/fprules/{rule_id}/toggle")
async def toggle_fp_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """Active/desactive une regle ACTIVE (interrupteur en production, journalise)."""
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    if rule.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="Seule une règle en production peut être activée/désactivée.")
    rule.enabled = not bool(rule.enabled)
    _log_rule_change(db, rule, "ENABLED" if rule.enabled else "DISABLED", param_user["username"])
    db.commit()
    db.refresh(rule)
    return {"message": f"Règle {'activée' if rule.enabled else 'désactivée'}.", **_fp_rule_summary(rule, with_code=True)}


# ---- Banc d'essai du mode DEV ----

@app.get("/api/fprules/{rule_id}/tests")
async def list_fp_rule_tests(
    rule_id: int,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    tests = db.query(FpRuleTest).filter(FpRuleTest.rule_id == rule_id) \
             .order_by(FpRuleTest.id.asc()).all()
    return {"items": [
        {
            "id": t.id, "name": t.name, "ctx": t.ctx, "expected": t.expected,
            "last_result": t.last_result, "last_error": t.last_error,
            "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
        } for t in tests
    ]}


@app.post("/api/fprules/{rule_id}/tests")
async def create_fp_rule_test(
    rule_id: int,
    payload: FpRuleTestCreate,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    test = FpRuleTest(
        rule_id=rule_id, name=payload.name.strip() or "Cas de test",
        ctx=payload.ctx, expected=bool(payload.expected),
        created_by=param_user["username"],
    )
    db.add(test)
    db.commit()
    db.refresh(test)
    return {"id": test.id, "name": test.name, "ctx": test.ctx, "expected": test.expected}


@app.delete("/api/fprules/{rule_id}/tests/{test_id}")
async def delete_fp_rule_test(
    rule_id: int,
    test_id: int,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    test = db.query(FpRuleTest).filter(FpRuleTest.id == test_id, FpRuleTest.rule_id == rule_id).first()
    if not test:
        raise HTTPException(status_code=404, detail="Cas de test introuvable.")
    db.delete(test)
    db.commit()
    return {"message": "Cas de test supprimé."}


@app.post("/api/fprules/{rule_id}/tests/run")
async def run_fp_rule_tests(
    rule_id: int,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    report = _run_rule_tests(db, rule)
    db.commit()
    return report


@app.post("/api/fprules/{rule_id}/bench")
async def bench_fp_rule(
    rule_id: int,
    payload: FpRuleBenchRequest,
    db: Session = Depends(get_db),
    param_user: Dict[str, Any] = Depends(require_fprules)
):
    """
    Banc d'essai a blanc d'une regle (mode DEV, sans toucher la production) :
    - source 'history' : rejeu des N dernieres alertes reelles du canal, avec
      garde-fou VRAIS POSITIFS (alertes CLOSED_CONFIRMED qui seraient supprimees) ;
    - source 'panel' : criblage a blanc d'un panel de pseudo-clients (canal
      SCREENING uniquement) — chaque hit devient un contexte de test.
    """
    rule = db.query(FpRule).filter(FpRule.id == rule_id).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Règle introuvable.")
    try:
        compile_rule(rule.code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    suppressed, kept, errors = 0, 0, 0
    true_positive_hits, samples = [], []

    if payload.source == "panel":
        if rule.channel != "SCREENING":
            raise HTTPException(status_code=400, detail="Le banc d'essai par panel ne concerne que le canal criblage.")
        panel = db.query(Snapshot).filter(Snapshot.snapshot_id == payload.panel_snapshot_id).first()
        if not panel or panel.file_type not in PANEL_FILE_TYPES or panel.status != "READY":
            raise HTTPException(status_code=400, detail="Panel introuvable (base clients ou panel de test généré).")
        from fiskr.backtest import _panel_clients, _client_label
        from fiskr.rescreen import _entity_dicts
        prod_ids = [s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_type.in_(WATCHLIST_FILE_TYPES), Snapshot.status == "READY").all()]
        entities = _entity_dicts(db, prod_ids) if prod_ids else []
        screening_cfg = blocking_config_for(watchlist_index_layout)
        index: Dict[str, List[Dict[str, Any]]] = {}
        for ent in entities:
            for key in generate_blocking_keys(ent, screening_cfg):
                index.setdefault(key, []).append(ent)
        for client in _panel_clients(db, panel.snapshot_id):
            cands = {}
            for key in generate_blocking_keys(client, screening_cfg):
                for ent in index.get(key, []):
                    cands[ent["entity_id"]] = ent
            best, best_ent = None, None
            for ent in cands.values():
                sc = match_entities(client, ent, config)
                if best is None or sc["final_score"] > best["final_score"]:
                    best, best_ent = sc, ent
            if not best or best.get("status") != "ALERT":
                continue
            ctx = build_screening_ctx(client, best_ent, best)
            result, error = run_rule(rule.code, ctx)
            if error:
                errors += 1
            elif result:
                suppressed += 1
                if len(samples) < 50:
                    samples.append({"client_name": _client_label(client), "entity_name": best_ent.get("primary_name"),
                                    "final_score": round(best["final_score"], 1)})
            else:
                kept += 1
    else:
        # Rejeu de l'historique reel du canal
        alerts = db.query(Alert).filter(
            (Alert.channel == rule.channel) |
            (Alert.channel.is_(None) if rule.channel == "SCREENING" else False)
        ).order_by(Alert.created_at.desc()).limit(max(1, min(payload.sample_size, 2000))).all()
        for a in alerts:
            ctx = _ctx_from_alert(db, a)
            ctx["channel"] = rule.channel
            result, error = run_rule(rule.code, ctx)
            if error:
                errors += 1
            elif result:
                suppressed += 1
                if a.status == "CLOSED_CONFIRMED":
                    true_positive_hits.append({"alert_id": a.id, "client_name": a.client_name,
                                               "entity_name": a.watchlist_name, "final_score": a.final_score})
                if len(samples) < 50:
                    samples.append({"alert_id": a.id, "client_name": a.client_name,
                                    "entity_name": a.watchlist_name, "final_score": a.final_score,
                                    "status": a.status})
            else:
                kept += 1

    return {
        "source": payload.source,
        "suppressed": suppressed,
        "kept": kept,
        "errors": errors,
        "true_positive_hits": true_positive_hits,
        "samples": samples,
    }


def _get_pending_snapshot(db: Session, snapshot_id: str) -> Snapshot:
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snapshot_id).first()
    if not snap:
        raise HTTPException(status_code=404, detail="Snapshot introuvable.")
    if snap.file_type not in WATCHLIST_FILE_TYPES:
        raise HTTPException(status_code=400, detail="Ce snapshot n'est pas une watchlist.")
    if snap.status != "PENDING_REVIEW":
        raise HTTPException(
            status_code=409,
            detail=f"Ce snapshot n'est pas en attente d'homologation (statut: {snap.status})."
        )
    return snap

def _snapshot_summary(db: Session, snap: Snapshot) -> Dict[str, Any]:
    excluded_count = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == snap.snapshot_id,
        WatchlistEntity.excluded.is_(True)
    ).count()
    return {
        "snapshot_id": snap.snapshot_id,
        "file_type": snap.file_type,
        "file_name": snap.file_name,
        "file_hash": snap.file_hash,
        "record_count": snap.record_count,
        "uploaded_at": snap.uploaded_at.isoformat() if snap.uploaded_at else None,
        "status": snap.status,
        "excluded_count": excluded_count,
        "reviewed_by": snap.reviewed_by,
        "reviewed_at": snap.reviewed_at.isoformat() if snap.reviewed_at else None,
        "review_comment": snap.review_comment,
    }

@app.get("/api/review/pending")
async def list_pending_reviews(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Snapshots watchlist en attente d'homologation (plus recents d'abord)."""
    snaps = db.query(Snapshot).filter(
        Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
        Snapshot.status == "PENDING_REVIEW"
    ).order_by(Snapshot.uploaded_at.desc()).all()
    return {"pending": [_snapshot_summary(db, s) for s in snaps]}

@app.get("/api/review/snapshots/{snapshot_id}")
async def get_review_detail(
    snapshot_id: str,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Detail d'un snapshot en attente : metadonnees + delta calcule a la volee
    par rapport a la liste actuellement en production (toujours a jour).
    """
    snap = _get_pending_snapshot(db, snapshot_id)
    production = _latest_ready_snapshot(db, snap.file_type)
    old_entities = _snapshot_entity_dicts(db, production.snapshot_id) if production else []
    new_entities = _snapshot_entity_dicts(db, snapshot_id)
    delta = calculate_delta(old_entities, new_entities, "entity_id")
    return {
        **_snapshot_summary(db, snap),
        "production_snapshot_id": production.snapshot_id if production else None,
        "delta_summary": delta["summary"],
        "delta_details": _truncate_delta_details(delta),
        "backtest_report": snap.backtest_report,
        "backtest_at": snap.backtest_at.isoformat() if snap.backtest_at else None,
        "backtest_by": snap.backtest_by,
    }

class BacktestRequest(BaseModel):
    panel_snapshot_id: str
    # Regle anti-FP candidate a evaluer (ajoutee cote candidat uniquement) :
    # l'ecart chiffre montre l'effet de la regle avant sa validation 4-yeux
    candidate_rule_id: Optional[int] = None

@app.post("/api/review/snapshots/{snapshot_id}/backtest")
async def run_review_backtest(
    snapshot_id: str,
    payload: BacktestRequest,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Cahier de tests d'homologation : criblage A/B A BLANC du panel choisi
    contre la production actuelle et contre l'univers candidat (le snapshot en
    attente remplacant les listes du meme type). Mesure l'ecart de taux
    d'interception, liste les nouvelles alertes et les alertes resolues.
    Aucune alerte ni ligne d'audit n'est creee. Le rapport est archive avec le
    snapshot (auditable apres promotion).
    """
    snap = _get_pending_snapshot(db, snapshot_id)
    panel = db.query(Snapshot).filter(Snapshot.snapshot_id == payload.panel_snapshot_id).first()
    if not panel or panel.file_type not in PANEL_FILE_TYPES or panel.status != "READY":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Panel introuvable : choisissez une base clients (CLIENT_BASE) ou un panel de test généré."
        )
    if not (panel.record_count or 0):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Le panel choisi est vide.")

    try:
        report = run_backtest(db, snap, panel.snapshot_id,
                              threshold_pct=backtest_max_gap_pct(db),
                              executed_by=reviewer["username"],
                              candidate_rule_id=payload.candidate_rule_id)
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    snap.backtest_report = report
    snap.backtest_at = datetime.utcnow()
    snap.backtest_by = reviewer["username"]
    db.commit()
    return report

@app.get("/api/testpanels")
async def list_test_panels(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Panels utilisables par le cahier de tests : bases clients reelles
    (CLIENT_BASE) et panels de pseudo-clients generes (CLIENT_TEST_PANEL).
    """
    snaps = db.query(Snapshot).filter(
        Snapshot.file_type.in_(PANEL_FILE_TYPES),
        Snapshot.status == "READY"
    ).order_by(Snapshot.uploaded_at.desc()).all()
    return {
        "panels": [
            {
                "snapshot_id": s.snapshot_id,
                "file_type": s.file_type,
                "file_name": s.file_name,
                "record_count": s.record_count,
                "uploaded_at": s.uploaded_at.isoformat() if s.uploaded_at else None,
                "generated": s.file_type == TEST_PANEL_FILE_TYPE,
            }
            for s in snaps
        ]
    }

class TestPanelGenerateRequest(BaseModel):
    # Snapshot candidat dont les entites servent de base aux hits attendus
    snapshot_id: Optional[str] = None
    size: int = 500
    seed: Optional[int] = None

@app.post("/api/testpanels/generate")
async def generate_test_panel_endpoint(
    payload: TestPanelGenerateRequest,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Genere un panel de pseudo-clients (copies exactes, variantes typo/inversion,
    quasi-collisions, clients neutres) derive du snapshot candidat et de la
    production. Stocke en CLIENT_TEST_PANEL : jamais repris par le re-criblage
    du referentiel clients reel.
    """
    if not (50 <= payload.size <= 5000):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="La taille du panel doit être entre 50 et 5000.")
    source_ids = []
    if payload.snapshot_id:
        source = db.query(Snapshot).filter(Snapshot.snapshot_id == payload.snapshot_id).first()
        if not source or source.file_type not in WATCHLIST_FILE_TYPES:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Snapshot source introuvable ou non-watchlist.")
        source_ids.append(source.snapshot_id)
    source_ids.extend(
        s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
            Snapshot.status == "READY"
        ).all()
    )
    try:
        snap = generate_test_panel(db, source_ids, size=payload.size,
                                   seed=payload.seed, created_by=reviewer["username"])
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    return {
        "message": f"Panel de {snap.record_count} pseudo-clients généré.",
        "snapshot_id": snap.snapshot_id,
        "file_name": snap.file_name,
        "record_count": snap.record_count,
    }

@app.get("/api/review/snapshots/{snapshot_id}/entities")
async def list_review_entities(
    snapshot_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Entites paginees d'un snapshot en attente, avec leur etat d'exclusion."""
    _get_pending_snapshot(db, snapshot_id)
    query = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == snapshot_id)
    if search:
        needle = f"%{search.strip()}%"
        query = query.filter(
            (WatchlistEntity.primary_name.ilike(needle)) | (WatchlistEntity.entity_id.ilike(needle))
        )
    total = query.count()
    rows = query.order_by(WatchlistEntity.id.asc()).offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": r.id,
                "entity_id": r.entity_id,
                "entity_type": r.entity_type,
                "primary_name": r.primary_name,
                "excluded": bool(r.excluded),
                "exclusion_justification": r.exclusion_justification,
                "exclusion_file_name": r.exclusion_file_name,
                "excluded_by": r.excluded_by,
            }
            for r in rows
        ],
    }

@app.post("/api/review/snapshots/{snapshot_id}/exclusions")
async def set_review_exclusions(
    snapshot_id: str,
    entity_ids: str = Form(...),
    justification: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Exclut des entites d'un snapshot en attente d'homologation. La justification
    texte et la piece jointe sont exigees selon les reglages modulaires
    (review.exclusion_justification_required / review.exclusion_file_required).
    """
    snap = _get_pending_snapshot(db, snapshot_id)
    try:
        ids = json.loads(entity_ids)
        assert isinstance(ids, list) and all(isinstance(i, int) for i in ids) and ids
    except (json.JSONDecodeError, AssertionError):
        raise HTTPException(status_code=400, detail="entity_ids doit être une liste JSON non vide d'entiers.")

    rows = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == snapshot_id,
        WatchlistEntity.id.in_(ids)
    ).all()
    if len(rows) != len(set(ids)):
        raise HTTPException(status_code=400, detail="Certaines entités n'appartiennent pas à ce snapshot.")

    requirements = exclusion_requirements(db)
    justification = (justification or "").strip()
    if requirements["justification_required"] and not justification:
        raise HTTPException(
            status_code=400,
            detail="Une justification est obligatoire pour exclure une entité (réglage actif)."
        )
    if requirements["file_required"] and (file is None or not file.filename):
        raise HTTPException(
            status_code=400,
            detail="Une pièce jointe justificative est obligatoire pour exclure une entité (réglage actif)."
        )

    evidence_name = None
    evidence_path = None
    if file is not None and file.filename:
        safe_name = os.path.basename(file.filename).replace("..", "_")
        target_dir = EXCLUSION_EVIDENCE_DIR / snap.snapshot_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        evidence_name = safe_name
        evidence_path = str(target_path)

    now = datetime.utcnow()
    for row in rows:
        row.excluded = True
        row.exclusion_justification = justification or None
        row.exclusion_file_name = evidence_name
        row.exclusion_file_path = evidence_path
        row.excluded_by = reviewer["username"]
        row.excluded_at = now
    db.commit()
    return {
        "message": f"{len(rows)} entité(s) exclue(s) du snapshot.",
        "snapshot_id": snapshot_id,
        "excluded_ids": sorted(r.id for r in rows),
    }

@app.post("/api/review/snapshots/{snapshot_id}/exclusions/remove")
async def remove_review_exclusions(
    snapshot_id: str,
    payload: ExclusionRemoveRequest,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """Annule des exclusions posees sur un snapshot encore en attente d'homologation."""
    _get_pending_snapshot(db, snapshot_id)
    if not payload.entity_ids:
        raise HTTPException(status_code=400, detail="entity_ids ne peut pas être vide.")
    rows = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == snapshot_id,
        WatchlistEntity.id.in_(payload.entity_ids)
    ).all()
    if len(rows) != len(set(payload.entity_ids)):
        raise HTTPException(status_code=400, detail="Certaines entités n'appartiennent pas à ce snapshot.")
    for row in rows:
        row.excluded = False
        row.exclusion_justification = None
        row.exclusion_file_name = None
        row.exclusion_file_path = None
        row.excluded_by = None
        row.excluded_at = None
    db.commit()
    return {"message": f"{len(rows)} exclusion(s) annulée(s).", "snapshot_id": snapshot_id}

@app.get("/api/review/exclusion-evidence/{entity_pk}")
async def download_exclusion_evidence(
    entity_pk: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Telecharge la piece justificative associee a l'exclusion d'une entite (audit)."""
    row = db.query(WatchlistEntity).filter(WatchlistEntity.id == entity_pk).first()
    if not row or not row.exclusion_file_path:
        raise HTTPException(status_code=404, detail="Aucune pièce justificative pour cette entité.")
    file_path = Path(row.exclusion_file_path)
    # La piece doit rester dans le repertoire d'archivage des exclusions
    if not file_path.exists() or EXCLUSION_EVIDENCE_DIR.resolve() not in file_path.resolve().parents:
        raise HTTPException(status_code=404, detail="Pièce justificative introuvable.")
    return FileResponse(str(file_path), filename=row.exclusion_file_name or file_path.name)

@app.post("/api/review/snapshots/{snapshot_id}/approve")
async def approve_pending_snapshot(
    snapshot_id: str,
    payload: ReviewDecisionRequest,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Approuve un snapshot en attente : promotion en production (READY), les
    anciens snapshots du meme type passent en SUPERSEDED et le cache de
    criblage est recharge sans les entites exclues.
    """
    snap = _get_pending_snapshot(db, snapshot_id)

    # Filet de securite : si les exigences de justification ont durci depuis la
    # pose des exclusions, on refuse la promotion tant qu'elles ne sont pas conformes.
    requirements = exclusion_requirements(db)
    excluded_rows = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == snapshot_id,
        WatchlistEntity.excluded.is_(True)
    ).all()
    if requirements["justification_required"] and any(not (r.exclusion_justification or "").strip() for r in excluded_rows):
        raise HTTPException(
            status_code=400,
            detail="Des exclusions n'ont pas de justification alors que le réglage l'exige. Complétez-les avant d'approuver."
        )
    if requirements["file_required"] and any(not r.exclusion_file_path for r in excluded_rows):
        raise HTTPException(
            status_code=400,
            detail="Des exclusions n'ont pas de pièce jointe alors que le réglage l'exige. Complétez-les avant d'approuver."
        )

    # Cahier de tests obligatoire (reglage) : un rapport au verdict OK est exige
    if backtest_required(db):
        if not snap.backtest_report:
            raise HTTPException(
                status_code=400,
                detail="Le cahier de tests est obligatoire avant la mise en production (réglage actif). Exécutez-le depuis l'étape « Cahier de tests »."
            )
        if snap.backtest_report.get("verdict") != "OK":
            raise HTTPException(
                status_code=400,
                detail="Le dernier cahier de tests signale un écart de taux d'interception au-delà du seuil toléré. Posez des Good Guys (liste blanche) ou des exclusions, puis relancez le cahier de tests."
            )

    # Snapshot de production remplace (pour cibler le re-criblage post-delta)
    previous_prod = _latest_ready_snapshot(db, snap.file_type)

    snap.status = "READY"
    snap.reviewed_by = reviewer["username"]
    snap.reviewed_at = datetime.utcnow()
    snap.review_comment = (payload.comment or "").strip() or None
    _supersede_previous_snapshots(db, snap.file_type, snap.snapshot_id)
    db.commit()
    load_watchlist_cache(db)

    rescreen_result = None
    if auto_rescreen_enabled(db):
        rescreen_result = rescreen_after_snapshot_change(
            db, snap.file_type, snap.snapshot_id,
            previous_prod.snapshot_id if previous_prod else None
        )
    return {
        "message": "Snapshot approuvé et promu en production.",
        "snapshot_id": snapshot_id,
        "status": snap.status,
        "excluded_count": len(excluded_rows),
        "rescreen": rescreen_result,
    }

@app.post("/api/review/snapshots/{snapshot_id}/reject")
async def reject_pending_snapshot(
    snapshot_id: str,
    payload: ReviewDecisionRequest,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Rejette un snapshot en attente : il n'entrera jamais en production. Les
    entites sont conservees en base pour l'audit (meme retention que SUPERSEDED).
    """
    snap = _get_pending_snapshot(db, snapshot_id)
    comment = (payload.comment or "").strip()
    if not comment:
        raise HTTPException(status_code=400, detail="Un commentaire est requis pour rejeter un snapshot.")
    snap.status = "REJECTED"
    snap.reviewed_by = reviewer["username"]
    snap.reviewed_at = datetime.utcnow()
    snap.review_comment = comment
    db.commit()
    return {
        "message": "Snapshot rejeté. Il ne sera pas mis en production.",
        "snapshot_id": snapshot_id,
        "status": snap.status,
    }

# ------------------ GESTION DES ALERTES (CYCLE DE VIE + 4-YEUX) ------------------

class AlertAssignRequest(BaseModel):
    assignee: Optional[str] = None

class AlertCommentRequest(BaseModel):
    comment: str

class AlertProposeRequest(BaseModel):
    decision: str  # CONFIRMED | FALSE_POSITIVE
    comment: str

class AlertValidateRequest(BaseModel):
    approve: bool
    comment: Optional[str] = None

def _alert_summary(alert: Alert) -> Dict[str, Any]:
    return {
        "id": alert.id,
        "audit_id": alert.audit_id,
        "channel": alert.channel or "SCREENING",
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
        "client_id": alert.client_id,
        "client_name": alert.client_name,
        "watchlist_entity_id": alert.watchlist_entity_id,
        "watchlist_name": alert.watchlist_name,
        "final_score": alert.final_score,
        "list_type": alert.list_type,
        "status": alert.status,
        "assigned_to": alert.assigned_to,
        "proposed_decision": alert.proposed_decision,
        "proposed_by": alert.proposed_by,
        "proposed_at": alert.proposed_at.isoformat() if alert.proposed_at else None,
        "proposal_comment": alert.proposal_comment,
        "decided_by": alert.decided_by,
        "decided_at": alert.decided_at.isoformat() if alert.decided_at else None,
        "decision_comment": alert.decision_comment,
        "priority": alert.priority,
        "due_at": alert.due_at.isoformat() if alert.due_at else None,
        # En retard = echeance depassee ET toujours ouverte
        "overdue": bool(
            alert.due_at and alert.due_at < datetime.utcnow()
            and alert.status in ALERT_OPEN_STATUSES
        ),
    }

def _apply_list_type_filter(query, column, list_type_param: Optional[str]):
    """
    Filtre CSV par type de liste (motif de status_filter). La valeur speciale
    UNKNOWN cible les enregistrements sans type (anterieurs a la colonne).
    """
    if not list_type_param:
        return query
    values = [v.strip().upper() for v in list_type_param.split(",") if v.strip()]
    if not values:
        return query
    conditions = []
    concrete = [v for v in values if v != "UNKNOWN"]
    if concrete:
        conditions.append(column.in_(concrete))
    if "UNKNOWN" in values:
        conditions.append(column.is_(None))
    from sqlalchemy import or_
    return query.filter(or_(*conditions))

def _get_open_alert(db: Session, alert_id: int) -> Alert:
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    if alert.status in ALERT_CLOSED_STATUSES:
        raise HTTPException(status_code=409, detail=f"Alerte déjà close ({alert.status}).")
    return alert

def _log_alert_event(db: Session, alert_id: int, username: str, action: str, detail: str = "") -> None:
    db.add(AlertEvent(alert_id=alert_id, username=username, action=action, detail=detail or None))

# ------------------ DOSSIER D'INVESTIGATION ------------------

class ChecklistToggleRequest(BaseModel):
    index: int
    done: bool

class ChecklistSettingsUpdate(BaseModel):
    items: List[str]

def _checklist_payload(db: Session, alert: Alert) -> List[Dict[str, Any]]:
    """Checklist effective de l'alerte : items du reglage + etat coche par item."""
    items = investigation_checklist(db)
    state = alert.checklist_state or {}
    return [
        {"index": i, "label": label, **(
            {"done": bool(item_state.get("done")), "by": item_state.get("by"),
             "at": item_state.get("at")}
            if isinstance(item_state := state.get(str(i)), dict)
            else {"done": False, "by": None, "at": None})}
        for i, label in enumerate(items)
    ]

@app.get("/api/alerts/{alert_id}/casefile")
async def get_alert_casefile(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Dossier d'investigation : tout ce qu'un analyste doit examiner autour
    d'une alerte, en un seul appel — alerte, arbre de decision, historique
    d'actions, pieces jointes, checklist d'instruction, contexte client
    (criblages et alertes anterieurs, liste blanche) et relations/risque
    herite de la fiche listee (regle des 50 %).
    """
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    audit = db.query(AuditTrail).filter(AuditTrail.id == alert.audit_id).first()
    events = db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id) \
               .order_by(AlertEvent.timestamp.asc(), AlertEvent.id.asc()).all()
    attachments = db.query(AlertAttachment).filter(AlertAttachment.alert_id == alert_id) \
                    .order_by(AlertAttachment.uploaded_at.asc()).all()

    client_context = None
    if alert.client_id and not str(alert.client_id).startswith("TXN:"):
        past_audits = db.query(AuditTrail).filter(AuditTrail.client_id == alert.client_id).count()
        past_alerts = db.query(Alert).filter(
            Alert.client_id == alert.client_id, Alert.id != alert.id).count()
        wl_pairs = db.query(WhitelistPair).filter(
            WhitelistPair.client_id == alert.client_id).count()
        client_context = {"client_id": alert.client_id, "screenings": past_audits,
                          "other_alerts": past_alerts, "whitelist_pairs": wl_pairs}

    relations = db.query(EntityRelationship).filter(
        (EntityRelationship.from_entity_id == alert.watchlist_entity_id)
        | (EntityRelationship.to_entity_id == alert.watchlist_entity_id)).count()
    inherited = compute_inherited_risk(db, alert.watchlist_entity_id, max_depth=2)

    return {
        **_alert_summary(alert),
        "decision_tree": audit.decision_tree if audit else None,
        "events": [
            {"timestamp": e.timestamp.isoformat() if e.timestamp else None,
             "username": e.username, "action": e.action, "detail": e.detail}
            for e in events
        ],
        "attachments": [
            {"id": att.id, "file_name": att.file_name, "comment": att.comment,
             "uploaded_by": att.uploaded_by,
             "uploaded_at": att.uploaded_at.isoformat() if att.uploaded_at else None}
            for att in attachments
        ],
        "checklist": _checklist_payload(db, alert),
        "client_context": client_context,
        "entity_relations": {"count": relations, "inherited_risk": inherited},
    }

@app.post("/api/alerts/{alert_id}/checklist")
async def toggle_checklist_item(
    alert_id: int,
    payload: ChecklistToggleRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Coche/decoche un point de controle du dossier (trace CHECKLIST,
    append-only comme toute action d'alerte)."""
    alert = _get_open_alert(db, alert_id)
    items = investigation_checklist(db)
    if not (0 <= payload.index < len(items)):
        raise HTTPException(status_code=400, detail="Point de contrôle inconnu.")
    state = dict(alert.checklist_state or {})
    state[str(payload.index)] = {
        "done": bool(payload.done), "by": current_user["username"],
        "at": datetime.utcnow().isoformat(),
    }
    alert.checklist_state = state
    _log_alert_event(db, alert.id, current_user["username"], "CHECKLIST",
                     f"{'☑' if payload.done else '☐'} {items[payload.index]}")
    db.commit()
    done_count = sum(1 for s in state.values() if isinstance(s, dict) and s.get("done"))
    return {"message": "Point de contrôle mis à jour.",
            "done": done_count, "total": len(items),
            "checklist": _checklist_payload(db, alert)}

@app.get("/api/settings/checklist")
async def get_checklist_settings(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Points de controle d'instruction effectifs (reglage a chaud)."""
    return {"items": investigation_checklist(db), "default": list(DEFAULT_CHECKLIST)}

@app.put("/api/settings/checklist")
async def update_checklist_settings(
    payload: ChecklistSettingsUpdate,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """Regle la checklist d'instruction (1 a 20 points, journalise).
    Liste vide = retour a la checklist par defaut."""
    items = [i.strip() for i in payload.items if isinstance(i, str) and i.strip()]
    if len(items) > 20 or any(len(i) > 200 for i in items):
        raise HTTPException(status_code=400,
                            detail="Checklist invalide : 20 points maximum, 200 caractères par point.")
    before = investigation_checklist(db)
    set_setting(db, SETTING_CHECKLIST, items or None, updated_by=admin_user["username"])
    after = investigation_checklist(db)
    if before != after:
        log_admin_action(db, admin_user["username"], "SETTINGS_UPDATED", target="checklist",
                         before={"items": before}, after={"items": after})
        db.commit()
    return {"message": "Checklist d'instruction mise à jour.", "items": after}

@app.get("/api/alerts/{alert_id}/casefile/print")
async def print_alert_casefile(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Dossier d'investigation HTML autonome imprimable (impression -> PDF) :
    la piece unique a remettre au regulateur pour une alerte."""
    from html import escape
    casefile = await get_alert_casefile(alert_id, db, current_user)

    def esc(value):
        return escape(str(value)) if value not in (None, "") else "—"

    checklist_html = "\n".join(
        f"<li>{'☑' if item['done'] else '☐'} {esc(item['label'])}"
        + (f" <small>— {esc(item['by'])}</small>" if item["done"] and item["by"] else "")
        + "</li>"
        for item in casefile["checklist"])
    events_html = "\n".join(
        f"<tr><td>{esc((e['timestamp'] or '')[:19].replace('T', ' '))}</td>"
        f"<td>@{esc(e['username'])}</td><td>{esc(e['action'])}</td><td>{esc(e['detail'])}</td></tr>"
        for e in casefile["events"])
    attachments_html = "\n".join(
        f"<li>{esc(att['file_name'])} <small>({esc(att['uploaded_by'])})</small></li>"
        for att in casefile["attachments"]) or "<li>Aucune pièce jointe.</li>"
    adjustments = ((casefile.get("decision_tree") or {}).get("adjustments")) or {}
    adjustments_html = "\n".join(
        f"<tr><td>{esc(key)}</td><td>{esc(value.get('score'))}</td><td>{esc(value.get('description'))}</td></tr>"
        for key, value in adjustments.items())
    inherited = casefile["entity_relations"]["inherited_risk"]
    inherited_html = ("<p><strong>⚠ Règle des 50 % :</strong> " + "; ".join(
        esc(r.get("owner_name") or r.get("owner_id")) for r in inherited) + "</p>") if inherited else ""
    context = casefile.get("client_context")
    context_html = (
        f"<p>Criblages antérieurs : {context['screenings']} — autres alertes : "
        f"{context['other_alerts']} — paires de liste blanche : {context['whitelist_pairs']}</p>"
        if context else "<p>Partie de transaction (pas de dossier client).</p>")

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Fiskr — Dossier d'investigation — Alerte #{casefile['id']}</title>
<style>
body {{ font-family: Arial, sans-serif; color: #111; margin: 2rem auto; max-width: 860px; }}
h1 {{ font-size: 1.35rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.5rem; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 0.5rem; }}
th, td {{ border: 1px solid #ccc; padding: 5px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f0f0f0; }} ul {{ margin: 0.5rem 0 0 1.2rem; }} .sub {{ color: #555; }}
@media print {{ .no-print {{ display: none; }} body {{ margin: 0; }} }}
</style></head><body>
<button class="no-print" onclick="window.print()">🖨 Imprimer / PDF</button>
<h1>Dossier d'investigation — Alerte #{casefile['id']}</h1>
<p class="sub">{esc(casefile['client_name'])} × {esc(casefile['watchlist_name'])}
 ({esc(casefile['watchlist_entity_id'])}) — score {casefile['final_score']:.1f} % —
 statut {esc(casefile['status'])} — priorité {esc(casefile['priority'])} —
 généré le {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC.</p>
<h2>Checklist d'instruction</h2><ul>{checklist_html}</ul>
<h2>Contexte client</h2>{context_html}
<h2>Relations de la fiche listée</h2>
<p>{casefile['entity_relations']['count']} relation(s) connue(s).</p>{inherited_html}
<h2>Ajustements contextuels du score</h2>
<table><thead><tr><th>Critère</th><th>Score</th><th>Détail</th></tr></thead>
<tbody>{adjustments_html}</tbody></table>
<h2>Pièces jointes</h2><ul>{attachments_html}</ul>
<h2>Historique des actions (append-only)</h2>
<table><thead><tr><th>Date</th><th>Utilisateur</th><th>Action</th><th>Détail</th></tr></thead>
<tbody>{events_html}</tbody></table>
</body></html>"""
    return HTMLResponse(content=html)

# ------------------ PROJET DE DECLARATION DE SOUPCON (TRACFIN) ------------------

def _institution_config() -> Dict[str, Any]:
    """Identite de l'etablissement declarant (rubrique declarant du projet)."""
    cfg = config.get("institution", {}) or {}
    return {
        "name": str(cfg.get("name") or "").strip(),
        "siren": str(cfg.get("siren") or "").strip(),
        "correspondent_name": str(cfg.get("correspondent_name") or "").strip(),
        "correspondent_email": str(cfg.get("correspondent_email") or "").strip(),
        "correspondent_phone": str(cfg.get("correspondent_phone") or "").strip(),
    }


STR_DISCLAIMER = (
    "PROJET généré automatiquement par Fiskr à partir des données tracées au "
    "journal d'audit. Il doit être relu, complété et validé par le correspondant "
    "TRACFIN désigné avant toute télédéclaration sur ERMES — aucune transmission "
    "automatique n'est effectuée."
)


def _build_str_draft(db: Session, alert: Alert, username: str) -> Dict[str, Any]:
    """Assemble le projet de declaration de soupcon structure aux rubriques
    d'une teledeclaration TRACFIN, exclusivement depuis les donnees tracees."""
    audit = db.query(AuditTrail).filter(AuditTrail.id == alert.audit_id).first()
    tree = (audit.decision_tree if audit else {}) or {}
    events = db.query(AlertEvent).filter(AlertEvent.alert_id == alert.id) \
               .order_by(AlertEvent.timestamp.asc(), AlertEvent.id.asc()).all()

    # Personne concernee : fiche KYC du dernier referentiel en production
    # (criblage clients) ou partie du message (filtrage transactionnel)
    person: Dict[str, Any] = {"nom_complet": alert.client_name, "reference_interne": alert.client_id}
    kyc_row = None
    if alert.client_id and not str(alert.client_id).startswith("TXN:"):
        kyc_row = (
            db.query(ClientEntity, Snapshot)
              .join(Snapshot, ClientEntity.snapshot_id == Snapshot.snapshot_id)
              .filter(ClientEntity.client_id == alert.client_id, Snapshot.status == "READY")
              .order_by(Snapshot.uploaded_at.desc()).first()
        )
    if kyc_row:
        entity, _snap = kyc_row
        person.update({
            "type": "Personne physique" if entity.client_type == "PP" else "Personne morale",
            "prenom": entity.client_first_name, "nom": entity.client_last_name,
            "raison_sociale": entity.client_company_name,
            "date_naissance": entity.client_dob,
            "lieu_naissance": entity.client_place_of_birth,
            "adresse": entity.client_address, "ville": entity.client_city,
            "pays": entity.client_country, "nationalites": (entity.client_countries or {}).get("nationality"),
            "iban": entity.client_iban, "bic": entity.client_bic,
            "identifiant_fiscal": entity.client_tax_id,
            "telephone": entity.client_phone, "email": entity.client_email,
            "segment": entity.client_segment, "secteur_activite": entity.client_activity_sector,
            "notation_risque": entity.client_risk_rating,
            "ppe_declare": bool(entity.client_pep_flag),
            "entree_en_relation": entity.client_relationship_start,
        })

    # Operation concernee (filtrage transactionnel) : donnees du message
    operation = None
    if (alert.channel or "SCREENING") == "FILTERING":
        operation = {
            "reference_message": alert.client_id,
            "type_message": tree.get("message_type"),
            "partie_en_cause": alert.client_name,
            "role_partie": tree.get("party_role"),
        }

    listed_entity = tree.get("watchlist_entity") or {}
    inherited = compute_inherited_risk(db, alert.watchlist_entity_id, max_depth=2)

    adjustments = tree.get("adjustments") or {}
    motifs = {
        "resume": (
            f"Correspondance de criblage entre « {alert.client_name} » et la personne/entité "
            f"listée « {alert.watchlist_name} » ({alert.list_type or 'liste inconnue'}), "
            f"score final {float(alert.final_score or 0):.1f} %."
        ),
        "score_final": alert.final_score,
        "score_base": tree.get("base_score"),
        "correspondance_exacte": bool(tree.get("hard_match_triggered", False)),
        "seuil_applique": tree.get("cut_off_applied"),
        "ajustements": adjustments,
        "regle_des_50_pct": [
            {"detenteur": r.get("owner_name") or r.get("owner_id"), "detail": r}
            for r in (inherited or [])
        ],
        "decision_analyste": {
            "statut": alert.status, "decide_par": alert.decided_by,
            "decide_le": alert.decided_at.isoformat() if alert.decided_at else None,
            "commentaire": alert.decision_comment,
        },
    }

    return {
        "type": "PROJET_DECLARATION_SOUPCON",
        "avertissement": STR_DISCLAIMER,
        "genere_le": datetime.utcnow().isoformat() + "Z",
        "genere_par": username,
        "declarant": _institution_config(),
        "alerte": {
            "id": alert.id, "canal": alert.channel or "SCREENING",
            "creee_le": alert.created_at.isoformat() if alert.created_at else None,
            "statut": alert.status, "priorite": alert.priority,
            "liste": alert.list_type, "score": alert.final_score,
        },
        "personne_concernee": person,
        "personne_listee": {
            "entity_id": alert.watchlist_entity_id,
            "nom": alert.watchlist_name,
            "liste": alert.list_type,
            "programmes": listed_entity.get("programs"),
            "motifs_designation": listed_entity.get("designation_reasons"),
            "reference_officielle": listed_entity.get("official_reference"),
            "fiche": listed_entity or None,
        },
        "operation_concernee": operation,
        "motifs": motifs,
        "chronologie": [
            {"date": e.timestamp.isoformat() if e.timestamp else None,
             "par": e.username, "action": e.action, "detail": e.detail}
            for e in events
        ],
    }


@app.get("/api/alerts/{alert_id}/str-draft")
async def get_alert_str_draft(
    alert_id: int,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Projet de declaration de soupcon TRACFIN pre-rempli (JSON structure) :
    declarant (config institution), personne concernee (KYC), personne listee,
    operation (filtrage), motifs traces (scores, seuil, regle des 50 %) et
    chronologie. AUCUNE transmission automatique (ERMES est un portail humain) ;
    la generation est tracee STR_DRAFT_GENERATED dans l'historique de l'alerte.
    """
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    draft = _build_str_draft(db, alert, reviewer["username"])
    _log_alert_event(db, alert.id, reviewer["username"], "STR_DRAFT_GENERATED",
                     "Projet de déclaration de soupçon généré (format JSON)")
    db.commit()
    return draft


@app.get("/api/alerts/{alert_id}/str-draft/print")
async def print_alert_str_draft(
    alert_id: int,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """Projet de declaration de soupcon en HTML autonome imprimable, avec
    bandeau « projet a valider par le correspondant TRACFIN »."""
    from html import escape
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    draft = _build_str_draft(db, alert, reviewer["username"])
    _log_alert_event(db, alert.id, reviewer["username"], "STR_DRAFT_GENERATED",
                     "Projet de déclaration de soupçon généré (format imprimable)")
    db.commit()

    def esc(value):
        return escape(str(value)) if value not in (None, "", []) else "—"

    def rows(mapping: Dict[str, Any], labels: Dict[str, str]) -> str:
        out = []
        for key, label in labels.items():
            value = mapping.get(key)
            if isinstance(value, list):
                value = ", ".join(str(v) for v in value) if value else None
            if isinstance(value, dict):
                continue
            out.append(f"<tr><th>{escape(label)}</th><td>{esc(value)}</td></tr>")
        return "\n".join(out)

    declarant = draft["declarant"]
    person = draft["personne_concernee"]
    listed = draft["personne_listee"]
    motifs = draft["motifs"]
    adjustments_html = "\n".join(
        f"<tr><td>{esc(k)}</td><td>{esc((v or {}).get('score'))}</td><td>{esc((v or {}).get('description'))}</td></tr>"
        for k, v in (motifs.get("ajustements") or {}).items())
    fifty_html = "".join(
        f"<li>{esc(r.get('detenteur'))}</li>" for r in motifs.get("regle_des_50_pct") or [])
    chrono_html = "\n".join(
        f"<tr><td>{esc((e['date'] or '')[:19].replace('T', ' '))}</td><td>@{esc(e['par'])}</td>"
        f"<td>{esc(e['action'])}</td><td>{esc(e['detail'])}</td></tr>"
        for e in draft["chronologie"])
    operation = draft.get("operation_concernee")
    operation_html = (
        "<h2>Opération concernée</h2><table><tbody>"
        + rows(operation, {"reference_message": "Référence du message",
                           "type_message": "Type de message",
                           "partie_en_cause": "Partie en cause",
                           "role_partie": "Rôle de la partie"})
        + "</tbody></table>") if operation else ""

    html = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Fiskr — Projet de déclaration de soupçon — Alerte #{alert.id}</title>
<style>
body {{ font-family: Arial, sans-serif; color: #111; margin: 2rem auto; max-width: 860px; }}
h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.02rem; margin-top: 1.4rem; border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 0.85rem; margin-top: 0.5rem; }}
th, td {{ border: 1px solid #ccc; padding: 5px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f0f0f0; width: 220px; }}
.banner {{ border: 2px solid #b45309; background: #fef3c7; color: #7c2d12; padding: 0.7rem 1rem; border-radius: 6px; font-size: 0.9rem; margin: 1rem 0; }}
.sub {{ color: #555; }} ul {{ margin: 0.4rem 0 0 1.2rem; }}
@media print {{ .no-print {{ display: none; }} body {{ margin: 0; }} }}
</style></head><body>
<button class="no-print" onclick="window.print()">🖨 Imprimer / PDF</button>
<h1>Projet de déclaration de soupçon (TRACFIN) — Alerte #{alert.id}</h1>
<p class="sub">Généré le {datetime.utcnow().strftime('%d/%m/%Y %H:%M')} UTC par @{esc(reviewer['username'])} — support de préparation à la télédéclaration ERMES.</p>
<div class="banner">⚠ {escape(STR_DISCLAIMER)}</div>
<h2>1. Déclarant (établissement)</h2>
<table><tbody>{rows(declarant, {"name": "Établissement", "siren": "SIREN",
    "correspondent_name": "Correspondant TRACFIN", "correspondent_email": "Email",
    "correspondent_phone": "Téléphone"})}</tbody></table>
<h2>2. Personne concernée</h2>
<table><tbody>{rows(person, {"type": "Type", "nom_complet": "Nom complet", "prenom": "Prénom",
    "nom": "Nom", "raison_sociale": "Raison sociale", "date_naissance": "Date de naissance",
    "lieu_naissance": "Lieu de naissance", "adresse": "Adresse", "ville": "Ville", "pays": "Pays",
    "nationalites": "Nationalité(s)", "iban": "IBAN", "bic": "BIC",
    "identifiant_fiscal": "Identifiant fiscal", "telephone": "Téléphone", "email": "Email",
    "segment": "Segment", "secteur_activite": "Secteur d'activité",
    "notation_risque": "Notation de risque", "ppe_declare": "PPE auto-déclaré",
    "entree_en_relation": "Entrée en relation", "reference_interne": "Référence interne"})}</tbody></table>
<h2>3. Personne / entité listée</h2>
<table><tbody>{rows(listed, {"nom": "Nom sur la liste", "entity_id": "Identifiant",
    "liste": "Liste d'origine", "programmes": "Programmes de sanctions",
    "motifs_designation": "Motifs de la désignation", "reference_officielle": "Référence officielle"})}</tbody></table>
{operation_html}
<h2>4. Motifs du soupçon (données tracées)</h2>
<p>{esc(motifs.get('resume'))}</p>
<table><tbody>{rows(motifs, {"score_final": "Score final (%)", "score_base": "Score de base (%)",
    "correspondance_exacte": "Correspondance exacte (hard match)", "seuil_applique": "Seuil appliqué (%)"})}</tbody></table>
{'<h3>Ajustements contextuels</h3><table><thead><tr><th>Critère</th><th>Score</th><th>Détail</th></tr></thead><tbody>' + adjustments_html + '</tbody></table>' if adjustments_html else ''}
{'<p><strong>⚠ Règle des 50 % — détentions par des personnes listées :</strong></p><ul>' + fifty_html + '</ul>' if fifty_html else ''}
<h2>5. Chronologie du traitement (append-only)</h2>
<table><thead><tr><th>Date</th><th>Utilisateur</th><th>Action</th><th>Détail</th></tr></thead>
<tbody>{chrono_html}</tbody></table>
</body></html>"""
    return HTMLResponse(content=html)


# ------------------ QUALITE DES DONNEES CLIENTS ------------------

# Champs KYC evalues : (attribut, libelle, PP seulement)
_QUALITY_FIELDS = [
    ("client_dob", "Date de naissance", True),
    ("client_countries", "Pays (nationalité...)", False),
    ("client_address", "Adresse", False),
    ("client_first_name", "Prénom", True),
    ("client_tax_id", "Identifiant fiscal", False),
    ("client_email", "Email", False),
    ("client_phone", "Téléphone", False),
    ("client_risk_rating", "Notation de risque", False),
    ("client_segment", "Segment", False),
    ("client_activity_sector", "Secteur d'activité", False),
    ("client_relationship_start", "Entrée en relation", False),
]


def _quality_field_filled(entity: ClientEntity, attr: str) -> bool:
    value = getattr(entity, attr)
    if attr == "client_countries":
        countries = value or {}
        return any(countries.get(k) for k in ("nationality", "residence",
                                              "birth_country", "registration_country")) \
            or bool(entity.client_country)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value != {} and value != []


@app.get("/api/quality/clients")
async def get_client_data_quality(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Tableau de bord qualite des donnees du referentiel clients (dernier
    snapshot CLIENT_BASE en production) : taux de completude par champ KYC,
    ventilation par segment, fiches a risque pour le criblage (DOB manquante,
    pays manquant, PP sans prenom) et score global. Lecture seule.
    """
    snap = db.query(Snapshot).filter(
        Snapshot.file_type == "CLIENT_BASE", Snapshot.status == "READY"
    ).order_by(Snapshot.uploaded_at.desc()).first()
    if not snap:
        return {"snapshot": None, "message": "Aucune base clients en production."}

    fields = {attr: {"label": label, "pp_only": pp_only, "filled": 0, "total": 0}
              for attr, label, pp_only in _QUALITY_FIELDS}
    segments: Dict[str, Dict[str, int]] = {}
    risky = {"dob_missing_pp": 0, "country_missing": 0, "pp_without_first_name": 0}
    total = 0
    pp_total = 0

    query = db.query(ClientEntity).filter(ClientEntity.snapshot_id == snap.snapshot_id)
    for entity in query.yield_per(2000):
        total += 1
        is_pp = entity.client_type == "PP"
        if is_pp:
            pp_total += 1
        seg = (entity.client_segment or "(non renseigné)").strip() or "(non renseigné)"
        seg_bucket = segments.setdefault(seg, {"total": 0, "filled": 0, "checks": 0})
        seg_bucket["total"] += 1

        for attr, label, pp_only in _QUALITY_FIELDS:
            if pp_only and not is_pp:
                continue
            bucket = fields[attr]
            bucket["total"] += 1
            seg_bucket["checks"] += 1
            if _quality_field_filled(entity, attr):
                bucket["filled"] += 1
                seg_bucket["filled"] += 1

        if is_pp and not (entity.client_dob or "").strip():
            risky["dob_missing_pp"] += 1
        if not _quality_field_filled(entity, "client_countries"):
            risky["country_missing"] += 1
        if is_pp and not (entity.client_first_name or "").strip():
            risky["pp_without_first_name"] += 1

    def _pct(filled, denom):
        return round(filled * 100.0 / denom, 1) if denom else None

    field_rows = [
        {"field": attr, "label": bucket["label"], "pp_only": bucket["pp_only"],
         "filled": bucket["filled"], "total": bucket["total"],
         "pct": _pct(bucket["filled"], bucket["total"])}
        for attr, bucket in fields.items()
    ]
    checked = [r for r in field_rows if r["total"]]
    global_score = round(sum(r["pct"] for r in checked) / len(checked), 1) if checked else None
    segment_rows = sorted(
        ({"segment": seg, "clients": b["total"], "pct": _pct(b["filled"], b["checks"])}
         for seg, b in segments.items()),
        key=lambda r: (r["pct"] if r["pct"] is not None else 101))

    return {
        "snapshot": {
            "snapshot_id": snap.snapshot_id, "file_name": snap.file_name,
            "uploaded_at": snap.uploaded_at.isoformat() if snap.uploaded_at else None,
            "record_count": total, "pp_count": pp_total,
        },
        "global_score_pct": global_score,
        "fields": field_rows,
        "segments": segment_rows,
        "risky_records": risky,
    }


# ------------------ WEBHOOKS ENTRANTS (SI AMONT) ------------------

_HOOK_DELIVERY_TTL_DAYS = 90


def _hooks_secret() -> str:
    return str((config.get("hooks", {}) or {}).get("secret") or "").strip()


async def _verify_hook_request(request: Request, current_user: Dict[str, Any]) -> bytes:
    """Garde commune des webhooks entrants : reserve aux cles d'API (comptes
    de service), signature HMAC-SHA256 du corps brut obligatoire si
    hooks.secret est configure. Retourne le corps brut (une seule lecture)."""
    if not current_user.get("is_api_key"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Les webhooks entrants sont réservés aux clés d'API (X-API-Key)."
        )
    body = await request.body()
    secret = _hooks_secret()
    if secret:
        import hmac as hmac_mod
        provided = (request.headers.get("X-Fiskr-Signature") or "").strip().lower()
        expected = hmac_mod.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not provided or not hmac_mod.compare_digest(provided, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Signature X-Fiskr-Signature absente ou invalide (HMAC-SHA256 du corps brut)."
            )
    return body


def _hook_idempotency_replay(db: Session, request: Request) -> Tuple[Optional[str], Optional[JSONResponse]]:
    """Si X-Idempotency-Key correspond a une livraison connue, rejoue la
    reponse d'origine. Purge opportuniste des livraisons expirees."""
    key = (request.headers.get("X-Idempotency-Key") or "").strip()
    if not key:
        return None, None
    if len(key) > 200:
        raise HTTPException(status_code=400, detail="X-Idempotency-Key : 200 caractères maximum.")
    cutoff = datetime.utcnow() - timedelta(days=_HOOK_DELIVERY_TTL_DAYS)
    db.query(HookDelivery).filter(HookDelivery.created_at < cutoff).delete(synchronize_session=False)
    existing = db.query(HookDelivery).filter(HookDelivery.idempotency_key == key).first()
    if existing:
        return key, JSONResponse(
            status_code=existing.status_code,
            content=existing.response_json,
            headers={"X-Idempotency-Replayed": "true"},
        )
    return key, None


def _hook_store_delivery(db: Session, key: Optional[str], endpoint: str,
                         caller: str, status_code: int, response: Dict[str, Any]) -> None:
    if not key:
        return
    db.add(HookDelivery(idempotency_key=key, endpoint=endpoint, caller=caller,
                        status_code=status_code, response_json=response))
    db.commit()


@app.post("/api/hooks/screening")
async def hook_screening(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Webhook entrant de criblage temps reel (SI amont -> Fiskr) : meme charge
    utile que POST /api/screen, authentifie par cle d'API fsk_ (+ signature
    HMAC-SHA256 si hooks.secret est configure), idempotent par X-Idempotency-Key
    (la reponse d'origine est rejouee a la retransmission). Meme cœur de
    criblage que le temps reel : audit immuable et alerte crees a l'identique.
    """
    body = await _verify_hook_request(request, current_user)
    key, replay = _hook_idempotency_replay(db, request)
    if replay is not None:
        return replay
    try:
        payload = ScreenClientRequest.model_validate_json(body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Charge utile invalide : {e}")
    client_dict = payload.model_dump()
    client_dict.pop("screening_lists", None)
    requested_lists = _validate_screening_lists(payload.screening_lists)
    result = screen_client_profile(db, client_dict, current_user["username"], requested_lists)
    _hook_store_delivery(db, key, "screening", current_user["username"], 200, result)
    return result


class HookClientUpsert(BaseModel):
    # Fiche client unitaire : memes champs que l'import CLIENT_BASE
    client_id: str
    client_type: str = "PP"
    client_first_name: Optional[str] = None
    client_last_name: Optional[str] = None
    client_maiden_name: Optional[str] = None
    client_company_name: Optional[str] = None
    client_dob: Optional[str] = None
    client_gender: Optional[str] = "U"
    client_countries: Optional[Dict[str, Any]] = None
    client_address: Optional[str] = None
    client_city: Optional[str] = None
    client_country: Optional[str] = None
    client_iban: Optional[str] = None
    client_bic: Optional[str] = None
    client_tax_id: Optional[str] = None
    client_phone: Optional[str] = None
    client_email: Optional[str] = None
    client_risk_rating: Optional[str] = None
    client_pep_flag: Optional[bool] = None
    client_segment: Optional[str] = None
    client_activity_sector: Optional[str] = None
    client_relationship_start: Optional[str] = None
    client_status: Optional[str] = None


@app.post("/api/hooks/client-upsert")
async def hook_client_upsert(
    request: Request,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Webhook entrant d'upsert d'une fiche client (SI amont -> Fiskr) dans le
    dernier referentiel CLIENT_BASE en production : mise a jour par client_id
    ou creation. Trace au journal des actions d'administration
    (CLIENT_UPSERT_HOOK), idempotent par X-Idempotency-Key.
    """
    body = await _verify_hook_request(request, current_user)
    key, replay = _hook_idempotency_replay(db, request)
    if replay is not None:
        return replay
    try:
        payload = HookClientUpsert.model_validate_json(body)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Charge utile invalide : {e}")
    if payload.client_type not in ("PP", "PM"):
        raise HTTPException(status_code=400, detail="client_type doit valoir PP ou PM.")

    snap = db.query(Snapshot).filter(
        Snapshot.file_type == "CLIENT_BASE", Snapshot.status == "READY"
    ).order_by(Snapshot.uploaded_at.desc()).first()
    if not snap:
        raise HTTPException(
            status_code=409,
            detail="Aucune base clients en production : importez d'abord un référentiel CLIENT_BASE."
        )

    values = payload.model_dump()
    existing = db.query(ClientEntity).filter(
        ClientEntity.snapshot_id == snap.snapshot_id,
        ClientEntity.client_id == payload.client_id,
    ).first()
    if existing:
        before = {k: getattr(existing, k) for k, v in values.items()
                  if k != "client_id" and getattr(existing, k) != v}
        for field_name, value in values.items():
            if field_name != "client_id":
                setattr(existing, field_name, value)
        existing.entity_checksum = compute_checksum(values)
        action_detail = "updated"
        changed_fields = sorted(before.keys())
    else:
        db.add(ClientEntity(snapshot_id=snap.snapshot_id,
                            entity_checksum=compute_checksum(values), **values))
        snap.record_count = (snap.record_count or 0) + 1
        action_detail = "created"
        changed_fields = sorted(k for k, v in values.items() if v is not None and k != "client_id")

    log_admin_action(db, current_user["username"], "CLIENT_UPSERT_HOOK",
                     target=payload.client_id,
                     after={"operation": action_detail, "fields": changed_fields,
                            "snapshot_id": snap.snapshot_id})
    db.commit()
    result = {
        "message": f"Fiche client {payload.client_id} {'mise à jour' if action_detail == 'updated' else 'créée'} dans le référentiel en production.",
        "operation": action_detail,
        "client_id": payload.client_id,
        "snapshot_id": snap.snapshot_id,
        "changed_fields": changed_fields,
    }
    _hook_store_delivery(db, key, "client-upsert", current_user["username"], 200, result)
    return result


@app.get("/api/alerts/workload")
async def get_alerts_workload(
    channel: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Charge de travail de la file : par analyste assigne — alertes ouvertes
    par priorite, retards SLA, prochaine echeance — plus la file non
    assignee et les validations 4-yeux en attente. Le tableau de bord du
    responsable d'equipe pour repartir le travail.
    """
    now = datetime.utcnow()
    query = db.query(Alert).filter(Alert.status.in_(ALERT_OPEN_STATUSES))
    if channel:
        ch = channel.strip().upper()
        if ch == "SCREENING":
            query = query.filter((Alert.channel == "SCREENING") | (Alert.channel.is_(None)))
        else:
            query = query.filter(Alert.channel == ch)
    open_alerts = query.all()

    def _bucket():
        return {"open_total": 0, "by_priority": {p: 0 for p in ALERT_PRIORITIES},
                "overdue": 0, "next_due_at": None, "pending_validation": 0}

    analysts: Dict[str, Dict[str, Any]] = {}
    unassigned = _bucket()
    for alert in open_alerts:
        bucket = analysts.setdefault(alert.assigned_to, _bucket()) if alert.assigned_to else unassigned
        bucket["open_total"] += 1
        if alert.priority in bucket["by_priority"]:
            bucket["by_priority"][alert.priority] += 1
        if alert.due_at:
            if alert.due_at < now:
                bucket["overdue"] += 1
            if bucket["next_due_at"] is None or alert.due_at < bucket["next_due_at"]:
                bucket["next_due_at"] = alert.due_at
        if alert.status == "PENDING_VALIDATION":
            bucket["pending_validation"] += 1

    def _serialize(bucket):
        return {**bucket,
                "next_due_at": bucket["next_due_at"].isoformat() if bucket["next_due_at"] else None}

    return {
        "generated_at": now.isoformat(),
        "analysts": [
            {"username": username, **_serialize(bucket)}
            for username, bucket in sorted(
                analysts.items(),
                key=lambda kv: (-kv[1]["overdue"], -kv[1]["open_total"]))
        ],
        "unassigned": _serialize(unassigned),
        "totals": {
            "open": len(open_alerts),
            "overdue": sum(1 for a in open_alerts if a.due_at and a.due_at < now),
            "pending_validation": sum(1 for a in open_alerts if a.status == "PENDING_VALIDATION"),
        },
    }

@app.get("/api/alerts")
async def list_alerts(
    status_filter: Optional[str] = Query(None, alias="status"),
    assigned_to: Optional[str] = Query(None),
    list_type: Optional[str] = Query(None),
    channel: Optional[str] = Query(None),
    priority: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """File de travail des alertes, triee par priorite puis echeance puis
    risque (score). Le canal separe le criblage clients (SCREENING) du
    filtrage transactionnel (FILTERING). `search` cherche sur le client,
    la fiche listee et leurs identifiants (recherche globale Ctrl+K)."""
    from sqlalchemy import case, or_
    query = db.query(Alert)
    if channel:
        ch = channel.strip().upper()
        if ch == "SCREENING":
            # Inclut les alertes anterieures a la colonne channel (NULL = criblage)
            query = query.filter((Alert.channel == "SCREENING") | (Alert.channel.is_(None)))
        else:
            query = query.filter(Alert.channel == ch)
    if status_filter:
        statuses = [s.strip().upper() for s in status_filter.split(",") if s.strip()]
        query = query.filter(Alert.status.in_(statuses))
    if assigned_to:
        query = query.filter(Alert.assigned_to == assigned_to)
    if priority:
        prios = [p.strip().upper() for p in priority.split(",") if p.strip()]
        bad = [p for p in prios if p not in ALERT_PRIORITIES]
        if bad:
            raise HTTPException(status_code=400, detail=f"Priorité inconnue ({', '.join(ALERT_PRIORITIES)}).")
        query = query.filter(Alert.priority.in_(prios))
    if search and search.strip():
        needle = f"%{search.strip()}%"
        query = query.filter(or_(
            Alert.client_name.ilike(needle), Alert.client_id.ilike(needle),
            Alert.watchlist_name.ilike(needle), Alert.watchlist_entity_id.ilike(needle),
        ))
    query = _apply_list_type_filter(query, Alert.list_type, list_type)
    total = query.count()
    open_count = query.filter(Alert.status.in_(ALERT_OPEN_STATUSES)).count()
    # CRITICAL d'abord, puis echeance la plus proche, puis score : la file
    # se lit de haut en bas dans l'ordre de traitement attendu
    priority_rank = case(
        {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3},
        value=Alert.priority, else_=2
    )
    rows = query.order_by(priority_rank.asc(), Alert.due_at.asc().nullslast(),
                          Alert.final_score.desc(), Alert.created_at.desc()) \
                .offset((page - 1) * page_size).limit(page_size).all()
    return {
        "total": total,
        "open_count": open_count,
        "page": page,
        "page_size": page_size,
        "items": [_alert_summary(a) for a in rows],
    }

@app.get("/api/alerts/{alert_id}")
async def get_alert_detail(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Detail d'une alerte : decision_tree du journal d'audit lie + historique des actions."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    audit = db.query(AuditTrail).filter(AuditTrail.id == alert.audit_id).first()
    events = db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id) \
               .order_by(AlertEvent.timestamp.asc(), AlertEvent.id.asc()).all()
    attachments = db.query(AlertAttachment).filter(AlertAttachment.alert_id == alert_id) \
                    .order_by(AlertAttachment.uploaded_at.asc()).all()
    return {
        **_alert_summary(alert),
        "attachments": [
            {
                "id": att.id, "file_name": att.file_name, "comment": att.comment,
                "uploaded_by": att.uploaded_by,
                "uploaded_at": att.uploaded_at.isoformat() if att.uploaded_at else None,
            }
            for att in attachments
        ],
        "decision_tree": audit.decision_tree if audit else None,
        "watchlist_version": audit.watchlist_version if audit else None,
        "events": [
            {
                "timestamp": e.timestamp.isoformat() if e.timestamp else None,
                "username": e.username,
                "action": e.action,
                "detail": e.detail,
            }
            for e in events
        ],
    }

@app.post("/api/alerts/{alert_id}/assign")
async def assign_alert(
    alert_id: int,
    payload: AlertAssignRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """S'assigner une alerte (ou l'assigner a un autre analyste : admin uniquement).
    Si l'assigne est absent, l'alerte va a son delegue (delegation d'absence)."""
    alert = _get_open_alert(db, alert_id)
    assignee = (payload.assignee or "").strip() or current_user["username"]
    if assignee != current_user["username"] and "admin" not in parse_roles(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Seul un administrateur peut assigner une alerte à un autre analyste.")
    assignee, redirected_from = resolve_delegate(db, assignee)
    alert.assigned_to = assignee
    if alert.status == "OPEN":
        alert.status = "IN_PROGRESS"
    _log_alert_event(db, alert.id, current_user["username"], "ASSIGNED",
                     f"Assignée à {assignee}."
                     + (f" (au lieu de @{redirected_from}, absent — délégation)" if redirected_from else ""))
    db.commit()
    return {"message": f"Alerte assignée à {assignee}"
                       + (f" (délégué de @{redirected_from}, absent)." if redirected_from else "."),
            **_alert_summary(alert)}

@app.post("/api/alerts/{alert_id}/comment")
async def comment_alert(
    alert_id: int,
    payload: AlertCommentRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Ajoute un commentaire a l'historique de l'alerte."""
    alert = _get_open_alert(db, alert_id)
    comment = (payload.comment or "").strip()
    if not comment:
        raise HTTPException(status_code=400, detail="Le commentaire ne peut pas être vide.")
    _log_alert_event(db, alert.id, current_user["username"], "COMMENT", comment)
    db.commit()
    return {"message": "Commentaire ajouté."}

@app.post("/api/alerts/{alert_id}/escalate")
async def escalate_alert(
    alert_id: int,
    payload: AlertCommentRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Escalade l'alerte (motif obligatoire)."""
    alert = _get_open_alert(db, alert_id)
    comment = (payload.comment or "").strip()
    if not comment:
        raise HTTPException(status_code=400, detail="Un motif est requis pour escalader une alerte.")
    alert.status = "ESCALATED"
    _log_alert_event(db, alert.id, current_user["username"], "ESCALATED", comment)
    db.commit()
    return {"message": "Alerte escaladée.", **_alert_summary(alert)}

def _close_alert(alert: Alert, decision: str, username: str, comment: str) -> None:
    alert.status = "CLOSED_CONFIRMED" if decision == "CONFIRMED" else "CLOSED_FALSE_POSITIVE"
    alert.decided_by = username
    alert.decided_at = datetime.utcnow()
    alert.decision_comment = comment

@app.post("/api/alerts/{alert_id}/propose")
async def propose_alert_decision(
    alert_id: int,
    payload: AlertProposeRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Propose une decision (vrai positif confirme / faux positif), commentaire
    obligatoire. Avec le 4-yeux actif, l'alerte attend la validation d'un
    reviewer DIFFERENT ; sinon la proposition clot directement l'alerte.
    """
    alert = _get_open_alert(db, alert_id)
    if alert.status == "PENDING_VALIDATION":
        raise HTTPException(status_code=409, detail="Une décision est déjà en attente de validation.")
    decision = (payload.decision or "").strip().upper()
    if decision not in ("CONFIRMED", "FALSE_POSITIVE"):
        raise HTTPException(status_code=400, detail="Décision invalide (CONFIRMED ou FALSE_POSITIVE).")
    comment = (payload.comment or "").strip()
    if not comment:
        raise HTTPException(status_code=400, detail="Un commentaire est obligatoire pour proposer une décision.")

    username = current_user["username"]
    alert.proposed_decision = decision
    alert.proposed_by = username
    alert.proposed_at = datetime.utcnow()
    alert.proposal_comment = comment
    label = "vrai positif confirmé" if decision == "CONFIRMED" else "faux positif"

    if alert_four_eyes_required(db):
        alert.status = "PENDING_VALIDATION"
        _log_alert_event(db, alert.id, username, "PROPOSED", f"Décision proposée : {label}. {comment}")
        message = "Décision proposée, en attente de validation 4-yeux."
        if notification_events(db).get("alert_pending_validation"):
            notify_event("alert_pending_validation", {
                "alert_id": alert.id, "proposee_par": username, "decision": label,
                "client": alert.client_name, "fiche_listee": alert.watchlist_name,
            })
    else:
        _close_alert(alert, decision, username, comment)
        _log_alert_event(db, alert.id, username, "PROPOSED", f"Décision : {label}. {comment}")
        _log_alert_event(db, alert.id, username, "VALIDATED", "Clôture directe (validation 4-yeux désactivée).")
        message = "Alerte clôturée (validation 4-yeux désactivée)."
    db.commit()
    return {"message": message, **_alert_summary(alert)}

@app.post("/api/alerts/{alert_id}/validate")
async def validate_alert_decision(
    alert_id: int,
    payload: AlertValidateRequest,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Validation 4-yeux d'une decision proposee (reviewer ou admin). Le
    validateur doit etre DIFFERENT du proposeur. Refuser renvoie l'alerte
    en cours d'analyse (commentaire obligatoire).
    """
    alert = _get_open_alert(db, alert_id)
    if alert.status != "PENDING_VALIDATION":
        raise HTTPException(status_code=409, detail="Aucune décision en attente de validation sur cette alerte.")
    username = reviewer["username"]
    if username == alert.proposed_by:
        raise HTTPException(
            status_code=403,
            detail="Validation 4-yeux : le validateur doit être différent du proposeur."
        )
    comment = (payload.comment or "").strip()
    if payload.approve:
        _close_alert(alert, alert.proposed_decision, username, comment or alert.proposal_comment)
        _log_alert_event(db, alert.id, username, "VALIDATED", comment or "Décision validée.")
        message = f"Décision validée, alerte clôturée ({alert.status})."
    else:
        if not comment:
            raise HTTPException(status_code=400, detail="Un commentaire est requis pour refuser une décision.")
        alert.status = "IN_PROGRESS"
        alert.proposed_decision = None
        alert.proposed_by = None
        alert.proposed_at = None
        alert.proposal_comment = None
        _log_alert_event(db, alert.id, username, "RETURNED", comment)
        message = "Décision refusée, alerte renvoyée en analyse."
    db.commit()
    return {"message": message, **_alert_summary(alert)}

class AlertPriorityRequest(BaseModel):
    priority: str

@app.post("/api/alerts/{alert_id}/priority")
async def set_alert_priority(
    alert_id: int,
    payload: AlertPriorityRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Modifie la priorite d'une alerte ouverte ; l'echeance SLA est recalculee
    depuis la date de creation (reglage alerts.sla_hours)."""
    alert = _get_open_alert(db, alert_id)
    new_priority = (payload.priority or "").strip().upper()
    if new_priority not in ALERT_PRIORITIES:
        raise HTTPException(status_code=400, detail=f"Priorité invalide ({', '.join(ALERT_PRIORITIES)}).")
    old_priority = alert.priority or "—"
    alert.priority = new_priority
    alert.due_at = compute_due_at(db, new_priority, alert.created_at)
    _log_alert_event(db, alert.id, current_user["username"], "PRIORITY_CHANGED",
                     f"Priorité {old_priority} → {new_priority}.")
    db.commit()
    return {"message": f"Priorité passée à {new_priority}.", **_alert_summary(alert)}

class AlertBulkRequest(BaseModel):
    ids: List[int]
    action: str  # "assign" | "priority"
    assignee: Optional[str] = None
    priority: Optional[str] = None

_ALERT_BULK_MAX = 200

@app.post("/api/alerts/bulk")
async def bulk_alert_action(
    payload: AlertBulkRequest,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Action en masse sur des alertes OUVERTES (≤ 200 a la fois) : assignation
    ou changement de priorite. Memes regles que les actions unitaires
    (assigner a un tiers = admin), meme journalisation : un AlertEvent par
    alerte traitee — jamais d'action silencieuse. Les alertes cloturees ou
    introuvables sont ignorees et restituees dans `skipped`.
    """
    ids = list(dict.fromkeys(payload.ids or []))
    if not ids:
        raise HTTPException(status_code=400, detail="Aucune alerte sélectionnée.")
    if len(ids) > _ALERT_BULK_MAX:
        raise HTTPException(status_code=400, detail=f"Maximum {_ALERT_BULK_MAX} alertes par action en masse.")
    action = (payload.action or "").strip().lower()
    if action not in ("assign", "priority"):
        raise HTTPException(status_code=400, detail="Action en masse inconnue (assign ou priority).")

    if action == "assign":
        assignee = (payload.assignee or "").strip() or current_user["username"]
        if assignee != current_user["username"] and "admin" not in parse_roles(current_user.get("role")):
            raise HTTPException(status_code=403,
                                detail="Seul un administrateur peut assigner des alertes à un autre analyste.")
        assignee, _redirected = resolve_delegate(db, assignee)
    else:
        new_priority = (payload.priority or "").strip().upper()
        if new_priority not in ALERT_PRIORITIES:
            raise HTTPException(status_code=400, detail=f"Priorité invalide ({', '.join(ALERT_PRIORITIES)}).")

    alerts = {a.id: a for a in db.query(Alert).filter(Alert.id.in_(ids)).all()}
    updated, skipped = [], []
    for alert_id in ids:
        alert = alerts.get(alert_id)
        if alert is None or alert.status not in ALERT_OPEN_STATUSES:
            skipped.append(alert_id)
            continue
        if action == "assign":
            alert.assigned_to = assignee
            if alert.status == "OPEN":
                alert.status = "IN_PROGRESS"
            _log_alert_event(db, alert.id, current_user["username"], "ASSIGNED",
                             f"Assignée à {assignee} (action en masse).")
        else:
            old_priority = alert.priority or "—"
            alert.priority = new_priority
            alert.due_at = compute_due_at(db, new_priority, alert.created_at)
            _log_alert_event(db, alert.id, current_user["username"], "PRIORITY_CHANGED",
                             f"Priorité {old_priority} → {new_priority} (action en masse).")
        updated.append(alert_id)
    db.commit()
    return {"updated": updated, "skipped": skipped,
            "message": f"{len(updated)} alerte(s) mise(s) à jour, {len(skipped)} ignorée(s)."}

# ------------------ VUES SAUVEGARDEES (filtres des files d'alertes) ------------------

class SavedViewCreate(BaseModel):
    name: str
    channel: str = "SCREENING"
    filters: Dict[str, Any] = {}

_SAVED_VIEW_FILTER_KEYS = ("status", "priority", "list_type")

def _saved_view_summary(v: SavedView) -> Dict[str, Any]:
    return {"id": v.id, "name": v.name, "channel": v.channel, "filters": v.filters or {},
            "created_at": v.created_at.isoformat() if v.created_at else None}

@app.get("/api/views")
async def list_saved_views(
    channel: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Vues sauvegardees de l'utilisateur courant (filtres de file memorises)."""
    query = db.query(SavedView).filter(SavedView.username == current_user["username"])
    if channel:
        query = query.filter(SavedView.channel == channel.strip().upper())
    rows = query.order_by(SavedView.name.asc()).all()
    return {"items": [_saved_view_summary(v) for v in rows]}

@app.post("/api/views")
async def create_saved_view(
    payload: SavedViewCreate,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Sauvegarde la combinaison de filtres courante sous un nom (par utilisateur)."""
    name = (payload.name or "").strip()
    if not name or len(name) > 100:
        raise HTTPException(status_code=400, detail="Nom de vue requis (100 caractères max).")
    channel = (payload.channel or "SCREENING").strip().upper()
    if channel not in ("SCREENING", "FILTERING"):
        raise HTTPException(status_code=400, detail="Canal inconnu (SCREENING ou FILTERING).")
    filters = {k: str(v) for k, v in (payload.filters or {}).items()
               if k in _SAVED_VIEW_FILTER_KEYS and v is not None and str(v).strip()}
    existing = db.query(SavedView).filter(
        SavedView.username == current_user["username"],
        SavedView.channel == channel, SavedView.name == name).first()
    if existing:
        # Meme nom = mise a jour de la vue (comportement attendu d'un « enregistrer »)
        existing.filters = filters
        db.commit()
        return {"message": f"Vue « {name} » mise à jour.", **_saved_view_summary(existing)}
    view = SavedView(username=current_user["username"], name=name, channel=channel, filters=filters)
    db.add(view)
    db.commit()
    return {"message": f"Vue « {name} » sauvegardée.", **_saved_view_summary(view)}

@app.delete("/api/views/{view_id}")
async def delete_saved_view(
    view_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Supprime une de ses vues (un admin peut supprimer n'importe laquelle)."""
    view = db.query(SavedView).filter(SavedView.id == view_id).first()
    if not view:
        raise HTTPException(status_code=404, detail="Vue introuvable.")
    if view.username != current_user["username"] and "admin" not in parse_roles(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Cette vue appartient à un autre utilisateur.")
    db.delete(view)
    db.commit()
    return {"message": f"Vue « {view.name} » supprimée."}

# Pieces jointes des alertes (justificatifs d'instruction)
ALERT_EVIDENCE_DIR = PROJECT_ROOT / "alert_evidence"

@app.post("/api/alerts/{alert_id}/attachments")
async def add_alert_attachment(
    alert_id: int,
    file: UploadFile = File(...),
    comment: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Attache une piece justificative a une alerte (meme motif de stockage
    que les preuves de liste blanche)."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    safe_name = re.sub(r"[^\w.\-]", "_", file.filename or "piece_jointe")
    ALERT_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    target_path = ALERT_EVIDENCE_DIR / f"{alert_id}_{uuid.uuid4().hex[:8]}_{safe_name}"
    with open(target_path, "wb") as out:
        shutil.copyfileobj(file.file, out)
    attachment = AlertAttachment(
        alert_id=alert_id, file_name=safe_name, file_path=str(target_path),
        comment=(comment or "").strip() or None, uploaded_by=current_user["username"],
    )
    db.add(attachment)
    db.flush()
    _log_alert_event(db, alert_id, current_user["username"], "ATTACHMENT",
                     f"Pièce jointe ajoutée : {safe_name}." + (f" {comment.strip()}" if comment and comment.strip() else ""))
    db.commit()
    return {"message": "Pièce jointe ajoutée.", "attachment_id": attachment.id, "file_name": safe_name}

@app.get("/api/alerts/attachments/{attachment_id}")
async def download_alert_attachment(
    attachment_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Telecharge une piece jointe d'alerte."""
    attachment = db.query(AlertAttachment).filter(AlertAttachment.id == attachment_id).first()
    if not attachment:
        raise HTTPException(status_code=404, detail="Pièce jointe introuvable.")
    file_path = Path(attachment.file_path)
    if not file_path.exists() or ALERT_EVIDENCE_DIR.resolve() not in file_path.resolve().parents:
        raise HTTPException(status_code=404, detail="Fichier indisponible.")
    return FileResponse(str(file_path), filename=attachment.file_name)

@app.get("/api/alerts/{alert_id}/report", response_class=HTMLResponse)
async def alert_report(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Rapport d'alerte autonome et imprimable (impression navigateur -> PDF) :
    identites, score et arbre de decision, historique complet des actions
    4-yeux, pieces jointes — pret pour un controle ACPR/FED. Aucune dependance.
    """
    from html import escape
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    audit = db.query(AuditTrail).filter(AuditTrail.id == alert.audit_id).first()
    events = db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id) \
               .order_by(AlertEvent.timestamp.asc(), AlertEvent.id.asc()).all()
    attachments = db.query(AlertAttachment).filter(AlertAttachment.alert_id == alert_id).all()
    tree = (audit.decision_tree or {}) if audit else {}

    def dt(value):
        return value.strftime("%d/%m/%Y %H:%M UTC") if value else "—"

    def field(label, value):
        return f"<tr><th>{escape(label)}</th><td>{escape(str(value if value not in (None, '') else '—'))}</td></tr>"

    adjustments = tree.get("adjustments") or {}
    adj_rows = "".join(
        f"<tr><th>{escape(name)}</th><td>{escape(str((a or {}).get('score', '—')))} — {escape(str((a or {}).get('description', '')))}</td></tr>"
        for name, a in adjustments.items()
    )
    fp_rule = tree.get("fp_rule_applied") or {}
    event_rows = "".join(
        f"<tr><td>{dt(e.timestamp)}</td><td>{escape(e.username)}</td>"
        f"<td>{escape(e.action)}</td><td>{escape(e.detail or '')}</td></tr>"
        for e in events
    )
    attachment_rows = "".join(
        f"<li>{escape(att.file_name)} — déposée par {escape(att.uploaded_by)} le {dt(att.uploaded_at)}"
        f"{(' : ' + escape(att.comment)) if att.comment else ''}</li>"
        for att in attachments
    ) or "<li>Aucune pièce jointe.</li>"

    html_doc = f"""<!DOCTYPE html>
<html lang="fr"><head><meta charset="utf-8">
<title>Rapport d'alerte #{alert.id} — Fiskr</title>
<style>
  body {{ font-family: Georgia, 'Times New Roman', serif; color: #111; margin: 2.2cm; line-height: 1.45; }}
  h1 {{ font-size: 1.4rem; border-bottom: 2px solid #111; padding-bottom: 0.3rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 1.6rem; border-bottom: 1px solid #999; padding-bottom: 0.2rem; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 0.85rem; margin-top: 0.5rem; }}
  th, td {{ border: 1px solid #bbb; padding: 0.35rem 0.6rem; text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; width: 220px; font-weight: 600; }}
  .events th {{ width: auto; }}
  .meta {{ color: #555; font-size: 0.8rem; }}
  .noprint {{ margin: 1rem 0; }}
  @media print {{ .noprint {{ display: none; }} body {{ margin: 0.5cm; }} }}
</style></head><body>
<div class="noprint"><button onclick="window.print()">🖨 Imprimer / Enregistrer en PDF</button></div>
<h1>Rapport d'alerte #{alert.id} — Fiskr</h1>
<p class="meta">Généré le {datetime.utcnow().strftime("%d/%m/%Y %H:%M UTC")} par {escape(current_user["username"])} —
document de travail issu du journal d'audit immuable (audit #{alert.audit_id}).</p>

<h2>1. Synthèse</h2>
<table>
{field("Canal", "Filtrage transactionnel" if alert.channel == "FILTERING" else "Criblage clients")}
{field("Statut", alert.status)}{field("Priorité", alert.priority)}
{field("Échéance SLA", dt(alert.due_at))}
{field("Client / partie", f"{alert.client_name} ({alert.client_id or '—'})")}
{field("Fiche listée", f"{alert.watchlist_name} ({alert.watchlist_entity_id})")}
{field("Liste", alert.list_type)}{field("Score final", f"{alert.final_score:.1f} %")}
{field("Créée le", dt(alert.created_at))}{field("Assignée à", alert.assigned_to)}
</table>

<h2>2. Décision</h2>
<table>
{field("Décision proposée", alert.proposed_decision)}{field("Proposée par", alert.proposed_by)}
{field("Proposée le", dt(alert.proposed_at))}{field("Commentaire de proposition", alert.proposal_comment)}
{field("Décidée par", alert.decided_by)}{field("Décidée le", dt(alert.decided_at))}
{field("Commentaire de décision", alert.decision_comment)}
</table>

<h2>3. Arbre de décision du moteur</h2>
<table>
{field("Score de base", tree.get("base_score"))}
{field("Hard match", "Oui — " + str(tree.get("hard_match_details", "")) if tree.get("hard_match_triggered") else "Non")}
{field("Seuil appliqué", tree.get("cut_off_applied"))}
{field("Version de watchlist", audit.watchlist_version if audit else None)}
{adj_rows}
{field("Règle anti-FP appliquée", f"{fp_rule.get('name')} (v{fp_rule.get('version')})" if fp_rule else None)}
</table>

<h2>4. Historique des actions (append-only)</h2>
<table class="events"><tr><th>Horodatage</th><th>Utilisateur</th><th>Action</th><th>Détail</th></tr>
{event_rows or '<tr><td colspan="4">Aucun événement.</td></tr>'}
</table>

<h2>5. Pièces jointes</h2>
<ul>{attachment_rows}</ul>
</body></html>"""
    return HTMLResponse(content=html_doc)

@app.post("/api/alerts/{alert_id}/narrative")
async def generate_narrative(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Génère un PROJET de narratif d'investigation depuis les données tracées
    de l'alerte (decision_tree, identités, historique). Déterministe par
    construction, reformulation LLM optionnelle (narrative.llm_enabled).
    Jamais de décision automatique : le narratif est un brouillon à relire.
    """
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alerte introuvable.")
    audit = db.query(AuditTrail).filter(AuditTrail.id == alert.audit_id).first()
    events = db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id) \
               .order_by(AlertEvent.timestamp.asc(), AlertEvent.id.asc()).all()
    narrative, llm_used = generate_alert_narrative(alert, audit, events)
    _log_alert_event(
        db, alert.id, current_user["username"], "NARRATIVE",
        f"Projet de narratif généré ({'reformulation LLM' if llm_used else 'composeur déterministe'})."
    )
    db.commit()
    return {"narrative": narrative, "llm_used": llm_used, "alert_id": alert.id}

# ------------------ LISTE BLANCHE CLIENT x LISTE & RE-CRIBLAGE ------------------

# Pieces justificatives des mises en liste blanche (valeur probante en audit)
WHITELIST_EVIDENCE_DIR = PROJECT_ROOT / "whitelist_evidence"

class WhitelistRevokeRequest(BaseModel):
    comment: str

class RescreenRunRequest(BaseModel):
    file_type: Optional[str] = None

def _whitelist_summary(pair: WhitelistPair) -> Dict[str, Any]:
    now = datetime.utcnow()
    if pair.revoked_at:
        state = "REVOKED"
    elif pair.expires_at and pair.expires_at <= now:
        state = "EXPIRED"
    else:
        state = "ACTIVE"
    return {
        "id": pair.id,
        "client_id": pair.client_id,
        "client_name": pair.client_name,
        "watchlist_entity_id": pair.watchlist_entity_id,
        "watchlist_name": pair.watchlist_name,
        "list_type": pair.list_type,
        "justification": pair.justification,
        "evidence_file_name": pair.evidence_file_name,
        "created_by": pair.created_by,
        "created_at": pair.created_at.isoformat() if pair.created_at else None,
        "expires_at": pair.expires_at.isoformat() if pair.expires_at else None,
        "revoked_by": pair.revoked_by,
        "revoked_at": pair.revoked_at.isoformat() if pair.revoked_at else None,
        "revoke_comment": pair.revoke_comment,
        "state": state,
    }

@app.post("/api/whitelist")
async def create_whitelist_pair(
    client_id: str = Form(...),
    watchlist_entity_id: str = Form(...),
    justification: Optional[str] = Form(None),
    expires_at: Optional[str] = Form(None),
    client_name: Optional[str] = Form(None),
    watchlist_name: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    Met une paire client x liste en liste blanche (« Good Guys ») : les
    alertes futures de cette paire sont supprimees de facon TRACEE (statut
    WHITELISTED dans le journal d'audit). Justification et piece jointe
    exigees selon les reglages modulaires.
    """
    client_id = client_id.strip()
    watchlist_entity_id = watchlist_entity_id.strip()
    if not client_id or not watchlist_entity_id:
        raise HTTPException(status_code=400, detail="client_id et watchlist_entity_id sont requis.")

    if is_whitelisted(db, client_id, watchlist_entity_id):
        raise HTTPException(status_code=409, detail="Une paire active existe déjà pour ce couple client × listé.")

    requirements = whitelist_requirements(db)
    justification = (justification or "").strip()
    if requirements["justification_required"] and not justification:
        raise HTTPException(status_code=400, detail="Une justification est obligatoire pour une mise en liste blanche (réglage actif).")
    if requirements["file_required"] and (file is None or not file.filename):
        raise HTTPException(status_code=400, detail="Une pièce jointe justificative est obligatoire pour une mise en liste blanche (réglage actif).")

    expires_dt = None
    if expires_at and expires_at.strip():
        try:
            expires_dt = datetime.strptime(expires_at.strip()[:10], "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="Format de date d'expiration invalide (attendu: YYYY-MM-DD).")

    evidence_name = None
    evidence_path = None
    if file is not None and file.filename:
        safe_name = os.path.basename(file.filename).replace("..", "_")
        WHITELIST_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        target_path = WHITELIST_EVIDENCE_DIR / f"{uuid.uuid4().hex[:8]}_{safe_name}"
        with open(target_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        evidence_name = safe_name
        evidence_path = str(target_path)

    # Type de liste derive cote serveur (jamais fourni par le client) :
    # cache de production d'abord, sinon la derniere alerte de la paire
    list_type = next(
        (e.get("_list_type") for e in watchlist_store if e.get("entity_id") == watchlist_entity_id),
        None
    )
    if list_type is None:
        last_alert = db.query(Alert).filter(
            Alert.client_id == client_id,
            Alert.watchlist_entity_id == watchlist_entity_id,
            Alert.list_type.isnot(None)
        ).order_by(Alert.created_at.desc()).first()
        list_type = last_alert.list_type if last_alert else None

    pair = WhitelistPair(
        client_id=client_id,
        watchlist_entity_id=watchlist_entity_id,
        client_name=(client_name or "").strip() or None,
        watchlist_name=(watchlist_name or "").strip() or None,
        list_type=list_type,
        justification=justification or None,
        evidence_file_name=evidence_name,
        evidence_file_path=evidence_path,
        created_by=reviewer["username"],
        expires_at=expires_dt,
    )
    db.add(pair)
    db.commit()
    db.refresh(pair)
    return {"message": "Paire mise en liste blanche.", **_whitelist_summary(pair)}

class WhitelistBulkPair(BaseModel):
    client_id: str
    watchlist_entity_id: str
    client_name: Optional[str] = None
    watchlist_name: Optional[str] = None
    list_type: Optional[str] = None

class WhitelistBulkRequest(BaseModel):
    pairs: List[WhitelistBulkPair]
    justification: Optional[str] = None

@app.post("/api/whitelist/bulk")
async def create_whitelist_pairs_bulk(
    payload: WhitelistBulkRequest,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """
    « Good Guys » en masse (cahier de tests d'homologation) : met plusieurs
    paires client x liste en liste blanche avec une justification commune.
    Les paires deja actives sont sautees (pas d'echec global). La piece jointe
    eventuelle reste du ressort de la pose unitaire.
    """
    if not payload.pairs:
        raise HTTPException(status_code=400, detail="Aucune paire fournie.")
    if len(payload.pairs) > 500:
        raise HTTPException(status_code=400, detail="Maximum 500 paires par appel.")

    requirements = whitelist_requirements(db)
    justification = (payload.justification or "").strip()
    if requirements["justification_required"] and not justification:
        raise HTTPException(
            status_code=400,
            detail="Une justification commune est obligatoire pour une mise en liste blanche (réglage actif)."
        )

    created, skipped = [], []
    for p in payload.pairs:
        client_id = p.client_id.strip()
        entity_id = p.watchlist_entity_id.strip()
        if not client_id or not entity_id:
            skipped.append({"client_id": client_id, "watchlist_entity_id": entity_id, "reason": "identifiants vides"})
            continue
        if is_whitelisted(db, client_id, entity_id):
            skipped.append({"client_id": client_id, "watchlist_entity_id": entity_id, "reason": "paire déjà active"})
            continue
        # Type de liste : fourni par le rapport de backtest, sinon derive du cache
        list_type = (p.list_type or "").strip() or next(
            (e.get("_list_type") for e in watchlist_store if e.get("entity_id") == entity_id), None
        )
        pair = WhitelistPair(
            client_id=client_id,
            watchlist_entity_id=entity_id,
            client_name=(p.client_name or "").strip() or None,
            watchlist_name=(p.watchlist_name or "").strip() or None,
            list_type=list_type,
            justification=justification or None,
            created_by=reviewer["username"],
        )
        db.add(pair)
        created.append({"client_id": client_id, "watchlist_entity_id": entity_id})
    db.commit()
    return {
        "message": f"{len(created)} paire(s) mise(s) en liste blanche, {len(skipped)} sautée(s).",
        "created": created,
        "skipped": skipped,
    }

@app.get("/api/whitelist")
async def list_whitelist_pairs(
    active_only: bool = Query(False),
    list_type: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Liste des paires en liste blanche avec leur etat (active / expiree / revoquee)."""
    query = db.query(WhitelistPair)
    if active_only:
        now = datetime.utcnow()
        query = query.filter(
            WhitelistPair.revoked_at.is_(None),
            (WhitelistPair.expires_at.is_(None)) | (WhitelistPair.expires_at > now)
        )
    query = _apply_list_type_filter(query, WhitelistPair.list_type, list_type)
    total = query.count()
    rows = query.order_by(WhitelistPair.created_at.desc()) \
                .offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size,
            "items": [_whitelist_summary(p) for p in rows]}

@app.post("/api/whitelist/{pair_id}/revoke")
async def revoke_whitelist_pair(
    pair_id: int,
    payload: WhitelistRevokeRequest,
    db: Session = Depends(get_db),
    reviewer: Dict[str, Any] = Depends(require_reviewer)
):
    """Revocation douce d'une paire (motif obligatoire) : les alertes reprennent."""
    pair = db.query(WhitelistPair).filter(WhitelistPair.id == pair_id).first()
    if not pair:
        raise HTTPException(status_code=404, detail="Paire introuvable.")
    if pair.revoked_at:
        raise HTTPException(status_code=409, detail="Paire déjà révoquée.")
    comment = (payload.comment or "").strip()
    if not comment:
        raise HTTPException(status_code=400, detail="Un motif est requis pour révoquer une paire.")
    pair.revoked_by = reviewer["username"]
    pair.revoked_at = datetime.utcnow()
    pair.revoke_comment = comment
    log_admin_action(db, reviewer["username"], "WHITELIST_REVOKED",
                     target=f"{pair.client_id} × {pair.watchlist_entity_id}",
                     detail=comment)
    db.commit()
    return {"message": "Paire révoquée : les alertes de ce couple reprendront.", **_whitelist_summary(pair)}

@app.get("/api/whitelist/evidence/{pair_id}")
async def download_whitelist_evidence(
    pair_id: int,
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Telecharge la piece justificative d'une mise en liste blanche (audit)."""
    pair = db.query(WhitelistPair).filter(WhitelistPair.id == pair_id).first()
    if not pair or not pair.evidence_file_path:
        raise HTTPException(status_code=404, detail="Aucune pièce justificative pour cette paire.")
    file_path = Path(pair.evidence_file_path)
    if not file_path.exists() or WHITELIST_EVIDENCE_DIR.resolve() not in file_path.resolve().parents:
        raise HTTPException(status_code=404, detail="Pièce justificative introuvable.")
    return FileResponse(str(file_path), filename=pair.evidence_file_name or file_path.name)

@app.post("/api/rescreen/run")
async def run_manual_rescreen(
    payload: RescreenRunRequest,
    db: Session = Depends(get_db),
    admin_user: Dict[str, Any] = Depends(require_admin)
):
    """
    Lookback manuel (guidance Wolfsberg) : re-crible tout le referentiel
    clients contre les listes en production (un type donne, ou toutes).
    """
    file_type = (payload.file_type or "").strip().upper() or None
    if file_type and file_type not in WATCHLIST_FILE_TYPES:
        raise HTTPException(status_code=400, detail=f"Type de liste inconnu ({', '.join(WATCHLIST_FILE_TYPES)}).")
    result = rescreen_lookback(db, file_type)
    return {"message": "Lookback exécuté.", **result}

# ------------------ KPI CONFORMITE (PILOTAGE) ------------------

@app.get("/api/kpi")
async def get_compliance_kpis(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Indicateurs de pilotage du dispositif : volumes et statuts d'alertes,
    taux de faux positifs, delai moyen de decision, liste blanche, etat des
    listes en production et historique des synchronisations.
    """
    from sqlalchemy import func

    # Alertes par statut
    alert_counts = dict(
        db.query(Alert.status, func.count(Alert.id)).group_by(Alert.status).all()
    )
    open_alerts = sum(alert_counts.get(s, 0) for s in ALERT_OPEN_STATUSES)
    closed_fp = alert_counts.get("CLOSED_FALSE_POSITIVE", 0)
    closed_tp = alert_counts.get("CLOSED_CONFIRMED", 0)
    closed_total = closed_fp + closed_tp
    fp_rate = round(closed_fp / closed_total * 100.0, 1) if closed_total else None

    # Delai moyen de decision (creation -> cloture) sur les 500 dernieres closes
    closed_rows = db.query(Alert.created_at, Alert.decided_at).filter(
        Alert.status.in_(ALERT_CLOSED_STATUSES),
        Alert.decided_at.isnot(None)
    ).order_by(Alert.decided_at.desc()).limit(500).all()
    if closed_rows:
        avg_hours = sum(
            (decided - created).total_seconds() for created, decided in closed_rows
        ) / len(closed_rows) / 3600.0
        avg_decision_hours = round(avg_hours, 1)
    else:
        avg_decision_hours = None

    # Liste blanche active
    now = datetime.utcnow()
    active_whitelist = db.query(WhitelistPair).filter(
        WhitelistPair.revoked_at.is_(None),
        (WhitelistPair.expires_at.is_(None)) | (WhitelistPair.expires_at > now)
    ).count()

    # Listes en production (entites par type) et snapshots par statut
    ready_by_type = dict(
        db.query(Snapshot.file_type, func.sum(Snapshot.record_count))
          .filter(Snapshot.status == "READY", Snapshot.file_type.in_(WATCHLIST_FILE_TYPES))
          .group_by(Snapshot.file_type).all()
    )
    snapshot_counts = dict(
        db.query(Snapshot.status, func.count(Snapshot.snapshot_id)).group_by(Snapshot.status).all()
    )

    # Decisions d'audit par statut (volumetrie de criblage)
    audit_counts = dict(
        db.query(AuditTrail.status, func.count(AuditTrail.id)).group_by(AuditTrail.status).all()
    )

    # Dernieres synchronisations
    recent_syncs = db.query(SyncReport).order_by(SyncReport.executed_at.desc()).limit(15).all()

    # ---- Series temporelles 30 jours (accueil / tendances) ----
    # func.date() est valide sur SQLite ET PostgreSQL
    since = now - timedelta(days=30)
    created_rows = (
        db.query(func.date(Alert.created_at), Alert.channel, func.count(Alert.id))
          .filter(Alert.created_at >= since)
          .group_by(func.date(Alert.created_at), Alert.channel).all()
    )
    closed_rows_series = (
        db.query(func.date(Alert.decided_at), func.count(Alert.id))
          .filter(Alert.decided_at.isnot(None), Alert.decided_at >= since)
          .group_by(func.date(Alert.decided_at)).all()
    )
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    created_map: Dict[str, Dict[str, int]] = {}
    for day, channel, count in created_rows:
        day_key = str(day)[:10]
        created_map.setdefault(day_key, {})[channel or "SCREENING"] = int(count)
    closed_map = {str(day)[:10]: int(count) for day, count in closed_rows_series}
    timeseries = [
        {
            "date": d,
            "created_screening": created_map.get(d, {}).get("SCREENING", 0),
            "created_filtering": created_map.get(d, {}).get("FILTERING", 0),
            "closed": closed_map.get(d, 0),
        }
        for d in days
    ]

    # ---- Ventilations : alertes ouvertes par liste, traitement par analyste ----
    open_by_list = dict(
        db.query(Alert.list_type, func.count(Alert.id))
          .filter(Alert.status.in_(ALERT_OPEN_STATUSES))
          .group_by(Alert.list_type).all()
    )
    analyst_rows = (
        db.query(Alert.decided_by, func.count(Alert.id))
          .filter(Alert.status.in_(["CLOSED_CONFIRMED", "CLOSED_FALSE_POSITIVE"]),
                  Alert.decided_by.isnot(None))
          .group_by(Alert.decided_by).all()
    )
    by_analyst = []
    for username, decided_count in sorted(analyst_rows, key=lambda r: -r[1]):
        pair_rows = db.query(Alert.created_at, Alert.decided_at).filter(
            Alert.decided_by == username, Alert.decided_at.isnot(None)
        ).order_by(Alert.decided_at.desc()).limit(200).all()
        avg_h = (
            round(sum((dec - cre).total_seconds() for cre, dec in pair_rows) / len(pair_rows) / 3600.0, 1)
            if pair_rows else None
        )
        by_analyst.append({"analyst": username, "decided": int(decided_count), "avg_decision_hours": avg_h})

    # ---- Efficacite des regles anti-faux positifs (hit_count en base) ----
    fp_rules_stats = [
        {
            "id": r.id, "name": r.name, "channel": r.channel, "status": r.status,
            "version": r.version, "enabled": bool(r.enabled), "hit_count": int(r.hit_count or 0),
        }
        for r in db.query(FpRule)
                   .filter(FpRule.status == "ACTIVE")
                   .order_by(FpRule.hit_count.desc()).limit(20).all()
    ]

    # Alertes ouvertes les plus anciennes (liste « à traiter » de l'accueil)
    oldest_open = (
        db.query(Alert)
          .filter(Alert.status.in_(ALERT_OPEN_STATUSES))
          .order_by(Alert.created_at.asc()).limit(5).all()
    )

    return {
        "alerts": {
            "by_status": alert_counts,
            "open": open_alerts,
            "open_by_list_type": {k or "UNKNOWN": int(v) for k, v in open_by_list.items()},
            "closed_false_positive": closed_fp,
            "closed_confirmed": closed_tp,
            "false_positive_rate_pct": fp_rate,
            "avg_decision_hours": avg_decision_hours,
            "timeseries_30d": timeseries,
            "by_analyst": by_analyst,
            "oldest_open": [
                {
                    "id": a.id, "client_name": a.client_name, "watchlist_name": a.watchlist_name,
                    "channel": a.channel, "status": a.status, "final_score": float(a.final_score or 0.0),
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in oldest_open
            ],
        },
        "fp_rules": fp_rules_stats,
        "whitelist_active_pairs": active_whitelist,
        "screening": {"decisions_by_status": audit_counts},
        "lists": {
            "production_entities_by_type": {k: int(v or 0) for k, v in ready_by_type.items()},
            "snapshots_by_status": snapshot_counts,
        },
        "recent_syncs": [
            {
                "source": r.source,
                "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                "trigger": r.trigger,
                "status": r.status,
                "added": r.added_count, "modified": r.modified_count, "removed": r.removed_count,
            }
            for r in recent_syncs
        ],
    }

# Serve static dashboard
static_dir = PROJECT_ROOT / "fiskr" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/login", response_class=HTMLResponse)
@app.get("/login.html", response_class=HTMLResponse)
async def serve_login():
    login_path = static_dir / "login.html"
    if not login_path.exists():
        raise HTTPException(status_code=404, detail="Login page not found")
    with open(login_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200)

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard(request: Request):
    token = request.cookies.get("fiskr_access_token")
    if token and decode_access_token(token):
        index_path = static_dir / "index.html"
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    return RedirectResponse(url="/login", status_code=status.HTTP_302_FOUND)


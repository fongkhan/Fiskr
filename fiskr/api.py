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
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, Query, status, UploadFile, File, Form, Response, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, StreamingResponse
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
    AlertAttachment, AdminAuditLog, ALERT_PRIORITIES
)
from fiskr.alerts import open_or_redetect_alert, is_whitelisted, compute_due_at
from fiskr.notify import notify_event
from fiskr.fprules import (
    evaluate_fp_rules, build_screening_ctx, annotate_suppression, compile_rule,
    run_rule, FP_RULE_CHANNELS, RULE_TEMPLATE
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
    create_access_token, decode_access_token, parse_roles, normalize_roles
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
    SETTING_ALERT_SLA_HOURS, SETTING_NOTIFICATIONS, DEFAULT_NOTIFICATION_EVENTS
)



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
    ).all()

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

async def _daily_sync_scheduler():
    """Boucle asynchrone declenchant les synchronisations chaque matin (sync.schedule_time)."""
    while True:
        schedule_time = get_sync_config()["schedule_time"]
        try:
            hour, minute = (int(p) for p in schedule_time.split(":"))
        except ValueError:
            logger.error(f"sync.schedule_time invalide ({schedule_time}), format attendu HH:MM. Planificateur arrete.")
            return
        now = datetime.now()
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if next_run <= now:
            next_run += timedelta(days=1)
        logger.info(f"Prochaine synchronisation automatique des sources: {next_run}")
        await asyncio.sleep((next_run - now).total_seconds())
        try:
            await asyncio.to_thread(_run_scheduled_syncs)
        except Exception as e:
            logger.error(f"Echec de la synchronisation planifiee: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Fiskr application...")
    init_db()
    # Populate the cache from database
    db = next(get_db())
    load_watchlist_cache(db)
    # Start the daily source synchronization scheduler if enabled
    scheduler_task = None
    if get_sync_config()["auto_enabled"]:
        scheduler_task = asyncio.create_task(_daily_sync_scheduler())
    yield
    # Shutdown
    if scheduler_task:
        scheduler_task.cancel()
    logger.info("Stopping Fiskr application...")

app = FastAPI(
    title="Fiskr API Server",
    description="Compliance PEP/Sanctions Engine with Snapshots and Versioning Delta Engine",
    version="2.0.0",
    lifespan=lifespan
)

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


# ------------------ AUTHENTICATION ENDPOINTS ------------------

@app.post("/api/auth/login")
async def login(
    response: Response,
    request_data: LoginRequest,
    db: Session = Depends(get_db)
):
    """Authenticates user credentials and sets an HttpOnly access cookie."""
    if not request_data.username or not request_data.password:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nom d'utilisateur et mot de passe requis."
        )
        
    user = db.query(User).filter(User.username == request_data.username).first()
    if not user or not verify_password(request_data.password, user.hashed_password, user.salt):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Identifiants incorrects. Veuillez réessayer."
        )
        
    token = create_access_token({"sub": user.username, "role": user.role})
    response.set_cookie(
        key="fiskr_access_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=86400
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
async def logout(response: Response):
    """Logs out the user by clearing the authentication token cookie."""
    response.delete_cookie("fiskr_access_token")
    return {"message": "Déconnexion réussie."}

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
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Le nouveau mot de passe doit contenir au moins 6 caractères.")
        
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
            "created_at": u.created_at.isoformat() if u.created_at else None
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
        if len(payload.password.strip()) < 6:
            raise HTTPException(status_code=400, detail="Le mot de passe doit contenir au moins 6 caractères.")
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


# ------------------ DATA ENDPOINTS ------------------

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

    # Restriction eventuelle du perimetre de criblage (defaut : toutes listes).
    # Validee strictement et retiree du profil client (elle n'en fait pas partie).
    client_dict.pop("screening_lists", None)
    requested_lists = None
    if request.screening_lists:
        requested_lists = sorted({v.strip().upper() for v in request.screening_lists if v and v.strip()})
        invalid = [v for v in requested_lists if v not in WATCHLIST_FILE_TYPES]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Type(s) de liste inconnu(s) : {', '.join(invalid)} (valeurs possibles : {', '.join(WATCHLIST_FILE_TYPES)})."
            )
        if set(requested_lists) == set(WATCHLIST_FILE_TYPES):
            requested_lists = None  # toutes les listes = aucune restriction

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
            
    # Scoring
    matches = []
    best_match = None
    best_score = -1.0
    
    for item_id, candidate in candidates.items():
        score_res = match_entities(cleansed_client, candidate, config)
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
                db, audit_record, client_dict.get("client_id"), best_match, current_user["username"],
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
            "cut_off_applied": config.get("scoring", {}).get("cut_off_threshold", 75.0),
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


@app.post("/api/snapshots/ingest")
@app.post("/api/ingest")
async def ingest_snapshot(
    file_type: str = Form(...),
    file: UploadFile = File(...),
    delimiter: str = Form(","),
    ssie_selectors: Optional[str] = Form(None),
    ssie_source_format: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """
    Ingest XML, CSV or PDF files into the database.
    Performs data quality validation and saves snapshot.
    WATCHLIST_SSIE runs the Smart Sanctions Ingestion Engine pipeline
    (Discovery -> Resolution -> Restitution) with configurable tag selectors.
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
        with open(temp_file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
            
        # 2. Compute file checksum hash
        with open(temp_file_path, "rb") as f:
            content = f.read()
            fhash = hashlib.sha256(content).hexdigest()
            
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
                # OFAC Advanced XML parsing (iterparse)
                parser_stream = parse_ofac_advanced_xml(str(temp_file_path))

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
                
        # Update Snapshot status. En mode homologation, les watchlists attendent
        # un pointage humain (PENDING_REVIEW) et restent hors du cache de criblage.
        staging = file_type in WATCHLIST_FILE_TYPES and require_approval_enabled(db)
        snap.status = "PENDING_REVIEW" if staging else "READY"
        snap.record_count = record_count
        db.commit()

        # Reload cache to integrate newly loaded watchlists
        rescreen_result = None
        if file_type in WATCHLIST_FILE_TYPES and not staging:
            load_watchlist_cache(db)
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

@app.get("/api/watchlist")
async def get_watchlist(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Returns the active loaded in-memory watchlist."""
    return {
        "version": watchlist_version,
        "hash": watchlist_hash,
        "items": watchlist_store
    }

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
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    """Configuration active de la synchronisation automatique des sources."""
    cfg = get_sync_config()
    cfg["email_configured"] = bool(os.getenv("SMTP_HOST") and os.getenv("SYNC_EMAIL_TO"))
    return cfg

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
            and payload.alert_sla_hours is None and payload.notification_events is None):
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
            audit = db.query(AuditTrail).filter(AuditTrail.id == a.audit_id).first()
            tree = (audit.decision_tree if audit else {}) or {}
            ctx = {
                "channel": rule.channel,
                "client_id": a.client_id, "client_name": a.client_name,
                "entity_id": a.watchlist_entity_id, "entity_name": a.watchlist_name,
                "list_type": a.list_type, "final_score": float(a.final_score or 0.0),
                "base_score": float(tree.get("base_score", 0.0)),
                "hard_match": bool(tree.get("hard_match_triggered", False)),
                "adjustments": tree.get("adjustments") or {},
                "client": None, "entity": (tree.get("watchlist_entity") or {}),
                "party": None, "message": None,
            }
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

    report = run_backtest(db, snap, panel.snapshot_id,
                          threshold_pct=backtest_max_gap_pct(db),
                          executed_by=reviewer["username"])
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
    """S'assigner une alerte (ou l'assigner a un autre analyste : admin uniquement)."""
    alert = _get_open_alert(db, alert_id)
    assignee = (payload.assignee or "").strip() or current_user["username"]
    if assignee != current_user["username"] and "admin" not in parse_roles(current_user.get("role")):
        raise HTTPException(status_code=403, detail="Seul un administrateur peut assigner une alerte à un autre analyste.")
    alert.assigned_to = assignee
    if alert.status == "OPEN":
        alert.status = "IN_PROGRESS"
    _log_alert_event(db, alert.id, current_user["username"], "ASSIGNED", f"Assignée à {assignee}.")
    db.commit()
    return {"message": f"Alerte assignée à {assignee}.", **_alert_summary(alert)}

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


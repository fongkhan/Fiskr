import os
import uuid
import json
import asyncio
import hashlib
import logging
import shutil
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, Query, status, UploadFile, File, Form, Response, Request
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fiskr.config import config, PROJECT_ROOT
from fiskr.quality import evaluate_and_clean
from fiskr.blocking import generate_blocking_keys
from fiskr.scoring import match_entities
from fiskr.delta import calculate_delta
from fiskr.ingest import parse_ofac_advanced_xml, parse_csv_file, parse_pdf_watchlist
from fiskr.ssie import parse_ssie_xml, merge_ssie_selectors, DEFAULT_SOURCE_FORMAT
from fiskr.database import (
    get_db, init_db, log_compliance_decision, AuditTrail, Snapshot,
    WatchlistEntity, ClientEntity, compute_checksum, User, verify_password, hash_password,
    SyncReport
)
from fiskr.sync import (
    run_ofac_sync, run_eurlex_sync, get_sync_config, EURLEX_ARCHIVE_DIR,
    _supersede_previous_snapshots, _snapshot_entity_dicts, _latest_ready_snapshot,
    _truncate_delta_details
)
from fiskr.names import ensure_parsed_name
from fiskr.auth import (
    get_current_user, require_admin, require_reviewer, create_access_token,
    decode_access_token, parse_roles, normalize_roles
)
from fiskr.settings import (
    require_approval_enabled, exclusion_requirements, get_setting_with_source, set_setting,
    SETTING_REQUIRE_APPROVAL, SETTING_EXCLUSION_JUSTIFICATION_REQUIRED, SETTING_EXCLUSION_FILE_REQUIRED
)



logger = logging.getLogger("fiskr.api")

# Snapshot file types persisted as WatchlistEntity records
WATCHLIST_FILE_TYPES = ["WATCHLIST_OFAC", "WATCHLIST_EU", "WATCHLIST_SSIE"]

# In-memory index cache
watchlist_store: List[Dict[str, Any]] = []
watchlist_index: Dict[str, List[Dict[str, Any]]] = {}
watchlist_version: str = "Database Active Snapshot"
watchlist_hash: str = "N/A"

def load_watchlist_cache(db: Session):
    """Loads the active READY watchlist entities from the database into the in-memory cache."""
    global watchlist_store, watchlist_index, watchlist_hash
    
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
    entities = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_(snapshot_ids),
        WatchlistEntity.excluded.isnot(True)
    ).all()
    
    temp_store = []
    temp_index = {}
    
    for ent in entities:
        # Convert SQLAlchemy object to dictionary for cache
        ent_dict = {c.name: getattr(ent, c.name) for c in ent.__table__.columns}
        temp_store.append(ent_dict)
        
        # Index by blocking key
        keys = generate_blocking_keys(ent_dict, config)
        for k in keys:
            if k not in temp_index:
                temp_index[k] = []
            temp_index[k].append(ent_dict)
            
    watchlist_store = temp_store
    watchlist_index = temp_index
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
    try:
        if sync_cfg["ofac"]["enabled"]:
            run_ofac_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db))
        if sync_cfg["eurlex"]["enabled"]:
            run_eurlex_sync(db, trigger="SCHEDULED", reload_cache=lambda: load_watchlist_cache(db))
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
    client_id: Optional[str] = Field(None, example="CUST-0091")
    client_type: str = Field(..., example="PP", description="PP (Individu) ou PM (Entreprise)")
    client_first_name: Optional[str] = Field(None, example="Vladimir")
    client_last_name: Optional[str] = Field(None, example="Putin")
    client_maiden_name: Optional[str] = Field(None, example="")
    client_company_name: Optional[str] = Field(None, example="")
    client_dob: Optional[str] = Field(None, example="1952-10-07")
    client_gender: Optional[str] = Field("U", example="M")
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
    client_lei_number: Optional[str] = None
    
    client_national_registry_ids: List[Dict[str, Any]] = []
    client_other_registration_ids: List[Dict[str, Any]] = []
    client_passport_documents: List[Dict[str, Any]] = []
    client_national_id_documents: List[Dict[str, Any]] = []
    client_other_id_documents: List[Dict[str, Any]] = []

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
    
    # Generate blocking keys
    client_keys = generate_blocking_keys(cleansed_client, config)
    
    # Retrieve candidates matching blocking keys
    candidates = {}
    for key in client_keys:
        for item in watchlist_index.get(key, []):
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
    if best_match:
        audit_record = log_compliance_decision(
            db,
            client_dict,
            best_match["watchlist_entity"],
            best_match,
            watchlist_version,
            watchlist_hash
        )
        audit_id = audit_record.id
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
            "cut_off_applied": config.get("scoring", {}).get("cut_off_threshold", 75.0)
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
        "audit_trail_id": audit_id
    }

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
        if file_type in ("WATCHLIST_OFAC", "WATCHLIST_SSIE"):
            if file_type == "WATCHLIST_SSIE":
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
                    alternative_addresses=alt_addrs_ofac,
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
                        alternative_addresses=alt_addrs_pdf,
                        imo_number=item.get("imo_number"),
                        entity_checksum=ent_checksum
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
                        alternative_addresses=alt_addrs_csv,
                        lei_number=item.get("lei_number"),
                        entity_checksum=ent_checksum
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
        if file_type in WATCHLIST_FILE_TYPES and not staging:
            load_watchlist_cache(db)

        if staging:
            message = (
                f"{record_count} fiches importées, snapshot en attente d'homologation "
                "(pointage humain requis avant mise en production)."
            )
        else:
            message = f"Successfully imported {record_count} items."
        return {
            "message": message,
            "snapshot_id": snap_id,
            "record_count": record_count,
            "status": snap.status
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
    alternative_addresses: Optional[str] = None
    date_of_death: Optional[str] = None

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
        "alternative_addresses": alt_addrs
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
        alternative_addresses=ent_dict["alternative_addresses"],
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

@app.get("/api/watchlist")
async def get_watchlist(current_user: Dict[str, Any] = Depends(get_current_user)):
    """Returns the active loaded in-memory watchlist."""
    return {
        "version": watchlist_version,
        "hash": watchlist_hash,
        "items": watchlist_store
    }

@app.get("/api/history")
async def get_audit_history(
    db: Session = Depends(get_db),
    current_user: Dict[str, Any] = Depends(get_current_user)
):
    return db.query(AuditTrail).order_by(AuditTrail.timestamp.desc()).all()

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
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Source inconnue (valeurs possibles: OFAC, EURLEX)."
        )

    return _serialize_sync_report(report)

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

class ReviewDecisionRequest(BaseModel):
    comment: Optional[str] = None

class ExclusionRemoveRequest(BaseModel):
    entity_ids: List[int]

def _settings_payload(db: Session) -> Dict[str, Any]:
    approval = get_setting_with_source(db, SETTING_REQUIRE_APPROVAL, False)
    justif = get_setting_with_source(db, SETTING_EXCLUSION_JUSTIFICATION_REQUIRED, True)
    evidence = get_setting_with_source(db, SETTING_EXCLUSION_FILE_REQUIRED, False)
    return {
        "require_approval": bool(approval["value"]),
        "exclusion_justification_required": bool(justif["value"]),
        "exclusion_file_required": bool(evidence["value"]),
        "sources": {
            "require_approval": approval["source"],
            "exclusion_justification_required": justif["source"],
            "exclusion_file_required": evidence["source"],
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
    }
    changed = {k: v for k, v in updates.items() if v is not None}
    if not changed:
        raise HTTPException(status_code=400, detail="Aucun réglage fourni.")
    for key, value in changed.items():
        set_setting(db, key, bool(value), updated_by=admin_user["username"])
    return {"message": "Réglages d'homologation mis à jour.", **_settings_payload(db)}

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

    snap.status = "READY"
    snap.reviewed_by = reviewer["username"]
    snap.reviewed_at = datetime.utcnow()
    snap.review_comment = (payload.comment or "").strip() or None
    _supersede_previous_snapshots(db, snap.file_type, snap.snapshot_id)
    db.commit()
    load_watchlist_cache(db)
    return {
        "message": "Snapshot approuvé et promu en production.",
        "snapshot_id": snapshot_id,
        "status": snap.status,
        "excluded_count": len(excluded_rows),
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


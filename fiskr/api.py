import os
import uuid
import json
import hashlib
import logging
import shutil
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, Query, status, UploadFile, File, Form
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fiskr.config import config, PROJECT_ROOT
from fiskr.quality import evaluate_and_clean
from fiskr.blocking import generate_blocking_keys
from fiskr.scoring import match_entities
from fiskr.delta import calculate_delta
from fiskr.ingest import parse_ofac_advanced_xml, parse_csv_file, parse_pdf_watchlist
from fiskr.database import (
    get_db, init_db, log_compliance_decision, AuditTrail, Snapshot, 
    WatchlistEntity, ClientEntity, compute_checksum
)

logger = logging.getLogger("fiskr.api")

# In-memory index cache
watchlist_store: List[Dict[str, Any]] = []
watchlist_index: Dict[str, List[Dict[str, Any]]] = {}
watchlist_version: str = "Database Active Snapshot"
watchlist_hash: str = "N/A"

def load_watchlist_cache(db: Session):
    """Loads the active READY watchlist entities from the database into the in-memory cache."""
    global watchlist_store, watchlist_index, watchlist_hash
    
    # 1. Look for latest READY snapshots in DB of types WATCHLIST_OFAC / WATCHLIST_EU
    snapshots = db.query(Snapshot).filter(
        Snapshot.file_type.in_(["WATCHLIST_OFAC", "WATCHLIST_EU"]),
        Snapshot.status == "READY"
    ).order_by(Snapshot.uploaded_at.desc()).all()
    
    if not snapshots:
        # Fallback: Ingest watchlist.json if it exists to seed the database
        seed_watchlist_json(db)
        # Re-fetch
        snapshots = db.query(Snapshot).filter(
            Snapshot.file_type.in_(["WATCHLIST_OFAC", "WATCHLIST_EU"]),
            Snapshot.status == "READY"
        ).order_by(Snapshot.uploaded_at.desc()).all()
        
    if not snapshots:
        logger.warning("No watchlist snapshots found in database to load cache.")
        return
        
    # Get active watchlist hash
    active_hash = snapshots[0].file_hash
    watchlist_hash = active_hash
    
    # Load all entities for these active snapshots
    snapshot_ids = [s.snapshot_id for s in snapshots]
    entities = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snapshot_ids)).all()
    
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Fiskr application...")
    init_db()
    # Populate the cache from database
    db = next(get_db())
    load_watchlist_cache(db)
    yield
    # Shutdown
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

# ------------------ ENDPOINTS ------------------

@app.post("/api/screen")
async def screen_client(request: ScreenClientRequest, db: Session = Depends(get_db)):
    """
    Screens a client profile against active watchlists in-memory cache.
    1. Runs Data Quality Gate evaluation.
    2. Runs exact Hard Match priority sequences.
    3. Runs fuzzy matching and contextual adjustment calculations.
    """
    client_dict = request.model_dump()
    
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

@app.post("/api/ingest")
async def ingest_file(
    file_type: str = Form(...), # WATCHLIST_OFAC, WATCHLIST_EU, CLIENT_BASE
    file: UploadFile = File(...),
    delimiter: str = Form(","),
    db: Session = Depends(get_db)
):
    """
    Ingest XML, CSV or PDF files into the database.
    Performs data quality validation and saves snapshot.
    """
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
        for fs in failed_snapshots:
            db.delete(fs)
        if failed_snapshots:
            db.commit()
            
        # Validate that snapshot hash doesn't already exist to prevent redundant work
        exists = db.query(Snapshot).filter(Snapshot.file_hash == fhash).first()
        if exists:
            # Snapshot already loaded, we can skip or reload. Let's reuse it.
            return {
                "message": "Snapshot with this hash already uploaded.",
                "snapshot_id": exists.snapshot_id,
                "record_count": exists.record_count
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
        if file_type == "WATCHLIST_OFAC":
            # XML parsing (iterparse)
            for item in parse_ofac_advanced_xml(str(temp_file_path)):
                # Validate quality gate
                report = evaluate_and_clean(item)
                if not report["is_valid"]:
                    continue
                    
                # Create checksum
                ent_checksum = compute_checksum(item)
                
                parsed_name = item.get("individual_name_parsed") or {}
                
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
                    report = evaluate_and_clean(item)
                    if not report["is_valid"]:
                        continue
                    ent_checksum = compute_checksum(item)
                    
                    db_ent = WatchlistEntity(
                        snapshot_id=snap_id,
                        entity_id=item.get("entity_id"),
                        entity_type=item.get("entity_type"),
                        primary_name=report["cleansed_name"],
                        individual_name_parsed={"first_name": "", "last_name": "", "maiden_name": ""},
                        aliases={"high_priority": [], "low_priority": []},
                        dates_of_birth=[],
                        is_deceased=False,
                        gender="U",
                        countries=item.get("countries", {}),
                        imo_number=item.get("imo_number"),
                        entity_checksum=ent_checksum
                    )
                    db.add(db_ent)
                    record_count += 1
            else:
                for item in parse_csv_file(str(temp_file_path), delimiter=delimiter):
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
                    db_ent = WatchlistEntity(
                        snapshot_id=snap_id,
                        entity_id=item.get("entity_id") or item.get("id") or str(uuid.uuid4())[:8],
                        entity_type=etype,
                        primary_name=report["cleansed_name"],
                        individual_name_parsed={"first_name": "", "last_name": "", "maiden_name": ""},
                        aliases=aliases,
                        dates_of_birth=dob_arr,
                        is_deceased=False,
                        gender=report["resolved_gender"],
                        countries=countries,
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
                
        # Update Snapshot status
        snap.status = "READY"
        snap.record_count = record_count
        db.commit()
        
        # Reload cache to integrate newly loaded watchlists
        if file_type in ["WATCHLIST_OFAC", "WATCHLIST_EU"]:
            load_watchlist_cache(db)
            
        return {
            "message": f"Successfully imported {record_count} items.",
            "snapshot_id": snap_id,
            "record_count": record_count
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
async def get_snapshots(db: Session = Depends(get_db)):
    """Lists loaded snapshots."""
    return db.query(Snapshot).order_by(Snapshot.uploaded_at.desc()).all()

@app.post("/api/snapshots/compare")
async def compare_snapshots(request: DeltaRequest, db: Session = Depends(get_db)):
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
    if snap_old.file_type in ["WATCHLIST_OFAC", "WATCHLIST_EU"]:
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

@app.get("/api/watchlist")
async def get_watchlist():
    """Returns the active loaded in-memory watchlist."""
    return {
        "version": watchlist_version,
        "hash": watchlist_hash,
        "items": watchlist_store
    }

@app.get("/api/history")
async def get_audit_history(db: Session = Depends(get_db)):
    return db.query(AuditTrail).order_by(AuditTrail.timestamp.desc()).all()

@app.get("/api/config")
async def get_active_config():
    return config

@app.post("/api/snapshots/purge")
async def purge_failed_snapshots(db: Session = Depends(get_db)):
    """
    Purges (deletes) all snapshots and their associated entities (watchlist and clients)
    that are in status 'ERROR' or 'PROCESSING' (aborted/failed).
    """
    try:
        # Find failed / processing snapshots
        failed_snapshots = db.query(Snapshot).filter(Snapshot.status.in_(["ERROR", "PROCESSING"])).all()
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

# Serve static dashboard
static_dir = PROJECT_ROOT / "fiskr" / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    index_path = static_dir / "index.html"
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200)

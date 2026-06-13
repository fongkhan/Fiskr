import os
import json
import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from fiskr.config import config, PROJECT_ROOT
from fiskr.quality import evaluate_and_clean
from fiskr.blocking import generate_blocking_keys
from fiskr.scoring import match_entities
from fiskr.database import get_db, init_db, log_compliance_decision, AuditTrail

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fiskr.api")

# In-memory storage for watchlists
watchlist_store: List[Dict[str, Any]] = []
watchlist_index: Dict[str, List[Dict[str, Any]]] = {}
watchlist_version: str = "1.0.0"
watchlist_hash: str = ""

def load_watchlist():
    """Loads, validates, cleans and indexes the watchlist from watchlist.json."""
    global watchlist_store, watchlist_index, watchlist_hash
    
    watchlist_path = PROJECT_ROOT / "watchlist.json"
    if not watchlist_path.exists():
        logger.warning(f"Watchlist file not found at {watchlist_path}")
        return
        
    try:
        with open(watchlist_path, "rb") as f:
            content = f.read()
            # Compute SHA-256 hash of the raw watchlist file
            watchlist_hash = hashlib.sha256(content).hexdigest()
            data = json.loads(content)
            
        temp_store = []
        temp_index = {}
        
        for idx, item in enumerate(data):
            # Enforce unique entity_id if missing
            if "entity_id" not in item:
                item["entity_id"] = f"WL-GEN-{idx}"
                
            # Run through Data Quality Gate
            report = evaluate_and_clean(item)
            if not report["is_valid"]:
                logger.error(f"Watchlist item {item.get('entity_id')} rejected by Quality Gate: {report['errors']}")
                continue
                
            # Create cleansed entry for execution
            cleansed_item = item.copy()
            cleansed_item["primary_name"] = report["cleansed_name"]
            cleansed_item["aliases"] = report["cleansed_aliases"]
            cleansed_item["quality_status"] = report["status"]
            cleansed_item["quality_warnings"] = report["warnings"]
            
            temp_store.append(cleansed_item)
            
            # Generate blocking keys on the cleansed item
            keys = generate_blocking_keys(cleansed_item, config)
            for k in keys:
                if k not in temp_index:
                    temp_index[k] = []
                temp_index[k].append(cleansed_item)
                
        watchlist_store = temp_store
        watchlist_index = temp_index
        logger.info(f"Loaded and indexed {len(watchlist_store)} active watchlist entities across {len(watchlist_index)} blocking blocks.")
    except Exception as e:
        logger.error(f"Error loading watchlist: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Initializing Fiskr application...")
    init_db()
    load_watchlist()
    yield
    # Shutdown
    logger.info("Stopping Fiskr application...")

app = FastAPI(
    title="Fiskr",
    description="Système de Criblage et Filtrage de Sanctions / PEP à Haute Volumétrie",
    version="1.0.0",
    lifespan=lifespan
)

# ------------------ PYDANTIC MODELS ------------------

class EntityCountries(BaseModel):
    citizenship: List[str] = Field(default_factory=list, example=["FR"])
    residence: List[str] = Field(default_factory=list, example=["RU"])
    birth_country: List[str] = Field(default_factory=list, example=["LY"])

class ScreenRequest(BaseModel):
    entity_id: Optional[str] = Field(None, example="CLI-9821")
    entity_type: str = Field(..., example="PP", description="PP (Individu) ou PM (Entreprise)")
    primary_name: str = Field(..., example="Vladimir Putin")
    aliases: List[str] = Field(default_factory=list, example=["PUTIN Vlad"])
    dates_of_birth: List[str] = Field(default_factory=list, example=["1952-10-07"])
    genders: List[str] = Field(default_factory=list, example=["M"])
    countries: EntityCountries = Field(default_factory=EntityCountries)

class WatchlistItemCreate(BaseModel):
    entity_id: str
    entity_type: str
    primary_name: str
    aliases: List[str] = []
    dates_of_birth: List[str] = []
    genders: List[str] = []
    countries: EntityCountries = EntityCountries()

# ------------------ ENDPOINTS ------------------

@app.post("/api/screen")
async def screen_client(request: ScreenRequest, db: Session = Depends(get_db)):
    """
    Screens a client profile against in-memory sanctions watchlists.
    - Validates data quality.
    - Blocks search space.
    - Scores and adjusts matches.
    - Saves audit trails.
    """
    client_dict = request.dict()
    
    # 1. Quality Gate check
    quality_report = evaluate_and_clean(client_dict)
    if not quality_report["is_valid"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Reject by Data Quality Gate",
                "errors": quality_report["errors"]
            }
        )
        
    cleansed_client = client_dict.copy()
    cleansed_client["primary_name"] = quality_report["cleansed_name"]
    cleansed_client["aliases"] = quality_report["cleansed_aliases"]
    
    # 2. Blocking
    client_keys = generate_blocking_keys(cleansed_client, config)
    
    # 3. Retrieve candidates
    candidates = {}
    for key in client_keys:
        for item in watchlist_index.get(key, []):
            candidates[item["entity_id"]] = item
            
    # 4. Scoring
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
            
    # 5. Database Audit Trail Persistence
    audit_id = None
    if best_match:
        # Log the highest matching screening result
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
        # Log a dummy NO_MATCH result since no candidates matched blocking keys
        no_match_result = {
            "status": "NO_MATCH",
            "base_score": 0.0,
            "final_score": 0.0,
            "best_client_name": quality_report["cleansed_name"],
            "best_watchlist_name": "Aucun candidat trouvé (Bloqué)",
            "adjustments": {
                "dob": {"score": 0.0, "description": "N/A"},
                "gender": {"score": 0.0, "description": "N/A"},
                "geography": {"score": 0.0, "description": "N/A"}
            },
            "cut_off_applied": config.get("scoring", {}).get("cut_off_threshold", 75.0)
        }
        dummy_wl = {
            "entity_id": "NONE",
            "primary_name": "Aucune fiche correspondante"
        }
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
        "client_quality_report": quality_report,
        "blocking_keys_generated": list(client_keys),
        "candidates_count": len(candidates),
        "best_match": best_match,
        "all_matches": sorted(matches, key=lambda x: x["final_score"], reverse=True),
        "audit_trail_id": audit_id
    }

@app.get("/api/watchlist")
async def get_watchlist():
    """Returns the loaded watchlist."""
    return {
        "version": watchlist_version,
        "hash": watchlist_hash,
        "items": watchlist_store
    }

@app.post("/api/watchlist", status_code=status.HTTP_201_CREATED)
async def add_watchlist_item(item: WatchlistItemCreate):
    """Appends an entity to the in-memory watchlist and re-indexes."""
    global watchlist_store
    
    item_dict = item.dict()
    report = evaluate_and_clean(item_dict)
    if not report["is_valid"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"errors": report["errors"]}
        )
        
    cleansed_item = item_dict.copy()
    cleansed_item["primary_name"] = report["cleansed_name"]
    cleansed_item["aliases"] = report["cleansed_aliases"]
    cleansed_item["quality_status"] = report["status"]
    cleansed_item["quality_warnings"] = report["warnings"]
    
    # Check if duplicate id
    for existing in watchlist_store:
        if existing["entity_id"] == cleansed_item["entity_id"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Entity ID {cleansed_item['entity_id']} already exists in watchlist."
            )
            
    watchlist_store.append(cleansed_item)
    
    # Index new item
    keys = generate_blocking_keys(cleansed_item, config)
    for k in keys:
        if k not in watchlist_index:
            watchlist_index[k] = []
        watchlist_index[k].append(cleansed_item)
        
    return {"message": "Watchlist item added and indexed successfully", "item": cleansed_item}

@app.get("/api/history")
async def get_audit_history(db: Session = Depends(get_db)):
    """Retrieves all compliance audit logs from the database, newest first."""
    logs = db.query(AuditTrail).order_by(AuditTrail.timestamp.desc()).all()
    return logs

@app.get("/api/config")
async def get_active_config():
    """Exposes active configuration."""
    return config

# Serve static dashboard
static_dir = PROJECT_ROOT / "fiskr" / "static"
if not static_dir.exists():
    static_dir.mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    """Serves the main dashboard html file."""
    index_path = static_dir / "index.html"
    if not index_path.exists():
        return HTMLResponse(
            content="<h3>Dashboard template static/index.html is being created... Please refresh in a moment.</h3>",
            status_code=200
        )
    with open(index_path, "r", encoding="utf-8") as f:
        return HTMLResponse(content=f.read(), status_code=200)

import json
import hashlib
import logging
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, JSON, Boolean, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from fiskr.config import config

logger = logging.getLogger("fiskr.database")

Base = declarative_base()

class Snapshot(Base):
    __tablename__ = "snapshots"
    
    snapshot_id = Column(String(50), primary_key=True)
    file_type = Column(String(50), nullable=False) # WATCHLIST_OFAC, WATCHLIST_EU, CLIENT_BASE
    file_name = Column(String(255), nullable=False)
    file_hash = Column(String(64), nullable=False)
    record_count = Column(Integer, default=0)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="PROCESSING") # PROCESSING, READY, SUPERSEDED, ERROR

class WatchlistEntity(Base):
    __tablename__ = "watchlist_entities"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(50), ForeignKey("snapshots.snapshot_id"), nullable=False)
    entity_id = Column(String(100), nullable=False)
    entity_type = Column(String(10), nullable=False) # I, E, V, O
    primary_name = Column(String(255), nullable=False)
    
    # Parsed structure
    individual_name_parsed = Column(JSON, nullable=True) # first_name, last_name, maiden_name
    aliases = Column(JSON, nullable=True) # {"high_priority": [], "low_priority": []}
    dates_of_birth = Column(JSON, nullable=True) # list of YYYY-MM-DD
    date_of_death = Column(String(50), nullable=True)
    is_deceased = Column(Boolean, default=False)
    gender = Column(String(5), default="U")
    countries = Column(JSON, nullable=True) # citizenship, residence, birth_country, jurisdiction_country
    
    # Identifiers
    imo_number = Column(String(20), nullable=True)
    aircraft_tail_number = Column(String(50), nullable=True)
    lei_number = Column(String(50), nullable=True)
    
    # JSON arrays of objects
    national_registry_ids = Column(JSON, nullable=True) # number, country, registry_name
    other_registration_ids = Column(JSON, nullable=True) # id_type, number
    passport_documents = Column(JSON, nullable=True) # number, issuing_country, expiration_date
    national_id_documents = Column(JSON, nullable=True) # number, issuing_country
    other_id_documents = Column(JSON, nullable=True) # doc_type, number, issuing_country
    
    # Checksum for version comparisons
    entity_checksum = Column(String(64), nullable=False)

class ClientEntity(Base):
    __tablename__ = "client_entities"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(50), ForeignKey("snapshots.snapshot_id"), nullable=False)
    client_id = Column(String(100), nullable=False)
    client_type = Column(String(10), nullable=False) # PP, PM
    
    client_first_name = Column(String(100), nullable=True)
    client_last_name = Column(String(100), nullable=True)
    client_maiden_name = Column(String(100), nullable=True)
    client_company_name = Column(String(255), nullable=True)
    client_dob = Column(String(50), nullable=True)
    client_gender = Column(String(5), default="U")
    client_is_deceased = Column(Boolean, default=False)
    client_countries = Column(JSON, nullable=True) # nationality, residence, birth_country, registration_country
    
    # Identifiers
    transaction_vessel_imo = Column(String(20), nullable=True)
    transaction_aircraft_registration = Column(String(50), nullable=True)
    client_lei_number = Column(String(50), nullable=True)
    
    # JSON arrays of objects
    client_national_registry_ids = Column(JSON, nullable=True)
    client_other_registration_ids = Column(JSON, nullable=True)
    client_passport_documents = Column(JSON, nullable=True)
    client_national_id_documents = Column(JSON, nullable=True)
    client_other_id_documents = Column(JSON, nullable=True)
    
    # Checksum for version comparisons
    entity_checksum = Column(String(64), nullable=False)

class AuditTrail(Base):
    __tablename__ = "compliance_audit_trail"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    client_id = Column(String(100), nullable=True)
    client_name = Column(String(255), nullable=False)
    client_type = Column(String(10), nullable=False)
    watchlist_id = Column(String(100), nullable=False)
    watchlist_name = Column(String(255), nullable=False)
    base_score = Column(Float, nullable=False)
    final_score = Column(Float, nullable=False)
    status = Column(String(20), nullable=False)
    decision_tree = Column(JSON, nullable=False)
    config_state = Column(JSON, nullable=False)
    watchlist_version = Column(String(50), nullable=False)
    watchlist_hash = Column(String(64), nullable=False)

# Setup Database Engine
db_config = config.get("database", {})
pg_url = db_config.get("url", "postgresql://postgres:postgres@localhost:5432/fiskr")
sqlite_path = db_config.get("sqlite_path", "fiskr.sqlite3")
fallback = db_config.get("fallback_to_sqlite", True)

engine = None
SessionLocal = None

def init_db():
    global engine, SessionLocal
    try:
        if pg_url.startswith("postgresql"):
            logger.info("Attempting to connect to PostgreSQL database...")
            engine = create_engine(pg_url, connect_args={"connect_timeout": 3})
            # Test connection
            with engine.connect() as conn:
                pass
            logger.info("Successfully connected to PostgreSQL.")
        else:
            raise ValueError("Not a PostgreSQL URL")
    except Exception as e:
        if fallback:
            try:
                err_msg = str(e)
            except Exception:
                err_msg = repr(e)
            logger.warning(f"Failed to connect to PostgreSQL: {err_msg}. Falling back to SQLite.")
            sqlite_url = f"sqlite:///{sqlite_path}"
            engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        else:
            try:
                err_msg = str(e)
            except Exception:
                err_msg = repr(e)
            logger.error(f"Failed to connect to database and fallback is disabled: {err_msg}")
            raise e

    # Create tables
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def log_compliance_decision(
    db,
    client: dict,
    watchlist_entry: dict,
    scoring_result: dict,
    wl_version: str,
    wl_hash: str
) -> AuditTrail:
    """Inserts a compliance screening decision into the audit trail database."""
    from fiskr.config import config as active_config
    config_audit = {k: v for k, v in active_config.items() if k != "database"}
    
    # Handle the difference in client keys (for client_last_name / primary_name)
    cname = client.get("primary_name", "")
    if not cname:
        fname = client.get("client_first_name", "")
        lname = client.get("client_last_name", "")
        cname = f"{fname} {lname}".strip() or client.get("client_company_name", "")
        
    ctype = client.get("entity_type") or client.get("client_type") or "PP"
    
    db_entry = AuditTrail(
        client_id=client.get("entity_id") or client.get("client_id"),
        client_name=cname or "Inconnu",
        client_type=ctype,
        watchlist_id=watchlist_entry.get("entity_id", "NONE"),
        watchlist_name=watchlist_entry.get("primary_name", "Aucun match"),
        base_score=scoring_result.get("base_score", 0.0),
        final_score=scoring_result.get("final_score", 0.0),
        status=scoring_result.get("status", "NO_MATCH"),
        decision_tree=scoring_result,
        config_state=config_audit,
        watchlist_version=wl_version,
        watchlist_hash=wl_hash
    )
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)
    return db_entry

# Helper function to compute entity checksums
def compute_checksum(data: dict) -> str:
    """Computes a SHA-256 checksum of normalized fields in a dictionary."""
    # Serialize sorted keys, filtering out metadata keys like id, snapshot_id, entity_checksum
    filtered_data = {k: v for k, v in data.items() if k not in ["id", "snapshot_id", "entity_checksum"]}
    dumped = json.dumps(filtered_data, sort_keys=True, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()

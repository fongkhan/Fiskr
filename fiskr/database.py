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
    status = Column(String(20), default="PROCESSING") # PROCESSING, PENDING_REVIEW, READY, SUPERSEDED, REJECTED, ERROR
    # Homologation (pointage humain avant mise en production)
    reviewed_by = Column(String(100), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_comment = Column(Text, nullable=True)

class WatchlistEntity(Base):
    __tablename__ = "watchlist_entities"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(50), ForeignKey("snapshots.snapshot_id"), nullable=False)
    entity_id = Column(String(100), nullable=False)
    entity_type = Column(String(10), nullable=False) # I, E, V, O
    primary_name = Column(String(1000), nullable=False)
    
    # Parsed structure
    individual_name_parsed = Column(JSON, nullable=True) # first_name, last_name, maiden_name
    aliases = Column(JSON, nullable=True) # {"high_priority": [], "low_priority": []}
    dates_of_birth = Column(JSON, nullable=True) # list of YYYY-MM-DD
    date_of_death = Column(String(50), nullable=True)
    is_deceased = Column(Boolean, default=False)
    gender = Column(String(5), default="U")
    countries = Column(JSON, nullable=True) # citizenship, residence, birth_country, jurisdiction_country
    
    # New fields requested
    place_of_birth = Column(String(255), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(255), nullable=True)
    state = Column(String(255), nullable=True)
    country = Column(String(100), nullable=True)
    origin = Column(String(255), nullable=True)
    designation = Column(String(500), nullable=True)
    designation_reasons = Column(Text, nullable=True)  # Motifs de la designation (annexes EUR-Lex, notes OFAC)
    additional_informations = Column(Text, nullable=True)
    alternative_addresses = Column(JSON, nullable=True)

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

    # Exclusion par un reviseur lors de l'homologation (NULL = non exclu, lignes legacy)
    excluded = Column(Boolean, default=False, nullable=True)
    exclusion_justification = Column(Text, nullable=True)
    exclusion_file_name = Column(String(255), nullable=True)
    exclusion_file_path = Column(String(500), nullable=True)
    excluded_by = Column(String(100), nullable=True)
    excluded_at = Column(DateTime, nullable=True)

class ClientEntity(Base):
    __tablename__ = "client_entities"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(50), ForeignKey("snapshots.snapshot_id"), nullable=False)
    client_id = Column(String(100), nullable=False)
    client_type = Column(String(10), nullable=False) # PP, PM
    
    client_first_name = Column(String(100), nullable=True)
    client_last_name = Column(String(100), nullable=True)
    client_maiden_name = Column(String(100), nullable=True)
    client_company_name = Column(String(1000), nullable=True)
    client_dob = Column(String(50), nullable=True)
    client_gender = Column(String(5), default="U")
    client_is_deceased = Column(Boolean, default=False)
    client_countries = Column(JSON, nullable=True) # nationality, residence, birth_country, registration_country
    
    # New fields requested
    client_place_of_birth = Column(String(255), nullable=True)
    client_address = Column(Text, nullable=True)
    client_city = Column(String(255), nullable=True)
    client_state = Column(String(255), nullable=True)
    client_country = Column(String(100), nullable=True)
    client_origin = Column(String(255), nullable=True)
    client_designation = Column(String(500), nullable=True)
    client_additional_informations = Column(Text, nullable=True)
    client_alternative_addresses = Column(JSON, nullable=True)
    client_date_of_death = Column(String(50), nullable=True)
    
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
    client_name = Column(String(1000), nullable=False)
    client_type = Column(String(10), nullable=False)
    watchlist_id = Column(String(100), nullable=False)
    watchlist_name = Column(String(1000), nullable=False)
    base_score = Column(Float, nullable=False)
    final_score = Column(Float, nullable=False)
    status = Column(String(20), nullable=False)
    decision_tree = Column(JSON, nullable=False)
    config_state = Column(JSON, nullable=False)
    watchlist_version = Column(String(50), nullable=False)
    watchlist_hash = Column(String(64), nullable=False)

class SyncReport(Base):
    __tablename__ = "sync_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False)             # OFAC, EURLEX
    executed_at = Column(DateTime, default=datetime.utcnow)
    trigger = Column(String(20), default="MANUAL")          # MANUAL, SCHEDULED
    status = Column(String(30), nullable=False)             # SUCCESS, NO_CHANGE, NO_PUBLICATION, ERROR
    message = Column(Text, nullable=True)
    snapshot_id = Column(String(50), nullable=True)
    previous_snapshot_id = Column(String(50), nullable=True)
    added_count = Column(Integer, default=0)
    modified_count = Column(Integer, default=0)
    removed_count = Column(Integer, default=0)
    delta_report = Column(JSON, nullable=True)              # truncated delta details for the UI
    email_sent = Column(Boolean, default=False)

class Alert(Base):
    """
    Alerte de criblage : objet de travail avec cycle de vie et decision 4-yeux.
    OPEN -> IN_PROGRESS (assignee) -> PENDING_VALIDATION (decision proposee)
    -> CLOSED_CONFIRMED | CLOSED_FALSE_POSITIVE ; ESCALATED en derivation.
    Le journal immuable reste compliance_audit_trail (audit_id).
    """
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(Integer, ForeignKey("compliance_audit_trail.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Denormalise pour la file de travail
    client_id = Column(String(100), nullable=True)
    client_name = Column(String(1000), nullable=False)
    watchlist_entity_id = Column(String(100), nullable=False)
    watchlist_name = Column(String(1000), nullable=False)
    final_score = Column(Float, nullable=False)
    # Cycle de vie
    status = Column(String(30), default="OPEN", index=True)
    assigned_to = Column(String(100), nullable=True)
    # Decision proposee (1er regard)
    proposed_decision = Column(String(30), nullable=True)  # CONFIRMED, FALSE_POSITIVE
    proposed_by = Column(String(100), nullable=True)
    proposed_at = Column(DateTime, nullable=True)
    proposal_comment = Column(Text, nullable=True)
    # Decision finale (2e regard, ou 1er si 4-yeux desactive)
    decided_by = Column(String(100), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_comment = Column(Text, nullable=True)

ALERT_OPEN_STATUSES = ("OPEN", "IN_PROGRESS", "ESCALATED", "PENDING_VALIDATION")
ALERT_CLOSED_STATUSES = ("CLOSED_CONFIRMED", "CLOSED_FALSE_POSITIVE")

class AlertEvent(Base):
    """Historique append-only des actions sur une alerte (jamais modifie)."""
    __tablename__ = "alert_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_id = Column(Integer, ForeignKey("alerts.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    username = Column(String(100), nullable=False)
    action = Column(String(30), nullable=False)  # CREATED, REDETECTED, ASSIGNED, COMMENT, ESCALATED, PROPOSED, VALIDATED, RETURNED
    detail = Column(Text, nullable=True)

class WhitelistPair(Base):
    """
    Liste blanche client x liste (« Good Guys », guidance Wolfsberg) : supprime
    les alertes recurrentes d'un faux positif avere, avec justification
    gouvernee. Revocation douce uniquement (jamais de suppression physique) ;
    chaque suppression d'alerte reste tracee dans le journal d'audit.
    """
    __tablename__ = "whitelist_pairs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(100), nullable=False, index=True)
    watchlist_entity_id = Column(String(100), nullable=False, index=True)
    client_name = Column(String(1000), nullable=True)
    watchlist_name = Column(String(1000), nullable=True)
    justification = Column(Text, nullable=True)
    evidence_file_name = Column(String(255), nullable=True)
    evidence_file_path = Column(String(500), nullable=True)
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)  # gouvernance : revue periodique
    revoked_by = Column(String(100), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revoke_comment = Column(Text, nullable=True)

class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(100), nullable=True)

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    salt = Column(String(64), nullable=False)
    full_name = Column(String(255), nullable=True)
    role = Column(String(50), default="admin")
    created_at = Column(DateTime, default=datetime.utcnow)

import secrets
import os

def hash_password(password: str, salt_hex: str = None) -> tuple[str, str]:
    """Hashes a password securely using PBKDF2 HMAC SHA-256 and 100,000 iterations."""
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
    return hashed.hex(), salt.hex()

def verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    """Verifies a plain-text password against a stored hash and salt."""
    try:
        salt = bytes.fromhex(stored_salt)
        hashed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
        return secrets.compare_digest(hashed.hex(), stored_hash)
    except Exception:
        return False


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
            if "codec" in err_msg.lower() and ("decode" in err_msg.lower() or "utf-8" in err_msg.lower()):
                err_msg = "OperationalError (Connection refused or database unreachable on localhost:5432)"
            logger.warning(f"Failed to connect to PostgreSQL: {err_msg}. Falling back to SQLite.")
            sqlite_url = f"sqlite:///{sqlite_path}"
            engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        else:
            try:
                err_msg = str(e)
            except Exception:
                err_msg = repr(e)
            if "codec" in err_msg.lower() and ("decode" in err_msg.lower() or "utf-8" in err_msg.lower()):
                err_msg = "OperationalError (Connection refused or database unreachable on localhost:5432)"
            logger.error(f"Failed to connect to database and fallback is disabled: {err_msg}")
            raise e

    from sqlalchemy import inspect, text
    try:
        inspector = inspect(engine)
        if "watchlist_entities" in inspector.get_table_names():
            columns = [c["name"] for c in inspector.get_columns("watchlist_entities")]
            if "place_of_birth" not in columns:
                logger.info("Database schema outdated. Dropping and recreating tables...")
                Base.metadata.drop_all(bind=engine)
            elif "designation_reasons" not in columns:
                # Migration additive (colonne nullable) : les donnees existantes sont conservees
                logger.info("Adding missing column watchlist_entities.designation_reasons...")
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE watchlist_entities ADD COLUMN designation_reasons TEXT"))

        # Migrations additives (colonnes nullables) : homologation / exclusions
        _additive_migrations = {
            "snapshots": [
                ("reviewed_by", "VARCHAR(100)"),
                ("reviewed_at", "TIMESTAMP"),
                ("review_comment", "TEXT"),
            ],
            "watchlist_entities": [
                ("excluded", "BOOLEAN"),
                ("exclusion_justification", "TEXT"),
                ("exclusion_file_name", "VARCHAR(255)"),
                ("exclusion_file_path", "VARCHAR(500)"),
                ("excluded_by", "VARCHAR(100)"),
                ("excluded_at", "TIMESTAMP"),
            ],
        }
        inspector = inspect(engine)
        for table_name, cols in _additive_migrations.items():
            if table_name not in inspector.get_table_names():
                continue
            existing_cols = [c["name"] for c in inspector.get_columns(table_name)]
            for col_name, col_type in cols:
                if col_name not in existing_cols:
                    logger.info(f"Adding missing column {table_name}.{col_name}...")
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"))
    except Exception as e:
        logger.warning(f"Failed to inspect database schema: {e}")
    Base.metadata.create_all(bind=engine)
    
    # Check if we need to alter column lengths (e.g. if we are on postgresql)
    if engine.dialect.name == "postgresql":
        try:
            from sqlalchemy import text
            with engine.begin() as conn:
                conn.execute(text("SET lock_timeout = '2s'"))
                conn.execute(text("ALTER TABLE watchlist_entities ALTER COLUMN primary_name TYPE VARCHAR(1000)"))
                conn.execute(text("ALTER TABLE client_entities ALTER COLUMN client_company_name TYPE VARCHAR(1000)"))
                conn.execute(text("ALTER TABLE compliance_audit_trail ALTER COLUMN client_name TYPE VARCHAR(1000)"))
                conn.execute(text("ALTER TABLE compliance_audit_trail ALTER COLUMN watchlist_name TYPE VARCHAR(1000)"))
            logger.info("Successfully checked and upgraded column lengths in PostgreSQL.")
        except Exception as alter_err:
            logger.warning(f"Could not automatically alter column types in PostgreSQL: {alter_err}")
            
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Seed default admin user if missing
    from fiskr.config import ADMIN_USERNAME, ADMIN_PASSWORD
    db = SessionLocal()
    try:
        admin_user = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if not admin_user:
            h_pass, salt_str = hash_password(ADMIN_PASSWORD)
            new_admin = User(
                username=ADMIN_USERNAME,
                hashed_password=h_pass,
                salt=salt_str,
                full_name="Administrator",
                role="admin"
            )
            db.add(new_admin)
            db.commit()
            logger.info(f"Seeded default admin user: '{ADMIN_USERNAME}'")
    except Exception as user_err:
        db.rollback()
        logger.warning(f"Failed to seed admin user: {user_err}")
    finally:
        db.close()


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

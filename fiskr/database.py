import json
import logging
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from fiskr.config import config

logger = logging.getLogger("fiskr.database")

Base = declarative_base()

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
    # Try PostgreSQL first
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
            logger.warning(f"Failed to connect to PostgreSQL: {e}. Falling back to SQLite.")
            sqlite_url = f"sqlite:///{sqlite_path}"
            engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
        else:
            logger.error(f"Failed to connect to database and fallback is disabled: {e}")
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
    # Serialize configuration context
    from fiskr.config import config as active_config
    # Strip database credentials for privacy in audit trail
    config_audit = {k: v for k, v in active_config.items() if k != "database"}
    
    db_entry = AuditTrail(
        client_id=client.get("entity_id"),
        client_name=client.get("primary_name", ""),
        client_type=client.get("entity_type", "PP"),
        watchlist_id=watchlist_entry.get("entity_id", ""),
        watchlist_name=watchlist_entry.get("primary_name", ""),
        base_score=scoring_result["base_score"],
        final_score=scoring_result["final_score"],
        status=scoring_result["status"],
        decision_tree=scoring_result,
        config_state=config_audit,
        watchlist_version=wl_version,
        watchlist_hash=wl_hash
    )
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)
    return db_entry

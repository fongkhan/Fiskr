"""
Tests des items P2 : translitteration multi-ecritures, seuils de cut-off par
liste, connecteurs PEP (OpenSanctions) et UK OFSI, endpoint KPI.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.config import config
from fiskr.database import Base, Snapshot
from fiskr.quality import strip_accents
from fiskr.scoring import compute_base_score, resolve_cut_off, match_entities
from fiskr.ingest import parse_pep_targets_csv, parse_ofsi_conlist_csv
from fiskr.sync import run_pep_sync, run_ofsi_sync


# ------------------ TRANSLITTERATION MULTI-ECRITURES ------------------

def test_transliteration_cyrillic_to_latin():
    assert strip_accents("Владимир Путин").upper() == "VLADIMIR PUTIN"
    # Les diacritiques latins restent geres comme avant
    assert strip_accents("Müller") == "Muller"


def test_cross_script_scoring_matches():
    score = compute_base_score("VLADIMIR PUTIN", "Владимир Путин", config)
    assert score > 90.0


# ------------------ SEUILS DE CUT-OFF PAR LISTE ------------------

def test_resolve_cut_off_overrides():
    cfg = {"scoring": {"cut_off_threshold": 75.0, "cut_off_overrides": {"WATCHLIST_PEP": 85.0}}}
    assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_PEP"}) == 85.0
    assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_DGT"}) == 75.0
    assert resolve_cut_off(cfg, {}) == 75.0
    assert resolve_cut_off(cfg, None) == 75.0


def test_per_list_threshold_changes_alert_status():
    client = {
        "client_id": "C1", "client_type": "PP",
        "client_first_name": "Vladimir", "client_last_name": "Poutine",  # variante -> score < 100
        "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    }
    entity = {
        "entity_id": "X-1", "entity_type": "I", "primary_name": "VLADIMIR PUTIN",
        "individual_name_parsed": {"first_name": "Vladimir", "last_name": "PUTIN", "maiden_name": ""},
        "aliases": {"high_priority": [], "low_priority": []},
        "dates_of_birth": [], "gender": "M",
        "countries": {"citizenship": ["RU"], "residence": [], "birth_country": [], "jurisdiction_country": []},
        "_list_type": "WATCHLIST_PEP",
    }
    base_cfg = {"scoring": dict(config.get("scoring", {}))}
    base_cfg["scoring"]["cut_off_overrides"] = {}
    result_default = match_entities(client, entity, base_cfg)
    assert result_default["status"] == "ALERT"

    # Seuil PEP releve au-dessus du score obtenu -> NO_MATCH sur cette liste
    strict_cfg = {"scoring": dict(base_cfg["scoring"])}
    strict_cfg["scoring"]["cut_off_overrides"] = {"WATCHLIST_PEP": result_default["final_score"] + 1}
    result_strict = match_entities(client, entity, strict_cfg)
    assert result_strict["status"] == "NO_MATCH"
    assert result_strict["cut_off_applied"] == result_default["final_score"] + 1


# ------------------ PARSEUR PEP (OPENSANCTIONS) ------------------

PEP_CSV = """id,schema,name,aliases,birth_date,countries,addresses,identifiers,sanctions,phones,emails,dataset,first_seen,last_seen,last_change
Q7747,Person,Vladimir Putin,Wladimir Putin;Poutine,1952-10-07,ru,Moscow Kremlin,passport: 750123456,President of Russia,+7 495 606 36 02,press@kremlin.example,wd_peps,2020-01-01,2026-07-01,2026-06-01
Q123456,Organization,United Russia Party,,,ru,,,,,,wd_peps,2020-01-01,2026-07-01,2026-06-01
Q999,Person,Jane Politician,,1980,us;fr,,,,,,wd_peps,2020-01-01,2026-07-01,2026-06-01
"""


def test_parse_pep_targets_csv(tmp_path):
    csv_file = tmp_path / "targets.simple.csv"
    csv_file.write_text(PEP_CSV, encoding="utf-8")
    entities = {e["entity_id"]: e for e in parse_pep_targets_csv(str(csv_file))}
    assert len(entities) == 3

    putin = entities["PEP-Q7747"]
    assert putin["entity_type"] == "I"
    assert putin["primary_name"] == "Vladimir Putin"
    assert "Wladimir Putin" in putin["aliases"]["high_priority"]
    assert putin["dates_of_birth"] == ["1952-10-07"]
    assert putin["countries"]["citizenship"] == ["RU"]
    assert putin["designation_reasons"] == "Personne Politiquement Exposée (PEP)"
    assert putin["origin"] == "OpenSanctions PEP"
    # Champs etendus : fonction PEP, premiere apparition, contacts
    assert putin["pep_role"] == "President of Russia"
    assert putin["listed_on"] == "2020-01-01"
    assert putin["phone_numbers"] == ["+7 495 606 36 02"]
    assert putin["email_addresses"] == ["press@kremlin.example"]

    assert entities["PEP-Q123456"]["entity_type"] == "E"
    # Date partielle (annee seule) normalisee
    assert entities["PEP-Q999"]["dates_of_birth"] == ["1980-01-01"]
    assert entities["PEP-Q999"]["countries"]["citizenship"] == ["FR", "US"]


# ------------------ PARSEUR UK OFSI ------------------

OFSI_CSV = """Last Updated:,15/07/2026,,,,,,,,,,,,,,,,,,,
Name 6,Name 1,Name 2,Name 3,Name 4,Name 5,Title,DOB,Town of Birth,Country of Birth,Nationality,Position,Address 1,Address 2,Address 3,Post/Zip Code,Country,Other Information,Group Type,Alias Type,Regime,Group ID,Name Non-Latin Script,Passport Number,NI Number,Listed On
PETROV,Igor,,,,,Gen,12/03/1965,Moscow,Russia,Russian,Minister,12 Tverskaya Street,,,125009,Russia,Senior official,Individual,Primary name,Russia,10001,Игорь Петров,750123456,AB123456C,14/08/2016
PETROV,Igor Petrovitch,,,,,,,,,,,,,,,,,Individual,aka,Russia,10001,,,,
VOLGA STAR,,,,,,,,,,,,,,,,,IMO 9876543,Ship,Primary name,Russia,10002,,,,
"""


def test_parse_ofsi_conlist_csv(tmp_path):
    csv_file = tmp_path / "ConList.csv"
    csv_file.write_text(OFSI_CSV, encoding="utf-8")
    entities = {e["entity_id"]: e for e in parse_ofsi_conlist_csv(str(csv_file))}
    assert len(entities) == 2

    petrov = entities["OFSI-10001"]
    assert petrov["entity_type"] == "I"
    assert petrov["primary_name"] == "Igor PETROV"
    assert petrov["individual_name_parsed"]["first_name"] == "Igor"
    assert petrov["individual_name_parsed"]["last_name"] == "PETROV"
    assert petrov["dates_of_birth"] == ["1965-03-12"]  # jj/mm/aaaa converti
    assert petrov["countries"]["citizenship"] == ["RU"]  # 'Russian' -> ISO2
    assert petrov["place_of_birth"] == "Moscow, Russia"
    assert "Igor Petrovitch PETROV" in petrov["aliases"]["high_priority"]
    assert petrov["designation"] == "Minister"
    assert petrov["designation_reasons"] == "Russia"
    # Champs etendus : titre, date d'inscription (jj/mm/aaaa), regime en programme,
    # script non latin (colonne + alias de matching), passeport et NI number
    assert petrov["title"] == "Gen"
    assert petrov["listed_on"] == "2016-08-14"
    assert petrov["sanction_programs"] == ["Russia"]
    assert petrov["name_original_script"] == "Игорь Петров"
    assert "Игорь Петров" in petrov["aliases"]["high_priority"]
    assert petrov["passport_documents"][0]["number"] == "750123456"
    assert petrov["national_id_documents"] == [{"number": "AB123456C", "issuing_country": "GB"}]

    assert entities["OFSI-10002"]["entity_type"] == "V"


# ------------------ SYNCS ------------------

@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'p2_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _fetcher(content):
    from pathlib import Path
    return lambda url, dest: Path(dest).write_text(content, encoding="utf-8")


def test_pep_and_ofsi_sync_lifecycle(db):
    report = run_pep_sync(db, fetcher=_fetcher(PEP_CSV))
    assert report.status == "SUCCESS"
    assert report.source == "PEP"
    assert report.added_count == 3
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_PEP"

    report2 = run_ofsi_sync(db, fetcher=_fetcher(OFSI_CSV))
    assert report2.status == "SUCCESS"
    assert report2.source == "OFSI"
    assert report2.added_count == 2

    # Dedup par hash
    assert run_pep_sync(db, fetcher=_fetcher(PEP_CSV)).status == "NO_CHANGE"


# ------------------ KPI ------------------

@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "role": "admin", "roles": ["admin"]
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_kpi_endpoint_structure(client):
    response = client.get("/api/kpi")
    assert response.status_code == 200
    k = response.json()
    assert "alerts" in k and "open" in k["alerts"] and "false_positive_rate_pct" in k["alerts"]
    assert "whitelist_active_pairs" in k
    assert "production_entities_by_type" in k["lists"]
    assert "snapshots_by_status" in k["lists"]
    assert isinstance(k["recent_syncs"], list)

"""
Tests de la progression des operations longues :
- registre memoire fiskr/progress.py (update / get / finish / TTL) ;
- endpoint GET /api/progress (registre + repli colonnes Snapshot) ;
- POST /api/ingest avec progress_id : contrat de reponse INCHANGE, colonnes
  processed_count/phase persistees, registre finalise ;
- persist_pivot_items : commits intermediaires + callback de progression.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import progress
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Base, Snapshot, WatchlistEntity


# ------------------ REGISTRE MEMOIRE ------------------

def test_registry_update_get_finish():
    token = f"tok-{uuid.uuid4().hex[:8]}"
    progress.update(token, phase="PERSIST", processed=500, total=1000, snapshot_id="snap-1")
    state = progress.get(token)
    assert state["phase"] == "PERSIST"
    assert state["processed"] == 500
    assert state["total"] == 1000
    assert state["pct"] == 50.0
    assert state["snapshot_id"] == "snap-1"
    assert state["status"] == "RUNNING"

    progress.finish(token)
    state = progress.get(token)
    assert state["status"] == "DONE"
    assert state["phase"] == "DONE"


def test_registry_none_token_is_noop():
    # La progression est optionnelle partout : token absent = aucun effet
    progress.update(None, phase="UPLOAD", processed=10)
    progress.finish(None)
    assert progress.get("token-inconnu-xyz") is None


def test_registry_finish_error_keeps_phase_and_message():
    token = f"tok-{uuid.uuid4().hex[:8]}"
    progress.update(token, phase="PERSIST", processed=10)
    progress.finish(token, status="ERROR", error="disque plein")
    state = progress.get(token)
    assert state["status"] == "ERROR"
    assert state["error"] == "disque plein"
    assert state["phase"] == "PERSIST"  # la phase d'echec reste visible


def test_registry_ttl_expiry():
    token = f"tok-{uuid.uuid4().hex[:8]}"
    progress.update(token, phase="UPLOAD")
    # Vieillit l'entree au-dela du TTL : elle doit disparaitre a la lecture
    with progress._lock:
        progress._registry[token]["_touched"] -= progress._TTL_SECONDS + 1
    assert progress.get(token) is None


def test_registry_pct_none_without_total():
    token = f"tok-{uuid.uuid4().hex[:8]}"
    progress.update(token, phase="PERSIST", processed=1234)
    state = progress.get(token)
    assert state["total"] is None
    assert state["pct"] is None


# ------------------ API ------------------

def _override_admin():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "testeur", "full_name": "Testeur", "role": "admin",
        "roles": ["admin"],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_progress_%")).all()
        ids = [s.snapshot_id for s in snaps]
        if ids:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_admin()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _watchlist_csv(n):
    rows = "\n".join(
        f"EU-PRG{i:05d},I,Testeur Progression {i},RU" for i in range(n)
    )
    return f"entity_id,entity_type,primary_name,nationality\n{rows}\n"


def test_progress_endpoint_unknown_token(client):
    response = client.get("/api/progress", params={"id": "jeton-inconnu"})
    assert response.status_code == 404


def test_ingest_with_progress_id_contract_unchanged(client):
    progress_id = str(uuid.uuid4())
    file_name = f"test_progress_{uuid.uuid4().hex[:8]}.csv"
    files = {"file": (file_name, _watchlist_csv(5), "text/csv")}
    response = client.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_EU", "progress_id": progress_id},
        files=files,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    # Contrat synchrone inchange : la reponse porte toujours le resultat complet
    assert "snapshot_id" in data
    assert "message" in data
    assert data["record_count"] == 5

    # Registre finalise : phase DONE lisible apres la requete
    state = progress.get(progress_id)
    assert state is not None
    assert state["status"] == "DONE"
    assert state["phase"] == "DONE"

    # Colonnes persistees sur le snapshot (repli apres redemarrage)
    db = next(get_db())
    try:
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == data["snapshot_id"]).first()
        assert snap is not None
        assert snap.processed_count == 5
        assert snap.phase == "DONE"
    finally:
        db.close()

    # Le endpoint /api/progress sait aussi repondre depuis le snapshot_id (repli)
    response = client.get("/api/progress", params={"id": data["snapshot_id"]})
    assert response.status_code == 200
    fallback = response.json()
    assert fallback["snapshot_id"] == data["snapshot_id"]
    assert fallback["processed"] == 5


def test_ingest_without_progress_id_still_works(client):
    file_name = f"test_progress_{uuid.uuid4().hex[:8]}.csv"
    files = {"file": (file_name, _watchlist_csv(3), "text/csv")}
    response = client.post("/api/ingest", data={"file_type": "WATCHLIST_EU"}, files=files)
    assert response.status_code == 200, response.text
    assert response.json()["record_count"] == 3


def test_sync_config_exposes_running_syncs(client):
    response = client.get("/api/sync/config")
    assert response.status_code == 200
    data = response.json()
    assert "running" in data
    assert isinstance(data["running"], list)


# ------------------ PERSIST_PIVOT_ITEMS ------------------

@pytest.fixture
def isolated_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'progress_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _pivot_items(n):
    return [
        {
            "entity_id": f"EU-PVT{i:04d}",
            "entity_type": "I",
            "primary_name": f"Pivot Testeur {i}",
            "individual_name_parsed": {"first_name": "Pivot", "last_name": f"Testeur {i}"},
            "dates_of_birth": ["1980-01-01"],
            "countries": {"citizenship": ["RU"]},
        }
        for i in range(n)
    ]


def test_persist_pivot_items_periodic_commits_and_progress(isolated_db):
    from fiskr.sync import persist_pivot_items
    snap = Snapshot(snapshot_id="snap-pvt", file_type="WATCHLIST_EU",
                    file_name="test_progress_pivot.csv", file_hash="x" * 64,
                    status="PROCESSING")
    isolated_db.add(snap)
    isolated_db.commit()

    ticks = []
    count = persist_pivot_items(isolated_db, "snap-pvt", _pivot_items(5),
                                commit_every=2, progress=ticks.append)
    isolated_db.commit()
    assert count == 5
    # Callback appele a chaque commit intermediaire (2 puis 4)
    assert ticks == [2, 4]
    stored = isolated_db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == "snap-pvt").count()
    assert stored == 5


def test_persist_pivot_items_progress_callback_never_blocks(isolated_db):
    from fiskr.sync import persist_pivot_items
    snap = Snapshot(snapshot_id="snap-pvt2", file_type="WATCHLIST_EU",
                    file_name="test_progress_pivot2.csv", file_hash="y" * 64,
                    status="PROCESSING")
    isolated_db.add(snap)
    isolated_db.commit()

    def exploding(_count):
        raise RuntimeError("le suivi ne doit jamais casser l'import")

    count = persist_pivot_items(isolated_db, "snap-pvt2", _pivot_items(4),
                                commit_every=2, progress=exploding)
    isolated_db.commit()
    assert count == 4

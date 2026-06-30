import pytest
from fastapi.testclient import TestClient
from fiskr.api import app

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c

def test_get_config(client):
    response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert "blocking" in data
    assert "scoring" in data

def test_get_watchlist(client):
    response = client.get("/api/watchlist")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "hash" in data

def test_screen_valid_client(client):
    # Screen Vladimir Putin - should trigger an alert against WL-001 (seeded watchlist item)
    payload = {
        "client_id": "CUST-0091",
        "client_type": "PP",
        "client_first_name": "Vladimir",
        "client_last_name": "Putin",
        "client_dob": "1952-10-07",
        "client_gender": "M",
        "client_countries": {
            "nationality": ["RU"],
            "residence": [],
            "birth_country": [],
            "registration_country": []
        }
    }
    response = client.post("/api/screen", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "best_match" in data
    assert data["best_match"] is not None
    assert data["best_match"]["status"] == "ALERT"
    assert data["best_match"]["final_score"] >= 75.0

def test_screen_rejected_by_quality_gate(client):
    # Empty first & last name should fail quality check with HTTP 400
    payload = {
        "client_id": "CUST-0092",
        "client_type": "PP",
        "client_first_name": " ",
        "client_last_name": "  ",
        "client_dob": "1952-10-07",
        "client_gender": "M",
        "client_countries": {
            "nationality": ["RU"],
            "residence": [],
            "birth_country": [],
            "registration_country": []
        }
    }
    response = client.post("/api/screen", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    assert "errors" in data["detail"]
    assert any("Rule_B04" in err for err in data["detail"]["errors"])

def test_get_history(client):
    # Get screening audit trail
    response = client.get("/api/history")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)


def test_ingest_reupload_error_snapshot(client):
    # 1. Create a mock CSV content
    csv_data = "client_id,client_type,client_first_name,client_last_name,nationality\nCUST-999,PP,John,Doe,US\n"
    
    # 2. Upload the CSV snapshot first time (should succeed)
    files = {"file": ("test_ingest_reupload.csv", csv_data, "text/csv")}
    response = client.post("/api/ingest", data={"file_type": "CLIENT_BASE"}, files=files)
    assert response.status_code == 200
    snap_info = response.json()
    snap_id = snap_info["snapshot_id"]
    
    # 3. Manually modify this snapshot in the database to status="ERROR"
    from fiskr.database import get_db, Snapshot
    db = next(get_db())
    snapshot = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
    assert snapshot is not None
    snapshot.status = "ERROR"
    db.commit()
    
    # 4. Upload the same file again (with same content/hash)
    # Previously, this would return "Snapshot with this hash already uploaded."
    # Now, it should clean up the old failed snapshot and run ingestion again successfully!
    files2 = {"file": ("test_ingest_reupload.csv", csv_data, "text/csv")}
    response2 = client.post("/api/ingest", data={"file_type": "CLIENT_BASE"}, files=files2)
    assert response2.status_code == 200
    snap_info2 = response2.json()
    new_snap_id = snap_info2["snapshot_id"]
    assert new_snap_id != snap_id  # A new snapshot UUID should be generated
    
    # 5. Verify the old snapshot with ERROR is deleted
    old_snap_check = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
    assert old_snap_check is None
    
    # 6. Verify the new snapshot is READY
    new_snap_check = db.query(Snapshot).filter(Snapshot.snapshot_id == new_snap_id).first()
    assert new_snap_check is not None
    assert new_snap_check.status == "READY"

def test_purge_failed_snapshots(client):
    import uuid
    from fiskr.database import get_db, Snapshot, WatchlistEntity, ClientEntity
    db = next(get_db())
    
    # 1. Create a snapshot in ERROR status and a snapshot in PROCESSING status
    err_snap_id = f"snap-error-{uuid.uuid4()}"
    proc_snap_id = f"snap-proc-{uuid.uuid4()}"
    ready_snap_id = f"snap-ready-{uuid.uuid4()}"
    
    err_snap = Snapshot(snapshot_id=err_snap_id, file_type="WATCHLIST_OFAC", file_name="err.xml", file_hash=f"hash-{uuid.uuid4()}", status="ERROR")
    proc_snap = Snapshot(snapshot_id=proc_snap_id, file_type="CLIENT_BASE", file_name="proc.csv", file_hash=f"hash-{uuid.uuid4()}", status="PROCESSING")
    ready_snap = Snapshot(snapshot_id=ready_snap_id, file_type="WATCHLIST_OFAC", file_name="ready.xml", file_hash=f"hash-{uuid.uuid4()}", status="READY")
    
    db.add_all([err_snap, proc_snap, ready_snap])
    db.commit()
    
    # 2. Add some dummy entities linked to these snapshots
    wl_err_ent = WatchlistEntity(
        snapshot_id=err_snap_id, entity_id="WL-ERR-1", entity_type="I", 
        primary_name="Failed Entity", entity_checksum="checksum-err"
    )
    wl_ready_ent = WatchlistEntity(
        snapshot_id=ready_snap_id, entity_id="WL-READY-1", entity_type="I", 
        primary_name="Success Entity", entity_checksum="checksum-ready"
    )
    client_proc_ent = ClientEntity(
        snapshot_id=proc_snap_id, client_id="CLI-PROC-1", client_type="PP",
        client_first_name="Pending", client_last_name="Client", entity_checksum="checksum-proc"
    )
    
    db.add_all([wl_err_ent, wl_ready_ent, client_proc_ent])
    db.commit()
    
    # 3. Call the purge endpoint
    response = client.post("/api/snapshots/purge")
    assert response.status_code == 200
    data = response.json()
    assert data["purged_snapshots_count"] >= 2 # err_snap and proc_snap (and potentially others)
    assert data["purged_watchlist_entities"] >= 1 # wl_err_ent
    assert data["purged_client_entities"] >= 1 # client_proc_ent
    
    # 4. Verify DB state
    # err_snap and proc_snap are deleted
    assert db.query(Snapshot).filter(Snapshot.snapshot_id == err_snap_id).first() is None
    assert db.query(Snapshot).filter(Snapshot.snapshot_id == proc_snap_id).first() is None
    # ready_snap remains
    assert db.query(Snapshot).filter(Snapshot.snapshot_id == ready_snap_id).first() is not None
    
    # wl_err_ent and client_proc_ent are deleted
    assert db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == err_snap_id).first() is None
    assert db.query(ClientEntity).filter(ClientEntity.snapshot_id == proc_snap_id).first() is None
    # wl_ready_ent remains
    assert db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == ready_snap_id).first() is not None
    
    # Clean up the ready snapshot and its entity so we don't pollute other tests
    db.delete(wl_ready_ent)
    db.commit()
    ready_snap = db.query(Snapshot).filter(Snapshot.snapshot_id == ready_snap_id).first()
    if ready_snap:
        db.delete(ready_snap)
        db.commit()


def test_create_watchlist_entity_success(client):
    payload = {
        "entity_type": "I",
        "primary_name": "Test Person Manual",
        "first_name": "Test",
        "last_name": "Person",
        "nationality": "FR",
        "residence": "FR",
        "aliases": "AliasTest"
    }
    response = client.post("/api/watchlist/entity", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "entity_id" in data
    assert data["primary_name"] == "TEST PERSON MANUAL"

    # Clean up the manual entity from database so it doesn't affect other tests
    from fiskr.database import get_db, WatchlistEntity
    db = next(get_db())
    db.query(WatchlistEntity).filter(WatchlistEntity.entity_id == data["entity_id"]).delete()
    db.commit()


def test_create_watchlist_entity_quality_gate_failure(client):
    payload = {
        "entity_type": "I",
        "primary_name": " ",
        "first_name": " ",
        "last_name": " "
    }
    response = client.post("/api/watchlist/entity", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    assert "errors" in data["detail"]



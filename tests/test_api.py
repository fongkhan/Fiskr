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


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
    # Screen Vladimir Putin - should trigger an alert against WL-001
    payload = {
        "entity_type": "PP",
        "primary_name": "Vladimir Putin",
        "dates_of_birth": ["1952-10-07"],
        "genders": ["M"],
        "countries": {
            "citizenship": ["RU"],
            "residence": [],
            "birth_country": []
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
    # Empty name should fail quality check with HTTP 400
    payload = {
        "entity_type": "PP",
        "primary_name": "  ",
        "dates_of_birth": ["1952-10-07"],
        "genders": ["M"],
        "countries": {
            "citizenship": ["RU"],
            "residence": [],
            "birth_country": []
        }
    }
    response = client.post("/api/screen", json=payload)
    assert response.status_code == 400
    data = response.json()
    assert "detail" in data
    assert "errors" in data["detail"]
    assert any("Rule_B01" in err for err in data["detail"]["errors"])

def test_get_history(client):
    # Get screening audit trail
    response = client.get("/api/history")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)

"""
Tests du cycle de vie des alertes et de la validation 4-yeux.

Une decision de criblage ALERT ouvre une alerte de travail (dedupliquee par
paire client x liste), instruite puis cloturee en deux regards : proposition
(commentaire obligatoire) puis validation par un reviewer DIFFERENT du
proposeur — sauf si le reglage modulaire 4-yeux est desactive.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AppSetting
from fiskr.settings import SETTING_ALERT_FOUR_EYES


def _override_user(username: str, role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key == SETTING_ALERT_FOUR_EYES).delete(synchronize_session=False)
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_alert_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("analyste1", "user")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _screen_putin(client, client_id):
    """Crible un client homonyme de Vladimir Putin (watchlist seed) -> ALERT."""
    payload = {
        "client_id": client_id,
        "client_type": "PP",
        "client_first_name": "Vladimir",
        "client_last_name": "Putin",
        "client_dob": "1952-10-07",
        "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    }
    response = client.post("/api/screen", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _new_client_id():
    return f"test_alert_{uuid.uuid4().hex[:8]}"


# ------------------ CREATION & DEDUPLICATION ------------------

def test_alert_created_on_screening_alert(client):
    cid = _new_client_id()
    data = _screen_putin(client, cid)
    assert data["best_match"]["status"] == "ALERT"
    assert data["alert_id"] is not None

    detail = client.get(f"/api/alerts/{data['alert_id']}").json()
    assert detail["status"] == "OPEN"
    assert detail["client_id"] == cid
    assert detail["audit_id"] == data["audit_trail_id"]
    assert detail["decision_tree"] is not None
    assert [e["action"] for e in detail["events"]] == ["CREATED"]


def test_no_alert_on_no_match(client):
    response = client.post("/api/screen", json={
        "client_id": _new_client_id(), "client_type": "PP",
        "client_first_name": "Jean", "client_last_name": "Dupontel",
        "client_dob": "1990-01-01", "client_gender": "M",
        "client_countries": {"nationality": ["FR"], "residence": [], "birth_country": [], "registration_country": []}
    })
    assert response.status_code == 200
    data = response.json()
    assert data["alert_id"] is None


def test_alert_deduplicated_on_rescreen(client):
    cid = _new_client_id()
    first = _screen_putin(client, cid)
    second = _screen_putin(client, cid)
    assert second["alert_id"] == first["alert_id"]
    detail = client.get(f"/api/alerts/{first['alert_id']}").json()
    actions = [e["action"] for e in detail["events"]]
    assert actions == ["CREATED", "REDETECTED"]


# ------------------ PARCOURS NOMINAL 4-YEUX ------------------

def test_full_lifecycle_with_four_eyes(client):
    alert_id = _screen_putin(client, _new_client_id())["alert_id"]

    # Assignation a soi-meme -> IN_PROGRESS
    response = client.post(f"/api/alerts/{alert_id}/assign", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "IN_PROGRESS"
    assert response.json()["assigned_to"] == "analyste1"

    # Proposition sans commentaire -> 400
    assert client.post(f"/api/alerts/{alert_id}/propose",
                       json={"decision": "FALSE_POSITIVE", "comment": ""}).status_code == 400

    # Proposition FP -> PENDING_VALIDATION
    response = client.post(f"/api/alerts/{alert_id}/propose",
                           json={"decision": "FALSE_POSITIVE", "comment": "Homonymie avérée, DOB différente au dossier."})
    assert response.status_code == 200
    assert response.json()["status"] == "PENDING_VALIDATION"

    # 4-yeux : le proposeur ne peut pas valider (meme avec le role reviewer)
    _override_user("analyste1", "user,reviewer")
    assert client.post(f"/api/alerts/{alert_id}/validate",
                       json={"approve": True}).status_code == 403

    # Un simple 'user' different ne peut pas valider (403 role)
    _override_user("analyste2", "user")
    assert client.post(f"/api/alerts/{alert_id}/validate",
                       json={"approve": True}).status_code == 403

    # Un reviewer DIFFERENT valide -> CLOSED_FALSE_POSITIVE
    _override_user("valideur1", "reviewer")
    response = client.post(f"/api/alerts/{alert_id}/validate", json={"approve": True, "comment": "Validé."})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "CLOSED_FALSE_POSITIVE"
    assert body["decided_by"] == "valideur1"
    assert body["proposed_by"] == "analyste1"

    # Historique complet et actions interdites une fois close
    detail = client.get(f"/api/alerts/{alert_id}").json()
    assert [e["action"] for e in detail["events"]] == ["CREATED", "ASSIGNED", "PROPOSED", "VALIDATED"]
    assert client.post(f"/api/alerts/{alert_id}/propose",
                       json={"decision": "CONFIRMED", "comment": "x"}).status_code == 409
    _override_user("analyste1", "user")


def test_validation_refusal_returns_to_analysis(client):
    alert_id = _screen_putin(client, _new_client_id())["alert_id"]
    client.post(f"/api/alerts/{alert_id}/propose",
                json={"decision": "CONFIRMED", "comment": "Match exact passeport."})

    _override_user("valideur1", "reviewer")
    # Refus sans motif -> 400
    assert client.post(f"/api/alerts/{alert_id}/validate",
                       json={"approve": False, "comment": ""}).status_code == 400
    # Refus motive -> retour IN_PROGRESS, proposition effacee
    response = client.post(f"/api/alerts/{alert_id}/validate",
                           json={"approve": False, "comment": "Vérifier la date de naissance."})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "IN_PROGRESS"
    assert body["proposed_by"] is None
    _override_user("analyste1", "user")


# ------------------ REGLAGE MODULAIRE ------------------

def test_four_eyes_disabled_closes_directly(client):
    _override_user("admin1", "admin")
    response = client.put("/api/settings/ingestion", json={"alert_four_eyes_required": False})
    assert response.status_code == 200
    assert response.json()["alert_four_eyes_required"] is False

    _override_user("analyste1", "user")
    alert_id = _screen_putin(client, _new_client_id())["alert_id"]
    response = client.post(f"/api/alerts/{alert_id}/propose",
                           json={"decision": "FALSE_POSITIVE", "comment": "Homonymie."})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "CLOSED_FALSE_POSITIVE"
    assert body["decided_by"] == "analyste1"


# ------------------ AUTRES GARDES ------------------

def test_escalate_and_worklist_filters(client):
    alert_id = _screen_putin(client, _new_client_id())["alert_id"]
    # Escalade sans motif -> 400
    assert client.post(f"/api/alerts/{alert_id}/escalate", json={"comment": ""}).status_code == 400
    response = client.post(f"/api/alerts/{alert_id}/escalate", json={"comment": "Cas sensible, PPE probable."})
    assert response.status_code == 200
    assert response.json()["status"] == "ESCALATED"

    # File de travail filtree
    listing = client.get("/api/alerts", params={"status": "ESCALATED"}).json()
    assert any(a["id"] == alert_id for a in listing["items"])
    assert listing["open_count"] >= 1

    # Assigner un autre analyste sans etre admin -> 403
    assert client.post(f"/api/alerts/{alert_id}/assign", json={"assignee": "quelqu_un"}).status_code == 403

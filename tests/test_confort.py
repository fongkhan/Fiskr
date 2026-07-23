"""
Tests du lot confort :
- compteurs enrichis (/api/counters : pending_validation, overdue_alerts) ;
- vue client 360 (/api/clients/{id}/overview : KYC, criblages, alertes,
  liste blanche, compteurs) y compris client inconnu ;
- pagination serveur de la file d'alertes et de la liste blanche ;
- idempotence des index composites de performance (init_db rejouable).
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, init_db, Alert, AlertEvent, AuditTrail


def _override_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "analyste_confort", "full_name": "Analyste Confort",
        "role": "user", "roles": ["user"],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_conf_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like("test_conf_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_conf_{uuid.uuid4().hex[:8]}"


def _screen_putin(client, client_id):
    response = client.post("/api/screen", json={
        "client_id": client_id, "client_type": "PP",
        "client_first_name": "Vladimir", "client_last_name": "Putin",
        "client_dob": "1952-10-07", "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["alert_id"] is not None
    return data


# ------------------ COMPTEURS ENRICHIS ------------------

def test_counters_expose_new_fields(client):
    data = client.get("/api/counters").json()
    for key in ("open_alerts", "open_alerts_screening", "open_alerts_filtering",
                "pending_validation", "overdue_alerts", "pending_reviews"):
        assert key in data, f"compteur manquant : {key}"
        assert isinstance(data[key], int) and data[key] >= 0


def test_overdue_counter_counts_late_open_alerts(client):
    cid = _new_client_id()
    data = _screen_putin(client, cid)
    before = client.get("/api/counters").json()["overdue_alerts"]

    # Echeance depassee sur une alerte ouverte -> comptee en retard
    db = next(get_db())
    try:
        alert = db.query(Alert).filter(Alert.id == data["alert_id"]).first()
        alert.due_at = datetime.utcnow() - timedelta(hours=2)
        db.commit()
    finally:
        db.close()
    after = client.get("/api/counters").json()["overdue_alerts"]
    assert after == before + 1

    # Une alerte cloturee ne compte plus, meme en retard
    db = next(get_db())
    try:
        alert = db.query(Alert).filter(Alert.id == data["alert_id"]).first()
        alert.status = "CLOSED_FALSE_POSITIVE"
        db.commit()
    finally:
        db.close()
    assert client.get("/api/counters").json()["overdue_alerts"] == before


# ------------------ VUE CLIENT 360 ------------------

def test_client_overview_aggregates_everything(client):
    cid = _new_client_id()
    _screen_putin(client, cid)

    response = client.get(f"/api/clients/{cid}/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["client_id"] == cid
    # Client ad hoc (hors referentiel) : pas de fiche KYC
    assert data["kyc"] is None
    # Criblage trace + alerte creee
    assert data["counts"]["screenings"] >= 1
    assert data["counts"]["alerts"] >= 1
    assert len(data["screenings"]) >= 1
    assert data["screenings"][0]["watchlist_name"]
    assert data["screenings"][0]["final_score"] is not None
    assert len(data["alerts"]) >= 1
    assert data["alerts"][0]["client_id"] == cid
    assert isinstance(data["whitelist_pairs"], list)


def test_client_overview_unknown_client_is_empty(client):
    response = client.get("/api/clients/test_conf_inconnu_xyz/overview")
    assert response.status_code == 200
    data = response.json()
    assert data["kyc"] is None
    assert data["screenings"] == [] and data["alerts"] == [] and data["whitelist_pairs"] == []
    assert data["counts"] == {"screenings": 0, "alerts": 0, "whitelist_pairs": 0}


# ------------------ PAGINATION SERVEUR ------------------

def test_alerts_queue_pagination(client):
    for _ in range(3):
        _screen_putin(client, _new_client_id())

    page1 = client.get("/api/alerts", params={"channel": "SCREENING", "page": 1, "page_size": 2}).json()
    assert page1["page"] == 1 and page1["page_size"] == 2
    assert page1["total"] >= 3
    assert len(page1["items"]) == 2

    page2 = client.get("/api/alerts", params={"channel": "SCREENING", "page": 2, "page_size": 2}).json()
    assert page2["page"] == 2
    assert len(page2["items"]) >= 1
    # Pas de recouvrement entre pages
    ids1 = {a["id"] for a in page1["items"]}
    ids2 = {a["id"] for a in page2["items"]}
    assert not (ids1 & ids2)

    # Page au-dela de la fin : vide mais valide
    far = client.get("/api/alerts", params={"channel": "SCREENING", "page": 9999, "page_size": 200}).json()
    assert far["items"] == []
    # Bornes validees
    assert client.get("/api/alerts", params={"page": 0}).status_code == 422
    assert client.get("/api/alerts", params={"page_size": 500}).status_code == 422


def test_whitelist_pagination_shape(client):
    data = client.get("/api/whitelist", params={"page": 1, "page_size": 1}).json()
    assert set(data.keys()) == {"total", "page", "page_size", "items"}
    assert data["page"] == 1 and data["page_size"] == 1
    assert len(data["items"]) <= 1
    assert client.get("/api/whitelist", params={"page_size": 500}).status_code == 422


# ------------------ INDEX COMPOSITES ------------------

def test_init_db_reentrant_with_indexes():
    # init_db a deja tourne au demarrage de l'app : le rejouer ne doit pas
    # echouer (creation d'index idempotente, checkfirst)
    init_db()
    init_db()

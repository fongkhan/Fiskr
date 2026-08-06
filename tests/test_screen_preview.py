"""
Vérification ad-hoc d'un nom (GET /api/screen/preview) : criblage à blanc.

- même moteur que la production (blocking + scoring + seuils par liste) ;
- STRICTEMENT en lecture : aucune ligne d'audit, aucune alerte, aucun
  compteur touché — c'est la garantie qui distingue cette voie du criblage
  réglementaire `POST /api/screen` ;
- lentille GAFI réutilisée (un nom inconnu des listes mais rattaché à une
  juridiction à haut risque est quand même signalé) ;
- méthode GET, donc accessible à un auditeur (lecture seule).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AuditTrail


def _override(role="admin", username="preview_tester"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


@pytest.fixture
def client():
    _override("admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _upload_watchlist(client, rows):
    """rows = [(entity_id, name, dob)] ; liste EU mise directement en production."""
    assert client.put("/api/settings/ingestion", json={"require_approval": False}).status_code == 200
    body = "entity_id,entity_type,primary_name,nationality,dob\n" + "\n".join(
        f"{eid},I,{name},RU,{dob}" for eid, name, dob in rows) + "\n"
    r = client.post("/api/ingest", data={"file_type": "WATCHLIST_EU"},
                    files={"file": (f"prev_{uuid.uuid4().hex[:8]}.csv", body, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _counts(db):
    return db.query(Alert).count(), db.query(AuditTrail).count()


def test_preview_finds_a_fuzzy_match_and_writes_nothing(client):
    tag = uuid.uuid4().hex[:6].upper()
    _upload_watchlist(client, [(f"PREV-{tag}", f"Vladimir Sanctionov{tag}", "1970-01-01")])

    db = next(get_db())
    try:
        alerts_before, audits_before = _counts(db)
    finally:
        db.close()

    # Faute de frappe volontaire : le moteur flou doit quand même rapprocher
    res = client.get("/api/screen/preview", params={
        "name": f"Vladimir Sanctionov{tag}", "type": "I", "limit": 5}).json()
    assert res["preview"] is True
    assert res["candidates_count"] >= 1
    top = res["matches"][0]
    assert top["entity_id"] == f"PREV-{tag}"
    assert top["final_score"] >= 75 and top["status"] == "ALERT"
    assert top["list_type"] == "WATCHLIST_EU"

    # RIEN n'a été écrit : ni alerte, ni journal d'audit
    db = next(get_db())
    try:
        assert _counts(db) == (alerts_before, audits_before)
    finally:
        db.close()


def test_preview_no_match_is_empty_and_side_effect_free(client):
    db = next(get_db())
    try:
        alerts_before, audits_before = _counts(db)
    finally:
        db.close()

    res = client.get("/api/screen/preview", params={
        "name": "Personne Parfaitement Inconnuezzz", "type": "I"}).json()
    assert res["preview"] is True
    assert res["alert_count"] == 0
    assert all(m["status"] != "ALERT" for m in res["matches"])

    db = next(get_db())
    try:
        assert _counts(db) == (alerts_before, audits_before)
    finally:
        db.close()


def test_preview_carries_the_fatf_country_risk_lens(client):
    # Un nom inconnu des listes mais rattaché à l'Iran : signalé par la lentille
    res = client.get("/api/screen/preview", params={
        "name": "Jean Neutre Inconnu", "type": "I", "country": "IR,FR"}).json()
    assert res["country_risk"] is not None
    assert res["country_risk"]["tier"] == "BLACKLIST"
    assert res["preview"] is True


def test_preview_is_available_to_an_auditor(client):
    # Méthode GET : l'auditeur en lecture seule peut l'utiliser
    _override("auditor", "preview_auditor")
    res = client.get("/api/screen/preview", params={"name": "Test Auditeur", "type": "I"})
    assert res.status_code == 200
    assert res.json()["preview"] is True


def test_preview_rejects_a_too_short_name(client):
    assert client.get("/api/screen/preview", params={"name": "A"}).status_code == 422

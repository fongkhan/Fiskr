"""
Tests de la liste blanche client x liste (« Good Guys ») : creation gouvernee
(justification/piece modulaires, role reviewer), suppression TRACEE des alertes
au criblage (statut WHITELISTED dans l'audit), revocation et expiration.
"""
import io
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AppSetting, WhitelistPair, AuditTrail
from fiskr.settings import (
    SETTING_WHITELIST_JUSTIFICATION_REQUIRED, SETTING_WHITELIST_FILE_REQUIRED
)

PUTIN_ENTITY_ID = "WL-001"  # watchlist.json seed


def _override_user(username: str, role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_([
            SETTING_WHITELIST_JUSTIFICATION_REQUIRED, SETTING_WHITELIST_FILE_REQUIRED
        ])).delete(synchronize_session=False)
        db.query(WhitelistPair).filter(WhitelistPair.client_id.like("test_wl_%")).delete(synchronize_session=False)
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_wl_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("reviseur1", "reviewer")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_wl_{uuid.uuid4().hex[:8]}"


def _screen_putin(client, client_id):
    response = client.post("/api/screen", json={
        "client_id": client_id, "client_type": "PP",
        "client_first_name": "Vladimir", "client_last_name": "Putin",
        "client_dob": "1952-10-07", "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    })
    assert response.status_code == 200, response.text
    return response.json()


def _whitelist(client, client_id, entity_id=PUTIN_ENTITY_ID, justification="Faux positif avéré", files=None):
    data = {"client_id": client_id, "watchlist_entity_id": entity_id}
    if justification is not None:
        data["justification"] = justification
    return client.post("/api/whitelist", data=data, files=files or {})


# ------------------ CREATION GOUVERNEE ------------------

def test_whitelist_creation_governance(client):
    cid = _new_client_id()

    # 'user' simple -> 403
    _override_user("analyste1", "user")
    assert _whitelist(client, cid).status_code == 403
    _override_user("reviseur1", "reviewer")

    # Sans justification (exigee par defaut) -> 400
    assert _whitelist(client, cid, justification="").status_code == 400

    # Creation OK
    response = _whitelist(client, cid)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ACTIVE"

    # Doublon actif -> 409
    assert _whitelist(client, cid).status_code == 409


def test_whitelist_file_requirement_and_evidence(client):
    _override_user("admin1", "admin")
    assert client.put("/api/settings/ingestion", json={"whitelist_file_required": True}).status_code == 200
    _override_user("reviseur1", "reviewer")

    cid = _new_client_id()
    # Piece exigee -> 400 sans fichier
    assert _whitelist(client, cid).status_code == 400
    # Avec fichier -> OK + retelechargeable
    response = _whitelist(client, cid, files={"file": ("preuve.pdf", io.BytesIO(b"%PDF-1.4 preuve wl"), "application/pdf")})
    assert response.status_code == 200, response.text
    pair_id = response.json()["id"]
    evidence = client.get(f"/api/whitelist/evidence/{pair_id}")
    assert evidence.status_code == 200
    assert evidence.content.startswith(b"%PDF-1.4 preuve wl")


# ------------------ SUPPRESSION TRACEE AU CRIBLAGE ------------------

def test_whitelisted_pair_suppresses_alert_with_audit_trace(client):
    cid = _new_client_id()

    # Avant liste blanche : ALERT + alerte de travail
    first = _screen_putin(client, cid)
    assert first["best_match"]["status"] == "ALERT"
    assert first["alert_id"] is not None

    # Mise en liste blanche puis re-criblage : suppression TRACEE, pas de nouvelle alerte
    assert _whitelist(client, cid).status_code == 200
    second = _screen_putin(client, cid)
    assert second["whitelisted"] is True
    assert second["best_match"]["status"] == "WHITELISTED"
    assert second["alert_id"] is None

    # Le journal d'audit immuable porte la ligne WHITELISTED avec les scores
    db = next(get_db())
    try:
        audit = db.query(AuditTrail).filter(AuditTrail.id == second["audit_trail_id"]).first()
        assert audit.status == "WHITELISTED"
        assert audit.final_score >= 75.0
    finally:
        db.close()


def test_revocation_and_expiry_restore_alerts(client):
    cid = _new_client_id()
    pair_id = _whitelist(client, cid).json()["id"]

    # Paire active -> supprimee
    assert _screen_putin(client, cid)["whitelisted"] is True

    # Revocation (motif obligatoire) -> les alertes reprennent
    assert client.post(f"/api/whitelist/{pair_id}/revoke", json={"comment": ""}).status_code == 400
    response = client.post(f"/api/whitelist/{pair_id}/revoke", json={"comment": "Revue périodique : situation changée."})
    assert response.status_code == 200
    assert response.json()["state"] == "REVOKED"
    after = _screen_putin(client, cid)
    assert after["whitelisted"] is False
    assert after["alert_id"] is not None

    # Paire expiree -> ne supprime pas non plus
    cid2 = _new_client_id()
    response = client.post("/api/whitelist", data={
        "client_id": cid2, "watchlist_entity_id": PUTIN_ENTITY_ID,
        "justification": "Test expiration", "expires_at": "2020-01-01"
    })
    assert response.status_code == 200
    assert response.json()["state"] == "EXPIRED"
    assert _screen_putin(client, cid2)["whitelisted"] is False

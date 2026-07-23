"""
Tests du lot securite :
- anti-brute-force : verrouillage apres N echecs, deverrouillage, remise a zero ;
- politique de mots de passe (creation, reset admin, changement personnel) ;
- tracage des sessions au journal d'administration (LOGIN / LOGIN_FAILED /
  ACCOUNT_LOCKED) ;
- en-tetes de securite HTTP sur toutes les reponses ;
- healthcheck non authentifie ;
- cles d'API techniques : creation (cle montree une fois), authentification
  X-API-Key, revocation immediate, admin interdit.
"""
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user, validate_password, security_config
from fiskr.database import get_db, User, ApiKey, AdminAuditLog

STRONG_PW = "TresSolide2026x"


def _override_admin():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin_sec", "full_name": "Admin Sec", "role": "admin",
        "roles": ["admin"],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(User).filter(User.username.like("test_sec_%")).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.name.like("test_sec_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username.like("test_sec_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_sec").delete(synchronize_session=False)
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


def _create_user(client, username, password=STRONG_PW, role="user"):
    response = client.post("/api/users", json={
        "username": username, "password": password, "full_name": username, "role": role,
    })
    return response


# ------------------ POLITIQUE DE MOTS DE PASSE ------------------

def test_password_policy_rules():
    validate_password(STRONG_PW)  # conforme : ne leve pas
    for weak in ("court1A", "toutenminuscules2026", "TOUTENMAJUSCULES2026", "SansChiffreIci"):
        with pytest.raises(ValueError):
            validate_password(weak)


def test_password_policy_enforced_on_endpoints(client):
    username = f"test_sec_{uuid.uuid4().hex[:8]}"
    weak = _create_user(client, username, password="faible")
    assert weak.status_code == 400
    assert "faible" in weak.json()["detail"].lower()

    assert _create_user(client, username).status_code == 200
    # Reset admin avec un mot de passe faible -> 400
    db = next(get_db())
    try:
        uid = db.query(User).filter(User.username == username).first().id
    finally:
        db.close()
    assert client.put(f"/api/users/{uid}", json={"password": "123"}).status_code == 400


# ------------------ ANTI-BRUTE-FORCE (VERROUILLAGE) ------------------

def test_login_lockout_after_repeated_failures(client):
    username = f"test_sec_{uuid.uuid4().hex[:8]}"
    assert _create_user(client, username).status_code == 200
    max_failures = security_config()["max_login_failures"]

    for _ in range(max_failures):
        response = client.post("/api/auth/login", json={"username": username, "password": "Mauvais2026xx"})
        assert response.status_code == 401
    # Compte verrouille : meme le BON mot de passe est refuse (423)
    locked = client.post("/api/auth/login", json={"username": username, "password": STRONG_PW})
    assert locked.status_code == 423
    assert "verrouillé" in locked.json()["detail"].lower()

    # Verrouillage et echecs traces au journal d'administration
    db = next(get_db())
    try:
        actions = {r.action for r in db.query(AdminAuditLog).filter(AdminAuditLog.username == username).all()}
        assert "LOGIN_FAILED" in actions and "ACCOUNT_LOCKED" in actions
        # Deverrouillage manuel (simule l'expiration de la fenetre)
        user = db.query(User).filter(User.username == username).first()
        user.locked_until = None
        db.commit()
    finally:
        db.close()

    # Connexion reussie : compteur remis a zero, session tracee LOGIN
    success = client.post("/api/auth/login", json={"username": username, "password": STRONG_PW})
    assert success.status_code == 200
    assert success.json()["access_token"]
    db = next(get_db())
    try:
        user = db.query(User).filter(User.username == username).first()
        assert user.failed_login_count == 0 and user.locked_until is None
        assert db.query(AdminAuditLog).filter(
            AdminAuditLog.username == username, AdminAuditLog.action == "LOGIN"
        ).count() >= 1
    finally:
        db.close()


# ------------------ EN-TETES DE SECURITE & HEALTHCHECK ------------------

def test_security_headers_present(client):
    response = client.get("/api/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert "Referrer-Policy" in response.headers


def test_healthcheck_unauthenticated(client):
    # Aucune authentification requise, contenu volontairement minimal
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] in ("ok", "degraded")
    assert set(data.keys()) == {"status", "database", "watchlist_cache_loaded"}
    assert data["database"] is True


# ------------------ CLES D'API TECHNIQUES ------------------

def test_api_key_lifecycle(client):
    name = f"test_sec_cle_{uuid.uuid4().hex[:6]}"
    created = client.post("/api/apikeys", json={"name": name, "role": "user"})
    assert created.status_code == 200, created.text
    data = created.json()
    full_key = data["api_key"]
    assert full_key.startswith("fsk_") and data["prefix"] == full_key[:12]
    key_id = data["id"]

    # La liste n'expose jamais la cle complete
    listed = client.get("/api/apikeys").json()["items"]
    row = next(k for k in listed if k["id"] == key_id)
    assert row["active"] is True and "api_key" not in row

    # Authentification reelle par X-API-Key (sans override de dependance)
    saved_override = app.dependency_overrides.pop(get_current_user)
    try:
        ok = client.get("/api/counters", headers={"X-API-Key": full_key})
        assert ok.status_code == 200
        # Mauvaise cle -> 401
        assert client.get("/api/counters", headers={"X-API-Key": "fsk_fausse_cle_x"}).status_code == 401
    finally:
        app.dependency_overrides[get_current_user] = saved_override

    # Revocation immediate
    assert client.post(f"/api/apikeys/{key_id}/revoke").status_code == 200
    saved_override = app.dependency_overrides.pop(get_current_user)
    try:
        assert client.get("/api/counters", headers={"X-API-Key": full_key}).status_code == 401
    finally:
        app.dependency_overrides[get_current_user] = saved_override
    # Double revocation -> 409
    assert client.post(f"/api/apikeys/{key_id}/revoke").status_code == 409
    # Usage trace (last_used_at renseigne par l'appel reussi)
    row = next(k for k in client.get("/api/apikeys").json()["items"] if k["id"] == key_id)
    assert row["last_used_at"] is not None


def test_api_key_admin_role_forbidden(client):
    response = client.post("/api/apikeys", json={"name": f"test_sec_{uuid.uuid4().hex[:6]}", "role": "admin"})
    assert response.status_code == 400
    assert "moindre privilège" in response.json()["detail"]

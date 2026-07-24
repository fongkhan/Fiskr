"""
Tests du lot operations :
- MFA TOTP : generateur RFC 6238 (vecteur de test officiel), fenetre de
  tolerance, enrolement -> confirmation -> login en 2 temps -> desactivation,
  reinitialisation par un admin, echecs comptes au verrouillage ;
- actions en masse sur les alertes : assignation et priorite avec un
  AlertEvent par alerte, alertes cloturees ignorees, garde admin et bornes ;
- digest KPI periodique : contenu de la synthese, reglage a chaud avec
  validation de l'expression cron.
"""
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app, build_kpi_digest
from fiskr.auth import get_current_user, totp_code, verify_totp, generate_totp_secret
from fiskr.database import get_db, User, Alert, AlertEvent, AdminAuditLog, AppSetting
from fiskr.settings import digest_settings, SETTING_DIGEST

STRONG_PW = "OperationsFort2026"


def _override_user(username: str, role: str = "user", user_id: int = 1):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": user_id, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_ops_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(User).filter(User.username.like("test_ops_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username.like("test_ops_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_ops").delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key == SETTING_DIGEST).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin_ops", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_ops_{uuid.uuid4().hex[:8]}"


def _create_user(client, username, role="user"):
    response = client.post("/api/users", json={
        "username": username, "password": STRONG_PW, "full_name": username, "role": role,
    })
    assert response.status_code == 200, response.text
    db = next(get_db())
    try:
        return db.query(User).filter(User.username == username).first().id
    finally:
        db.close()


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


# ------------------ MFA TOTP : GENERATEUR RFC 6238 ------------------

def test_totp_rfc6238_test_vector():
    # Vecteur officiel RFC 6238 (SHA-1) : secret ASCII "12345678901234567890",
    # T=59 s -> 94287082 ; nos 6 chiffres = les 6 derniers
    secret_b32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_code(secret_b32, 59) == "287082"
    assert totp_code(secret_b32, 1111111109) == "081804"


def test_verify_totp_window_and_rejects():
    secret = generate_totp_secret()
    import time
    now = time.time()
    assert verify_totp(secret, totp_code(secret, now)) is True
    # Tolerance d'un pas de 30 s (derive d'horloge)
    assert verify_totp(secret, totp_code(secret, now - 30)) is True
    # Codes invalides : mauvais format, mauvais code
    assert verify_totp(secret, "12345") is False
    assert verify_totp(secret, "abcdef") is False
    wrong = "000000" if totp_code(secret, now) != "000000" else "111111"
    assert verify_totp(secret, wrong) is False
    assert verify_totp("", "123456") is False


# ------------------ MFA TOTP : CYCLE COMPLET ------------------

def test_totp_full_lifecycle(client):
    username = f"test_ops_{uuid.uuid4().hex[:8]}"
    user_id = _create_user(client, username)

    # Enrolement au nom de l'utilisateur (setup -> secret -> confirmation)
    _override_user(username, "user", user_id)
    setup = client.post("/api/auth/totp/setup")
    assert setup.status_code == 200, setup.text
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    # Confirmation avec un mauvais code -> refusee, MFA toujours inactive
    assert client.post("/api/auth/totp/confirm", json={"code": "000001"}).status_code == 400
    confirmed = client.post("/api/auth/totp/confirm", json={"code": totp_code(secret)})
    assert confirmed.status_code == 200, confirmed.text
    # Double activation -> 409
    assert client.post("/api/auth/totp/setup").status_code == 409

    # Login sans code -> 401 avec le drapeau totp_required (pas un echec compte)
    no_code = client.post("/api/auth/login", json={"username": username, "password": STRONG_PW})
    assert no_code.status_code == 401
    assert no_code.json()["detail"]["totp_required"] is True
    db = next(get_db())
    try:
        assert db.query(User).filter(User.username == username).first().failed_login_count == 0
    finally:
        db.close()

    # Mauvais code -> 401 comptabilise comme echec (anti-brute-force)
    bad = client.post("/api/auth/login", json={
        "username": username, "password": STRONG_PW, "totp_code": "000001"})
    assert bad.status_code == 401
    db = next(get_db())
    try:
        assert db.query(User).filter(User.username == username).first().failed_login_count == 1
    finally:
        db.close()

    # Bon code -> 200 (session ouverte, compteur remis a zero)
    good = client.post("/api/auth/login", json={
        "username": username, "password": STRONG_PW, "totp_code": totp_code(secret)})
    assert good.status_code == 200, good.text
    assert good.json()["access_token"]

    # Desactivation : mauvais mot de passe refuse, bon mot de passe accepte
    assert client.post("/api/auth/totp/disable", json={"password": "Mauvais2026xx"}).status_code == 401
    assert client.post("/api/auth/totp/disable", json={"password": STRONG_PW}).status_code == 200
    # Login redevient mot de passe seul
    plain = client.post("/api/auth/login", json={"username": username, "password": STRONG_PW})
    assert plain.status_code == 200
    # Activation/desactivation tracees au journal d'administration
    db = next(get_db())
    try:
        actions = {r.action for r in db.query(AdminAuditLog).filter(
            AdminAuditLog.username == username).all()}
        assert "MFA_ENABLED" in actions and "MFA_DISABLED" in actions
    finally:
        db.close()


def test_totp_admin_reset(client):
    username = f"test_ops_{uuid.uuid4().hex[:8]}"
    user_id = _create_user(client, username)
    _override_user(username, "user", user_id)
    secret = client.post("/api/auth/totp/setup").json()["secret"]
    assert client.post("/api/auth/totp/confirm", json={"code": totp_code(secret)}).status_code == 200

    # Reinitialisation par un admin (telephone perdu)
    _override_user("admin_ops", "admin")
    assert client.post(f"/api/users/{user_id}/totp/reset").status_code == 200
    # MFA non active -> nouvelle reinitialisation refusee
    assert client.post(f"/api/users/{user_id}/totp/reset").status_code == 409
    # L'utilisateur se reconnecte au mot de passe seul
    plain = client.post("/api/auth/login", json={"username": username, "password": STRONG_PW})
    assert plain.status_code == 200
    # La liste admin expose l'etat MFA
    row = next(u for u in client.get("/api/users").json() if u["id"] == user_id)
    assert row["totp_enabled"] is False


# ------------------ ACTIONS EN MASSE SUR LES ALERTES ------------------

def test_bulk_assign_and_priority(client):
    alert_ids = [_screen_putin(client, _new_client_id())["alert_id"] for _ in range(3)]

    # Assignation en masse a soi-meme
    assigned = client.post("/api/alerts/bulk", json={"ids": alert_ids, "action": "assign"})
    assert assigned.status_code == 200, assigned.text
    assert sorted(assigned.json()["updated"]) == sorted(alert_ids)
    db = next(get_db())
    try:
        for alert_id in alert_ids:
            alert = db.query(Alert).filter(Alert.id == alert_id).first()
            assert alert.assigned_to == "admin_ops" and alert.status == "IN_PROGRESS"
            # Un evenement par alerte : jamais d'action silencieuse
            assert db.query(AlertEvent).filter(
                AlertEvent.alert_id == alert_id, AlertEvent.action == "ASSIGNED").count() == 1
    finally:
        db.close()

    # Priorite en masse : echeance SLA recalculee
    prio = client.post("/api/alerts/bulk", json={
        "ids": alert_ids, "action": "priority", "priority": "CRITICAL"})
    assert prio.status_code == 200
    db = next(get_db())
    try:
        for alert_id in alert_ids:
            alert = db.query(Alert).filter(Alert.id == alert_id).first()
            assert alert.priority == "CRITICAL" and alert.due_at is not None
        # Une alerte cloturee est ignoree au passage suivant
        closed = db.query(Alert).filter(Alert.id == alert_ids[0]).first()
        closed.status = "CLOSED_FALSE_POSITIVE"
        db.commit()
    finally:
        db.close()
    again = client.post("/api/alerts/bulk", json={
        "ids": alert_ids, "action": "priority", "priority": "LOW"})
    assert again.status_code == 200
    assert alert_ids[0] in again.json()["skipped"]
    assert sorted(again.json()["updated"]) == sorted(alert_ids[1:])


def test_bulk_validation_and_permissions(client):
    alert_id = _screen_putin(client, _new_client_id())["alert_id"]
    # Bornes et validations
    assert client.post("/api/alerts/bulk", json={"ids": [], "action": "assign"}).status_code == 400
    assert client.post("/api/alerts/bulk", json={"ids": [alert_id], "action": "explode"}).status_code == 400
    assert client.post("/api/alerts/bulk", json={
        "ids": [alert_id], "action": "priority", "priority": "EXTREME"}).status_code == 400
    assert client.post("/api/alerts/bulk", json={
        "ids": list(range(1, 202)), "action": "assign"}).status_code == 400
    # Assigner a un tiers exige le role admin
    _override_user("test_ops_analyste", "user", 42)
    forbidden = client.post("/api/alerts/bulk", json={
        "ids": [alert_id], "action": "assign", "assignee": "quelqu_un_d_autre"})
    assert forbidden.status_code == 403


# ------------------ DIGEST KPI PERIODIQUE ------------------

def test_digest_content_and_setting(client):
    _screen_putin(client, _new_client_id())
    db = next(get_db())
    try:
        digest = build_kpi_digest(db)
    finally:
        db.close()
    for key in ("Alertes ouvertes — criblage", "Alertes ouvertes — filtrage",
                "Alertes en retard SLA", "Décisions en attente 4-yeux",
                "Snapshots à homologuer", "Alertes créées (24 h)",
                "Alertes clôturées (24 h)", "Dernières synchronisations"):
        assert key in digest, f"cle manquante au digest : {key}"
    assert digest["Alertes ouvertes — criblage"] >= 1
    assert digest["Alertes créées (24 h)"] >= 1

    # Reglage a chaud : cron invalide rejete, cron valide enregistre
    bad = client.put("/api/settings/ingestion", json={"digest": {"enabled": True, "cron": "61 24 * * *"}})
    assert bad.status_code == 400
    ok = client.put("/api/settings/ingestion", json={"digest": {"enabled": True, "cron": "0 7 * * 1-5"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["digest"] == {"enabled": True, "cron": "0 7 * * 1-5"}
    db = next(get_db())
    try:
        assert digest_settings(db) == {"enabled": True, "cron": "0 7 * * 1-5"}
    finally:
        db.close()

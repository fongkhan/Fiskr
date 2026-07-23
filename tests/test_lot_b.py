"""
Tests du lot B (conformite operationnelle) :
- priorites calculees + echeances SLA + retard, changement de priorite journalise ;
- pieces jointes d'alertes (upload, listing, telechargement) ;
- exports CSV (alertes, journal d'audit, vue base) et rapport d'alerte HTML ;
- journal des actions d'administration (users, reglages) + garde admin ;
- reglages SLA / notifications a chaud + robustesse du module notify ;
- parametre search sur la file d'alertes (recherche globale).
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, Alert, AlertEvent, AlertAttachment, AdminAuditLog, AppSetting, User,
)
from fiskr.settings import (
    SETTING_ALERT_FOUR_EYES, SETTING_ALERT_SLA_HOURS, SETTING_NOTIFICATIONS,
    DEFAULT_ALERT_SLA_HOURS,
)


def _override_user(username: str, role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_([
            SETTING_ALERT_FOUR_EYES, SETTING_ALERT_SLA_HOURS, SETTING_NOTIFICATIONS,
        ])).delete(synchronize_session=False)
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_lotb_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertAttachment).filter(AlertAttachment.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(User).filter(User.username.like("test_lotb_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_lotb").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("analyste_lotb", "user")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_lotb_{uuid.uuid4().hex[:8]}"


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


# ------------------ PRIORITES & SLA ------------------

def test_priority_and_due_at_computed_on_creation(client):
    data = _screen_putin(client, _new_client_id())
    detail = client.get(f"/api/alerts/{data['alert_id']}").json()
    # Score tres eleve sans hard match -> HIGH, echeance SLA posee, pas en retard
    assert detail["priority"] == "HIGH"
    assert detail["due_at"] is not None
    assert detail["overdue"] is False


def test_change_priority_recomputes_due_and_logs_event(client):
    data = _screen_putin(client, _new_client_id())
    alert_id = data["alert_id"]

    response = client.post(f"/api/alerts/{alert_id}/priority", json={"priority": "CRITICAL"})
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["priority"] == "CRITICAL"

    detail = client.get(f"/api/alerts/{alert_id}").json()
    assert "PRIORITY_CHANGED" in [e["action"] for e in detail["events"]]
    # Echeance recalculee depuis la creation avec le SLA CRITICAL (defaut 24 h)
    created = datetime.fromisoformat(detail["created_at"])
    due = datetime.fromisoformat(detail["due_at"])
    assert abs((due - created) - timedelta(hours=DEFAULT_ALERT_SLA_HOURS["CRITICAL"])).total_seconds() < 5

    # Priorite inconnue -> 400
    assert client.post(f"/api/alerts/{alert_id}/priority", json={"priority": "URGENTISSIME"}).status_code == 400


def test_overdue_flag_when_due_date_passed(client):
    data = _screen_putin(client, _new_client_id())
    db = next(get_db())
    try:
        alert = db.query(Alert).filter(Alert.id == data["alert_id"]).first()
        alert.due_at = datetime.utcnow() - timedelta(hours=2)
        db.commit()
    finally:
        db.close()
    detail = client.get(f"/api/alerts/{data['alert_id']}").json()
    assert detail["overdue"] is True


def test_sla_setting_drives_due_date(client):
    _override_user("admin_lotb", "admin")
    response = client.put("/api/settings/ingestion", json={"alert_sla_hours": {"CRITICAL": 2}})
    assert response.status_code == 200, response.text
    assert response.json()["alert_sla_hours"]["CRITICAL"] == 2

    data = _screen_putin(client, _new_client_id())
    client.post(f"/api/alerts/{data['alert_id']}/priority", json={"priority": "CRITICAL"})
    detail = client.get(f"/api/alerts/{data['alert_id']}").json()
    created = datetime.fromisoformat(detail["created_at"])
    due = datetime.fromisoformat(detail["due_at"])
    assert abs((due - created) - timedelta(hours=2)).total_seconds() < 5


# ------------------ PIECES JOINTES ------------------

def test_alert_attachment_upload_and_download(client):
    data = _screen_putin(client, _new_client_id())
    alert_id = data["alert_id"]

    response = client.post(
        f"/api/alerts/{alert_id}/attachments",
        files={"file": ("justificatif KYC.txt", b"contenu probant", "text/plain")},
        data={"comment": "Justificatif de test"},
    )
    assert response.status_code == 200, response.text
    attachment_id = response.json()["attachment_id"]

    detail = client.get(f"/api/alerts/{alert_id}").json()
    atts = detail["attachments"]
    assert len(atts) == 1
    assert atts[0]["comment"] == "Justificatif de test"
    assert "ATTACHMENT" in [e["action"] for e in detail["events"]]

    download = client.get(f"/api/alerts/attachments/{attachment_id}")
    assert download.status_code == 200
    assert download.content == b"contenu probant"


# ------------------ EXPORTS & RAPPORT ------------------

def test_export_alerts_csv(client):
    cid = _new_client_id()
    _screen_putin(client, cid)
    response = client.get("/api/export/alerts.csv", params={"channel": "SCREENING"})
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    body = response.content.decode("utf-8")
    assert body.startswith("﻿")  # BOM Excel
    assert body.splitlines()[0].lstrip("﻿").startswith("id;cree_le;canal;priorite")
    assert cid in body


def test_export_history_and_watchlist_csv(client):
    _screen_putin(client, _new_client_id())
    history = client.get("/api/export/history.csv", params={"status": "ALERT"})
    assert history.status_code == 200
    assert history.content.decode("utf-8").splitlines()[0].startswith("﻿id;horodatage")

    watchlist = client.get("/api/export/watchlist.csv")
    assert watchlist.status_code == 200
    assert watchlist.content.decode("utf-8").splitlines()[0].startswith("﻿entity_id;liste")
    # Scope invalide -> 400
    assert client.get("/api/export/watchlist.csv", params={"scope": "nimporte"}).status_code == 400


def test_alert_report_html(client):
    cid = _new_client_id()
    data = _screen_putin(client, cid)
    response = client.get(f"/api/alerts/{data['alert_id']}/report")
    assert response.status_code == 200
    html = response.text
    assert f"Rapport d'alerte #{data['alert_id']}" in html
    assert "Arbre de décision du moteur" in html
    assert "Historique des actions" in html
    assert cid in html


# ------------------ JOURNAL D'ADMINISTRATION ------------------

def test_admin_actions_are_logged(client):
    _override_user("admin_lotb", "admin")
    username = f"test_lotb_{uuid.uuid4().hex[:6]}"
    response = client.post("/api/users", json={
        "username": username, "password": "MotDePasse2026x", "full_name": "Testeur LotB", "role": "user",
    })
    assert response.status_code == 200, response.text
    user_id = response.json()["user"]["id"]

    # Changement de reglage egalement trace (avant -> apres)
    assert client.put("/api/settings/ingestion", json={"auto_rescreen": True}).status_code == 200

    client.delete(f"/api/users/{user_id}")

    log = client.get("/api/admin-log", params={"page_size": 50}).json()
    actions = {(r["action"], r["target"]) for r in log["items"]}
    assert ("USER_CREATED", username) in actions
    assert ("USER_DELETED", username) in actions
    assert any(r["action"] == "SETTINGS_UPDATED" for r in log["items"])
    created_row = next(r for r in log["items"] if r["action"] == "USER_CREATED" and r["target"] == username)
    assert created_row["after"]["role"] == "user"


def test_admin_log_requires_admin(client):
    # Fixture = role "user" -> acces refuse
    assert client.get("/api/admin-log").status_code == 403


# ------------------ NOTIFICATIONS ------------------

def test_notification_settings_roundtrip_and_validation(client):
    _override_user("admin_lotb", "admin")
    response = client.put("/api/settings/ingestion", json={"notification_events": {"alert_created": True}})
    assert response.status_code == 200
    events = response.json()["notification_events"]
    assert events["alert_created"] is True
    assert events["sync_error"] is True  # defaut conserve
    # Evenement inconnu -> 400
    assert client.put("/api/settings/ingestion",
                      json={"notification_events": {"invente": True}}).status_code == 400


def test_notify_event_never_raises(monkeypatch):
    """Le canal de notification est fire-and-forget : meme un dispatch en
    echec complet ne remonte jamais a l'appelant."""
    from fiskr import notify

    def boom(*args, **kwargs):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(notify, "_send_email", boom)
    monkeypatch.setattr(notify, "_webhook_urls", lambda: ["http://127.0.0.1:1/x"])
    # Dispatch direct (synchrone) : les erreurs sont avalees et journalisees
    notify._dispatch("sync_error", {"source": "TEST"})
    # Enveloppe thread : jamais d'exception
    notify.notify_event("sync_error", {"source": "TEST"})


# ------------------ RECHERCHE (file d'alertes) ------------------

def test_alerts_search_param(client):
    cid = _new_client_id()
    _screen_putin(client, cid)
    found = client.get("/api/alerts", params={"search": cid}).json()
    assert found["total"] == 1
    assert found["items"][0]["client_id"] == cid
    # Terme sans rapport -> aucun resultat
    assert client.get("/api/alerts", params={"search": "zzz-introuvable-zzz"}).json()["total"] == 0


def test_alerts_priority_filter_and_ordering(client):
    cid_a, cid_b = _new_client_id(), _new_client_id()
    id_a = _screen_putin(client, cid_a)["alert_id"]
    _screen_putin(client, cid_b)
    client.post(f"/api/alerts/{id_a}/priority", json={"priority": "CRITICAL"})

    only_critical = client.get("/api/alerts", params={"priority": "CRITICAL", "search": "test_lotb_"}).json()
    assert {a["id"] for a in only_critical["items"]} == {id_a}
    # Tri : CRITICAL avant HIGH dans la file non filtree
    both = client.get("/api/alerts", params={"search": "test_lotb_"}).json()
    priorities = [a["priority"] for a in both["items"]]
    assert priorities.index("CRITICAL") < priorities.index("HIGH")
    # Priorite inconnue -> 400
    assert client.get("/api/alerts", params={"priority": "NOPE"}).status_code == 400

"""
Tests du lot gouvernance :
- retention/purge : politique a chaud avec garde-fous (minimum 30 jours,
  cron valide), previsualisation, purge reelle (alertes cloturees + events,
  audit orphelin seulement, rapports de sync), trace RETENTION_PURGE,
  journal admin jamais purge ;
- vues sauvegardees : CRUD par utilisateur, mise a jour au meme nom,
  isolation entre utilisateurs, suppression protegee ;
- rapport d'activite : contenu par periode, bornes de dates validees,
  export CSV.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, Alert, AlertEvent, AuditTrail, AdminAuditLog, AppSetting,
    SavedView, SyncReport,
)
from fiskr.retention import preview_retention, run_retention
from fiskr.settings import retention_policy, SETTING_RETENTION, RETENTION_MIN_DAYS


def _override_user(username: str, role: str = "user"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_gouv_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like("test_gouv_%")).delete(synchronize_session=False)
        db.query(SavedView).filter(SavedView.username.like("test_gouv_%")).delete(synchronize_session=False)
        db.query(SyncReport).filter(SyncReport.message == "test_gouv").delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username.like("test_gouv_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_gouv").delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key == SETTING_RETENTION).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin_gouv", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_gouv_{uuid.uuid4().hex[:8]}"


def _screen_putin(client, client_id):
    response = client.post("/api/screen", json={
        "client_id": client_id, "client_type": "PP",
        "client_first_name": "Vladimir", "client_last_name": "Putin",
        "client_dob": "1952-10-07", "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    })
    assert response.status_code == 200, response.text
    return response.json()


# ------------------ RETENTION ------------------

def test_retention_settings_guards(client):
    # Etat initial : conservation illimitee partout
    initial = client.get("/api/admin/retention").json()
    assert initial["policy"]["closed_alerts"] == 0
    assert initial["min_days"] == RETENTION_MIN_DAYS
    # Duree trop courte -> 400 (garde-fou)
    assert client.put("/api/settings/retention", json={"closed_alerts": 5}).status_code == 400
    # Cron invalide -> 400
    assert client.put("/api/settings/retention", json={"cron": "61 25 * * *"}).status_code == 400
    # Aucun reglage -> 400
    assert client.put("/api/settings/retention", json={}).status_code == 400
    # Reglage valide persiste (0 = illimite reste accepte)
    ok = client.put("/api/settings/retention", json={
        "closed_alerts": 90, "sync_reports": 30, "audit_trail": 0, "cron": "15 3 * * *"})
    assert ok.status_code == 200, ok.text
    policy = ok.json()["policy"]
    assert policy["closed_alerts"] == 90 and policy["sync_reports"] == 30
    assert policy["audit_trail"] == 0 and policy["cron"] == "15 3 * * *"


def test_retention_purge_end_to_end(client):
    cid = _new_client_id()
    data = _screen_putin(client, cid)
    alert_id = data["alert_id"]

    db = next(get_db())
    try:
        # Alerte cloturee il y a 100 jours + rapport de sync vieux de 100 jours
        old = datetime.utcnow() - timedelta(days=100)
        alert = db.query(Alert).filter(Alert.id == alert_id).first()
        audit_id = alert.audit_id
        alert.status = "CLOSED_FALSE_POSITIVE"
        alert.decided_at = old
        db.query(AuditTrail).filter(AuditTrail.id == audit_id).first().timestamp = old
        db.add(SyncReport(source="OFAC", executed_at=old, status="SUCCESS", message="test_gouv"))
        db.commit()
    finally:
        db.close()

    assert client.put("/api/settings/retention", json={
        "closed_alerts": 90, "sync_reports": 90, "audit_trail": 90}).status_code == 200

    db = next(get_db())
    try:
        # Previsualisation : l'audit encore reference par l'alerte n'est PAS purgeable
        preview = preview_retention(db)
        assert preview["closed_alerts"] >= 1
        assert preview["sync_reports"] >= 1

        deleted = run_retention(db, username="admin_gouv")
        assert deleted["closed_alerts"] >= 1 and deleted["sync_reports"] >= 1
        # L'alerte purgee libere sa ligne d'audit, purgee dans la MEME passe
        # (les alertes partent avant le journal de criblage)
        assert deleted["audit_trail"] >= 1
        assert db.query(Alert).filter(Alert.id == alert_id).first() is None
        assert db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id).count() == 0
        assert db.query(AuditTrail).filter(AuditTrail.id == audit_id).first() is None
        assert db.query(SyncReport).filter(SyncReport.message == "test_gouv").count() == 0
        # Purge tracee au journal admin (qui, lui, n'est jamais purge)
        trace = db.query(AdminAuditLog).filter(
            AdminAuditLog.username == "admin_gouv",
            AdminAuditLog.action == "RETENTION_PURGE").first()
        assert trace is not None and trace.after["closed_alerts"] >= 1

        # Second passage : plus rien a purger (idempotent)
        deleted2 = run_retention(db, username="admin_gouv")
        assert not any(deleted2.values())
    finally:
        db.close()


def test_retention_requires_admin(client):
    _override_user("test_gouv_user", "user")
    assert client.get("/api/admin/retention").status_code == 403
    assert client.put("/api/settings/retention", json={"closed_alerts": 90}).status_code == 403
    assert client.post("/api/admin/retention/run").status_code == 403


# ------------------ VUES SAUVEGARDEES ------------------

def test_saved_views_crud_and_isolation(client):
    me = f"test_gouv_{uuid.uuid4().hex[:6]}"
    _override_user(me, "user")
    filters = {"status": "OPEN,IN_PROGRESS", "priority": "CRITICAL", "list_type": ""}
    created = client.post("/api/views", json={
        "name": "Critiques à traiter", "channel": "SCREENING", "filters": filters})
    assert created.status_code == 200, created.text
    view_id = created.json()["id"]
    # Les filtres vides ne sont pas stockes
    assert created.json()["filters"] == {"status": "OPEN,IN_PROGRESS", "priority": "CRITICAL"}

    # Meme nom = mise a jour, pas de doublon
    updated = client.post("/api/views", json={
        "name": "Critiques à traiter", "channel": "SCREENING",
        "filters": {"status": "OPEN", "priority": "HIGH"}})
    assert updated.status_code == 200 and updated.json()["id"] == view_id

    listed = client.get("/api/views", params={"channel": "SCREENING"}).json()["items"]
    assert len([v for v in listed if v["id"] == view_id]) == 1
    assert client.get("/api/views", params={"channel": "FILTERING"}).json()["items"] == []

    # Validations
    assert client.post("/api/views", json={"name": "", "channel": "SCREENING"}).status_code == 400
    assert client.post("/api/views", json={"name": "x", "channel": "AUTRE"}).status_code == 400

    # Un autre utilisateur ne voit pas la vue et ne peut pas la supprimer
    _override_user(f"test_gouv_{uuid.uuid4().hex[:6]}", "user")
    assert client.get("/api/views").json()["items"] == []
    assert client.delete(f"/api/views/{view_id}").status_code == 403
    # Un admin peut ; le proprietaire aussi
    _override_user(me, "user")
    assert client.delete(f"/api/views/{view_id}").status_code == 200
    assert client.delete(f"/api/views/{view_id}").status_code == 404


# ------------------ RAPPORT D'ACTIVITE ------------------

def test_activity_report_content_and_bounds(client):
    cid = _new_client_id()
    _screen_putin(client, cid)
    today = datetime.utcnow().strftime("%Y-%m-%d")

    report = client.get("/api/reports/activity", params={"date_from": today, "date_to": today})
    assert report.status_code == 200, report.text
    data = report.json()
    assert data["period"] == {"from": today, "to": today}
    assert data["screenings"]["total"] >= 1
    assert data["alerts"]["created"] >= 1
    assert "SCREENING" in data["alerts"]["created_by_channel"]
    for section in ("whitelist", "syncs", "batch"):
        assert section in data

    # Une periode passee sans activite de ce client reste valide et coherente
    empty = client.get("/api/reports/activity",
                       params={"date_from": "2000-01-01", "date_to": "2000-01-31"}).json()
    assert empty["screenings"]["total"] == 0 and empty["alerts"]["created"] == 0

    # Bornes invalides
    assert client.get("/api/reports/activity",
                      params={"date_from": "pas-une-date"}).status_code == 400
    assert client.get("/api/reports/activity",
                      params={"date_from": today, "date_to": "2000-01-01"}).status_code == 400


def test_activity_report_csv_and_print(client):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    csv_resp = client.get("/api/reports/activity.csv",
                          params={"date_from": today, "date_to": today})
    assert csv_resp.status_code == 200
    assert "text/csv" in csv_resp.headers["content-type"]
    first_line = csv_resp.text.lstrip("﻿").splitlines()[0]
    assert first_line == "Section;Indicateur;Valeur"
    assert "Criblage" in csv_resp.text and "Liste blanche" in csv_resp.text

    printable = client.get("/api/reports/activity/print",
                           params={"date_from": today, "date_to": today})
    assert printable.status_code == 200
    assert "Rapport d'activité" in printable.text and "<table>" in printable.text

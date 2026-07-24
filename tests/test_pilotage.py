"""
Tests du lot pilotage & portabilite :
- archivage avant purge : JSON Lines par table dans retention_archive/,
  chemin trace dans RETENTION_PURGE, desactivable ;
- charge de travail des analystes : ventilation par assigne/priorite,
  retards, prochaine echeance, non-assignees, filtre canal ;
- import/export de configuration : export JSON sans secret, import avec
  cles inconnues ignorees, delta journalise SETTINGS_IMPORTED, garde admin.
"""
import json
import shutil
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AuditTrail, AdminAuditLog, AppSetting
from fiskr.retention import run_retention, ARCHIVE_DIR
from fiskr.settings import SETTING_RETENTION, SETTING_ALERT_FOUR_EYES, SETTING_DIGEST


def _override_user(username: str, role: str = "user"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_pp_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like("test_pp_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_pp").delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key.in_(
            [SETTING_RETENTION, SETTING_ALERT_FOUR_EYES, SETTING_DIGEST])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin_pp", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_pp_{uuid.uuid4().hex[:8]}"


def _screen_putin(client, client_id):
    response = client.post("/api/screen", json={
        "client_id": client_id, "client_type": "PP",
        "client_first_name": "Vladimir", "client_last_name": "Putin",
        "client_dob": "1952-10-07", "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    })
    assert response.status_code == 200, response.text
    return response.json()


# ------------------ ARCHIVAGE AVANT PURGE ------------------

def test_purge_archives_before_delete(client):
    cid = _new_client_id()
    alert_id = _screen_putin(client, cid)["alert_id"]
    db = next(get_db())
    try:
        old = datetime.utcnow() - timedelta(days=100)
        alert = db.query(Alert).filter(Alert.id == alert_id).first()
        alert.status = "CLOSED_FALSE_POSITIVE"
        alert.decided_at = old
        db.commit()
    finally:
        db.close()
    assert client.put("/api/settings/retention",
                      json={"closed_alerts": 90, "archive": True}).status_code == 200

    before_dirs = set(ARCHIVE_DIR.glob("purge_*")) if ARCHIVE_DIR.exists() else set()
    db = next(get_db())
    try:
        deleted = run_retention(db, username="admin_pp")
        assert deleted["closed_alerts"] >= 1
        # Une archive horodatee a ete creee avec l'alerte purgee dedans
        new_dirs = set(ARCHIVE_DIR.glob("purge_*")) - before_dirs
        assert len(new_dirs) == 1
        archive = new_dirs.pop()
        alerts_file = archive / "alerts.jsonl"
        assert alerts_file.exists()
        archived = [json.loads(line) for line in alerts_file.read_text(encoding="utf-8").splitlines()]
        assert any(row["id"] == alert_id and row["client_id"] == cid for row in archived)
        # Chemin de l'archive trace au journal admin
        trace = db.query(AdminAuditLog).filter(
            AdminAuditLog.username == "admin_pp",
            AdminAuditLog.action == "RETENTION_PURGE").order_by(AdminAuditLog.id.desc()).first()
        assert trace.after["archive"] == str(archive)
        shutil.rmtree(archive, ignore_errors=True)
    finally:
        db.close()


def test_purge_without_archive_when_disabled(client):
    cid = _new_client_id()
    alert_id = _screen_putin(client, cid)["alert_id"]
    db = next(get_db())
    try:
        alert = db.query(Alert).filter(Alert.id == alert_id).first()
        alert.status = "CLOSED_FALSE_POSITIVE"
        alert.decided_at = datetime.utcnow() - timedelta(days=100)
        db.commit()
    finally:
        db.close()
    ok = client.put("/api/settings/retention", json={"closed_alerts": 90, "archive": False})
    assert ok.status_code == 200 and ok.json()["policy"]["archive"] is False

    before_dirs = set(ARCHIVE_DIR.glob("purge_*")) if ARCHIVE_DIR.exists() else set()
    db = next(get_db())
    try:
        deleted = run_retention(db, username="admin_pp")
        assert deleted["closed_alerts"] >= 1
        after_dirs = set(ARCHIVE_DIR.glob("purge_*")) if ARCHIVE_DIR.exists() else set()
        assert after_dirs == before_dirs  # aucune archive creee
        trace = db.query(AdminAuditLog).filter(
            AdminAuditLog.username == "admin_pp",
            AdminAuditLog.action == "RETENTION_PURGE").order_by(AdminAuditLog.id.desc()).first()
        assert trace.after["archive"] is None
    finally:
        db.close()


# ------------------ CHARGE DE TRAVAIL ------------------

def test_workload_breakdown(client):
    analyst = f"test_pp_{uuid.uuid4().hex[:6]}"
    ids = [_screen_putin(client, _new_client_id())["alert_id"] for _ in range(3)]
    db = next(get_db())
    try:
        a0 = db.query(Alert).filter(Alert.id == ids[0]).first()
        a0.assigned_to = analyst
        a0.priority = "CRITICAL"
        a0.due_at = datetime.utcnow() - timedelta(hours=3)   # en retard
        a1 = db.query(Alert).filter(Alert.id == ids[1]).first()
        a1.assigned_to = analyst
        a1.priority = "LOW"
        a1.due_at = datetime.utcnow() + timedelta(hours=48)
        # ids[2] reste non assignee
        db.commit()
    finally:
        db.close()

    data = client.get("/api/alerts/workload", params={"channel": "SCREENING"}).json()
    me = next(x for x in data["analysts"] if x["username"] == analyst)
    assert me["open_total"] == 2
    assert me["by_priority"]["CRITICAL"] == 1 and me["by_priority"]["LOW"] == 1
    assert me["overdue"] == 1
    # Prochaine echeance = la plus proche (celle en retard)
    assert me["next_due_at"] is not None
    assert data["unassigned"]["open_total"] >= 1
    assert data["totals"]["open"] >= 3 and data["totals"]["overdue"] >= 1
    # L'analyste en retard est trie en tete
    assert data["analysts"][0]["overdue"] >= data["analysts"][-1]["overdue"]
    # Filtre canal : le filtrage n'inclut pas ces alertes de criblage
    filtering = client.get("/api/alerts/workload", params={"channel": "FILTERING"}).json()
    assert all(x["username"] != analyst for x in filtering["analysts"])


# ------------------ IMPORT / EXPORT DE CONFIGURATION ------------------

def test_config_export_import_roundtrip(client):
    # Pose deux reglages puis exporte
    assert client.put("/api/settings/ingestion", json={
        "alert_four_eyes_required": False,
        "digest": {"enabled": True, "cron": "0 9 * * 1-5"}}).status_code == 200
    export = client.get("/api/admin/config/export")
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    payload = json.loads(export.text)
    assert payload["application"] == "fiskr"
    assert payload["settings"]["review.alert_four_eyes_required"] is False
    assert payload["settings"]["notifications.digest"]["cron"] == "0 9 * * 1-5"
    # Jamais de secrets dans l'export
    assert not any("password" in k or "apikey" in k or "secret" in k for k in payload["settings"])

    # Modifie puis reimporte l'export : retour a l'etat exporte + journalisation
    assert client.put("/api/settings/ingestion",
                      json={"alert_four_eyes_required": True}).status_code == 200
    imported = client.post("/api/admin/config/import", json={
        "settings": {**payload["settings"], "cle.inconnue": 42}})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert "review.alert_four_eyes_required" in body["applied"]
    assert body["skipped"] == ["cle.inconnue"]
    assert "review.alert_four_eyes_required" in body["changed"]
    settings_after = client.get("/api/settings/ingestion").json()
    assert settings_after["alert_four_eyes_required"] is False
    db = next(get_db())
    try:
        trace = db.query(AdminAuditLog).filter(
            AdminAuditLog.username == "admin_pp",
            AdminAuditLog.action == "SETTINGS_IMPORTED").first()
        assert trace is not None
        assert trace.after["review.alert_four_eyes_required"] is False
    finally:
        db.close()

    # Fichier sans aucune cle connue -> 400 ; vide -> 400
    assert client.post("/api/admin/config/import",
                       json={"settings": {"foo": 1}}).status_code == 400
    assert client.post("/api/admin/config/import", json={"settings": {}}).status_code == 400


def test_config_endpoints_require_admin(client):
    _override_user("test_pp_user", "user")
    assert client.get("/api/admin/config/export").status_code == 403
    assert client.post("/api/admin/config/import",
                       json={"settings": {"review.alert_four_eyes_required": True}}).status_code == 403

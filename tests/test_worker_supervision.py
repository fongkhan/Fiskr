"""
Supervision du demon travailleur (mode « worker ») :
- GET /api/worker/status expose l'etat (battement de coeur, file, mode) ;
- « down » = demon requis + battement perime + travaux en attente ;
- un battement frais => alive, jamais « down » ;
- POST /api/worker/restart : reserve a l'admin, 400 hors mode worker.

Le but est de rendre visible la panne qui laissait une synchronisation
QUEUED sans jamais demarrer (aucun demon pour la prendre).
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr import api as api_module
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Job, AppSetting
from fiskr.settings import set_setting

HEARTBEAT_KEY = "jobs.worker"


def _override_user(username: str, role: str = "admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _set_heartbeat(age_seconds):
    """Ecrit un battement de coeur du demon vieux de `age_seconds` (None efface)."""
    db = next(get_db())
    try:
        if age_seconds is None:
            db.query(AppSetting).filter(AppSetting.key == HEARTBEAT_KEY).delete()
            db.commit()
            return
        at = (datetime.utcnow() - timedelta(seconds=age_seconds)).isoformat() + "Z"
        set_setting(db, HEARTBEAT_KEY, {"pid": 4242, "host": "vm-test", "at": at})
        db.commit()
    finally:
        db.close()


def _make_queued_job():
    db = next(get_db())
    try:
        job = Job(token=f"test_worker_{uuid.uuid4().hex[:8]}", kind="sync",
                  label="Synchronisation test", params={}, status="QUEUED",
                  phase="QUEUED", created_by="système")
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _cleanup():
    db = next(get_db())
    try:
        db.query(Job).filter(Job.token.like("test_worker_%")).delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key.in_((HEARTBEAT_KEY, "jobs.worker_autostart"))).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin_worker", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup()


def test_status_shape(client):
    r = client.get("/api/worker/status")
    assert r.status_code == 200
    body = r.json()
    for key in ("mode", "required", "alive", "down", "queued", "running", "last_autostart"):
        assert key in body


def test_down_when_worker_mode_stale_heartbeat_and_queued(client, monkeypatch):
    monkeypatch.setenv("FISKR_JOBS_MODE", "worker")
    _set_heartbeat(age_seconds=600)   # perime (> 120 s)
    _make_queued_job()
    body = client.get("/api/worker/status").json()
    assert body["required"] is True
    assert body["alive"] is False
    assert body["queued"] >= 1
    assert body["down"] is True


def test_fresh_heartbeat_is_alive_and_never_down(client, monkeypatch):
    monkeypatch.setenv("FISKR_JOBS_MODE", "worker")
    _set_heartbeat(age_seconds=5)     # frais
    _make_queued_job()
    body = client.get("/api/worker/status").json()
    assert body["alive"] is True
    assert body["down"] is False


def test_not_down_without_worker_mode(client, monkeypatch):
    # En mode thread/eager il n'y a pas de demon : jamais « down ».
    monkeypatch.setenv("FISKR_JOBS_MODE", "eager")
    _set_heartbeat(age_seconds=None)
    _make_queued_job()
    body = client.get("/api/worker/status").json()
    assert body["required"] is False
    assert body["down"] is False


def test_restart_requires_admin(monkeypatch):
    monkeypatch.setenv("FISKR_JOBS_MODE", "worker")
    # Utilisateur simple : la garde admin doit refuser (403), sans rien lancer.
    called = {"n": 0}
    monkeypatch.setattr(api_module, "ensure_worker", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    _override_user("simple_user", "user")
    try:
        with TestClient(app) as c:
            called["n"] = 0  # ignore l'appel d'autostart du lifespan au demarrage
            r = c.post("/api/worker/restart")
        assert r.status_code == 403
        assert called["n"] == 0  # la garde admin bloque avant le corps
    finally:
        app.dependency_overrides.clear()
        _cleanup()


def test_restart_400_outside_worker_mode(client, monkeypatch):
    # Admin, mais mode eager : aucun demon a relancer -> 400, pas de spawn.
    monkeypatch.setenv("FISKR_JOBS_MODE", "eager")
    called = {"n": 0}
    monkeypatch.setattr(api_module, "ensure_worker", lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    r = client.post("/api/worker/restart")
    assert r.status_code == 400
    assert called["n"] == 0


def test_restart_admin_worker_mode_calls_ensure_worker(client, monkeypatch):
    monkeypatch.setenv("FISKR_JOBS_MODE", "worker")
    _set_heartbeat(age_seconds=600)
    called = {"n": 0}
    # Ne lance PAS de vrai subprocess pendant le test.
    monkeypatch.setattr(api_module, "ensure_worker", lambda *a, **k: called.__setitem__("n", called["n"] + 1) or False)
    r = client.post("/api/worker/restart")
    assert r.status_code == 200
    assert called["n"] == 1
    assert "mode" in r.json()

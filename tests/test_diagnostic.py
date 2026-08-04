"""
Tests du diagnostic a distance (GET /api/diagnostic/jobs) :
- payload complet : versions (API vs disque vs demon), jobs (compteurs,
  RUNNING avec fraicheur du battement, file, erreurs recentes, groupe
  serialise), worker + verrou, systeme, reglages ;
- acces : admin OK, auditor OK (la voie cle d'API), user simple refuse ;
- cle d'API de role auditor : authentification reelle par X-API-Key,
  lecture seule par construction (toute ecriture refusee) ;
- empreintes buildinfo : stables dans un meme processus, publiees par le
  battement de coeur du demon.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr import buildinfo
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Job, ApiKey, AppSetting


def _override_user(role: str, username: str = "diag_testeur"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(Job).filter(Job.token.like("diagtest:%")).delete(synchronize_session=False)
        for key in db.query(ApiKey).filter(ApiKey.name.like("test_diag_%")).all():
            db.delete(key)
        # Le test du battement de coeur ecrit le reglage partage jobs.worker :
        # on l'efface pour ne pas faire croire aux autres tests qu'un demon vit
        db.query(AppSetting).filter(AppSetting.key == "jobs.worker") \
          .delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _cleanup_db()
    _override_user("admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _seed_jobs():
    db = next(get_db())
    try:
        now = datetime.utcnow()
        db.add(Job(token="diagtest:err", kind="backtest", label="diag err",
                   status="ERROR", error="MemoryError: criblage interrompu",
                   attempts=2, started_at=now - timedelta(minutes=5),
                   finished_at=now))
        db.add(Job(token="diagtest:q", kind="noop", label="diag en file",
                   status="QUEUED", priority=5))
        db.add(Job(token="diagtest:run", kind="backtest", label="diag en cours",
                   status="RUNNING", started_at=now, heartbeat_at=now,
                   claimed_by="hote-prod:1234"))
        db.commit()
    finally:
        db.close()


def test_diagnostic_payload_complete(client):
    _seed_jobs()
    response = client.get("/api/diagnostic/jobs")
    assert response.status_code == 200, response.text
    data = response.json()
    assert set(data) >= {"now", "versions", "worker", "jobs", "progress_active",
                         "system", "config", "worker_log_tail"}

    # Versions : dans les tests, API et disque sont le meme processus/code
    versions = data["versions"]
    assert versions["api"]["loaded"] == versions["disk"]
    assert versions["api"]["outdated"] is False
    assert len(versions["disk"]) == 12
    # Le demon n'a jamais battu ici : version inconnue, verdict indecidable
    assert "worker" in versions and "outdated" in versions["worker"]

    jobs = data["jobs"]
    assert jobs["counts"]["QUEUED"] >= 1 and jobs["counts"]["ERROR"] >= 1
    assert jobs["queued_by_kind"].get("noop", 0) >= 1

    running = [j for j in jobs["running"] if j["token"] == "diagtest:run"]
    assert running, jobs["running"]
    assert running[0]["claimed_by"] == "hote-prod:1234"
    assert running[0]["heartbeat_stale"] is False
    assert running[0]["heartbeat_age_s"] is not None

    errors = [j for j in jobs["recent_errors"] if j["token"] == "diagtest:err"]
    assert errors and "MemoryError" in errors[0]["error"]

    queued = [j for j in jobs["queued"] if j["token"] == "diagtest:q"]
    assert queued and queued[0]["queued_for_s"] is not None

    # Le backtest RUNNING au battement frais tient le groupe serialise
    serial = jobs["serial"]
    assert serial["busy"] is True and serial["holder"] is not None
    assert "backtest" in serial["kinds"] and serial["stale_after_s"] == 90.0

    assert data["config"]["jobs_mode"] in ("worker", "thread", "eager")
    assert data["config"]["slots"] >= 1
    assert isinstance(data["worker_log_tail"], list)
    assert "lock" in data["worker"]


def test_diagnostic_role_gate(client):
    _override_user("user")
    assert client.get("/api/diagnostic/jobs").status_code == 403
    _override_user("reviewer")
    assert client.get("/api/diagnostic/jobs").status_code == 403
    # Auditeur : la voie prevue pour le diagnostic exterieur
    _override_user("auditor")
    assert client.get("/api/diagnostic/jobs").status_code == 200


def test_diagnostic_via_api_key_auditor(client):
    created = client.post("/api/apikeys", json={
        "name": f"test_diag_{uuid.uuid4().hex[:6]}", "role": "auditor"})
    assert created.status_code == 200, created.text
    full_key = created.json()["api_key"]

    saved = app.dependency_overrides.pop(get_current_user)
    try:
        ok = client.get("/api/diagnostic/jobs", headers={"X-API-Key": full_key})
        assert ok.status_code == 200, ok.text
        assert ok.json()["versions"]["disk"]
        # La meme cle ne peut RIEN modifier : lecture seule par construction
        denied = client.post("/api/worker/restart", headers={"X-API-Key": full_key})
        assert denied.status_code == 403
        assert "lecture seule" in denied.json()["detail"]
    finally:
        app.dependency_overrides[get_current_user] = saved


def test_buildinfo_fingerprints():
    first = buildinfo.source_fingerprint()
    assert first == buildinfo.source_fingerprint() and len(first) == 12
    # Rien n'a change sur le disque depuis l'import du module
    assert buildinfo.LOADED_FINGERPRINT == first
    head = buildinfo.git_head()
    assert head is None or len(head) == 12


def test_worker_heartbeat_carries_version():
    from fiskr import worker
    from fiskr.settings import get_setting
    db = next(get_db())
    try:
        worker._write_heartbeat(db)
        db.commit()
        heartbeat = get_setting(db, worker.HEARTBEAT_SETTING, None)
        assert heartbeat["version"] == buildinfo.LOADED_FINGERPRINT
        assert heartbeat["python"] and heartbeat["started_at"]
    finally:
        db.close()
    # Et le diagnostic sait alors trancher : demon a jour
    client = TestClient(app)
    _override_user("admin")
    try:
        with client:
            versions = client.get("/api/diagnostic/jobs").json()["versions"]
        assert versions["worker"]["loaded"] == buildinfo.LOADED_FINGERPRINT
        assert versions["worker"]["outdated"] is False
    finally:
        app.dependency_overrides.clear()
        _cleanup_db()

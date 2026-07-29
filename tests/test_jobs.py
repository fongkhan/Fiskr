"""
Tests de la file de travaux persistee (fiskr.jobs) : depot, claim atomique,
reprise apres redemarrage (battement de coeur), exclusivite (dedupe), relance
et purge. C'est le socle de l'architecture a demon travailleur : chaque
garantie testee ici est une garantie d'exploitation.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr import jobs as job_queue
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import Job, get_db


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "role": "admin", "roles": ["admin"]
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup()


def _cleanup():
    db = next(get_db())
    try:
        db.query(Job).filter(Job.kind.like("test_jq_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    for kind in [k for k in job_queue.TASKS if k.startswith("test_jq_")]:
        job_queue.TASKS.pop(kind, None)


def _noop_task(kind="test_jq_noop"):
    if kind not in job_queue.TASKS:
        @job_queue.task(kind)
        def _noop(ctx, **params):
            ctx.set_result({"ok": True, **params})
    return kind


def _insert(db, *, kind, status="QUEUED", token=None, heartbeat_at=None,
            attempts=0, max_attempts=2, dedupe_key=None, created_at=None):
    job = Job(token=token or f"{kind}-{uuid.uuid4().hex[:6]}", kind=kind,
              status=status, heartbeat_at=heartbeat_at, attempts=attempts,
              max_attempts=max_attempts, dedupe_key=dedupe_key,
              created_at=created_at or datetime.utcnow())
    db.add(job)
    db.commit()
    return job


# ------------------ DEPOT ET EXECUTION ------------------

def test_submit_eager_runs_inline_and_persists_result(client):
    kind = _noop_task()
    job = job_queue.submit(kind, params={"x": 1}, label="Job de test")
    assert job.status == "DONE"
    assert job.result == {"ok": True, "x": 1}
    # Relisible par le canal public, meme sans registre memoire
    from fiskr import progress as progress_registry
    progress_registry.clear()
    state = client.get(f"/api/progress?id={job.token}").json()
    assert state["status"] == "DONE"
    assert state["result"] == {"ok": True, "x": 1}


def test_dedupe_key_refuses_concurrent_equivalent(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        _insert(db, kind=kind, status="RUNNING", dedupe_key="test_jq:excl",
                heartbeat_at=datetime.utcnow())
    finally:
        db.close()
    with pytest.raises(job_queue.JobConflict):
        job_queue.submit(kind, params={}, dedupe_key="test_jq:excl")


# ------------------ CLAIM ATOMIQUE ------------------

def test_claim_is_exclusive_between_two_workers(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        job = _insert(db, kind=kind, status="QUEUED")
        from fiskr.database import SessionLocal
        s1, s2 = SessionLocal(), SessionLocal()
        try:
            got1 = job_queue.claim_next(s1, "worker-a")
            got2 = job_queue.claim_next(s2, "worker-b")
        finally:
            s1.close(); s2.close()
        assert (got1 == job.id) != (got2 == job.id), "un seul des deux doit gagner"
        db.expire_all()
        row = db.get(Job, job.id)
        assert row.status == "RUNNING"
        assert row.attempts == 1
        assert row.claimed_by in ("worker-a", "worker-b")
    finally:
        db.close()


def test_claim_respects_backoff_not_before(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        job = _insert(db, kind=kind, status="QUEUED")
        job.not_before = datetime.utcnow() + timedelta(minutes=5)
        db.commit()
        assert job_queue.claim_next(db, "worker-a") is None
    finally:
        db.close()


# ------------------ REPRISE APRES REDEMARRAGE ------------------

def test_stale_running_is_requeued_when_worker_present(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        job = _insert(db, kind=kind, status="RUNNING", attempts=1,
                      heartbeat_at=datetime.utcnow() - timedelta(minutes=10))
        job_queue.requeue_stale(db, worker_present=True)
        db.refresh(job)
        assert job.status == "QUEUED"
        assert job.not_before is not None  # backoff lineaire
    finally:
        db.close()


def test_stale_running_beyond_max_attempts_becomes_error(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        job = _insert(db, kind=kind, status="RUNNING", attempts=2, max_attempts=2,
                      heartbeat_at=datetime.utcnow() - timedelta(minutes=10))
        job_queue.requeue_stale(db, worker_present=True)
        db.refresh(job)
        assert job.status == "ERROR"
        assert "redémarrage" in (job.error or "")
    finally:
        db.close()


def test_without_worker_orphans_become_error_not_queued(client):
    """Sans demon, une remise en file serait un mensonge : personne ne prendra
    jamais le job. Les orphelins (RUNNING perime ET QUEUED abandonnes) passent
    en ERROR, relancables a la main."""
    kind = _noop_task()
    db = next(get_db())
    try:
        stale_run = _insert(db, kind=kind, status="RUNNING", attempts=1,
                            heartbeat_at=datetime.utcnow() - timedelta(minutes=10))
        old_queued = _insert(db, kind=kind, status="QUEUED",
                             created_at=datetime.utcnow() - timedelta(minutes=10))
        job_queue.requeue_stale(db, worker_present=False)
        db.refresh(stale_run); db.refresh(old_queued)
        assert stale_run.status == "ERROR"
        assert old_queued.status == "ERROR"
    finally:
        db.close()


def test_live_running_job_is_left_alone(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        job = _insert(db, kind=kind, status="RUNNING", attempts=1,
                      heartbeat_at=datetime.utcnow())
        job_queue.requeue_stale(db, worker_present=True)
        db.refresh(job)
        assert job.status == "RUNNING"
    finally:
        db.close()


# ------------------ ENDPOINTS ------------------

def test_retry_endpoint_requeues_error_and_runs_it(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        job = _insert(db, kind=kind, status="ERROR", attempts=2)
        job.error = "panne d'origine"
        db.commit()
        job_id = job.id
    finally:
        db.close()
    response = client.post(f"/api/jobs/{job_id}/retry")
    assert response.status_code == 200, response.text
    db = next(get_db())
    try:
        row = db.get(Job, job_id)
        # En mode eager, la relance s'execute inline : le job est deja fini
        assert row.status == "DONE"
        assert row.result == {"ok": True}
    finally:
        db.close()


def test_retry_refuses_non_error_and_unknown_kind(client):
    db = next(get_db())
    try:
        done = _insert(db, kind=_noop_task(), status="DONE")
        alien = _insert(db, kind="test_jq_inconnu", status="ERROR")
        done_id, alien_id = done.id, alien.id
    finally:
        db.close()
    assert client.post(f"/api/jobs/{done_id}/retry").status_code == 400
    assert client.post(f"/api/jobs/{alien_id}/retry").status_code == 400


def test_cancel_only_queued(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        queued = _insert(db, kind=kind, status="QUEUED")
        running = _insert(db, kind=kind, status="RUNNING", heartbeat_at=datetime.utcnow())
        queued_id, running_id = queued.id, running.id
    finally:
        db.close()
    assert client.post(f"/api/jobs/{queued_id}/cancel").status_code == 200
    assert client.post(f"/api/jobs/{running_id}/cancel").status_code == 400
    db = next(get_db())
    try:
        assert db.get(Job, queued_id).status == "CANCELLED"
    finally:
        db.close()


def test_jobs_listing_reports_retryability(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        _insert(db, kind=kind, status="ERROR", token="test_jq_listing")
    finally:
        db.close()
    items = client.get("/api/jobs?status=ERROR&limit=50").json()["items"]
    row = next(i for i in items if i["token"] == "test_jq_listing")
    assert row["retryable"] is True


# ------------------ PURGE ------------------

def test_purge_removes_only_old_finished_jobs(client):
    kind = _noop_task()
    db = next(get_db())
    try:
        old_done = _insert(db, kind=kind, status="DONE",
                           created_at=datetime.utcnow() - timedelta(days=60))
        fresh_done = _insert(db, kind=kind, status="DONE")
        old_queued = _insert(db, kind=kind, status="QUEUED",
                             created_at=datetime.utcnow() - timedelta(days=60))
        ids = (old_done.id, fresh_done.id, old_queued.id)
        job_queue.purge_old(db, keep_days=30)
        assert db.get(Job, ids[0]) is None
        assert db.get(Job, ids[1]) is not None
        assert db.get(Job, ids[2]) is not None  # jamais purger une file vivante
    finally:
        db.close()

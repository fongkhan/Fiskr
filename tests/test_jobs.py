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


# ------------------ ANNULATION (retour a l'etat precedent) ------------------

def test_cancel_queued_job_never_runs(client):
    """Un job QUEUED s'annule : CANCELLED, trace au journal admin, et la
    tache ne s'execute JAMAIS (retour exact a l'etat d'avant soumission)."""
    from fiskr.database import AdminAuditLog
    kind = _noop_task("test_jq_cancel")
    db = next(get_db())
    try:
        job = _insert(db, kind=kind, status="QUEUED")
        job_id, token = job.id, job.token
    finally:
        db.close()

    response = client.post(f"/api/jobs/{job_id}/cancel")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "CANCELLED"

    db = next(get_db())
    try:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "CANCELLED"
        assert refreshed.finished_at is not None
        assert refreshed.result is None  # rien n'a ete execute
        logged = db.query(AdminAuditLog).filter(
            AdminAuditLog.action == "JOB_CANCELLED",
            AdminAuditLog.target == f"{kind}:{token}").first()
        assert logged is not None
        db.query(AdminAuditLog).filter(AdminAuditLog.id == (logged.id if logged else -1)).delete()
        db.commit()
    finally:
        db.close()

    # run_job sur un job annule : la garde refuse de l'executer
    job_queue.run_job(job_id)
    db = next(get_db())
    try:
        refreshed = db.get(Job, job_id)
        assert refreshed.status == "CANCELLED"
        assert refreshed.result is None
    finally:
        db.close()


def test_cancel_refuses_running_and_unknown(client):
    kind = _noop_task("test_jq_cancel_run")
    db = next(get_db())
    try:
        running = _insert(db, kind=kind, status="RUNNING",
                          heartbeat_at=datetime.utcnow())
        running_id = running.id
    finally:
        db.close()
    assert client.post(f"/api/jobs/{running_id}/cancel").status_code == 400
    assert client.post("/api/jobs/99999999/cancel").status_code == 404


def test_active_operations_expose_cancellable(client):
    """La liste des operations actives porte job_id + cancellable sur les
    QUEUED : le panneau peut afficher le bouton d'annulation."""
    kind = _noop_task("test_jq_cancel_view")
    db = next(get_db())
    try:
        queued = _insert(db, kind=kind, status="QUEUED")
        token = queued.token
    finally:
        db.close()
    items = client.get("/api/progress/active").json()["items"]
    row = next(i for i in items if i.get("token") == token)
    assert row["cancellable"] is True and row["job_id"]


# ------------------ ZOMBIES : la serialisation ne bloque plus ------------------

def test_stale_serial_running_does_not_block_kind(client):
    """Un backtest laisse RUNNING par un demon mort (coeur perime) ne doit
    plus bloquer la serialisation : le suivant se prend normalement.
    C'etait l'arret complet vu en production — zombies RUNNING eternels
    (requeue_stale ne tournait qu'au demarrage) + genre serialise bloque."""
    db = next(get_db())
    try:
        # Zombie : RUNNING avec un battement perime, du genre serialise
        zombie = _insert(db, kind="backtest", status="RUNNING",
                         heartbeat_at=datetime.utcnow() - timedelta(minutes=10))
        zombie.token = f"test_jq_zmb-{uuid.uuid4().hex[:6]}"
        queued = _insert(db, kind="backtest", status="QUEUED",
                         token=f"test_jq_zmb-{uuid.uuid4().hex[:6]}")
        queued.priority = 1  # pris avant tout job residuel d'un autre test
        db.commit()
        zombie_id, queued_id = zombie.id, queued.id

        # _serial_kind_busy : le zombie ne compte pas comme occupe
        assert job_queue._serial_kind_busy(db, "backtest") is False

        # claim_next : le QUEUED du meme genre est pris malgre le zombie
        claimed = job_queue.claim_next(db, "test-claimer")
        assert claimed == queued_id

        # Un backtest au coeur FRAIS, lui, bloque toujours (garde-fou RAM)
        db.execute(__import__("sqlalchemy").update(Job).where(Job.id == queued_id)
                   .values(heartbeat_at=datetime.utcnow()))
        db.commit()
        assert job_queue._serial_kind_busy(db, "backtest") is True
    finally:
        db.query(Job).filter(Job.id.in_([zombie_id, queued_id])).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_serial_group_is_exclusive_across_kinds(client):
    """Le groupe serialise est exclusif dans son ENSEMBLE : une simulation
    moteur en cours (coeur frais) bloque aussi la prise d'un cahier de tests
    — jamais deux univers de listes en memoire, quel que soit le melange."""
    noop_kind = _noop_task("test_jq_grp")
    db = next(get_db())
    created = []
    try:
        running_sim = _insert(db, kind="engine_simulation", status="RUNNING",
                              heartbeat_at=datetime.utcnow(),
                              token=f"test_jq_grp-{uuid.uuid4().hex[:6]}")
        queued_bt = _insert(db, kind="backtest", status="QUEUED",
                            token=f"test_jq_grp-{uuid.uuid4().hex[:6]}")
        queued_bt.priority = 1  # premier candidat s'il n'etait pas bloque
        other = _insert(db, kind=noop_kind, status="QUEUED")
        other.priority = 2
        db.commit()
        created = [running_sim.id, queued_bt.id, other.id]

        # Occupation croisee : le backtest attend la simulation en cours
        assert job_queue._serial_kind_busy(db, "backtest") is True
        # claim_next saute le groupe entier et sert le job ordinaire
        claimed = job_queue.claim_next(db, "test-claimer")
        assert claimed == other.id
    finally:
        db.query(Job).filter(Job.id.in_(created)).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_log_admin_action_importable_from_database():
    """La fouille d'homonymes (tasks.mining_task) importe log_admin_action
    depuis fiskr.database : quand la fonction ne vivait que dans fiskr.api,
    la tâche plantait chaque nuit en production sur l'ImportError."""
    from fiskr.database import log_admin_action, AdminAuditLog, get_db
    db = next(get_db())
    try:
        log_admin_action(db, "test_jobs", "TEST_IMPORT_MINING", target="resources")
        db.commit()
        row = db.query(AdminAuditLog) \
                .filter(AdminAuditLog.action == "TEST_IMPORT_MINING").first()
        assert row is not None and row.username == "test_jobs"
        db.delete(row)  # append-only en prod ; on nettoie le bac à sable
        db.commit()
    finally:
        db.close()

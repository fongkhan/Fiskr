"""
Examen d'homologation accelere apres une synchronisation :

- le delta memorise par la sync (SyncReport.delta_report) est servi TEL QUEL
  a l'ecran d'examen quand sa base de comparaison est toujours la production
  courante — affichage instantane, aucune fiche chargee, aucun recalcul ;
- si la production a change depuis (ou pour un import manuel sans SyncReport),
  le delta est recalcule pour rester exact ;
- une sync retenue en homologation avec un delta non nul soumet le cahier de
  tests automatiquement (reglage review.auto_backtest_enabled, panel force ou
  dernier panel de pseudo-clients genere), pour que le reviseur n'ait plus
  qu'a decider.
"""
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from fiskr import jobs
from fiskr import tasks as tasks_module
from fiskr import api as api_module
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, SyncReport, AppSetting
from fiskr.settings import (
    set_setting, SETTING_AUTO_BACKTEST_ENABLED, SETTING_AUTO_BACKTEST_PANEL,
)
from fiskr.tasks import _maybe_auto_backtest, _resolve_auto_backtest_panel


def _override_user(username: str, role: str = "admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _snap(db, *, file_type="WATCHLIST_SECO", status="READY", record_count=3,
          panel=False):
    snap = Snapshot(
        snapshot_id=f"test_fastpath_{uuid.uuid4().hex[:10]}",
        file_type="CLIENT_TEST_PANEL" if panel else file_type,
        file_name="fastpath_test.csv", file_hash=uuid.uuid4().hex,
        record_count=record_count, status=status,
    )
    db.add(snap)
    db.commit()
    return snap


STORED_DELTA = {
    "summary": {"added_count": 2, "modified_count": 1, "removed_count": 0,
                "unchanged_count": 7},
    "details": {"added": [{"id": "X-1", "type": "I", "primary_name": "AJOUT UN"}],
                "modified": [], "removed": []},
}


def _sync_report(db, *, snapshot_id, previous_snapshot_id, delta=STORED_DELTA):
    report = SyncReport(
        source="SECO", trigger="SCHEDULED", status="PENDING_REVIEW",
        message="test", snapshot_id=snapshot_id,
        previous_snapshot_id=previous_snapshot_id,
        added_count=delta["summary"]["added_count"],
        modified_count=delta["summary"]["modified_count"],
        removed_count=delta["summary"]["removed_count"],
        delta_report=delta,
    )
    db.add(report)
    db.commit()
    return report


def _cleanup():
    db = next(get_db())
    try:
        ids = [s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.snapshot_id.like("test_fastpath_%")).all()]
        if ids:
            db.query(SyncReport).filter(SyncReport.snapshot_id.in_(ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(ids)).delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key.in_(
            (SETTING_AUTO_BACKTEST_ENABLED, SETTING_AUTO_BACKTEST_PANEL))).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("reviewer_fp", "reviewer,user")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup()


@pytest.fixture
def db_session():
    db = next(get_db())
    yield db
    db.close()


# ------------------ DELTA INSTANTANE A L'EXAMEN ------------------

def test_stored_delta_served_without_recompute(client, db_session, monkeypatch):
    """Base de comparaison inchangee : le delta memorise est servi tel quel,
    et calculate_delta n'est JAMAIS appele (c'est ce qui rend l'ecran
    instantane sur les grandes listes)."""
    production = _snap(db_session, status="READY")
    pending = _snap(db_session, status="PENDING_REVIEW")
    _sync_report(db_session, snapshot_id=pending.snapshot_id,
                 previous_snapshot_id=production.snapshot_id)

    def _boom(*a, **k):
        raise AssertionError("calculate_delta ne doit pas etre appele sur le chemin memorise")
    monkeypatch.setattr(api_module, "calculate_delta", _boom)

    data = client.get(f"/api/review/snapshots/{pending.snapshot_id}").json()
    assert data["delta_source"] == "stored"
    assert data["delta_summary"] == STORED_DELTA["summary"]
    assert data["delta_details"]["details"]["added"][0]["id"] == "X-1"
    assert data["production_snapshot_id"] == production.snapshot_id


def test_stale_stored_delta_is_recomputed(client, db_session):
    """La production a change depuis la sync : le delta memorise mentirait,
    il est recalcule contre la production courante."""
    old_production = _snap(db_session, status="SUPERSEDED")
    _snap(db_session, status="READY")  # nouvelle production du meme type
    pending = _snap(db_session, status="PENDING_REVIEW", record_count=0)
    _sync_report(db_session, snapshot_id=pending.snapshot_id,
                 previous_snapshot_id=old_production.snapshot_id)

    data = client.get(f"/api/review/snapshots/{pending.snapshot_id}").json()
    assert data["delta_source"] == "computed"


def test_manual_import_delta_still_computed(client, db_session):
    """Un import manuel n'a pas de SyncReport : recalcul, comme avant."""
    pending = _snap(db_session, status="PENDING_REVIEW", record_count=0)
    data = client.get(f"/api/review/snapshots/{pending.snapshot_id}").json()
    assert data["delta_source"] == "computed"


def test_pending_list_carries_backtest_verdict(client, db_session):
    """La file d'attente montre le verdict du cahier de tests : le reviseur
    voit d'un coup d'oeil ce qui est pret a decider."""
    pending = _snap(db_session, status="PENDING_REVIEW")
    pending.backtest_report = {"verdict": "OK", "gap_pct": 1.5}
    db_session.commit()
    rows = client.get("/api/review/pending").json()["pending"]
    row = next(r for r in rows if r["snapshot_id"] == pending.snapshot_id)
    assert row["backtest_verdict"] == "OK"
    assert row["backtest_gap_pct"] == 1.5


# ------------------ CAHIER DE TESTS AUTOMATIQUE ------------------

def _fake_report(snapshot_id, *, status="PENDING_REVIEW", added=2, modified=0, removed=0):
    return SimpleNamespace(status=status, snapshot_id=snapshot_id, source="SECO",
                           added_count=added, modified_count=modified,
                           removed_count=removed)


def test_auto_backtest_submitted_with_latest_generated_panel(client, db_session, monkeypatch):
    panel = _snap(db_session, panel=True, record_count=50)
    pending = _snap(db_session, status="PENDING_REVIEW")

    submitted = {}
    def _spy(kind, **kwargs):
        submitted["kind"] = kind
        submitted.update(kwargs)
    monkeypatch.setattr(jobs, "submit", _spy)

    outcome = _maybe_auto_backtest(db_session, _fake_report(pending.snapshot_id))
    assert outcome["submitted"] is True
    assert outcome["panel_snapshot_id"] == panel.snapshot_id
    assert submitted["kind"] == "backtest"
    assert submitted["params"]["snapshot_id"] == pending.snapshot_id
    assert submitted["params"]["panel_snapshot_id"] == panel.snapshot_id
    # Meme dedupe que le lancement manuel : jamais deux cahiers en parallele
    assert submitted["dedupe_key"] == f"backtest:{pending.snapshot_id}"


def test_auto_backtest_prefers_configured_panel(client, db_session, monkeypatch):
    _snap(db_session, panel=True, record_count=50)          # plus recent, ignore
    forced = _snap(db_session, panel=True, record_count=10)  # impose par reglage
    set_setting(db_session, SETTING_AUTO_BACKTEST_PANEL, forced.snapshot_id)
    db_session.commit()
    assert _resolve_auto_backtest_panel(db_session) == forced.snapshot_id


def test_auto_backtest_skips_without_panel(client, db_session, monkeypatch):
    pending = _snap(db_session, status="PENDING_REVIEW")
    monkeypatch.setattr(jobs, "submit",
                        lambda *a, **k: pytest.fail("aucun job ne doit partir sans panel"))
    monkeypatch.setattr(tasks_module, "_resolve_auto_backtest_panel", lambda s: None)
    outcome = _maybe_auto_backtest(db_session, _fake_report(pending.snapshot_id))
    assert outcome["submitted"] is False
    assert "panel" in outcome["reason"]


def test_auto_backtest_respects_disable_setting(client, db_session, monkeypatch):
    pending = _snap(db_session, status="PENDING_REVIEW")
    set_setting(db_session, SETTING_AUTO_BACKTEST_ENABLED, False)
    db_session.commit()
    monkeypatch.setattr(jobs, "submit",
                        lambda *a, **k: pytest.fail("reglage coupe : aucun job"))
    outcome = _maybe_auto_backtest(db_session, _fake_report(pending.snapshot_id))
    assert outcome["submitted"] is False


def test_auto_backtest_skips_on_empty_delta_or_applied_sync(client, db_session, monkeypatch):
    pending = _snap(db_session, status="PENDING_REVIEW")
    monkeypatch.setattr(jobs, "submit", lambda *a, **k: pytest.fail("rien a soumettre"))
    # Delta vide : rien a valider de neuf
    outcome = _maybe_auto_backtest(
        db_session, _fake_report(pending.snapshot_id, added=0, modified=0, removed=0))
    assert outcome == {"submitted": False, "reason": "delta vide"}
    # Sync appliquee directement (pas d'homologation) : pas de cahier auto
    assert _maybe_auto_backtest(
        db_session, _fake_report(pending.snapshot_id, status="SUCCESS")) is None


# ------------------ REGLAGES ------------------

def test_settings_expose_and_validate_auto_backtest():
    _override_user("admin_fp", "admin")
    try:
        with TestClient(app) as c:
            data = c.get("/api/settings/ingestion").json()
            assert data["auto_backtest_enabled"] is True   # defaut
            assert data["auto_backtest_panel"] is None

            # Panel inconnu -> refus explicite
            bad = c.put("/api/settings/ingestion",
                        json={"auto_backtest_panel": "snapshot_inexistant"})
            assert bad.status_code == 400

            # Coupure + retour a l'etat par defaut du panel ("" = efface)
            ok = c.put("/api/settings/ingestion",
                       json={"auto_backtest_enabled": False, "auto_backtest_panel": ""})
            assert ok.status_code == 200
            data = c.get("/api/settings/ingestion").json()
            assert data["auto_backtest_enabled"] is False
            assert data["auto_backtest_panel"] is None
    finally:
        app.dependency_overrides.clear()
        _cleanup()

"""
Tests de la visibilité des opérations de fond :
- registre de progression (identité des opérations, `list_active`, fenêtre des
  terminées, rétro-compatibilité de `update`) ;
- `GET /api/progress/active` : agrégat du registre et des campagnes batch, sans
  fuite de donnée métier, garde d'authentification ;
- cahier de tests et approbation d'homologation en **asynchrone** : refus
  synchrones conservés (400), 202 + jeton, travail effectué en tâche de fond,
  progression publiée pendant l'exécution.
"""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr import progress as progress_registry
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, Snapshot, WatchlistEntity, ClientEntity, BatchCampaign, AppSetting,
)
from fiskr.settings import (
    SETTING_REQUIRE_APPROVAL, SETTING_BACKTEST_REQUIRED,
    SETTING_BACKTEST_MAX_GAP_PCT,
)
from tests.conftest import wait_for_job, post_and_wait

SETTING_KEYS = [SETTING_REQUIRE_APPROVAL, SETTING_BACKTEST_REQUIRED,
                SETTING_BACKTEST_MAX_GAP_PCT]


def _override_user(role: str, username: str = "testeur"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_(SETTING_KEYS)).delete(synchronize_session=False)
        snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_ops_%")).all()
        ids = [s.snapshot_id for s in snaps]
        if ids:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
            db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(ids)).delete(synchronize_session=False)
        db.query(BatchCampaign).filter(BatchCampaign.name.like("test_ops_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    progress_registry.clear()
    _cleanup_db()
    _override_user("admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    progress_registry.clear()
    _cleanup_db()


# ------------------ REGISTRE DE PROGRESSION ------------------

def test_registry_keeps_operation_identity():
    progress_registry.clear()
    progress_registry.update("job-1", phase="PARSE", kind="import",
                             label="Import WATCHLIST_EU", started_by="alice")
    # Les ticks suivants n'ont pas à répéter l'identité : elle est conservée
    progress_registry.update("job-1", phase="PERSIST", processed=500, total=1000)

    state = progress_registry.get("job-1")
    assert state["kind"] == "import"
    assert state["label"] == "Import WATCHLIST_EU"
    assert state["started_by"] == "alice"
    assert state["pct"] == 50.0
    assert state["started_at"] is not None


def test_registry_update_is_backward_compatible():
    """Les appels historiques (sans kind/label/started_by) restent valides."""
    progress_registry.clear()
    progress_registry.update("job-legacy", phase="UPLOAD", processed=10)
    state = progress_registry.get("job-legacy")
    assert state["phase"] == "UPLOAD"
    assert state["kind"] is None and state["label"] is None
    assert state["pct"] is None  # aucun total connu : pas de pourcentage inventé


def test_list_active_orders_and_keeps_finished_briefly():
    progress_registry.clear()
    progress_registry.update("job-a", phase="PARSE", kind="import", label="A")
    time.sleep(0.02)
    progress_registry.update("job-b", phase="PARSE", kind="sync", label="B")

    tokens = [item["token"] for item in progress_registry.list_active()]
    assert tokens == ["job-a", "job-b"]  # la plus ancienne d'abord

    # Une opération terminée reste listée le temps que le front voie la fin…
    progress_registry.finish("job-a")
    listed = {i["token"]: i for i in progress_registry.list_active()}
    assert listed["job-a"]["status"] == "DONE"

    # …puis disparaît une fois la fenêtre écoulée
    assert "job-a" not in {i["token"] for i in progress_registry.list_active(finished_window=0)}
    assert "job-b" in {i["token"] for i in progress_registry.list_active(finished_window=0)}


def test_list_active_reports_errors():
    progress_registry.clear()
    progress_registry.update("job-ko", phase="PERSIST", kind="import", label="KO")
    progress_registry.finish("job-ko", status="ERROR", error="disque plein")
    item = progress_registry.list_active()[0]
    assert item["status"] == "ERROR"
    assert item["error"] == "disque plein"


# ------------------ GET /api/progress/active ------------------

def test_active_endpoint_empty_then_populated(client):
    empty = client.get("/api/progress/active")
    assert empty.status_code == 200, empty.text
    assert empty.json() == {"items": [], "running": 0}

    progress_registry.update("test-ops-run", phase="PERSIST", kind="import",
                             label="Import de test", started_by="alice",
                             processed=25, total=100)
    data = client.get("/api/progress/active").json()
    assert data["running"] == 1
    item = data["items"][0]
    assert item["token"] == "test-ops-run"
    assert item["kind"] == "import"
    assert item["label"] == "Import de test"
    assert item["started_by"] == "alice"
    assert item["pct"] == 25.0
    # Lien profond vers l'écran concerné, pour rendre la ligne cliquable
    assert item["link"] == "#watchlist-mgmt/watchlist-snapshots"


def test_active_endpoint_merges_running_batch_campaigns(client):
    db = next(get_db())
    try:
        campaign = BatchCampaign(name=f"test_ops_campagne_{uuid.uuid4().hex[:6]}",
                                 trigger="manual", status="RUNNING",
                                 total_clients=200, processed_clients=50,
                                 created_by="bob")
        db.add(campaign)
        db.commit()
        campaign_id = campaign.id
    finally:
        db.close()

    items = client.get("/api/progress/active").json()["items"]
    batch = next(i for i in items if i["token"] == f"batch:{campaign_id}")
    assert batch["kind"] == "batch"
    assert batch["pct"] == 25.0
    assert batch["started_by"] == "bob"
    assert batch["link"] == "#batch"


def test_active_endpoint_exposes_no_business_data(client):
    progress_registry.update("test-ops-priv", phase="PERSIST", kind="backtest",
                             label="Cahier de tests — WATCHLIST_EU",
                             snapshot_id="snap-123", processed=1, total=2)
    item = client.get("/api/progress/active").json()["items"][0]
    # Libellés et compteurs uniquement : aucune charge utile métier
    assert set(item) == {
        "token", "kind", "label", "started_by", "phase", "processed", "total",
        "pct", "snapshot_id", "status", "error", "started_at", "updated_at", "link",
    }


def test_active_endpoint_requires_authentication(client):
    app.dependency_overrides.clear()
    try:
        assert client.get("/api/progress/active").status_code == 401
    finally:
        _override_user("admin")


# ------------------ CAHIER DE TESTS ASYNCHRONE ------------------

def _upload_watchlist(client, rows, require_approval):
    assert client.put("/api/settings/ingestion",
                      json={"require_approval": require_approval}).status_code == 200
    body = "entity_id,entity_type,primary_name,nationality,dob\n" + "\n".join(
        f"{eid},I,{name},RU,{dob}" for eid, name, dob in rows) + "\n"
    response = client.post(
        "/api/ingest", data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_ops_{uuid.uuid4().hex[:8]}.csv", body, "text/csv")})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def ab_setup(client):
    """Production : Boris. Candidat en attente : Boris + Igor. Panel : 2 fiches."""
    tag = uuid.uuid4().hex[:6].upper()
    boris = (f"OPS-{tag}-1", f"Boris Opsov{tag}", "1960-05-05")
    igor = (f"OPS-{tag}-2", f"Igor Opsnouveau{tag}", "1971-02-02")
    _upload_watchlist(client, [boris], require_approval=False)
    pending = _upload_watchlist(client, [boris, igor], require_approval=True)
    csv = ("client_id,client_type,client_first_name,client_last_name,client_dob,"
           "client_gender,nationality\n"
           f"OPSCLI-{tag}-1,PP,Igor,Opsnouveau{tag},1971-02-02,M,RU\n"
           f"OPSCLI-{tag}-2,PP,Paul,Opstranquille{tag},1985-09-09,M,RU\n")
    panel = client.post("/api/ingest", data={"file_type": "CLIENT_BASE"},
                        files={"file": (f"test_ops_cb_{uuid.uuid4().hex[:6]}.csv", csv, "text/csv")})
    assert panel.status_code == 200, panel.text
    return {"tag": tag, "pending_id": pending["snapshot_id"],
            "panel_id": panel.json()["snapshot_id"]}


def test_backtest_returns_202_and_persists_report(client, ab_setup):
    response = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
                           json={"panel_snapshot_id": ab_setup["panel_id"]})
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["job_token"] == f"backtest:{ab_setup['pending_id']}"
    assert payload["panel_size"] == 2
    # Le rapport n'est PAS dans la réponse : il arrive avec le job
    assert "verdict" not in payload

    state = wait_for_job(client, payload["job_token"])
    assert state["status"] == "DONE", state

    detail = client.get(f"/api/review/snapshots/{ab_setup['pending_id']}").json()
    assert detail["backtest_report"]["verdict"] in ("OK", "WARN")
    assert detail["backtest_by"] == "testeur"


def test_backtest_job_is_visible_in_active_operations(client, ab_setup):
    response = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
                           json={"panel_snapshot_id": ab_setup["panel_id"]})
    token = response.json()["job_token"]
    wait_for_job(client, token)

    # Le job reste listé (fenêtre des terminées) avec son identité complète
    item = next(i for i in client.get("/api/progress/active").json()["items"]
                if i["token"] == token)
    assert item["kind"] == "backtest"
    assert "Cahier de tests" in item["label"]
    assert item["started_by"] == "testeur"
    assert item["link"] == "#watchlist-mgmt/watchlist-review"


def test_backtest_publishes_progress_phases(client, ab_setup, monkeypatch):
    """Les deux passes A/B publient leur avancement : la barre progresse sur
    toute la durée du cahier de tests, pas seulement sur sa seconde moitié."""
    monkeypatch.setattr("fiskr.backtest._PROGRESS_EVERY", 1)
    seen = []
    real_update = progress_registry.update

    def _spy(token, **kwargs):
        if token and str(token).startswith("backtest:"):
            seen.append(kwargs.get("phase"))
        return real_update(token, **kwargs)

    monkeypatch.setattr(api.progress_registry, "update", _spy)
    response = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
                           json={"panel_snapshot_id": ab_setup["panel_id"]})
    wait_for_job(client, response.json()["job_token"])
    assert "SCREEN_CURRENT" in seen
    assert "SCREEN_CANDIDATE" in seen


def test_backtest_refusals_stay_synchronous(client, ab_setup):
    """Une demande invalide est refusée AVANT de lancer quoi que ce soit."""
    bad_panel = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
                            json={"panel_snapshot_id": "inexistant"})
    assert bad_panel.status_code == 400
    assert "job_token" not in bad_panel.json()

    bad_rule = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
                           json={"panel_snapshot_id": ab_setup["panel_id"],
                                 "candidate_rule_id": 99999999})
    assert bad_rule.status_code == 400
    assert "candidate" in bad_rule.json()["detail"].lower() \
        or "règle" in bad_rule.json()["detail"].lower()

    # Aucun job de cahier de tests n'a démarré
    tokens = [i["token"] for i in client.get("/api/progress/active").json()["items"]]
    assert f"backtest:{ab_setup['pending_id']}" not in tokens


# ------------------ APPROBATION ASYNCHRONE ------------------

def test_approval_promotes_synchronously_then_rescreens_in_background(client, ab_setup):
    response = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/approve",
                           json={"comment": "test_ops approbation"})
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["job_token"] == f"approve:{ab_setup['pending_id']}"
    assert payload["status"] == "READY"

    # La promotion est DÉJÀ actée quand la requête répond : acte de gouvernance
    db = next(get_db())
    try:
        snap = db.query(Snapshot).filter(
            Snapshot.snapshot_id == ab_setup["pending_id"]).first()
        assert snap.status == "READY"
        assert snap.reviewed_by == "testeur"
    finally:
        db.close()

    state = wait_for_job(client, payload["job_token"])
    assert state["status"] == "DONE", state

    item = next(i for i in client.get("/api/progress/active").json()["items"]
                if i["token"] == payload["job_token"])
    assert item["kind"] == "approve"
    assert "Mise en production" in item["label"]


def test_approval_refusals_stay_synchronous(client, ab_setup):
    """Le cahier de tests obligatoire bloque toujours en 400 immédiat, et le
    snapshot n'est pas promu."""
    assert client.put("/api/settings/ingestion",
                      json={"backtest_required": True}).status_code == 200
    response = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/approve",
                           json={"comment": "x"})
    assert response.status_code == 400
    assert "cahier de tests" in response.json()["detail"].lower()

    db = next(get_db())
    try:
        snap = db.query(Snapshot).filter(
            Snapshot.snapshot_id == ab_setup["pending_id"]).first()
        assert snap.status == "PENDING_REVIEW"  # rien n'a bougé
    finally:
        db.close()
    tokens = [i["token"] for i in client.get("/api/progress/active").json()["items"]]
    assert f"approve:{ab_setup['pending_id']}" not in tokens


def test_failing_job_is_reported_not_swallowed(client):
    """Un job qui casse est marqué ERROR dans le registre — jamais silencieux."""
    def _boom(job_token):
        raise RuntimeError("panne simulée")

    api._start_job("test-ops-boom", "import", "Job qui casse", _boom,
                   started_by="testeur")
    state = wait_for_job(client, "test-ops-boom", timeout=10)
    assert state["status"] == "ERROR"
    assert "panne simulée" in state["error"]

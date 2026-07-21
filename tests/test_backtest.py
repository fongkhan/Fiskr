"""
Tests du parcours de production de listes :
- cahier de tests (backtest) A/B a blanc : ecart de taux d'interception entre
  la production et la liste candidate, nouvelles alertes, dry-run strict ;
- generateur de panels de pseudo-clients (CLIENT_TEST_PANEL) isoles du
  referentiel clients reel ;
- Good Guys en masse (POST /api/whitelist/bulk) et reglages de gouvernance
  (cahier de tests obligatoire, seuil d'ecart) ;
- non-regression : POST /api/snapshots/compare retourne bien le rapport.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.backtest import TEST_PANEL_FILE_TYPE
from fiskr.database import (
    get_db, Snapshot, WatchlistEntity, ClientEntity, WhitelistPair,
    Alert, AuditTrail, AppSetting,
)
from fiskr.rescreen import _client_dicts
from fiskr.settings import (
    SETTING_REQUIRE_APPROVAL, SETTING_EXCLUSION_JUSTIFICATION_REQUIRED,
    SETTING_EXCLUSION_FILE_REQUIRED, SETTING_BACKTEST_REQUIRED,
    SETTING_BACKTEST_MAX_GAP_PCT, SETTING_WHITELIST_JUSTIFICATION_REQUIRED,
)

ALL_SETTING_KEYS = [
    SETTING_REQUIRE_APPROVAL, SETTING_EXCLUSION_JUSTIFICATION_REQUIRED,
    SETTING_EXCLUSION_FILE_REQUIRED, SETTING_BACKTEST_REQUIRED,
    SETTING_BACKTEST_MAX_GAP_PCT, SETTING_WHITELIST_JUSTIFICATION_REQUIRED,
]

WL_JUSTIF_MARKER = "test_bt Good Guy (cahier de tests)"


def _override_user(role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "testeur", "full_name": "Testeur", "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_(ALL_SETTING_KEYS)).delete(synchronize_session=False)
        db.query(WhitelistPair).filter(WhitelistPair.justification == WL_JUSTIF_MARKER).delete(synchronize_session=False)
        # Snapshots watchlist et bases clients du test + tous les panels generes
        snaps = db.query(Snapshot).filter(
            (Snapshot.file_name.like("test_bt_%")) | (Snapshot.file_type == TEST_PANEL_FILE_TYPE)
        ).all()
        snap_ids = [s.snapshot_id for s in snaps]
        if snap_ids:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
            db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _upload_watchlist(client, rows, require_approval):
    """rows = [(entity_id, name, dob)] ; retourne la reponse d'ingestion."""
    assert client.put("/api/settings/ingestion", json={"require_approval": require_approval}).status_code == 200
    body = "entity_id,entity_type,primary_name,nationality,dob\n" + "\n".join(
        f"{eid},I,{name},RU,{dob}" for eid, name, dob in rows
    ) + "\n"
    response = client.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_bt_{uuid.uuid4().hex[:8]}.csv", body, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload_client_base(client, rows):
    """rows = [(client_id, first, last, dob)] ; base clients reelle (READY)."""
    header = "client_id,client_type,client_first_name,client_last_name,client_dob,client_gender,nationality\n"
    body = header + "\n".join(
        f"{cid},PP,{first},{last},{dob},M,RU" for cid, first, last, dob in rows
    ) + "\n"
    response = client.post(
        "/api/ingest",
        data={"file_type": "CLIENT_BASE"},
        files={"file": (f"test_bt_clients_{uuid.uuid4().hex[:8]}.csv", body, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _audit_counts(db):
    return db.query(Alert).count(), db.query(AuditTrail).count()


@pytest.fixture
def ab_setup(client):
    """
    Production : liste EU avec Boris. Candidat en attente : Boris + Igor (ajout).
    Panel : base clients avec un pseudo-client Igor (hit attendu sur la
    candidate seulement) et un client neutre.
    """
    tag = uuid.uuid4().hex[:6].upper()
    boris = (f"BT-{tag}-1", f"Boris Backtestov{tag}", "1960-05-05")
    igor = (f"BT-{tag}-2", f"Igor Nouveauov{tag}", "1971-02-02")
    _upload_watchlist(client, [boris], require_approval=False)
    pending = _upload_watchlist(client, [boris, igor], require_approval=True)
    assert pending["status"] == "PENDING_REVIEW"
    panel = _upload_client_base(client, [
        (f"CLI-{tag}-IGOR", "Igor", f"Nouveauov{tag}", "1971-02-02"),
        (f"CLI-{tag}-NEUTRE", "Paul", f"Tranquillov{tag}", "1985-09-09"),
    ])
    return {"tag": tag, "pending_id": pending["snapshot_id"], "panel_id": panel["snapshot_id"],
            "igor_entity_id": igor[0], "igor_client_id": f"CLI-{tag}-IGOR"}


# ------------------ BACKTEST A/B ------------------

def test_backtest_detects_gap_and_is_dry_run(client, ab_setup):
    db = next(get_db())
    try:
        alerts_before, audits_before = _audit_counts(db)
    finally:
        db.close()

    response = client.post(
        f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
        json={"panel_snapshot_id": ab_setup["panel_id"]},
    )
    assert response.status_code == 200, response.text
    report = response.json()

    # L'ajout d'Igor dans la candidate cree une nouvelle alerte sur le panel
    assert report["candidate"]["alerts"] == report["current"]["alerts"] + 1
    assert report["gap_pct"] > 0
    assert report["panel_size"] == 2
    pair_keys = [(p["client_id"], p["entity_id"]) for p in report["new_pairs"]]
    assert (ab_setup["igor_client_id"], ab_setup["igor_entity_id"]) in pair_keys
    new_pair = report["new_pairs"][pair_keys.index((ab_setup["igor_client_id"], ab_setup["igor_entity_id"]))]
    assert new_pair["list_type"] == "WATCHLIST_EU"
    assert new_pair["score"] >= 75

    # Dry-run strict : aucune alerte ni ligne d'audit ecrite
    db = next(get_db())
    try:
        assert _audit_counts(db) == (alerts_before, audits_before)
        # Rapport archive avec le snapshot
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == ab_setup["pending_id"]).first()
        assert snap.backtest_report["gap_pct"] == report["gap_pct"]
        assert snap.backtest_by == "testeur"
    finally:
        db.close()

    # Restitue par le detail de revue
    detail = client.get(f"/api/review/snapshots/{ab_setup['pending_id']}").json()
    assert detail["backtest_report"]["verdict"] == report["verdict"]


def test_good_guys_bulk_then_backtest_ok(client, ab_setup):
    # Seuil bas pour forcer le verdict WARN au premier passage
    assert client.put("/api/settings/ingestion", json={"backtest_max_gap_pct": 5}).status_code == 200
    first = client.post(
        f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
        json={"panel_snapshot_id": ab_setup["panel_id"]},
    ).json()
    assert first["verdict"] == "WARN"

    # Good Guy en masse sur les nouvelles paires (justification commune)
    response = client.post("/api/whitelist/bulk", json={
        "pairs": [
            {"client_id": p["client_id"], "watchlist_entity_id": p["entity_id"],
             "client_name": p["client_name"], "watchlist_name": p["entity_name"],
             "list_type": p["list_type"]}
            for p in first["new_pairs"]
        ],
        "justification": WL_JUSTIF_MARKER,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["created"]) == len(first["new_pairs"])

    # Doublons sautes au second appel
    again = client.post("/api/whitelist/bulk", json={
        "pairs": [{"client_id": p["client_id"], "watchlist_entity_id": p["entity_id"]} for p in first["new_pairs"]],
        "justification": WL_JUSTIF_MARKER,
    }).json()
    assert len(again["created"]) == 0
    assert all(s["reason"] == "paire déjà active" for s in again["skipped"])

    # Re-backtest : la paire est supprimee par la liste blanche -> verdict OK
    second = client.post(
        f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
        json={"panel_snapshot_id": ab_setup["panel_id"]},
    ).json()
    assert second["candidate"]["whitelisted_suppressed"] >= 1
    assert second["candidate"]["alerts"] == second["current"]["alerts"]
    assert second["gap_pct"] == 0
    assert second["verdict"] == "OK"


def test_bulk_whitelist_requires_justification(client):
    response = client.post("/api/whitelist/bulk", json={
        "pairs": [{"client_id": "X", "watchlist_entity_id": "Y"}],
    })
    assert response.status_code == 400  # justification exigee par defaut


# ------------------ REGLAGES DE GOUVERNANCE ------------------

def test_backtest_required_gates_approval(client, ab_setup):
    assert client.put("/api/settings/ingestion",
                      json={"backtest_required": True, "backtest_max_gap_pct": 5}).status_code == 200

    # Sans rapport -> refus
    response = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/approve", json={"comment": "x"})
    assert response.status_code == 400
    assert "cahier de tests" in response.json()["detail"].lower()

    # Rapport WARN -> refus
    warn = client.post(
        f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
        json={"panel_snapshot_id": ab_setup["panel_id"]},
    ).json()
    assert warn["verdict"] == "WARN"
    response = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/approve", json={"comment": "x"})
    assert response.status_code == 400
    assert "écart" in response.json()["detail"].lower()

    # Seuil releve + re-test -> verdict OK -> approbation acceptee
    assert client.put("/api/settings/ingestion", json={"backtest_max_gap_pct": 500}).status_code == 200
    ok = client.post(
        f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
        json={"panel_snapshot_id": ab_setup["panel_id"]},
    ).json()
    assert ok["verdict"] == "OK"
    response = client.post(f"/api/review/snapshots/{ab_setup['pending_id']}/approve", json={"comment": "ok"})
    assert response.status_code == 200, response.text


def test_backtest_rejects_bad_panel_and_non_pending(client, ab_setup):
    response = client.post(
        f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
        json={"panel_snapshot_id": "inexistant"},
    )
    assert response.status_code == 400


# ------------------ GENERATEUR DE PANEL ------------------

def test_generate_panel_isolated_from_real_client_base(client, ab_setup):
    response = client.post("/api/testpanels/generate", json={
        "snapshot_id": ab_setup["pending_id"], "size": 60, "seed": 42,
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["record_count"] == 60

    db = next(get_db())
    try:
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == data["snapshot_id"]).first()
        assert snap.file_type == TEST_PANEL_FILE_TYPE
        assert snap.status == "READY"
        rows = db.query(ClientEntity).filter(ClientEntity.snapshot_id == snap.snapshot_id).count()
        assert rows == 60
        # Garde-fou : le panel genere n'entre JAMAIS dans le re-criblage reel
        real_snap_ids = {c["snapshot_id"] for c in _client_dicts(db)}
        assert snap.snapshot_id not in real_snap_ids
    finally:
        db.close()

    # Visible dans le selecteur de panels, marque comme genere
    panels = client.get("/api/testpanels").json()["panels"]
    generated = next(p for p in panels if p["snapshot_id"] == data["snapshot_id"])
    assert generated["generated"] is True

    # Utilisable par le cahier de tests (les hits copies interceptent)
    report = client.post(
        f"/api/review/snapshots/{ab_setup['pending_id']}/backtest",
        json={"panel_snapshot_id": data["snapshot_id"]},
    ).json()
    assert report["panel_size"] == 60
    assert report["candidate"]["alerts"] >= 1  # copies exactes -> interceptions attendues


def test_generate_panel_size_bounds(client):
    assert client.post("/api/testpanels/generate", json={"size": 10}).status_code == 400
    assert client.post("/api/testpanels/generate", json={"size": 9999}).status_code == 400


# ------------------ NON-REGRESSION COMPARATEUR ------------------

def test_compare_snapshots_returns_report(client, ab_setup):
    db = next(get_db())
    try:
        snaps = db.query(Snapshot).filter(
            Snapshot.file_name.like("test_bt_%"),
            Snapshot.file_type == "WATCHLIST_EU"
        ).order_by(Snapshot.uploaded_at.asc()).all()
        old_id, new_id = snaps[0].snapshot_id, snaps[1].snapshot_id
    finally:
        db.close()
    response = client.post("/api/snapshots/compare",
                           json={"snapshot_old_id": old_id, "snapshot_new_id": new_id})
    assert response.status_code == 200
    report = response.json()
    assert report is not None and "summary" in report and "details" in report
    assert report["comparison_metadata"]["file_type"] == "WATCHLIST_EU"

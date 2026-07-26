"""
Tests de la mesure d'impact des equivalences linguistiques.

Ce module comble un trou reel : jusqu'ici, la documentation renvoyait au
cahier de tests pour « chiffrer l'ecart avant/apres » une activation
d'equivalences — mais le cahier de tests compare deux univers de LISTES sous
un parametrage constant, et le simulateur de seuils rejoue des scores DEJA
CALCULES. Ni l'un ni l'autre ne savait faire varier le parametrage.

Le point que ces tests protegent en priorite : l'ISOLATION. La mesure tourne
dans un thread de fond pendant que l'API sert des criblages reels. Si elle
forcait le contexte globalement, elle corromprait la production le temps de
la mesure — un criblage rendu sous un parametrage que personne n'a demande.
"""
import threading
import time
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr import resource_impact, resources
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.config import config
from fiskr.database import (
    ClientEntity, LearnedEquivalence, Snapshot, WatchlistEntity, compute_checksum, get_db,
)
from fiskr.scoring import compute_base_score


SUFFIX = uuid.uuid4().hex[:8]
WL_ID = f"IMPACT-WL-{SUFFIX}"
PANEL_ID = f"IMPACT-PANEL-{SUFFIX}"

LISTED = ["Henri DUPONT", "Mohammad AL ASSAD", "Sofia MARCHETTI"]
PANEL = [
    ("Harry", "Dupont"),        # équivalent inter-langues : invisible sans table
    ("Mohammed", "Al Assad"),   # translittération : déjà rattrapée par les métriques
    ("Bruno", "Lefort"),        # sans rapport : ne doit jamais matcher
]

BOTH_NAMES = {resources.FIELD_GIVEN_NAME, resources.FIELD_SURNAME}


def _seed(db):
    db.add(Snapshot(snapshot_id=WL_ID, file_type="WATCHLIST_EU",
                    file_name=f"impact_{SUFFIX}.csv", file_hash=WL_ID,
                    record_count=len(LISTED), status="READY",
                    uploaded_at=datetime.utcnow()))
    for i, name in enumerate(LISTED):
        db.add(WatchlistEntity(
            snapshot_id=WL_ID, entity_id=f"{WL_ID}-{i}", entity_type="I",
            primary_name=name, countries={"citizenship": ["FR"]},
            entity_checksum=compute_checksum({"e": f"{WL_ID}-{i}"})))
    db.add(Snapshot(snapshot_id=PANEL_ID, file_type="CLIENT_TEST_PANEL",
                    file_name=f"panel_{SUFFIX}.csv", file_hash=PANEL_ID,
                    record_count=len(PANEL), status="READY",
                    uploaded_at=datetime.utcnow()))
    for i, (first, last) in enumerate(PANEL):
        db.add(ClientEntity(
            snapshot_id=PANEL_ID, client_id=f"{PANEL_ID}-{i}", client_type="PP",
            client_first_name=first, client_last_name=last,
            client_countries={"nationality": ["FR"]},
            entity_checksum=compute_checksum({"c": f"{PANEL_ID}-{i}"})))
    db.commit()


def _cleanup(db):
    db.query(ClientEntity).filter(
        ClientEntity.snapshot_id == PANEL_ID).delete(synchronize_session=False)
    db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == WL_ID).delete(synchronize_session=False)
    db.query(Snapshot).filter(
        Snapshot.snapshot_id.in_([WL_ID, PANEL_ID])).delete(synchronize_session=False)
    db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a.in_(["ZZIMPA", "ZZIMPB"])).delete(synchronize_session=False)
    db.commit()


@pytest.fixture
def db():
    session = next(get_db())
    _cleanup(session)
    _seed(session)
    yield session
    _cleanup(session)
    session.close()
    resources.set_index(None)
    resources.invalidate_context()


# ------------------ ISOLATION : LE POINT CRITIQUE ------------------

def test_use_context_does_not_leak_to_other_threads():
    """
    Une mesure tourne en tache de fond pendant que l'API crible pour de vrai.
    Si la surcharge fuyait, des decisions de production sortiraient sous un
    parametrage que personne n'a demande — et seraient inscrites au journal
    d'audit immuable.
    """
    index = resources.load_index(resources.default_directory())
    resources.set_index(index)
    resources._context_cache = {"index": None, "fields": set()}

    seen = []
    stop = threading.Event()

    def production():
        while not stop.is_set():
            seen.append(compute_base_score("Henri Dupont", "Harry Dupont", config))
            time.sleep(0.001)

    worker = threading.Thread(target=production)
    worker.start()
    try:
        time.sleep(0.03)
        with resources.use_context({resources.FIELD_GIVEN_NAME}, index):
            measured = compute_base_score("Henri Dupont", "Harry Dupont", config)
            time.sleep(0.03)
    finally:
        stop.set()
        worker.join()

    assert measured == 100.0                      # le thread de mesure voit la table
    assert seen and set(seen) == {82.0}           # la production n'a rien vu changer


def test_use_context_restores_previous_state_on_exception():
    resources._context_cache = {"index": None, "fields": set()}
    with pytest.raises(RuntimeError):
        with resources.use_context({resources.FIELD_GIVEN_NAME}):
            raise RuntimeError("échec au milieu de la mesure")
    assert resources.current_context()["fields"] == set()


def test_use_context_nests():
    resources._context_cache = {"index": None, "fields": set()}
    with resources.use_context({resources.FIELD_GIVEN_NAME}):
        with resources.use_context({resources.FIELD_COUNTRY}):
            assert resources.current_context()["fields"] == {resources.FIELD_COUNTRY}
        assert resources.current_context()["fields"] == {resources.FIELD_GIVEN_NAME}
    assert resources.current_context()["fields"] == set()


# ------------------ LA MESURE ------------------

def test_simulation_quantifies_the_gain(db):
    report = resource_impact.simulate_resource_impact(
        db, PANEL_ID, candidate_fields=BOTH_NAMES, baseline_fields=set())
    assert report["alerts_after"] > report["alerts_before"]
    assert report["delta"] == report["alerts_after"] - report["alerts_before"]
    assert report["gained_count"] >= 1
    assert report["panel_size"] == len(PANEL)


def test_gained_examples_carry_the_equivalence_that_produced_them(db):
    """
    Un responsable qui lit « +12 alertes » doit pouvoir voir lesquelles et
    pourquoi ; sinon le chiffre ne l'aide pas à décider.
    """
    report = resource_impact.simulate_resource_impact(
        db, PANEL_ID, candidate_fields=BOTH_NAMES, baseline_fields=set())
    harry = [g for g in report["gained_examples"] if "Harry" in (g["client_name"] or "")]
    assert harry, "le rapprochement Harry/Henri doit figurer dans les gains"
    pairs = {(e["source"], e["target"]) for e in harry[0]["equivalences"]}
    assert ("HARRY", "HENRI") in pairs


def test_identical_parameters_measure_no_change(db):
    """Le contrôle négatif : sans changement, l'écart doit être nul."""
    report = resource_impact.simulate_resource_impact(
        db, PANEL_ID, candidate_fields=set(), baseline_fields=set())
    assert report["delta"] == 0
    assert report["gained_count"] == 0 and report["lost_count"] == 0


def test_simulation_writes_nothing(db):
    from fiskr.database import Alert, AuditTrail

    before = (db.query(Alert).count(), db.query(AuditTrail).count())
    resource_impact.simulate_resource_impact(
        db, PANEL_ID, candidate_fields=BOTH_NAMES, baseline_fields=set())
    assert (db.query(Alert).count(), db.query(AuditTrail).count()) == before


def test_simulation_leaves_the_live_setting_untouched(db):
    from fiskr.settings import resource_fields

    before = dict(resource_fields(db))
    resource_impact.simulate_resource_impact(
        db, PANEL_ID, candidate_fields=BOTH_NAMES, baseline_fields=set())
    assert dict(resource_fields(db)) == before
    assert resources.current_context()["fields"] == set()


def test_baseline_defaults_to_the_live_configuration(db):
    """
    Sans référence explicite, la question posée est « qu'est-ce que mon
    changement ajoute à ce que je fais déjà », pas « au néant ».
    """
    report = resource_impact.simulate_resource_impact(
        db, PANEL_ID, candidate_fields=BOTH_NAMES)
    assert report["baseline_fields"] == []          # rien n'est actif par défaut


def test_report_by_list_sums_to_the_totals(db):
    report = resource_impact.simulate_resource_impact(
        db, PANEL_ID, candidate_fields=BOTH_NAMES, baseline_fields=set())
    assert sum(b["before"] for b in report["by_list"].values()) == report["alerts_before"]
    assert sum(b["after"] for b in report["by_list"].values()) == report["alerts_after"]


def test_report_states_what_it_cannot_measure(db):
    report = resource_impact.simulate_resource_impact(
        db, PANEL_ID, candidate_fields=BOTH_NAMES, baseline_fields=set())
    assert "vérité terrain" in report["caveat"]


def test_empty_panel_is_refused(db):
    with pytest.raises(ValueError):
        resource_impact.simulate_resource_impact(
            db, "panel-qui-nexiste-pas", candidate_fields=BOTH_NAMES)


# ------------------ ÉQUIVALENCES EN ATTENTE ------------------

def _add_pending(db):
    row = LearnedEquivalence(
        field=resources.FIELD_GIVEN_NAME, class_id="ZZIMPA",
        term_a="ZZIMPA", term_b="ZZIMPB",
        signature=f"{resources.FIELD_GIVEN_NAME}|ZZIMPA|ZZIMPB",
        source="ALIAS", occurrences=3, similarity=0.9, phonetic_match=True,
        confidence=0.8, evidence=[], status="PROPOSED")
    db.add(row)
    db.commit()
    return row


def test_pending_equivalences_can_be_measured_without_approving(db):
    row = _add_pending(db)
    index = resource_impact.build_candidate_index(
        db, {resources.FIELD_GIVEN_NAME}, include_pending_ids=[row.id])
    assert index.canonical("Zzimpb", resources.FIELD_GIVEN_NAME) == "ZZIMPA"
    # ...et la proposition n'a pas changé de statut au passage
    db.refresh(row)
    assert row.status == "PROPOSED"
    assert resources.get_index().canonical("Zzimpb", resources.FIELD_GIVEN_NAME) is None


def test_candidate_index_without_pending_is_the_live_index(db):
    assert resource_impact.build_candidate_index(
        db, {resources.FIELD_GIVEN_NAME}) is resources.get_index()


def test_no_active_field_means_no_index(db):
    assert resource_impact.build_candidate_index(db, set()) is None


# ------------------ API ------------------

def _override_user(username, role):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


@pytest.fixture
def admin_client(db):
    _override_user("impact_admin", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def user_client(db):
    _override_user("impact_user", "user")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _wait(client, token, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get(f"/api/progress?id={token}").json()
        if state.get("status") in ("DONE", "ERROR"):
            return state
        time.sleep(0.05)
    raise AssertionError("mesure toujours en cours")


def test_simulate_endpoint_returns_a_report(admin_client):
    response = admin_client.post("/api/resources/simulate", json={
        "panel_snapshot_id": PANEL_ID,
        "candidate_fields": sorted(BOTH_NAMES), "baseline_fields": []})
    assert response.status_code == 202
    state = _wait(admin_client, response.json()["job_token"])
    assert state["status"] == "DONE"
    assert state["result"]["delta"] >= 1


def test_simulate_requires_admin(user_client):
    assert user_client.post("/api/resources/simulate", json={
        "panel_snapshot_id": PANEL_ID}).status_code == 403


def test_simulate_rejects_unknown_panel(admin_client):
    assert admin_client.post("/api/resources/simulate", json={
        "panel_snapshot_id": "inconnu"}).status_code == 404


def test_simulate_rejects_a_non_panel_snapshot(admin_client):
    assert admin_client.post("/api/resources/simulate", json={
        "panel_snapshot_id": WL_ID}).status_code == 400


def test_simulate_rejects_unknown_field(admin_client):
    assert admin_client.post("/api/resources/simulate", json={
        "panel_snapshot_id": PANEL_ID, "candidate_fields": ["planete"]}).status_code == 400


def test_active_operations_never_expose_the_report(admin_client):
    """
    Le rapport contient des noms de clients et de fiches listées. La liste des
    opérations en cours est interrogée en boucle par le tableau de bord de
    CHAQUE utilisateur : le résultat ne doit sortir que sur le jeton nominatif.
    """
    response = admin_client.post("/api/resources/simulate", json={
        "panel_snapshot_id": PANEL_ID,
        "candidate_fields": sorted(BOTH_NAMES), "baseline_fields": []})
    token = response.json()["job_token"]
    items = admin_client.get("/api/progress/active").json()["items"]
    assert items and all("result" not in item for item in items)
    _wait(admin_client, token)
    assert "result" in admin_client.get(f"/api/progress?id={token}").json()

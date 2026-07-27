"""
Écran, API et mesure d'impact des capacités du moteur.

Trois exigences se croisent ici, et chacune a déjà été apprise à ses dépens
ailleurs dans ce dépôt :

1. DOUBLE INVALIDATION — l'index des fiches listées fige ses clés de blocking
   au chargement. Écrire le réglage sans recharger le cache ne changerait que
   la sonde du client : les deux côtés ne se rencontreraient jamais et le
   réglage serait sans effet visible.
2. ISOLATION DE LA MESURE — une simulation tourne en tâche de fond pendant que
   l'API sert des criblages réels. Une surcharge globale les corromprait, et
   les décisions fausses partiraient au journal d'audit immuable.
3. TRAÇABILITÉ — qui, quand, avant → après. Un dispositif qui permet de rendre
   le moteur aveugle doit laisser une trace opposable.
"""
import threading
import time
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr import capabilities as caps
from fiskr import engine_impact
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.config import config
from fiskr.database import (
    AdminAuditLog, AppSetting, ClientEntity, Snapshot, WatchlistEntity,
    compute_checksum, get_db,
)
from fiskr.scoring import compute_base_score
from fiskr.settings import SETTING_ENGINE_CAPABILITIES, engine_capabilities


SUFFIX = uuid.uuid4().hex[:8]
WL_ID = f"ENGIMP-WL-{SUFFIX}"
PANEL_ID = f"ENGIMP-PANEL-{SUFFIX}"

# Panel volontairement construit autour d'UNE capacité à fort effet : l'ordre
# de nom inversé. Les listes officielles écrivent les noms d'Asie de l'Est
# patronyme en tête, le référentiel client concatène « prénom nom ».
LISTED = [f"Zhaoxu ZHENGMIN{SUFFIX[:4].upper()}", f"Kuanyu LIANFENG{SUFFIX[:4].upper()}"]
PANEL = [
    (f"ZHENGMIN{SUFFIX[:4].upper()}", "Zhaoxu"),   # inversé : perdu si la capacité est coupée
    (f"LIANFENG{SUFFIX[:4].upper()}", "Kuanyu"),   # inversé aussi
    ("Bruno", f"LEFORT{SUFFIX[:4].upper()}"),      # sans rapport : ne matche jamais
]


def _seed(db):
    db.add(Snapshot(snapshot_id=WL_ID, file_type="WATCHLIST_EU",
                    file_name=f"eng_{SUFFIX}.csv", file_hash=WL_ID,
                    record_count=len(LISTED), status="READY",
                    uploaded_at=datetime.utcnow()))
    for i, name in enumerate(LISTED):
        db.add(WatchlistEntity(
            snapshot_id=WL_ID, entity_id=f"{WL_ID}-{i}", entity_type="I",
            primary_name=name, countries={"citizenship": ["CN"]},
            entity_checksum=compute_checksum({"e": f"{WL_ID}-{i}"})))
    db.add(Snapshot(snapshot_id=PANEL_ID, file_type="CLIENT_TEST_PANEL",
                    file_name=f"panel_{SUFFIX}.csv", file_hash=PANEL_ID,
                    record_count=len(PANEL), status="READY",
                    uploaded_at=datetime.utcnow()))
    for i, (first, last) in enumerate(PANEL):
        db.add(ClientEntity(
            snapshot_id=PANEL_ID, client_id=f"{PANEL_ID}-{i}", client_type="PP",
            client_first_name=first, client_last_name=last,
            client_countries={"nationality": ["CN"]},
            entity_checksum=compute_checksum({"c": f"{PANEL_ID}-{i}"})))
    db.commit()


def _cleanup(db):
    db.query(AppSetting).filter(
        AppSetting.key == SETTING_ENGINE_CAPABILITIES).delete(synchronize_session=False)
    db.query(ClientEntity).filter(
        ClientEntity.snapshot_id == PANEL_ID).delete(synchronize_session=False)
    db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == WL_ID).delete(synchronize_session=False)
    db.query(Snapshot).filter(
        Snapshot.snapshot_id.in_([WL_ID, PANEL_ID])).delete(synchronize_session=False)
    db.commit()
    caps.invalidate_context()


@pytest.fixture
def db():
    session = next(get_db())
    _cleanup(session)
    _seed(session)
    yield session
    _cleanup(session)
    session.close()
    caps._local.override = None


@pytest.fixture
def client(db):
    app.dependency_overrides[get_current_user] = lambda: {
        "username": "resp_conformite", "role": "admin", "roles": ["admin"]}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ------------------ CATALOGUE EXPOSÉ ------------------

def test_the_screen_is_entirely_generated_from_the_catalog(client):
    """
    Ajouter une capacité au catalogue doit suffire à la faire apparaître, avec
    son avertissement et sa dépendance. Rien à déclarer ailleurs.
    """
    data = client.get("/api/settings/engine").json()
    assert set(data["catalog"]) == set(caps.CAPABILITY_CATALOG)
    for cap_id, entry in data["catalog"].items():
        assert entry["label"] and entry["loss"]
        assert entry["family"] in data["families"]
    assert set(data["state"]) == set(caps.CHANNELS)
    assert data["state"]["SCREENING"]["capabilities"][caps.CAP_TRANSLIT] is True


def test_the_ten_script_toggles_are_exposed_with_their_prerequisite(client):
    data = client.get("/api/settings/engine").json()
    for script in data["scripts"]:
        entry = data["catalog"][f"translit.{script}"]
        assert entry["depends_on"] == [caps.CAP_TRANSLIT]


def test_a_capability_without_its_prerequisite_is_reported_as_inert(client, db):
    """L'écran doit le dire plutôt que de laisser croire que la bascule agit."""
    client.put("/api/settings/engine", json={
        "channel": "SCREENING", "capabilities": {caps.CAP_TRANSLIT: False}})
    data = client.get("/api/settings/engine").json()
    inertes = data["state"]["SCREENING"]["inert"]
    assert "translit.cyrillic" in inertes
    assert inertes["translit.cyrillic"] == [caps.CAP_TRANSLIT]


# ------------------ ÉCRITURE DU RÉGLAGE ------------------

def test_a_partial_update_leaves_the_rest_alone(client, db):
    response = client.put("/api/settings/engine", json={
        "channel": "SCREENING", "capabilities": {caps.CAP_NAMES_REVERSED: False}})
    assert response.status_code == 200, response.text
    effectif = engine_capabilities(db, "SCREENING")
    assert effectif[caps.CAP_NAMES_REVERSED] is False
    assert effectif[caps.CAP_TRANSLIT] is True


def test_the_response_carries_what_is_lost(client):
    """
    Un appel d'API doit porter l'avertissement, pas seulement l'écran : c'est
    la contrepartie du pouvoir de rendre le moteur aveugle.
    """
    body = client.put("/api/settings/engine", json={
        "channel": "SCREENING", "capabilities": {caps.CAP_TRANSLIT: False}}).json()
    assert caps.CAP_TRANSLIT in body["losses"]
    assert "OFAC" in body["losses"][caps.CAP_TRANSLIT]


def test_writing_the_screening_channel_reloads_the_index(client):
    """
    LA règle du dispositif. L'index fige ses clés de blocking au chargement :
    sans rechargement, seule la sonde du client changerait et les deux côtés
    ne se rencontreraient jamais.
    """
    body = client.put("/api/settings/engine", json={
        "channel": "SCREENING", "capabilities": {caps.CAP_TRANSLIT: False}}).json()
    assert body["cache_reloaded"] is True


def test_writing_the_filtering_channel_does_not_reload_the_screening_index(client):
    """L'index de production ne dépend pas du canal filtrage : pas de coût inutile."""
    body = client.put("/api/settings/engine", json={
        "channel": "FILTERING", "capabilities": {caps.CAP_TRANSLIT: False}}).json()
    assert body["cache_reloaded"] is False


def test_a_write_without_any_change_reloads_nothing(client):
    body = client.put("/api/settings/engine", json={
        "channel": "SCREENING", "capabilities": {caps.CAP_TRANSLIT: True}}).json()
    assert body["changed"] == {}
    assert body["cache_reloaded"] is False


def test_the_change_is_written_to_the_administration_journal(client, db):
    """Qui, quand, avant → après : la trace opposable qu'attend un contrôleur."""
    client.put("/api/settings/engine", json={
        "channel": "SCREENING", "capabilities": {caps.CAP_ADJUST_GEOGRAPHY: False}})
    entry = db.query(AdminAuditLog).filter(
        AdminAuditLog.action == "ENGINE_UPDATED").order_by(
        AdminAuditLog.id.desc()).first()
    assert entry is not None
    assert entry.username == "resp_conformite"
    assert entry.target == "engine:SCREENING"
    assert entry.after[caps.CAP_ADJUST_GEOGRAPHY] is False
    assert entry.before[caps.CAP_ADJUST_GEOGRAPHY] is True


def test_an_unknown_capability_is_refused(client):
    response = client.put("/api/settings/engine", json={
        "channel": "SCREENING", "capabilities": {"capacite.inventee": False}})
    assert response.status_code == 400
    assert "inconnue" in response.json()["detail"]


def test_an_unknown_channel_is_refused(client):
    response = client.put("/api/settings/engine", json={
        "channel": "BATCH", "capabilities": {caps.CAP_TRANSLIT: False}})
    assert response.status_code == 400


def test_the_setting_travels_between_environments(client):
    """
    `_PORTABLE_SETTINGS` est une liste blanche fermée : sans ajout, le réglage
    ne franchirait pas recette → production et l'import le classerait en
    « ignoré » silencieusement.
    """
    client.put("/api/settings/engine", json={
        "channel": "SCREENING", "capabilities": {caps.CAP_NAMES_MAIDEN: False}})
    export = client.get("/api/admin/config/export").json()
    assert SETTING_ENGINE_CAPABILITIES in export["settings"]


# ------------------ MESURE D'IMPACT ------------------

def test_the_measurement_quantifies_what_a_cut_capability_costs(db):
    actives = {c for c, on in engine_capabilities(db, "SCREENING").items() if on}
    report = engine_impact.simulate_engine_impact(
        db, PANEL_ID, candidate_capabilities=actives - {caps.CAP_NAMES_REVERSED},
        baseline_capabilities=actives)
    assert report["alerts_before"] >= 2
    assert report["alerts_after"] < report["alerts_before"]
    assert report["lost_count"] >= 1
    assert report["delta"] == report["alerts_after"] - report["alerts_before"]
    assert report["panel_size"] == len(PANEL)


def test_the_report_says_which_toggle_produced_the_gap(db):
    """Un écart de volume ne se lit pas sans savoir quelle bascule l'a produit."""
    actives = {c for c, on in engine_capabilities(db, "SCREENING").items() if on}
    report = engine_impact.simulate_engine_impact(
        db, PANEL_ID, candidate_capabilities=actives - {caps.CAP_NAMES_REVERSED},
        baseline_capabilities=actives)
    assert report["change"]["disabled"] == [caps.CAP_NAMES_REVERSED]
    assert report["change"]["enabled"] == []


def test_lost_pairs_are_listed_so_they_can_be_judged_one_by_one(db):
    """
    Le sens qui compte ici est l'inverse de celui des ressources : couper fait
    PERDRE des alertes, et ce sont les paires perdues qu'un responsable doit
    regarder avant de valider.
    """
    actives = {c for c, on in engine_capabilities(db, "SCREENING").items() if on}
    report = engine_impact.simulate_engine_impact(
        db, PANEL_ID, candidate_capabilities=actives - {caps.CAP_NAMES_REVERSED},
        baseline_capabilities=actives)
    assert report["lost_examples"]
    perdue = report["lost_examples"][0]
    assert perdue["client_name"] and perdue["entity_name"] and perdue["score"]


def test_an_identical_parameterisation_measures_no_change(db):
    """Le contrôle négatif : sans changement, l'écart doit être nul."""
    actives = {c for c, on in engine_capabilities(db, "SCREENING").items() if on}
    report = engine_impact.simulate_engine_impact(
        db, PANEL_ID, candidate_capabilities=actives, baseline_capabilities=actives)
    assert report["delta"] == 0
    assert report["gained_count"] == 0 and report["lost_count"] == 0


def test_the_measurement_writes_nothing(db):
    from fiskr.database import Alert, AuditTrail

    before = (db.query(Alert).count(), db.query(AuditTrail).count())
    actives = {c for c, on in engine_capabilities(db, "SCREENING").items() if on}
    engine_impact.simulate_engine_impact(
        db, PANEL_ID, candidate_capabilities=actives - {caps.CAP_NAMES_REVERSED},
        baseline_capabilities=actives)
    assert (db.query(Alert).count(), db.query(AuditTrail).count()) == before


def test_the_measurement_leaves_the_live_setting_untouched(db):
    before = dict(engine_capabilities(db, "SCREENING"))
    actives = {c for c, on in before.items() if on}
    engine_impact.simulate_engine_impact(
        db, PANEL_ID, candidate_capabilities=actives - {caps.CAP_TRANSLIT},
        baseline_capabilities=actives)
    assert dict(engine_capabilities(db, "SCREENING")) == before


def test_the_measurement_does_not_contaminate_a_parallel_screening():
    """
    La propriété qui rend la mesure utilisable en production. Si la surcharge
    fuyait, des décisions réelles sortiraient sous un paramétrage que personne
    n'a demandé — et partiraient au journal d'audit immuable.
    """
    caps.invalidate_context()
    # Sans cela, les tables d'équivalences rattraperaient « Müller » / « MULLER »
    # et la mesure ne verrait aucune coupure : on mesurerait deux mécanismes.
    from fiskr import resources
    resources.set_index(None)
    resources._context_cache = {"index": None, "fields": set()}
    vus = []
    stop = threading.Event()

    def production():
        while not stop.is_set():
            vus.append(compute_base_score("Müller", "MULLER", config))
            time.sleep(0.001)

    worker = threading.Thread(target=production)
    worker.start()
    try:
        time.sleep(0.03)
        actives = {c for c, on in caps.defaults_for_channel("SCREENING").items() if on}
        with caps.use_context("SCREENING", actives - {caps.CAP_DIACRITICS}):
            mesure = compute_base_score("Müller", "MULLER", config)
            time.sleep(0.03)
    finally:
        stop.set()
        worker.join()

    resources.invalidate_context()
    assert mesure < 100.0                 # le thread de mesure voit la coupure
    assert vus and set(vus) == {100.0}    # la production n'a rien vu changer


# ------------------ ENDPOINT DE SIMULATION ------------------

def test_the_simulation_endpoint_answers_202_with_a_job_token(client):
    response = client.post("/api/settings/engine/simulate", json={
        "panel_snapshot_id": PANEL_ID, "channel": "SCREENING",
        "capabilities": {caps.CAP_NAMES_REVERSED: False}})
    assert response.status_code == 202, response.text
    assert response.json()["job_token"].startswith("engsim-")


def test_a_simulation_without_any_change_is_refused(client):
    """Mesurer un écart nul consomme des minutes de criblage pour rien."""
    response = client.post("/api/settings/engine/simulate", json={
        "panel_snapshot_id": PANEL_ID, "channel": "SCREENING",
        "capabilities": {caps.CAP_NAMES_REVERSED: True}})
    assert response.status_code == 400
    assert "identique" in response.json()["detail"]


def test_an_unknown_panel_is_refused(client):
    response = client.post("/api/settings/engine/simulate", json={
        "panel_snapshot_id": "panel-inexistant", "channel": "SCREENING",
        "capabilities": {caps.CAP_NAMES_REVERSED: False}})
    assert response.status_code == 404


def test_a_watchlist_snapshot_is_not_a_panel(client):
    response = client.post("/api/settings/engine/simulate", json={
        "panel_snapshot_id": WL_ID, "channel": "SCREENING",
        "capabilities": {caps.CAP_NAMES_REVERSED: False}})
    assert response.status_code == 400

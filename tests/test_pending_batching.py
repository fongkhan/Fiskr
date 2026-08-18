"""
Écran d'homologation : lectures groupées au lieu d'une boucle N+1.

La boucle d'origine faisait TROIS requêtes par snapshot en attente — dont un
COUNT sur `watchlist_entities`, onze millions de lignes en production. Mesuré
par comptage des requêtes SQL (déterministe, contrairement à un chronomètre) :

    1 snapshot en attente   ->  4 requêtes
    11 snapshots en attente -> 34 requêtes

Après une vague de synchronisations — dix-neuf listes en attente relevées en
production — l'écran demandait donc une soixantaine de requêtes pour afficher
dix-neuf lignes.

Le regroupement ne vaut que s'il rend EXACTEMENT la même chose. Ces tests
comparent la sortie de l'endpoint à un calcul de référence fait ligne par
ligne, sur les cas qui distinguent les deux implémentations : plusieurs types
de listes, un delta mémorisé encore valable, un delta périmé, une liste sans
production, des exclusions.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from fiskr.api import app, _snapshot_summary, _stored_sync_delta
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, SyncReport, WatchlistEntity
from fiskr.sync import _latest_ready_snapshot

TAG = uuid.uuid4().hex[:6].upper()


def _snap(db, suffixe, file_type, status, fiches=0, exclues=0):
    sid = f"pb-{TAG}-{suffixe}"
    db.add(Snapshot(snapshot_id=sid, file_type=file_type, file_name=f"{sid}.csv",
                    file_hash=uuid.uuid4().hex, record_count=fiches + exclues,
                    status=status))
    for i in range(fiches + exclues):
        db.add(WatchlistEntity(
            snapshot_id=sid, entity_id=f"E-{TAG}-{suffixe}-{i}", entity_type="I",
            primary_name=f"Nom {suffixe} {i}", excluded=(i < exclues) or None,
            aliases={"high_priority": [], "low_priority": []},
            dates_of_birth=[], is_deceased=False,
            countries={"citizenship": [], "residence": [], "birth_country": [],
                       "jurisdiction_country": []},
            entity_checksum=f"c-{TAG}-{suffixe}-{i}"))
    return sid


@pytest.fixture()
def contexte():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "pb", "full_name": "pb", "role": "admin",
        "roles": ["admin"]}
    db = next(get_db())
    # Un jeu qui couvre les cas où les deux implémentations pourraient diverger
    prod_eu = _snap(db, "prod-eu", "WATCHLIST_EU", "READY", fiches=3)
    _snap(db, "prod-dgt", "WATCHLIST_DGT", "READY", fiches=2)
    att_eu = _snap(db, "att-eu", "WATCHLIST_EU", "PENDING_REVIEW", fiches=4, exclues=2)
    att_dgt = _snap(db, "att-dgt", "WATCHLIST_DGT", "PENDING_REVIEW", fiches=3)
    att_un = _snap(db, "att-un", "WATCHLIST_UN", "PENDING_REVIEW", fiches=1)  # sans production
    # Delta mémorisé ENCORE valable (base = production courante)
    db.add(SyncReport(source="EU", status="PENDING_REVIEW", snapshot_id=att_eu,
                      previous_snapshot_id=prod_eu, added_count=1,
                      delta_report={"summary": {"added_count": 1, "removed_count": 0,
                                                "modified_count": 0}, "details": {}}))
    # Delta mémorisé PÉRIMÉ (base ≠ production courante) : ne doit pas être servi
    db.add(SyncReport(source="DGT", status="PENDING_REVIEW", snapshot_id=att_dgt,
                      previous_snapshot_id="pb-obsolete", added_count=5,
                      delta_report={"summary": {"added_count": 5, "removed_count": 0,
                                                "modified_count": 0}, "details": {}}))
    db.commit()
    yield db, TestClient(app), {"eu": att_eu, "dgt": att_dgt, "un": att_un}
    app.dependency_overrides.clear()
    db.query(SyncReport).filter(
        SyncReport.snapshot_id.like(f"pb-{TAG}-%")).delete(synchronize_session=False)
    db.query(WatchlistEntity).filter(
        WatchlistEntity.entity_id.like(f"E-{TAG}-%")).delete(synchronize_session=False)
    db.query(Snapshot).filter(
        Snapshot.snapshot_id.like(f"pb-{TAG}-%")).delete(synchronize_session=False)
    db.commit()
    db.close()


def _reference(db, snaps):
    """La boucle d'ORIGINE, ligne par ligne — l'étalon de comparaison."""
    sortie = {}
    for snap in snaps:
        row = _snapshot_summary(db, snap)
        production = _latest_ready_snapshot(db, snap.file_type)
        stored = _stored_sync_delta(db, snap.snapshot_id,
                                    production.snapshot_id if production else None)
        row["delta_summary"] = stored["summary"] if stored else None
        row["delta_available"] = stored is not None
        row["is_first_import"] = production is None
        sortie[snap.snapshot_id] = row
    return sortie


def test_batched_output_is_identical_to_the_row_by_row_loop(contexte):
    """L'équivalence stricte : même sortie, champ par champ."""
    db, client, ids = contexte
    with client:
        obtenu = {r["snapshot_id"]: r
                  for r in client.get("/api/review/pending").json()["pending"]}

    snaps = db.query(Snapshot).filter(
        Snapshot.snapshot_id.in_(list(ids.values()))).all()
    attendu = _reference(db, snaps)

    for sid, ref in attendu.items():
        assert sid in obtenu, f"{sid} absent de la réponse"
        assert obtenu[sid] == ref, f"divergence sur {sid}"


def test_the_cases_that_distinguish_the_two_paths(contexte):
    """Les cas où un regroupement naïf se tromperait : delta périmé servi à
    tort, liste sans production, exclusions attribuées au mauvais snapshot."""
    db, client, ids = contexte
    with client:
        lignes = {r["snapshot_id"]: r
                  for r in client.get("/api/review/pending").json()["pending"]}

    # Delta mémorisé valable : servi
    assert lignes[ids["eu"]]["delta_available"] is True
    assert lignes[ids["eu"]]["delta_summary"]["added_count"] == 1
    # Delta PÉRIMÉ : jamais servi, il mentirait sur ce que l'approbation change
    assert lignes[ids["dgt"]]["delta_available"] is False
    assert lignes[ids["dgt"]]["delta_summary"] is None
    # Liste sans production : premier import
    assert lignes[ids["un"]]["is_first_import"] is True
    assert lignes[ids["eu"]]["is_first_import"] is False
    # Exclusions comptées sur LE BON snapshot
    assert lignes[ids["eu"]]["excluded_count"] == 2
    assert lignes[ids["dgt"]]["excluded_count"] == 0


def test_query_count_does_not_grow_with_the_number_of_pending(contexte):
    """Garde-fou de régression : le coût de l'écran doit rester constant. Une
    boucle réintroduite se verrait ici, et nulle part ailleurs."""
    db, client, _ = contexte
    compteur = {"n": 0}

    def _compte(conn, cursor, statement, params, context, executemany):
        compteur["n"] += 1

    with client:
        event.listen(Engine, "before_cursor_execute", _compte)
        try:
            compteur["n"] = 0
            client.get("/api/review/pending")
            avec_trois = compteur["n"]

            for i in range(10):
                _snap(db, f"extra-{i}", "WATCHLIST_OFSI", "PENDING_REVIEW", fiches=1)
            db.commit()

            compteur["n"] = 0
            client.get("/api/review/pending")
            avec_treize = compteur["n"]
        finally:
            event.remove(Engine, "before_cursor_execute", _compte)

    assert avec_treize <= avec_trois + 1, (
        f"{avec_trois} requêtes pour 3 snapshots, {avec_treize} pour 13 : "
        f"le coût croît avec le nombre de lignes (N+1 réintroduit)")

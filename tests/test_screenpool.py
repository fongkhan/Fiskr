"""
Tests du criblage parallele par tranches (fiskr.screenpool).

La garantie centrale : le chemin parallele rend EXACTEMENT le meme rapport
que le chemin sequentiel — memes paires, memes compteurs, memes restitutions.
Les deux executent le meme corps (screen_one) ; ces tests verrouillent
l'orchestration (decoupage, agregation, contexte des enfants, echec de
tranche).
"""
import sys
import uuid
from datetime import datetime

import pytest

from fiskr import screenpool
from fiskr.backtest import _dry_run_screen
from fiskr.database import (
    ClientEntity, Snapshot, WatchlistEntity, get_db,
)
from fiskr.rescreen import _entity_dicts

linux_only = pytest.mark.skipif(sys.platform != "linux",
                                reason="le pool de tranches repose sur fork()")

SUFFIX = uuid.uuid4().hex[:6].upper()
WL_ID = f"SPOOL-WL-{SUFFIX}"
PANEL_ID = f"SPOOL-PANEL-{SUFFIX}"

FIRST = ["Boris", "Igor", "Dmitri", "Sergei", "Alexei", "Nikolai"]
LAST = ["Volkov", "Petrov", "Sokolov", "Ivanov", "Smirnov"]


def _seed(db, n_entities=40, n_clients=120):
    db.add(Snapshot(snapshot_id=WL_ID, file_type="WATCHLIST_EU",
                    file_name=f"spool_{SUFFIX}.csv", file_hash=WL_ID,
                    record_count=n_entities, status="READY",
                    uploaded_at=datetime.utcnow()))
    for i in range(n_entities):
        first, last = FIRST[i % len(FIRST)], f"{LAST[i % len(LAST)]}{i}"
        db.add(WatchlistEntity(
            snapshot_id=WL_ID, entity_id=f"{WL_ID}-{i}", entity_type="I",
            primary_name=f"{first} {last}",
            individual_name_parsed={"first_name": first, "last_name": last, "maiden_name": ""},
            dates_of_birth=[f"19{60 + i % 30}-01-15"], gender="M",
            countries={"citizenship": ["RU"]}, entity_checksum=f"chk-{i}"))
    db.add(Snapshot(snapshot_id=PANEL_ID, file_type="CLIENT_TEST_PANEL",
                    file_name=f"spool_panel_{SUFFIX}.csv", file_hash=PANEL_ID,
                    record_count=n_clients, status="READY",
                    uploaded_at=datetime.utcnow()))
    for i in range(n_clients):
        # Un client sur trois est une copie d'un liste (hit attendu)
        if i % 3 == 0:
            j = i % n_entities
            first, last = FIRST[j % len(FIRST)], f"{LAST[j % len(LAST)]}{j}"
            dob = f"19{60 + j % 30}-01-15"
        else:
            first, last, dob = "Paul", f"Tranquille{i}", "1985-09-09"
        db.add(ClientEntity(
            snapshot_id=PANEL_ID, client_id=f"{PANEL_ID}-C{i}", client_type="PP",
            client_first_name=first, client_last_name=last, client_dob=dob,
            client_gender="M", client_countries={"nationality": ["RU"]},
            entity_checksum=f"cchk-{i}"))
    db.commit()


def _cleanup(db):
    db.query(ClientEntity).filter(ClientEntity.snapshot_id == PANEL_ID).delete(synchronize_session=False)
    db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == WL_ID).delete(synchronize_session=False)
    db.query(Snapshot).filter(Snapshot.snapshot_id.in_([WL_ID, PANEL_ID])).delete(synchronize_session=False)
    db.commit()


@pytest.fixture
def db():
    session = next(get_db())
    _cleanup(session)
    _seed(session)
    yield session
    _cleanup(session)
    session.close()


@linux_only
def test_parallel_equals_sequential(db):
    entities = _entity_dicts(db, [WL_ID], projection=screenpool.ENTITY_PROJECTION)
    assert entities, "univers vide : le test ne prouverait rien"

    sequential = _dry_run_screen(db, None, entities, rule_set=[],
                                 panel_snapshot_id=PANEL_ID, processes=1)
    parallel = _dry_run_screen(db, None, entities, rule_set=[],
                               panel_snapshot_id=PANEL_ID, processes=2)

    assert sequential["alerts"] > 0, "aucune alerte : le test ne prouverait rien"
    assert parallel == sequential


@linux_only
def test_parallel_reports_progress(db):
    entities = _entity_dicts(db, [WL_ID], projection=screenpool.ENTITY_PROJECTION)
    ticks = []
    _dry_run_screen(db, None, entities, rule_set=[],
                    panel_snapshot_id=PANEL_ID, processes=2,
                    progress=lambda done, total: ticks.append((done, total)))
    assert ticks, "aucun tick de progression"
    done, total = ticks[-1]
    assert done == total == 120


@linux_only
def test_failed_chunk_propagates(db, monkeypatch):
    """Une tranche qui casse fait echouer l'operation ENTIERE, visiblement —
    jamais un rapport partiel silencieux."""
    entities = _entity_dicts(db, [WL_ID], projection=screenpool.ENTITY_PROJECTION)

    def _boom(*args, **kwargs):
        raise RuntimeError("tranche cassée (simulation)")

    # fork herite du module patche : les enfants executent _boom
    monkeypatch.setattr(screenpool, "screen_one", _boom)
    with pytest.raises(Exception):
        _dry_run_screen(db, None, entities, rule_set=[],
                        panel_snapshot_id=PANEL_ID, processes=2)


def test_resolve_processes_bounds():
    # 1 force le sequentiel quel que soit l'univers
    assert screenpool.resolve_processes(750_000, requested=1) == 1
    if sys.platform == "linux":
        # l'auto est borne par les coeurs (cpu-2) et ne retombe jamais a zero
        auto = screenpool.resolve_processes(1000, requested=None)
        assert 1 <= auto <= max(1, (screenpool.os.cpu_count() or 2) - 2) or auto == 1
        # une demande explicite est plafonnee par les coeurs
        big = screenpool.resolve_processes(1000, requested=64)
        assert big <= max(1, (screenpool.os.cpu_count() or 2) - 2)

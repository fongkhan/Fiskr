"""
Chien de garde du pool de criblage parallele — le scenario de production
« workers bloques sur les cahiers de tests » :
- un enfant du pool tue par l'OOM killer perd sa tranche pour toujours
  (multiprocessing.Pool ne la relance jamais) -> l'attente etait INFINIE,
  le job gardait son battement de coeur, la serialisation bloquait tous les
  cahiers de tests suivants et un slot du demon etait consomme a vie ;
- desormais : PoolStalled (enfant mort OU silence prolonge), et le cahier
  de tests repart automatiquement en sequentiel (memoire minimale).
"""
import time
import uuid
from types import SimpleNamespace

import pytest

from fiskr import screenpool
from fiskr.screenpool import PoolStalled, _wait_with_watchdog


class _FakeQueue:
    def __init__(self, ticks=()):
        self._ticks = list(ticks)

    def empty(self):
        return not self._ticks

    def get(self):
        return self._ticks.pop(0)


class _FakeResult:
    def __init__(self, ready_after_calls=None):
        self._calls = 0
        self._ready_after = ready_after_calls

    def ready(self):
        self._calls += 1
        return self._ready_after is not None and self._calls > self._ready_after


def test_watchdog_detects_dead_child():
    # Un enfant mort avec un code non nul (OOM : -9) -> PoolStalled immediat
    pool = SimpleNamespace(_pool=[SimpleNamespace(exitcode=-9)])
    with pytest.raises(PoolStalled, match="mort"):
        _wait_with_watchdog(_FakeResult(), _FakeQueue(), pool,
                            total_clients=100, progress=None, stall_timeout_s=60)


def test_watchdog_detects_silence():
    # Jamais pret, aucun tick, enfants « vivants » -> PoolStalled au timeout
    pool = SimpleNamespace(_pool=[SimpleNamespace(exitcode=None)])
    t0 = time.monotonic()
    with pytest.raises(PoolStalled, match="progression"):
        # stall_timeout_s force sous le minimum config : parametre direct
        _wait_with_watchdog(_FakeResult(), _FakeQueue(), pool,
                            total_clients=100, progress=None, stall_timeout_s=0.5)
    assert time.monotonic() - t0 < 10, "le chien de garde doit lever vite"


def test_watchdog_lets_healthy_pool_finish():
    # Des ticks arrivent puis le resultat est pret : aucune levee, progression relayee
    seen = []
    pool = SimpleNamespace(_pool=[SimpleNamespace(exitcode=None)])
    _wait_with_watchdog(_FakeResult(ready_after_calls=3), _FakeQueue([500, 500]), pool,
                        total_clients=1000, progress=lambda d, t: seen.append((d, t)),
                        stall_timeout_s=60)
    assert seen and seen[-1][0] >= 500


def test_backtest_falls_back_to_sequential(monkeypatch):
    """PoolStalled pendant un cahier de tests -> repli sequentiel automatique,
    resultat coherent (le criblage aboutit au lieu de bloquer la file)."""
    from fiskr import backtest
    from fiskr.database import get_db, ClientEntity, Snapshot

    db = next(get_db())
    uid = uuid.uuid4().hex[:8]
    panel_id = f"test-stall-panel-{uid}"
    db.add(Snapshot(snapshot_id=panel_id, file_type="CLIENT_BASE",
                    file_name=f"{panel_id}.csv", file_hash=uuid.uuid4().hex,
                    record_count=1, status="READY"))
    db.add(ClientEntity(snapshot_id=panel_id, client_id=f"CL-STALL-{uid}",
                        client_type="PP", client_first_name="Vladimir",
                        client_last_name="Putin",
                        client_countries={"nationality": ["RU"], "residence": [],
                                          "birth_country": [], "registration_country": []},
                        entity_checksum=f"chk-stall-{uid}"))
    db.commit()
    try:
        entities = [{
            "entity_id": f"WL-STALL-{uid}", "entity_type": "I",
            "primary_name": "Vladimir Putin", "snapshot_id": "s", "entity_checksum": "c",
            "individual_name_parsed": {"first_name": "Vladimir", "last_name": "Putin", "maiden_name": ""},
            "aliases": {"high_priority": [], "low_priority": []},
            "countries": {"citizenship": ["RU"], "residence": [], "birth_country": [],
                          "jurisdiction_country": []},
            "dates_of_birth": [], "_list_type": "WATCHLIST_OFAC",
        }]

        def _boom(*args, **kwargs):
            raise PoolStalled("un processus de criblage est mort (simulé).")

        monkeypatch.setattr(screenpool, "parallel_dry_run", _boom)
        monkeypatch.setattr(screenpool, "resolve_processes", lambda *a, **k: 4)

        result = backtest._dry_run_screen(db, clients=None, entities=entities,
                                          panel_snapshot_id=panel_id)
        # Le repli sequentiel a bien crible : le client matche l'entite
        assert result["alerts"] == 1
        pair = next(iter(result["pairs"].values()))
        assert pair["entity_id"] == f"WL-STALL-{uid}"
    finally:
        db.query(ClientEntity).filter(ClientEntity.snapshot_id == panel_id).delete(synchronize_session=False)
        db.query(Snapshot).filter(Snapshot.snapshot_id == panel_id).delete(synchronize_session=False)
        db.commit()
        db.close()

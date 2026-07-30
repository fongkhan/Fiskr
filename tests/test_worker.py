"""
Tests du demon travailleur et de ses garanties inter-processus :
- verrou flock (jamais deux demons, quelle que soit la course) ;
- reference des listes en production derivee de la BASE (l'audit du
  re-criblage execute dans le demon ne doit jamais tracer « N/A ») ;
- epoque du cache de production (invalidation inter-processus : le demon
  bumpe un entier en base, chaque processus API recharge son cache).
"""
import uuid
from datetime import datetime, timedelta

import pytest

from fiskr import worker
from fiskr.database import Snapshot, get_db, production_watchlist_reference
from fiskr.settings import bump_watchlist_epoch, watchlist_epoch


# ------------------ VERROU D'UNICITE (flock) ------------------

def test_worker_lock_is_exclusive_even_within_a_process(tmp_path, monkeypatch):
    """
    flock refuse un second demon : les verrous portent sur le descripteur
    ouvert, pas sur le pid — une seconde ouverture du meme fichier echoue,
    y compris dans le meme processus (et a fortiori depuis un autre).
    """
    monkeypatch.setattr(worker, "LOCK_FILE", tmp_path / "worker.lock")
    first = worker.acquire_worker_lock()
    assert first is not None, "le premier demon doit obtenir le verrou"
    assert worker.acquire_worker_lock() is None, "un second demon doit etre refuse"
    # Le verrou meurt avec son detenteur (close = liberation par le noyau) :
    # pas de fichier de PID fantome a nettoyer apres un kill -9
    first.close()
    third = worker.acquire_worker_lock()
    assert third is not None, "verrou libere : un nouveau demon doit pouvoir naitre"
    third.close()


def test_worker_lock_file_carries_the_pid(tmp_path, monkeypatch):
    import os
    monkeypatch.setattr(worker, "LOCK_FILE", tmp_path / "worker.lock")
    fh = worker.acquire_worker_lock()
    try:
        assert (tmp_path / "worker.lock").read_text().strip() == str(os.getpid())
    finally:
        fh.close()


# ------------------ REFERENCE DES LISTES EN PRODUCTION ------------------

def test_production_watchlist_reference_reads_the_database():
    """
    Le re-criblage tourne desormais dans le demon, qui ne voit pas le cache
    memoire du processus API : la reference tracee au journal d'audit
    immuable doit etre derivee de la base, pas des globales d'api.py.
    """
    db = next(get_db())
    marker = f"test_worker_{uuid.uuid4().hex[:8]}"
    snap = Snapshot(
        snapshot_id=str(uuid.uuid4()), file_type="WATCHLIST_EU",
        file_name=f"{marker}.csv", file_hash=f"hash_{marker}",
        status="READY", record_count=1,
        # Le plus recent des snapshots READY : date volontairement future
        uploaded_at=datetime.utcnow() + timedelta(days=1),
    )
    db.add(snap)
    db.commit()
    try:
        version, file_hash = production_watchlist_reference(db)
        assert version == "Database Active Snapshot"
        assert file_hash == f"hash_{marker}"
    finally:
        db.query(Snapshot).filter(Snapshot.snapshot_id == snap.snapshot_id).delete()
        db.commit()
        db.close()


# ------------------ EPOQUE DU CACHE DE PRODUCTION ------------------

def test_epoch_bump_increments_monotonically():
    db = next(get_db())
    try:
        before = watchlist_epoch(db)
        bump_watchlist_epoch(db)
        assert watchlist_epoch(db) == before + 1
    finally:
        db.close()


def test_ensure_watchlist_cache_reloads_only_on_epoch_change(monkeypatch):
    """
    _ensure_watchlist_cache est le garde-fou des criblages unitaires hors
    requete HTTP (campagnes batch dans le demon) : il recharge le cache
    quand l'epoque a change, et SEULEMENT dans ce cas — recharger 750 000
    fiches a chaque campagne serait un contresens.
    """
    import fiskr.api as api_module

    calls = []
    monkeypatch.setattr(api_module, "load_watchlist_cache", lambda db: calls.append(1))
    # Cache considere charge (non vide) et epoque connue = epoque courante
    monkeypatch.setattr(api_module, "watchlist_index", {"K": []})

    db = next(get_db())
    try:
        current = watchlist_epoch(db)
        monkeypatch.setattr(api_module, "_last_epoch_seen", current)
        api_module._ensure_watchlist_cache(db)
        assert calls == [], "epoque inchangee : pas de rechargement"

        bump_watchlist_epoch(db)
        api_module._ensure_watchlist_cache(db)
        assert calls == [1], "epoque changee : rechargement obligatoire"

        # L'epoque vue a ete mise a jour : l'appel suivant ne recharge plus
        api_module._ensure_watchlist_cache(db)
        assert calls == [1]
    finally:
        db.close()


def test_ensure_watchlist_cache_loads_an_empty_cache(monkeypatch):
    """Premier criblage dans le demon : cache vide, chargement immediat
    quelle que soit l'epoque (il n'y a rien a invalider, tout a charger)."""
    import fiskr.api as api_module

    calls = []
    monkeypatch.setattr(api_module, "load_watchlist_cache", lambda db: calls.append(1))
    monkeypatch.setattr(api_module, "watchlist_index", {})
    monkeypatch.setattr(api_module, "_last_epoch_seen", None)

    db = next(get_db())
    try:
        api_module._ensure_watchlist_cache(db)
        assert calls == [1]
    finally:
        db.close()

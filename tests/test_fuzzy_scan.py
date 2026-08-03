"""
Balayage fuzzy par tranches de la vue base de donnees :
- au-dela de WATCHLIST_FUZZY_INLINE_MAX fiches dans le perimetre, la
  consultation ne calcule plus le repli fuzzy (40 s bloquantes mesurees a
  300k fiches) et repond immediatement match_mode="fuzzy_pending" ;
- GET /api/watchlist/db/fuzzy parcourt le perimetre tranche par tranche
  (curseur keyset sur l'id), retourne les correspondances de chaque tranche
  et un drapeau done — l'ecran affiche au fur et a mesure ;
- les petits perimetres gardent le repli fuzzy DANS la consultation
  (comportement historique, couvert par tests/test_watchlist_db.py).
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, WatchlistEntity

UID = uuid.uuid4().hex[:8].upper()
TARGET = f"Zanzibar-Qscan-{UID}"  # cherche avec une typo : Zanzibra


def _override_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "test_fs_admin", "full_name": "test_fs_admin",
        "role": "admin", "roles": ["admin"],
    }


@pytest.fixture()
def ctx():
    _override_user()
    db = next(get_db())
    snap_id = f"test-fs-{UID.lower()}"
    db.add(Snapshot(snapshot_id=snap_id, file_type="WATCHLIST_DGT", file_name=f"{snap_id}.json",
                    file_hash=uuid.uuid4().hex, record_count=4, status="READY"))
    names = [TARGET, f"Sans-Rapport-Un-{UID}", f"Sans-Rapport-Deux-{UID}", f"Sans-Rapport-Trois-{UID}"]
    for i, n in enumerate(names):
        db.add(WatchlistEntity(
            snapshot_id=snap_id, entity_id=f"FS{i}-{UID}", entity_type="I",
            primary_name=n, aliases={"high_priority": [], "low_priority": []},
            countries={"citizenship": [], "residence": [], "birth_country": [], "jurisdiction_country": []},
            entity_checksum=f"chk-fs-{i}",
        ))
    db.commit()
    yield {"db": db, "client": TestClient(app), "snap_id": snap_id}
    app.dependency_overrides.pop(get_current_user, None)
    try:
        db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == snap_id).delete(synchronize_session=False)
        db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _walk_fuzzy(client, search, chunk=500, max_rounds=100, **extra):
    """Enchaine les tranches comme le fait l'ecran ; retourne (matches, rounds)."""
    cursor, rounds, matches = 0, 0, []
    while rounds < max_rounds:
        rounds += 1
        params = {"search": search, "cursor": cursor, "chunk": chunk, **extra}
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        data = client.get(f"/api/watchlist/db/fuzzy?{qs}").json()
        matches.extend(data["matches"])
        cursor = data["next_cursor"]
        if data["done"]:
            return matches, rounds
    raise AssertionError("balayage jamais termine")


def test_browse_defers_fuzzy_on_large_corpus(ctx, monkeypatch):
    # Perimetre « grand » simule : le seuil inline tombe a 0
    monkeypatch.setattr(api, "WATCHLIST_FUZZY_INLINE_MAX", 0)
    data = ctx["client"].get(f"/api/watchlist/db?search=Zanzibra-Qscan-{UID}").json()
    assert data["match_mode"] == "fuzzy_pending"
    assert data["items"] == [] and data["total"] == 0
    assert data["fuzzy_scan_total"] >= 4  # au moins les fiches semees


def test_chunked_scan_finds_typo_match(ctx):
    client = ctx["client"]
    # Perimetre borne a la famille DGT : le balayage reste court meme sur une
    # base partagee chargee par d'autres tests
    matches, rounds = _walk_fuzzy(client, f"Zanzibra-Qscan-{UID}", list_type="WATCHLIST_DGT")
    ours = [m for m in matches if m["primary_name"] == TARGET]
    assert ours, "la typo doit retrouver la fiche par similarite"
    assert ours[0]["_fuzzy_score"] >= 80.0
    # Les fiches sans rapport ne matchent pas
    assert not any("Sans-Rapport" in m["primary_name"] for m in matches)


def test_chunked_scan_cursor_advances(ctx):
    client = ctx["client"]
    # Tranches minuscules : le curseur doit avancer et se terminer proprement
    matches, rounds = _walk_fuzzy(client, f"Zanzibra-Qscan-{UID}", chunk=2,
                                  max_rounds=100000, list_type="WATCHLIST_DGT")
    assert any(m["primary_name"] == TARGET for m in matches)
    assert rounds >= 2, "plusieurs tranches attendues avec chunk=2"


def test_chunked_scan_validation(ctx):
    client = ctx["client"]
    assert client.get("/api/watchlist/db/fuzzy?search=a").status_code == 422
    assert client.get(f"/api/watchlist/db/fuzzy?search=abcd&scope=INVENTE").status_code == 400
    assert client.get(f"/api/watchlist/db/fuzzy?search=abcd&search_field=INVENTE").status_code == 400

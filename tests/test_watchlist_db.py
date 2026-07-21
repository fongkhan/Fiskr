"""
Tests de la vue « Listés — Base de Données » (GET /api/watchlist/db) :
lecture en direct de la base, paginee/filtrable cote serveur, avec un
perimetre (scope) couvrant la production ET le hors-production
(en attente d'homologation, remplacees, rejetees, exclues).
"""
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, WatchlistEntity, AppSetting
from fiskr.settings import (
    SETTING_REQUIRE_APPROVAL,
    SETTING_EXCLUSION_JUSTIFICATION_REQUIRED,
    SETTING_EXCLUSION_FILE_REQUIRED,
)

ALL_SETTING_KEYS = [
    SETTING_REQUIRE_APPROVAL,
    SETTING_EXCLUSION_JUSTIFICATION_REQUIRED,
    SETTING_EXCLUSION_FILE_REQUIRED,
]


def _override_user(role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "testeur", "full_name": "Testeur", "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_(ALL_SETTING_KEYS)).delete(synchronize_session=False)
        test_snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_dbview_%")).all()
        snap_ids = [s.snapshot_id for s in test_snaps]
        if snap_ids:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
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


def _watchlist_csv(names):
    rows = "\n".join(
        f"EU-{uuid.uuid4().hex[:10]},{etype},{name},RU" for etype, name in names
    )
    return f"entity_id,entity_type,primary_name,nationality\n{rows}\n"


def _upload_watchlist(client, names):
    file_name = f"test_dbview_{uuid.uuid4().hex[:8]}.csv"
    files = {"file": (file_name, _watchlist_csv(names), "text/csv")}
    response = client.post("/api/ingest", data={"file_type": "WATCHLIST_EU"}, files=files)
    assert response.status_code == 200, response.text
    return response.json()


def _set_approval(client, enabled: bool, **extra):
    payload = {"require_approval": enabled, **extra}
    response = client.put("/api/settings/ingestion", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _browse(client, **params):
    response = client.get("/api/watchlist/db", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(data):
    return {item["primary_name"] for item in data["items"]}


# ------------------ PERIMETRE PRODUCTION (defaut) ------------------

def test_production_scope_reflects_ready_snapshots(client):
    _set_approval(client, False)
    marker = f"Dbprodov {uuid.uuid4().hex[:6]}"
    _upload_watchlist(client, [("I", marker)])

    # Visible immediatement en scope par defaut (production), lu depuis la base
    data = _browse(client, search=marker)
    assert data["scope"] == "production"
    assert data["total"] == 1
    item = data["items"][0]
    assert item["primary_name"] == marker.upper()
    assert item["snapshot_status"] == "READY"
    assert item["_list_type"] == "WATCHLIST_EU"
    assert item["snapshot_uploaded_at"] is not None

    # Le total production concorde avec le comptage SQL direct (READY non exclues)
    db = next(get_db())
    try:
        from fiskr.api import WATCHLIST_FILE_TYPES
        sql_count = (
            db.query(WatchlistEntity)
            .join(Snapshot, WatchlistEntity.snapshot_id == Snapshot.snapshot_id)
            .filter(Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
                    Snapshot.status == "READY",
                    WatchlistEntity.excluded.isnot(True))
            .count()
        )
    finally:
        db.close()
    assert _browse(client)["total"] == sql_count


# ------------------ HORS PRODUCTION : PENDING / SUPERSEDED / EXCLUDED ------------------

def test_pending_review_visible_outside_production(client):
    _set_approval(client, True)
    marker = f"Dbpendingov {uuid.uuid4().hex[:6]}"
    _upload_watchlist(client, [("I", marker)])

    # Absent de la production, present en attente d'homologation et en « tous »
    assert _browse(client, search=marker)["total"] == 0
    pending = _browse(client, search=marker, scope="PENDING_REVIEW")
    assert pending["total"] == 1
    assert pending["items"][0]["snapshot_status"] == "PENDING_REVIEW"
    assert marker.upper() in _names(_browse(client, search=marker, scope="all"))


def test_superseded_versions_remain_browsable(client):
    # Version 1 en production, puis version 2 approuvee : la promotion fait
    # passer la version 1 en SUPERSEDED (meme type de liste)
    _set_approval(client, False)
    old_marker = f"Dbancienov {uuid.uuid4().hex[:6]}"
    new_marker = f"Dbnouveauov {uuid.uuid4().hex[:6]}"
    _upload_watchlist(client, [("I", old_marker)])

    _set_approval(client, True)
    result = _upload_watchlist(client, [("I", new_marker)])
    assert client.post(
        f"/api/review/snapshots/{result['snapshot_id']}/approve", json={"comment": "ok"}
    ).status_code == 200

    # L'ancienne version est sortie de production mais reste consultable
    assert _browse(client, search=old_marker)["total"] == 0
    superseded = _browse(client, search=old_marker, scope="SUPERSEDED")
    assert superseded["total"] == 1
    assert superseded["items"][0]["snapshot_status"] == "SUPERSEDED"
    assert _browse(client, search=new_marker)["total"] == 1


def test_excluded_entities_scope(client):
    _set_approval(client, True, exclusion_justification_required=False, exclusion_file_required=False)
    kept = f"Dbgardeov {uuid.uuid4().hex[:6]}"
    excluded = f"Dbexcluov {uuid.uuid4().hex[:6]}"
    result = _upload_watchlist(client, [("I", kept), ("I", excluded)])
    snap_id = result["snapshot_id"]

    entities = client.get(f"/api/review/snapshots/{snap_id}/entities").json()["items"]
    to_exclude = next(e for e in entities if excluded.upper() in e["primary_name"])
    assert client.post(
        f"/api/review/snapshots/{snap_id}/exclusions",
        data={"entity_ids": json.dumps([to_exclude["id"]])},
    ).status_code == 200
    assert client.post(f"/api/review/snapshots/{snap_id}/approve", json={"comment": "ok"}).status_code == 200

    # L'entite exclue est absente de la production mais visible en scope EXCLUDED
    assert _browse(client, search=excluded)["total"] == 0
    assert _browse(client, search=kept)["total"] == 1
    found = _browse(client, search=excluded, scope="EXCLUDED")
    assert found["total"] == 1
    assert found["items"][0]["excluded"] is True


# ------------------ RECHERCHE / FILTRES / PAGINATION ------------------

def test_search_by_entity_id_and_list_type_filter(client):
    _set_approval(client, False)
    marker = f"Dbfiltrov {uuid.uuid4().hex[:6]}"
    _upload_watchlist(client, [("I", marker)])

    entity_id = _browse(client, search=marker)["items"][0]["entity_id"]
    by_id = _browse(client, search=entity_id)
    assert by_id["total"] == 1
    assert by_id["items"][0]["entity_id"] == entity_id

    # Filtre par type de liste : present en UE, absent en PEP
    assert marker.upper() in _names(_browse(client, search=marker, list_type="WATCHLIST_EU"))
    assert _browse(client, search=marker, list_type="WATCHLIST_PEP")["total"] == 0


def test_pagination_envelope(client):
    _set_approval(client, False)
    marker = f"Dbpaginov {uuid.uuid4().hex[:6]}"
    _upload_watchlist(client, [("I", f"{marker} Un"), ("I", f"{marker} Deux"), ("I", f"{marker} Trois")])

    page1 = _browse(client, search=marker, page=1, page_size=2)
    page2 = _browse(client, search=marker, page=2, page_size=2)
    assert page1["total"] == 3 and page2["total"] == 3
    assert len(page1["items"]) == 2 and len(page2["items"]) == 1
    assert not (_names(page1) & _names(page2))


def test_unknown_scope_rejected(client):
    response = client.get("/api/watchlist/db", params={"scope": "NOPE"})
    assert response.status_code == 400


def test_cache_endpoint_unchanged(client):
    # La vue base n'altere pas l'endpoint cache : enveloppe {hash, items} intacte
    data = client.get("/api/watchlist").json()
    assert "hash" in data
    assert isinstance(data["items"], list)

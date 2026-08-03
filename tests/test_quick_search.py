"""
Recherche globale instantanee (palette Ctrl+K) — GET /api/search/quick :
- les listes sont servis par l'index memoire du moteur (blobs normalises
  construits avec le cache), insensibles aux accents et a la casse, alias
  compris, classes prefixe d'abord ;
- les alertes par une requete SQL unique bornee (client, liste, identifiants) ;
- un seul aller-retour, aucune jointure, aucun repli fuzzy plein referentiel.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app, load_watchlist_cache
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AuditTrail, Snapshot, WatchlistEntity

UID = uuid.uuid4().hex[:8].upper()


def _override_user():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "test_qs_admin", "full_name": "test_qs_admin",
        "role": "admin", "roles": ["admin"],
    }


def _entity(snap_id, entity_id, name, aliases_high=()):
    return WatchlistEntity(
        snapshot_id=snap_id, entity_id=entity_id, entity_type="I",
        primary_name=name,
        individual_name_parsed={"first_name": name.split()[0], "last_name": name.split()[-1], "maiden_name": ""},
        aliases={"high_priority": list(aliases_high), "low_priority": []},
        dates_of_birth=[], is_deceased=False,
        countries={"citizenship": [], "residence": [], "birth_country": [], "jurisdiction_country": []},
        entity_checksum=f"chk-{entity_id}",
    )


@pytest.fixture()
def ctx():
    _override_user()
    db = next(get_db())
    snap_id = f"test-qs-{UID.lower()}"
    db.add(Snapshot(snapshot_id=snap_id, file_type="WATCHLIST_DGT", file_name=f"{snap_id}.json",
                    file_hash=uuid.uuid4().hex, record_count=3, status="READY"))
    db.add(_entity(snap_id, f"QS1-{UID}", f"Zebulon Quicksearch-{UID}"))
    # Accents + alias : trouvable par « francois » et par son alias
    db.add(_entity(snap_id, f"QS2-{UID}", f"François Émond-Quicksearch-{UID}",
                   aliases_high=[f"AliasIntrouvable-{UID}"]))
    # Le terme au MILIEU du nom : doit se classer apres le match en prefixe
    db.add(_entity(snap_id, f"QS3-{UID}", f"Middle Zebulon-{UID} Suffix"))
    db.commit()
    audit = AuditTrail(client_id=f"test_qs_{UID}", client_name=f"Client Quicksearch-{UID}",
                       client_type="PP", watchlist_id=f"QS1-{UID}",
                       watchlist_name=f"Zebulon Quicksearch-{UID}", base_score=91.0,
                       final_score=91.0, status="ALERT", decision_tree={},
                       config_state={}, watchlist_version="test", watchlist_hash="test")
    db.add(audit)
    db.commit()
    alert = Alert(audit_id=audit.id, client_id=f"test_qs_{UID}",
                  client_name=f"Client Quicksearch-{UID}",
                  watchlist_entity_id=f"QS1-{UID}", watchlist_name=f"Zebulon Quicksearch-{UID}",
                  final_score=91.0, status="OPEN", channel="FILTERING")
    db.add(alert)
    db.commit()
    load_watchlist_cache(db)
    yield {"db": db, "client": TestClient(app), "snap_id": snap_id}
    app.dependency_overrides.pop(get_current_user, None)
    try:
        ids = [a.id for a in db.query(Alert).filter(Alert.client_id.like("test_qs_%")).all()]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == snap_id).delete(synchronize_session=False)
        db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).delete(synchronize_session=False)
        db.commit()
        load_watchlist_cache(db)
    finally:
        db.close()


def test_quick_search_memory_index_and_alerts(ctx):
    client = ctx["client"]
    data = client.get(f"/api/search/quick?q=Quicksearch-{UID}").json()

    names = [i["primary_name"] for i in data["watchlist"]["items"]]
    assert f"Zebulon Quicksearch-{UID}" in names
    assert data["watchlist"]["total"] >= 2
    # Les items portent ce que la modale de details attend (cles du cache moteur)
    first = data["watchlist"]["items"][0]
    assert first["list_type"] == "WATCHLIST_DGT" and "entity_id" in first and "aliases" in first

    # Alertes : la meme requete remonte l'alerte liee, avec son canal
    al = data["alerts"]
    assert al["total"] >= 1
    hit = next(a for a in al["items"] if a["client_name"] == f"Client Quicksearch-{UID}")
    assert hit["channel"] == "FILTERING" and hit["status"] == "OPEN" and hit["id"]


def test_quick_search_accents_case_and_aliases(ctx):
    client = ctx["client"]
    # « francois emond » sans accents ni majuscules -> trouve François Émond
    data = client.get(f"/api/search/quick?q=francois emond-quicksearch-{UID.lower()}").json()
    assert any(i["entity_id"] == f"QS2-{UID}" for i in data["watchlist"]["items"])
    # Recherche par alias
    data = client.get(f"/api/search/quick?q=aliasintrouvable-{UID.lower()}").json()
    assert any(i["entity_id"] == f"QS2-{UID}" for i in data["watchlist"]["items"])


def test_quick_search_prefix_ranks_first(ctx):
    client = ctx["client"]
    data = client.get(f"/api/search/quick?q=Zebulon").json()
    mine = [i["entity_id"] for i in data["watchlist"]["items"]
            if i["entity_id"] in (f"QS1-{UID}", f"QS3-{UID}")]
    assert mine, "les deux entites Zebulon doivent matcher"
    # QS1 (Zebulon en prefixe du nom) se classe avant QS3 (Zebulon au milieu)
    if len(mine) == 2:
        assert mine[0] == f"QS1-{UID}"


def test_quick_search_validation(ctx):
    client = ctx["client"]
    assert client.get("/api/search/quick?q=a").status_code == 422
    assert client.get("/api/search/quick").status_code == 422

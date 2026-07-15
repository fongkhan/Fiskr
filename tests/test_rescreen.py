"""
Tests du re-criblage automatique post-delta : seules les entites nouvelles ou
modifiees d'un snapshot applique sont re-criblees contre le referentiel
clients ; les hits ouvrent des alertes dedupliquees, la liste blanche est
respectee (compteur trace), et le lookback manuel est reserve aux admins.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    Base, Snapshot, WatchlistEntity, ClientEntity, Alert, AlertEvent, WhitelistPair
)
from fiskr.rescreen import rescreen_after_snapshot_change, RESCREEN_USERNAME


@pytest.fixture
def db(tmp_path):
    """Session SQLAlchemy isolee (SQLite temporaire) pour ne pas toucher la base de dev."""
    engine = create_engine(f"sqlite:///{tmp_path / 'rescreen_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "role": "admin", "roles": ["admin"]
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _seed_client_base(db):
    snap = Snapshot(snapshot_id=f"clients-{uuid.uuid4().hex[:6]}", file_type="CLIENT_BASE",
                    file_name="clients.csv", file_hash=uuid.uuid4().hex, record_count=1, status="READY")
    db.add(snap)
    db.add(ClientEntity(
        snapshot_id=snap.snapshot_id, client_id="CUST-777", client_type="PP",
        client_first_name="IGOR", client_last_name="PETROV", client_dob="1965-03-12",
        client_gender="M",
        client_countries={"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []},
        entity_checksum=uuid.uuid4().hex
    ))
    db.commit()
    return snap.snapshot_id


def _watchlist_entity(snap_id, entity_id, name, first, last):
    return WatchlistEntity(
        snapshot_id=snap_id, entity_id=entity_id, entity_type="I",
        primary_name=name,
        individual_name_parsed={"first_name": first, "last_name": last, "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=["1965-03-12"], is_deceased=False, gender="M",
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [], "jurisdiction_country": []},
        entity_checksum=f"chk-{entity_id}"
    )


def _seed_watchlist_snapshot(db, snap_id, entities):
    db.add(Snapshot(snapshot_id=snap_id, file_type="WATCHLIST_DGT", file_name=f"{snap_id}.json",
                    file_hash=uuid.uuid4().hex, record_count=len(entities), status="READY"))
    for e in entities:
        db.add(e)
    db.commit()


# ------------------ MOTEUR (BASE ISOLEE) ------------------

def test_rescreen_targets_changed_entities_only(db):
    _seed_client_base(db)
    # v1 : une entite sans rapport avec le client
    _seed_watchlist_snapshot(db, "wl-v1", [
        _watchlist_entity("wl-v1", "DGT-1", "Sofia MARQUEZ", "Sofia", "MARQUEZ")
    ])
    # v2 : l'entite inchangee (meme checksum) + une NOUVELLE entite qui matche le client
    _seed_watchlist_snapshot(db, "wl-v2", [
        _watchlist_entity("wl-v2", "DGT-1", "Sofia MARQUEZ", "Sofia", "MARQUEZ"),
        _watchlist_entity("wl-v2", "DGT-2", "Igor PETROV", "Igor", "PETROV"),
    ])

    result = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "wl-v2", "wl-v1")
    assert result["changed_entities"] == 1  # seule DGT-2 est nouvelle
    assert result["clients_screened"] == 1
    assert result["new_alerts"] == 1

    alert = db.query(Alert).filter(Alert.client_id == "CUST-777").first()
    assert alert is not None
    assert alert.watchlist_entity_id == "DGT-2"
    event = db.query(AlertEvent).filter(AlertEvent.alert_id == alert.id).first()
    assert event.username == RESCREEN_USERNAME
    assert "Re-criblage automatique" in event.detail

    # Rejouer le meme delta : dedup -> pas de nouvelle alerte (REDETECTED)
    result2 = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "wl-v2", "wl-v1")
    assert result2["new_alerts"] == 1  # re-detection comptee comme hit...
    assert db.query(Alert).filter(Alert.client_id == "CUST-777").count() == 1
    actions = [e.action for e in db.query(AlertEvent).order_by(AlertEvent.id).all()]
    assert actions == ["CREATED", "REDETECTED"]


def test_rescreen_respects_whitelist(db):
    _seed_client_base(db)
    db.add(WhitelistPair(client_id="CUST-777", watchlist_entity_id="DGT-2",
                         justification="FP avéré", created_by="reviseur1"))
    db.commit()
    _seed_watchlist_snapshot(db, "wl-only", [
        _watchlist_entity("wl-only", "DGT-2", "Igor PETROV", "Igor", "PETROV"),
    ])

    result = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "wl-only", None)
    assert result["whitelisted_suppressed"] == 1
    assert result["new_alerts"] == 0
    assert db.query(Alert).count() == 0


# ------------------ ENDPOINTS (APP REELLE) ------------------

def test_manual_lookback_permissions_and_run(client):
    # 'user' simple -> 403
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 2, "username": "u1", "role": "user", "roles": ["user"]
    }
    assert client.post("/api/rescreen/run", json={}).status_code == 403

    # admin : type inconnu -> 400 ; lookback valide -> compteurs
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "role": "admin", "roles": ["admin"]
    }
    assert client.post("/api/rescreen/run", json={"file_type": "INCONNU"}).status_code == 400
    response = client.post("/api/rescreen/run", json={})
    assert response.status_code == 200
    body = response.json()
    for key in ("changed_entities", "clients_screened", "new_alerts", "whitelisted_suppressed"):
        assert key in body


def test_sync_response_carries_rescreen_counts(client, monkeypatch):
    """Une sync manuelle appliquee declenche le re-criblage et expose les compteurs."""
    import json as jsonlib
    from pathlib import Path
    sample = {
        "Publications": {
            "DatePublication": "2026-07-15 12:00:00",
            "PublicationDetail": [{
                "IdRegistre": 990001 + hash(uuid.uuid4().hex) % 1000,
                "Nature": "Personne physique",
                "Nom": f"RESCREENOV{uuid.uuid4().hex[:4].upper()}",
                "RegistreDetail": [{"TypeChamp": "PRENOM", "Valeur": [{"Prenom": "Test"}]}]
            }]
        }
    }
    monkeypatch.setattr(
        "fiskr.sync.download_to_file",
        lambda url, dest, timeout=300.0: Path(dest).write_text(jsonlib.dumps(sample), encoding="utf-8")
    )
    response = client.post("/api/sync/run", json={"source": "DGT"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "SUCCESS"
    assert "rescreen" in body
    assert "clients_screened" in body["rescreen"]

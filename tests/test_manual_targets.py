"""
Ajout manuel vers une liste existante + lot, et solde des rapports de
synchronisation a la decision d'homologation :
- POST /api/watchlist/entity avec list_type : snapshot d'ajouts manuels dedie
  a la liste (manual-watchlist-<type>), entite indexee et criblable dans
  cette liste ; sans list_type : snapshot generique historique inchange ;
- POST /api/watchlist/entities/batch : quality gate ligne par ligne (les refus
  ne bloquent pas les autres), un seul snapshot cible, plafond ;
- les snapshots manuels ne sont JAMAIS remplacés par _supersede_previous_snapshots ;
- reject/approve d'un snapshot en homologation : le SyncReport lie sort de
  PENDING_REVIEW (REJECTED / SUCCESS).
"""
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app, _settle_sync_reports
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, WatchlistEntity, SyncReport
from fiskr.sync import _supersede_previous_snapshots


def _override_user(username: str, role: str = "admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


@pytest.fixture()
def client():
    _override_user("test_mt_admin", "admin,reviewer")
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)
    db = next(get_db())
    try:
        db.query(WatchlistEntity).filter(
            WatchlistEntity.primary_name.like("TestMT %")).delete(synchronize_session=False)
        db.query(SyncReport).filter(
            SyncReport.snapshot_id.like("test-mt-%")).delete(synchronize_session=False)
        db.query(Snapshot).filter(
            Snapshot.snapshot_id.like("test-mt-%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_manual_add_targets_chosen_list(client):
    created = client.post("/api/watchlist/entity", json={
        "list_type": "WATCHLIST_UN",
        "entity_type": "I",
        "primary_name": "TestMT Manual Un",
        "first_name": "TestMT", "last_name": "Manual Un",
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["list_type"] == "WATCHLIST_UN"
    assert body["snapshot_id"] == "manual-watchlist-un"

    db = next(get_db())
    try:
        snap = db.query(Snapshot).filter(
            Snapshot.snapshot_id == "manual-watchlist-un").first()
        assert snap is not None and snap.status == "READY" and snap.file_type == "WATCHLIST_UN"
        ent = db.query(WatchlistEntity).filter(
            WatchlistEntity.entity_id == body["entity_id"]).first()
        assert ent is not None and ent.snapshot_id == "manual-watchlist-un"
    finally:
        db.close()

    # Type de liste inconnu refuse
    assert client.post("/api/watchlist/entity", json={
        "list_type": "WATCHLIST_NIMPORTE", "entity_type": "I",
        "primary_name": "TestMT Refus"}).status_code == 400

    # Sans list_type : le snapshot generique historique est utilise
    legacy = client.post("/api/watchlist/entity", json={
        "entity_type": "E", "primary_name": "TestMT Legacy Corp"})
    assert legacy.status_code == 200 and legacy.json()["snapshot_id"] == "manual-watchlist"


def test_manual_batch_partial_quality_gate(client):
    payload = {
        "list_type": "WATCHLIST_OFSI",
        "entities": [
            {"entity_type": "I", "primary_name": "TestMT Batch Un",
             "first_name": "TestMT", "last_name": "Batch Un"},
            {"entity_type": "E", "primary_name": ""},        # refusee par le quality gate
            {"entity_type": "V", "primary_name": "TestMT Batch Vessel"},
        ],
    }
    response = client.post("/api/watchlist/entities/batch", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["added"]) == 2
    assert len(data["rejected"]) == 1 and data["rejected"][0]["index"] == 1
    assert data["snapshot_id"] == "manual-watchlist-ofsi"

    # Les deux inserees vivent dans le snapshot dedie
    db = next(get_db())
    try:
        count = db.query(WatchlistEntity).filter(
            WatchlistEntity.snapshot_id == "manual-watchlist-ofsi",
            WatchlistEntity.primary_name.like("TestMT Batch%")).count()
        assert count == 2
    finally:
        db.close()

    # Bornes : lot vide et plafond
    assert client.post("/api/watchlist/entities/batch",
                       json={"entities": []}).status_code == 400
    too_many = {"entities": [{"entity_type": "I", "primary_name": f"TestMT X{i}"}
                             for i in range(501)]}
    assert client.post("/api/watchlist/entities/batch", json=too_many).status_code == 400


def test_supersede_spares_all_manual_snapshots(client):
    # Un snapshot manuel dedie existe (cree par le test precedent ou ici)
    client.post("/api/watchlist/entity", json={
        "list_type": "WATCHLIST_UN", "entity_type": "I",
        "primary_name": "TestMT Spare Me", "first_name": "TestMT", "last_name": "Spare"})
    db = next(get_db())
    try:
        newer = Snapshot(snapshot_id=f"test-mt-{uuid.uuid4().hex[:8]}",
                         file_type="WATCHLIST_UN", file_name="test-mt.xml",
                         file_hash=uuid.uuid4().hex, record_count=0,
                         uploaded_at=datetime.utcnow(), status="READY")
        db.add(newer)
        db.commit()
        _supersede_previous_snapshots(db, "WATCHLIST_UN", newer.snapshot_id)
        db.commit()
        manual = db.query(Snapshot).filter(
            Snapshot.snapshot_id == "manual-watchlist-un").first()
        assert manual.status == "READY", "un snapshot manuel ne doit jamais être remplacé"
    finally:
        db.close()


def test_review_decision_settles_sync_report(client):
    sid = f"test-mt-{uuid.uuid4().hex[:8]}"
    db = next(get_db())
    try:
        db.add(Snapshot(snapshot_id=sid, file_type="WATCHLIST_UN",
                        file_name="test-mt-pending.xml", file_hash=uuid.uuid4().hex,
                        record_count=0, uploaded_at=datetime.utcnow(),
                        status="PENDING_REVIEW"))
        db.add(SyncReport(source="UN", trigger="MANUAL", status="PENDING_REVIEW",
                          message="test", snapshot_id=sid))
        db.commit()
    finally:
        db.close()

    rejected = client.post(f"/api/review/snapshots/{sid}/reject",
                           json={"comment": "test de solde du rapport"})
    assert rejected.status_code == 200, rejected.text

    db = next(get_db())
    try:
        report = db.query(SyncReport).filter(SyncReport.snapshot_id == sid).first()
        assert report.status == "REJECTED"

        # Voie approbation : le meme helper passe le rapport en SUCCESS
        report.status = "PENDING_REVIEW"
        db.commit()
        _settle_sync_reports(db, sid, "SUCCESS")
        db.commit()
        assert db.query(SyncReport).filter(
            SyncReport.snapshot_id == sid).first().status == "SUCCESS"
    finally:
        db.close()

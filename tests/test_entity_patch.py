"""
Tests du patch de valeurs d'une fiche listee (PATCH /api/watchlist/entity/{id}) :
- modification de champs avec journal des modifications (qui/quand/avant/apres),
  recalcul du checksum et rechargement immediat du cache de criblage ;
- option touch_official_reference_date : la date contenue dans la reference
  officielle est ramenee a la date du jour, dans son format d'origine ;
- gardes : reviewer/admin uniquement, fiches en production uniquement.
"""
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, WatchlistEntity, WatchlistEntityChange, AppSetting
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
        test_snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_patch_%")).all()
        snap_ids = [s.snapshot_id for s in test_snaps]
        if snap_ids:
            rows = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snap_ids)).all()
            pks = [r.id for r in rows]
            if pks:
                db.query(WatchlistEntityChange).filter(WatchlistEntityChange.entity_pk.in_(pks)).delete(synchronize_session=False)
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    # admin : peut regler l'ingestion (upload des fiches de test) ET passe la
    # garde require_reviewer du patch ; le test de role verifie le refus de 'user'
    _override_user("admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _upload_entity(client, name, official_reference=None):
    """Cree une fiche en production via l'import CSV (approval off) et retourne (pk, fiche)."""
    settings = client.put("/api/settings/ingestion", json={"require_approval": False})
    assert settings.status_code == 200
    file_name = f"test_patch_{uuid.uuid4().hex[:8]}.csv"
    ref_col = f',{official_reference}' if official_reference is not None else ","
    csv_content = (
        "entity_id,entity_type,primary_name,nationality,official_reference\n"
        f"EU-{uuid.uuid4().hex[:10]},I,{name},RU{ref_col}\n"
    )
    response = client.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_EU"},
        files={"file": (file_name, csv_content, "text/csv")},
    )
    assert response.status_code == 200, response.text
    found = client.get("/api/watchlist/db", params={"search": name}).json()
    assert found["total"] == 1
    item = found["items"][0]
    return item["id"], item


def _patch(client, pk, payload, expected=200):
    response = client.patch(f"/api/watchlist/entity/{pk}", json=payload)
    assert response.status_code == expected, response.text
    return response.json()


# ------------------ PATCH + JOURNAL + CHECKSUM + CACHE ------------------

def test_patch_field_journals_and_recomputes_checksum(client):
    marker = f"Patchov {uuid.uuid4().hex[:6]}"
    pk, item = _upload_entity(client, marker)
    old_checksum = item["entity_checksum"]

    data = _patch(client, pk, {"designation": "Ministre des essais", "city": "Moscou"})
    assert sorted(data["changed_fields"]) == ["city", "designation"]
    entity = data["entity"]
    assert entity["designation"] == "Ministre des essais"
    assert entity["modified_by"] == "testeur"
    assert entity["modified_at"] is not None
    assert entity["entity_checksum"] != old_checksum

    # Journal : une ligne par champ, ancienne -> nouvelle valeur
    changes = client.get(f"/api/watchlist/entity/{pk}/changes").json()["items"]
    by_field = {c["field"]: c for c in changes}
    assert by_field["designation"]["old_value"] is None
    assert by_field["designation"]["new_value"] == "Ministre des essais"
    assert by_field["designation"]["changed_by"] == "testeur"

    # Cache de criblage recharge immediatement avec les nouvelles valeurs
    cache = client.get("/api/watchlist").json()["items"]
    cached = next(e for e in cache if e["id"] == pk)
    assert cached["designation"] == "Ministre des essais"


def test_patch_identical_value_is_noop(client):
    pk, item = _upload_entity(client, f"Noopov {uuid.uuid4().hex[:6]}")
    data = _patch(client, pk, {"primary_name": item["primary_name"]})
    assert data["changed_fields"] == []
    assert client.get(f"/api/watchlist/entity/{pk}/changes").json()["items"] == []


def test_patch_structured_fields(client):
    pk, item = _upload_entity(client, f"Structov {uuid.uuid4().hex[:6]}")
    data = _patch(client, pk, {
        "dates_of_birth": ["1965-03-12"],
        "individual_name_parsed": {"first_name": "Igor", "last_name": "STRUCTOV", "maiden_name": ""},
    })
    assert set(data["changed_fields"]) == {"dates_of_birth", "individual_name_parsed"}
    assert data["entity"]["dates_of_birth"] == ["1965-03-12"]
    changes = client.get(f"/api/watchlist/entity/{pk}/changes").json()["items"]
    dob_change = next(c for c in changes if c["field"] == "dates_of_birth")
    assert "1965-03-12" in dob_change["new_value"]  # valeurs JSON-serialisees


# ------------------ DATE DE LA REFERENCE OFFICIELLE ------------------

def test_touch_official_reference_date_french_format(client):
    pk, _ = _upload_entity(client, f"Datefrov {uuid.uuid4().hex[:6]}",
                           official_reference="Règlement (UE) 2022/336 du 28/02/2022")
    data = _patch(client, pk, {"designation": "Test date FR", "touch_official_reference_date": True})
    assert data["official_reference_date_touched"] is True
    today_fr = datetime.utcnow().strftime("%d/%m/%Y")
    assert data["entity"]["official_reference"] == f"Règlement (UE) 2022/336 du {today_fr}"
    # Le remplacement de date est lui-meme journalise
    changes = client.get(f"/api/watchlist/entity/{pk}/changes").json()["items"]
    ref_change = next(c for c in changes if c["field"] == "official_reference")
    assert "28/02/2022" in ref_change["old_value"]
    assert today_fr in ref_change["new_value"]


def test_touch_official_reference_date_iso_format_last_date(client):
    # Deux dates dans la reference : c'est la DERNIERE (date de maj) qui est patchee
    pk, _ = _upload_entity(client, f"Dateisov {uuid.uuid4().hex[:6]}",
                           official_reference="QDi.430 (inscrit 2016-08-14) (maj 2023-05-15)")
    data = _patch(client, pk, {"touch_official_reference_date": True})
    assert data["official_reference_date_touched"] is True
    today_iso = datetime.utcnow().date().isoformat()
    assert data["entity"]["official_reference"] == f"QDi.430 (inscrit 2016-08-14) (maj {today_iso})"


def test_touch_without_date_is_noop(client):
    pk, _ = _upload_entity(client, f"Sansdatov {uuid.uuid4().hex[:6]}", official_reference="QDe.001")
    data = _patch(client, pk, {"designation": "Test sans date", "touch_official_reference_date": True})
    assert data["official_reference_date_touched"] is False
    assert data["entity"]["official_reference"] == "QDe.001"


def test_patched_reference_is_touchable_in_same_request(client):
    # La date est cherchee dans la valeur patchee, pas seulement l'existante
    pk, _ = _upload_entity(client, f"Combov {uuid.uuid4().hex[:6]}")
    data = _patch(client, pk, {
        "official_reference": "Décision 2026/100 du 01/01/2026",
        "touch_official_reference_date": True,
    })
    today_fr = datetime.utcnow().strftime("%d/%m/%Y")
    assert data["entity"]["official_reference"] == f"Décision 2026/100 du {today_fr}"


# ------------------ GARDES ------------------

def test_patch_requires_reviewer_role(client):
    pk, _ = _upload_entity(client, f"Rolov {uuid.uuid4().hex[:6]}")
    _override_user("user")
    assert client.patch(f"/api/watchlist/entity/{pk}", json={"city": "Paris"}).status_code == 403
    # Le role reviewer empile suffit (pas besoin d'admin)
    _override_user("reviewer,user")
    assert client.patch(f"/api/watchlist/entity/{pk}", json={"city": "Paris"}).status_code == 200
    _override_user("admin")


def test_patch_rejected_outside_production(client):
    # Fiche en attente d'homologation -> non modifiable (409)
    assert client.put("/api/settings/ingestion", json={"require_approval": True}).status_code == 200
    marker = f"Pendpatchov {uuid.uuid4().hex[:6]}"
    file_name = f"test_patch_{uuid.uuid4().hex[:8]}.csv"
    response = client.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_EU"},
        files={"file": (file_name, f"entity_id,entity_type,primary_name,nationality\nEU-{uuid.uuid4().hex[:10]},I,{marker},RU\n", "text/csv")},
    )
    assert response.status_code == 200
    pending = client.get("/api/watchlist/db", params={"search": marker, "scope": "PENDING_REVIEW"}).json()
    pk = pending["items"][0]["id"]
    _patch(client, pk, {"city": "Paris"}, expected=409)


def test_patch_validations(client):
    pk, _ = _upload_entity(client, f"Validov {uuid.uuid4().hex[:6]}")
    _patch(client, pk, {"primary_name": "   "}, expected=400)
    _patch(client, pk, {"entity_type": "X"}, expected=400)
    _patch(client, 99999999, {"city": "Paris"}, expected=404)

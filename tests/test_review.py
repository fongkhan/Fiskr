"""
Tests du mode homologation : environnement de validation avant production.

Couvre le toggle a chaud, le cycle PENDING_REVIEW -> READY|REJECTED, les
exclusions d'entites avec justification/piece jointe modulaires, et le
controle d'acces par roles empilables (reviewer).
"""
import io
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
from tests.conftest import post_and_wait

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
    """Supprime les reglages et les snapshots crees par les tests de revue."""
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_(ALL_SETTING_KEYS)).delete(synchronize_session=False)
        test_snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_review_%")).all()
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
    """CSV WATCHLIST_EU au contenu unique (entity_id aleatoires -> hash unique)."""
    rows = "\n".join(
        f"EU-{uuid.uuid4().hex[:10]},{etype},{name},RU" for etype, name in names
    )
    return f"entity_id,entity_type,primary_name,nationality\n{rows}\n"


def _upload_watchlist(client, names, file_name=None):
    file_name = file_name or f"test_review_{uuid.uuid4().hex[:8]}.csv"
    files = {"file": (file_name, _watchlist_csv(names), "text/csv")}
    response = client.post("/api/ingest", data={"file_type": "WATCHLIST_EU"}, files=files)
    assert response.status_code == 200, response.text
    return response.json()


def _set_approval(client, enabled: bool, **extra):
    payload = {"require_approval": enabled, **extra}
    response = client.put("/api/settings/ingestion", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _watchlist_names(client):
    """Noms presents dans le cache de criblage charge en memoire.

    `limit` est porte au maximum : l'endpoint n'en rend qu'un echantillon par
    defaut (il rendait auparavant le cache ENTIER — plus d'un giga-octet en
    production), et un « absent » conclu depuis un echantillon ne vaudrait
    rien. Le controle porte donc aussi sur `truncated`, qui dirait franchement
    que la reponse ne permet plus de conclure."""
    data = client.get("/api/watchlist?limit=1000").json()
    assert not data["truncated"], (
        f"{data['total']} fiches en cache : l'echantillon ne permet plus de "
        "conclure a l'absence d'un nom")
    return {item["primary_name"] for item in data["items"]}


# ------------------ REGLAGES A CHAUD ------------------

def test_ingestion_settings_default_and_toggle(client):
    # Defaut : mode inactif, source = config.yaml
    data = client.get("/api/settings/ingestion").json()
    assert data["require_approval"] is False
    assert data["sources"]["require_approval"] == "config"

    # PUT admin -> stocke en base, effet immediat
    updated = _set_approval(client, True)
    assert updated["require_approval"] is True
    data = client.get("/api/settings/ingestion").json()
    assert data["require_approval"] is True
    assert data["sources"]["require_approval"] == "database"

    _set_approval(client, False)


def test_ingestion_settings_forbidden_for_user(client):
    _override_user("user")
    response = client.put("/api/settings/ingestion", json={"require_approval": True})
    assert response.status_code == 403
    _override_user("admin")


# ------------------ CYCLE DE VIE PENDING_REVIEW ------------------

def test_staging_on_creates_pending_invisible_to_cache(client):
    _set_approval(client, True)
    result = _upload_watchlist(client, [("I", "Boris Pendingov"), ("E", "Pending Holding")])
    assert result["status"] == "PENDING_REVIEW"

    # Invisible du moteur de criblage
    assert "BORIS PENDINGOV" not in _watchlist_names(client)

    # Present dans la file d'homologation, avec delta calcule au detail
    pending = client.get("/api/review/pending").json()["pending"]
    assert any(p["snapshot_id"] == result["snapshot_id"] for p in pending)
    detail = client.get(f"/api/review/snapshots/{result['snapshot_id']}").json()
    assert detail["delta_summary"]["added_count"] == 2


def test_staging_off_goes_straight_to_ready(client):
    _set_approval(client, False)
    result = _upload_watchlist(client, [("I", "Direct Readykov")])
    assert result["status"] == "READY"
    assert "DIRECT READYKOV" in _watchlist_names(client)


def test_approve_promotes_supersedes_and_excludes(client):
    # Un premier snapshot en production
    _set_approval(client, False)
    first = _upload_watchlist(client, [("I", "Ancien Productionov")])

    # Un second en attente, dont une entite sera exclue avec justification
    _set_approval(client, True, exclusion_justification_required=True, exclusion_file_required=False)
    second = _upload_watchlist(client, [("I", "Nouveau Approuvov"), ("I", "Faux Positivov")])
    snap_id = second["snapshot_id"]

    entities = client.get(f"/api/review/snapshots/{snap_id}/entities").json()["items"]
    to_exclude = next(e for e in entities if "FAUX" in e["primary_name"])

    # Exclusion sans justification -> 400 (reglage actif)
    response = client.post(
        f"/api/review/snapshots/{snap_id}/exclusions",
        data={"entity_ids": json.dumps([to_exclude["id"]])},
    )
    assert response.status_code == 400

    # Exclusion justifiee -> OK
    response = client.post(
        f"/api/review/snapshots/{snap_id}/exclusions",
        data={"entity_ids": json.dumps([to_exclude["id"]]), "justification": "Faux positif avéré"},
    )
    assert response.status_code == 200, response.text

    # Approbation avec commentaire (202 : le re-criblage post-delta suit en fond)
    response = post_and_wait(client, f"/api/review/snapshots/{snap_id}/approve",
                             json={"comment": "Pointage conforme"})
    assert response.status_code == 202, response.text
    assert response.json()["excluded_count"] == 1

    db = next(get_db())
    try:
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
        assert snap.status == "READY"
        assert snap.reviewed_by == "testeur"
        assert snap.reviewed_at is not None
        assert snap.review_comment == "Pointage conforme"
        old = db.query(Snapshot).filter(Snapshot.snapshot_id == first["snapshot_id"]).first()
        assert old.status == "SUPERSEDED"
        excluded_row = db.query(WatchlistEntity).filter(WatchlistEntity.id == to_exclude["id"]).first()
        assert excluded_row.excluded is True
        assert excluded_row.exclusion_justification == "Faux positif avéré"
        assert excluded_row.excluded_by == "testeur"
    finally:
        db.close()

    # Cache de production : l'entite approuvee crible, l'exclue non
    names = _watchlist_names(client)
    assert "NOUVEAU APPROUVOV" in names
    assert "FAUX POSITIVOV" not in names
    assert "ANCIEN PRODUCTIONOV" not in names


def test_reject_path(client):
    _set_approval(client, True)
    result = _upload_watchlist(client, [("I", "Mauvais Rejetov")])
    snap_id = result["snapshot_id"]

    # Rejet sans commentaire -> 400
    response = client.post(f"/api/review/snapshots/{snap_id}/reject", json={})
    assert response.status_code == 400

    response = client.post(f"/api/review/snapshots/{snap_id}/reject", json={"comment": "Source non fiable"})
    assert response.status_code == 200
    assert "MAUVAIS REJETOV" not in _watchlist_names(client)

    # Approve sur un snapshot REJECTED -> 409
    response = client.post(f"/api/review/snapshots/{snap_id}/approve", json={"comment": "x"})
    assert response.status_code == 409


def test_pending_survives_mode_disable(client):
    _set_approval(client, True)
    result = _upload_watchlist(client, [("I", "Survivant Pendingov")])
    _set_approval(client, False)

    # Toujours en attente, et toujours approuvable
    pending = client.get("/api/review/pending").json()["pending"]
    assert any(p["snapshot_id"] == result["snapshot_id"] for p in pending)
    response = post_and_wait(client, f"/api/review/snapshots/{result['snapshot_id']}/approve",
                             json={"comment": None})
    assert response.status_code == 202


# ------------------ JUSTIFICATION MODULAIRE ------------------

def test_exclusion_file_requirement_and_evidence_download(client):
    _set_approval(client, True, exclusion_justification_required=False, exclusion_file_required=True)
    result = _upload_watchlist(client, [("I", "Piece Jointov")])
    snap_id = result["snapshot_id"]
    entity = client.get(f"/api/review/snapshots/{snap_id}/entities").json()["items"][0]

    # Sans piece jointe -> 400 (reglage actif)
    response = client.post(
        f"/api/review/snapshots/{snap_id}/exclusions",
        data={"entity_ids": json.dumps([entity["id"]])},
    )
    assert response.status_code == 400

    # Avec piece jointe -> OK, archivee et retelechargeable
    response = client.post(
        f"/api/review/snapshots/{snap_id}/exclusions",
        data={"entity_ids": json.dumps([entity["id"]])},
        files={"file": ("preuve_exclusion.pdf", io.BytesIO(b"%PDF-1.4 preuve"), "application/pdf")},
    )
    assert response.status_code == 200, response.text

    evidence = client.get(f"/api/review/exclusion-evidence/{entity['id']}")
    assert evidence.status_code == 200
    assert evidence.content.startswith(b"%PDF-1.4 preuve")


def test_exclusion_bare_when_requirements_disabled(client):
    _set_approval(client, True, exclusion_justification_required=False, exclusion_file_required=False)
    result = _upload_watchlist(client, [("I", "Nu Exclusov"), ("I", "Retour Integrov")])
    snap_id = result["snapshot_id"]
    entities = client.get(f"/api/review/snapshots/{snap_id}/entities").json()["items"]
    ids = [e["id"] for e in entities]

    # Exclusion nue acceptee (les deux reglages inactifs)
    response = client.post(
        f"/api/review/snapshots/{snap_id}/exclusions",
        data={"entity_ids": json.dumps(ids)},
    )
    assert response.status_code == 200

    # Reintegration : les champs de justification sont effaces
    response = client.post(
        f"/api/review/snapshots/{snap_id}/exclusions/remove",
        json={"entity_ids": ids},
    )
    assert response.status_code == 200
    entities = client.get(f"/api/review/snapshots/{snap_id}/entities").json()["items"]
    assert all(not e["excluded"] for e in entities)


def test_approve_blocked_if_requirements_hardened(client):
    # Exclusion nue posee quand rien n'est exige...
    _set_approval(client, True, exclusion_justification_required=False, exclusion_file_required=False)
    result = _upload_watchlist(client, [("I", "Durci Reglagov")])
    snap_id = result["snapshot_id"]
    entity = client.get(f"/api/review/snapshots/{snap_id}/entities").json()["items"][0]
    response = client.post(
        f"/api/review/snapshots/{snap_id}/exclusions",
        data={"entity_ids": json.dumps([entity["id"]])},
    )
    assert response.status_code == 200

    # ...puis le reglage durcit : l'approbation est bloquee (filet de securite)
    _set_approval(client, True, exclusion_justification_required=True)
    response = client.post(f"/api/review/snapshots/{snap_id}/approve", json={"comment": "x"})
    assert response.status_code == 400
    assert "justification" in response.json()["detail"].lower()


# ------------------ ROLES EMPILABLES ------------------

def test_review_role_enforcement(client):
    _set_approval(client, True)
    result = _upload_watchlist(client, [("I", "Role Testov")])
    snap_id = result["snapshot_id"]

    # 'user' seul : ni exclure ni approuver
    _override_user("user")
    assert client.post(
        f"/api/review/snapshots/{snap_id}/exclusions",
        data={"entity_ids": json.dumps([1])},
    ).status_code == 403
    assert client.post(f"/api/review/snapshots/{snap_id}/approve", json={}).status_code == 403

    # 'user,reviewer' (roles empiles) : peut approuver
    _override_user("user,reviewer")
    response = post_and_wait(client, f"/api/review/snapshots/{snap_id}/approve", json={"comment": "ok"})
    assert response.status_code == 202

    _override_user("admin")


def test_stacked_roles_admin_and_user_crud(client):
    # 'admin,reviewer' conserve les droits admin
    _override_user("admin,reviewer")
    assert client.get("/api/users").status_code == 200

    # Creation d'un compte multi-roles : forme canonique stockee
    username = f"test_review_user_{uuid.uuid4().hex[:6]}"
    response = client.post("/api/users", json={"username": username, "password": "Secret123456ABC", "role": "user,reviewer"})
    assert response.status_code == 200
    created = response.json()["user"]
    assert created["role"] == "reviewer,user"

    # Role inconnu -> 400
    response = client.post("/api/users", json={"username": username + "x", "password": "Secret123456ABC", "role": "superuser"})
    assert response.status_code == 400

    # Nettoyage du compte cree
    client.delete(f"/api/users/{created['id']}")
    _override_user("admin")


# ------------------ HOMOLOGATION GROUPÉE ------------------

def _pending_snapshot_of_type(file_type, name):
    """Snapshot en attente d'un AUTRE type de liste : le lot porte sur des
    listes différentes (le cas réel de la file du matin), et seul
    WATCHLIST_EU accepte un CSV générique à l'import."""
    snapshot_id = f"test-bulk-{uuid.uuid4().hex[:10]}"
    db = next(get_db())
    try:
        db.add(Snapshot(snapshot_id=snapshot_id, file_type=file_type,
                        file_name=f"{snapshot_id}.csv", file_hash=uuid.uuid4().hex,
                        record_count=1, status="PENDING_REVIEW"))
        db.add(WatchlistEntity(snapshot_id=snapshot_id, entity_id=f"{snapshot_id}-1",
                               entity_type="I", primary_name=name,
                               entity_checksum=uuid.uuid4().hex))
        db.commit()
    finally:
        db.close()
    return {"snapshot_id": snapshot_id}


def test_bulk_approve_promotes_every_selected_list(client):
    """Plusieurs listes mises en production en un geste : chacune promue,
    chacune avec son job de rechargement."""
    _set_approval(client, True)
    a = _upload_watchlist(client, [("I", "Groupe Unov")], file_name="bulk_a.csv")
    b = _pending_snapshot_of_type("WATCHLIST_UN", "GROUPE DEUXOV")

    response = post_and_wait(client, "/api/review/snapshots/approve-bulk",
                             json={"snapshot_ids": [a["snapshot_id"], b["snapshot_id"]],
                                   "comment": "Pointage groupé"})
    assert response.status_code == 202, response.text
    data = response.json()
    assert len(data["approved"]) == 2 and data["refused"] == []
    assert len(data["job_tokens"]) == 2

    db = next(get_db())
    try:
        for snapshot_id in (a["snapshot_id"], b["snapshot_id"]):
            snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snapshot_id).first()
            assert snap.status == "READY"
            assert snap.review_comment == "Pointage groupé"
    finally:
        db.close()


def test_bulk_approve_refuses_individually_without_stopping_the_batch(client):
    """Une liste refusée (ici : cahier de tests obligatoire et absent) ne doit
    PAS empêcher les autres de passer — sinon le lot serait inutilisable dès
    qu'une seule liste coince."""
    _set_approval(client, True)
    conforme = _upload_watchlist(client, [("I", "Groupe Conformov")], file_name="bulk_ok.csv")
    bloquee = _pending_snapshot_of_type("WATCHLIST_UN", "GROUPE BLOQUOV")

    # Le cahier de tests devient obligatoire : aucune des deux n'en a...
    assert client.put("/api/settings/ingestion", json={"backtest_required": True}).status_code == 200
    # ...on en fabrique un, au verdict OK, pour la seule liste conforme
    db = next(get_db())
    try:
        snap = db.query(Snapshot).filter(
            Snapshot.snapshot_id == conforme["snapshot_id"]).first()
        snap.backtest_report = {"verdict": "OK", "gap_pct": 0.0}
        db.commit()
    finally:
        db.close()

    response = post_and_wait(client, "/api/review/snapshots/approve-bulk",
                             json={"snapshot_ids": [conforme["snapshot_id"],
                                                    bloquee["snapshot_id"]]})
    assert response.status_code == 202, response.text
    data = response.json()
    assert [a["snapshot_id"] for a in data["approved"]] == [conforme["snapshot_id"]]
    assert len(data["refused"]) == 1
    assert data["refused"][0]["snapshot_id"] == bloquee["snapshot_id"]
    assert "cahier de tests" in data["refused"][0]["reason"].lower()

    db = next(get_db())
    try:
        assert db.query(Snapshot).filter(
            Snapshot.snapshot_id == bloquee["snapshot_id"]).first().status == "PENDING_REVIEW"
    finally:
        db.close()
    client.put("/api/settings/ingestion", json={"backtest_required": False})


def test_bulk_approve_rejects_an_empty_selection(client):
    assert client.post("/api/review/snapshots/approve-bulk",
                       json={"snapshot_ids": []}).status_code == 400


def test_pending_queue_carries_the_delta_overview(client):
    """La file d'attente porte le delta de chaque liste : c'est ce qui permet
    de décider en lot sans ouvrir les listes une par une."""
    _set_approval(client, True)
    result = _upload_watchlist(client, [("I", "Vue Ensemblov")], file_name="apercu.csv")
    ligne = next(p for p in client.get("/api/review/pending").json()["pending"]
                 if p["snapshot_id"] == result["snapshot_id"])
    assert "delta_available" in ligne and "is_first_import" in ligne
    assert "backtest_verdict" in ligne


def test_bulk_approve_of_the_same_list_keeps_only_the_latest(client):
    """Deux versions de la MÊME liste dans un même lot : la plus récemment
    approuvée l'emporte et supersede l'autre — la règle habituelle, qu'un lot
    ne doit pas contourner."""
    _set_approval(client, True)
    ancien = _upload_watchlist(client, [("I", "Version Unov")], file_name="v1.csv")
    recent = _upload_watchlist(client, [("I", "Version Deuxov")], file_name="v2.csv")

    response = post_and_wait(client, "/api/review/snapshots/approve-bulk",
                             json={"snapshot_ids": [ancien["snapshot_id"],
                                                    recent["snapshot_id"]]})
    assert response.status_code == 202, response.text
    db = next(get_db())
    try:
        statuts = {s: db.query(Snapshot).filter(Snapshot.snapshot_id == s).first().status
                   for s in (ancien["snapshot_id"], recent["snapshot_id"])}
    finally:
        db.close()
    assert statuts[recent["snapshot_id"]] == "READY"
    assert statuts[ancien["snapshot_id"]] == "SUPERSEDED"

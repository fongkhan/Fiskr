"""
Tests du lot Conformite+ :
- projet de declaration de soupcon TRACFIN pre-rempli (rubriques, garde de
  role, evenement STR_DRAFT_GENERATED, HTML imprimable avec bandeau) ;
- tableau de bord qualite des donnees clients (completude par champ,
  segments, fiches a risque) ;
- webhooks entrants (cle d'API obligatoire, signature HMAC optionnelle,
  idempotence rejouee, upsert client trace au journal admin).
"""
import hashlib
import hmac
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.config import config
from fiskr.database import (
    get_db, Snapshot, WatchlistEntity, ClientEntity, Alert, AlertEvent,
    AuditTrail, AdminAuditLog, HookDelivery, ApiKey,
)


def _override_user(role: str, username: str = "testeur"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_cfp_%")).all()
        ids = [s.snapshot_id for s in snaps]
        if ids:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
            db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(ids)).delete(synchronize_session=False)
        alerts = db.query(Alert).filter(Alert.client_id.like("test_cfp_%")).all()
        aids = [a.id for a in alerts]
        if aids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(aids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(aids)).delete(synchronize_session=False)
        db.query(HookDelivery).filter(HookDelivery.idempotency_key.like("test_cfp_%")).delete(synchronize_session=False)
        keys = db.query(ApiKey).filter(ApiKey.name.like("test_cfp_%")).all()
        for k in keys:
            db.delete(k)
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


def _upload_watchlist(client, name, dob="1960-05-05"):
    body = ("entity_id,entity_type,primary_name,nationality,dob\n"
            f"CFP-{uuid.uuid4().hex[:8]},I,{name},RU,{dob}\n")
    response = client.post(
        "/api/ingest", data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_cfp_wl_{uuid.uuid4().hex[:6]}.csv", body, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _upload_client_base(client, rows_csv):
    response = client.post(
        "/api/ingest", data={"file_type": "CLIENT_BASE"},
        files={"file": (f"test_cfp_cb_{uuid.uuid4().hex[:6]}.csv", rows_csv, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _make_alert(client, tag):
    """Cree une alerte reelle par criblage (watchlist dediee au test)."""
    listed_name = f"Tracfinov{tag}"
    _upload_watchlist(client, f"Igor {listed_name}")
    response = client.post("/api/screen", json={
        "client_id": f"test_cfp_{tag}", "client_type": "PP",
        "client_first_name": "Igor", "client_last_name": listed_name,
        "client_dob": "1960-05-05", "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [],
                             "birth_country": [], "registration_country": []},
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["best_match"]["status"] == "ALERT", data
    db = next(get_db())
    try:
        alert = db.query(Alert).filter(Alert.client_id == f"test_cfp_{tag}").first()
        assert alert is not None
        return alert.id
    finally:
        db.close()


# ------------------ PROJET DE DECLARATION TRACFIN ------------------

def test_str_draft_structure_and_trace(client):
    tag = uuid.uuid4().hex[:6].upper()
    # KYC en production pour la personne concernee
    _upload_client_base(client, (
        "client_id,client_type,client_first_name,client_last_name,client_dob,"
        "client_gender,nationality,client_segment,client_email\n"
        f"test_cfp_{tag},PP,Igor,Tracfinov{tag},1960-05-05,M,RU,corporate,igor@example.test\n"
    ))
    alert_id = _make_alert(client, tag)

    response = client.get(f"/api/alerts/{alert_id}/str-draft")
    assert response.status_code == 200, response.text
    draft = response.json()

    assert draft["type"] == "PROJET_DECLARATION_SOUPCON"
    assert "TRACFIN" in draft["avertissement"]
    # Rubrique declarant : issue de config.yaml institution
    assert "name" in draft["declarant"]
    # Personne concernee : KYC fusionne
    person = draft["personne_concernee"]
    assert person["reference_interne"] == f"test_cfp_{tag}"
    assert person.get("date_naissance") == "1960-05-05"
    assert person.get("email") == "igor@example.test"
    assert person.get("segment") == "corporate"
    # Personne listee + motifs traces
    assert f"Tracfinov{tag}".upper() in draft["personne_listee"]["nom"].upper()
    motifs = draft["motifs"]
    assert motifs["score_final"] is not None
    assert "seuil_applique" in motifs
    assert isinstance(draft["chronologie"], list) and draft["chronologie"]
    # Aucune rubrique operation en canal criblage
    assert draft["operation_concernee"] is None

    # Generation tracee dans l'historique append-only de l'alerte
    db = next(get_db())
    try:
        events = db.query(AlertEvent).filter(
            AlertEvent.alert_id == alert_id,
            AlertEvent.action == "STR_DRAFT_GENERATED").all()
        assert len(events) == 1
    finally:
        db.close()


def test_str_draft_print_has_banner_and_role_guard(client):
    tag = uuid.uuid4().hex[:6].upper()
    alert_id = _make_alert(client, tag)

    html = client.get(f"/api/alerts/{alert_id}/str-draft/print")
    assert html.status_code == 200
    body = html.text
    assert "Projet de déclaration de soupçon" in body
    assert "correspondant" in body  # bandeau de validation humaine
    assert "ERMES" in body
    assert "Chronologie du traitement" in body

    # Garde de role : un simple analyste n'y accede pas
    _override_user("user")
    assert client.get(f"/api/alerts/{alert_id}/str-draft").status_code == 403
    assert client.get(f"/api/alerts/{alert_id}/str-draft/print").status_code == 403
    _override_user("reviewer,user")
    assert client.get(f"/api/alerts/{alert_id}/str-draft").status_code == 200

    _override_user("admin")
    assert client.get("/api/alerts/99999999/str-draft").status_code == 404


# ------------------ QUALITE DES DONNEES CLIENTS ------------------

def test_client_data_quality_dashboard(client):
    tag = uuid.uuid4().hex[:6]
    # 4 fiches : 2 completes, 1 PP sans DOB ni prenom, 1 PM sans pays
    csv = (
        "client_id,client_type,client_first_name,client_last_name,client_company_name,"
        "client_dob,client_gender,nationality,client_segment,client_email\n"
        f"test_cfp_q1{tag},PP,Alice,Martin,,1980-01-01,F,FR,particulier,alice@example.test\n"
        f"test_cfp_q2{tag},PP,Bruno,Durand,,1975-06-15,M,FR,particulier,bruno@example.test\n"
        f"test_cfp_q3{tag},PP,,Sanschamp,,,M,FR,pme,\n"
        f"test_cfp_q4{tag},PM,,,SOCIETE HORIZON {tag},,U,,corporate,\n"
    )
    _upload_client_base(client, csv)

    response = client.get("/api/quality/clients")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["snapshot"] is not None
    assert data["snapshot"]["record_count"] == 4
    assert data["snapshot"]["pp_count"] == 3

    fields = {f["field"]: f for f in data["fields"]}
    # DOB : evaluee sur les 3 PP uniquement, 2 remplies
    assert fields["client_dob"]["total"] == 3
    assert fields["client_dob"]["filled"] == 2
    # Email : 2 sur 4
    assert fields["client_email"]["filled"] == 2
    assert fields["client_email"]["total"] == 4
    assert 0 <= data["global_score_pct"] <= 100

    # Fiches a risque pour le criblage
    risky = data["risky_records"]
    assert risky["dob_missing_pp"] == 1
    assert risky["pp_without_first_name"] == 1
    assert risky["country_missing"] == 1  # la PM sans aucun pays

    # Ventilation par segment (les pires en premier)
    segments = {s["segment"]: s for s in data["segments"]}
    assert segments["particulier"]["clients"] == 2
    assert segments["pme"]["clients"] == 1
    assert segments["pme"]["pct"] < segments["particulier"]["pct"]


# ------------------ WEBHOOKS ENTRANTS ------------------

def _create_api_key(client, role="user"):
    created = client.post("/api/apikeys", json={
        "name": f"test_cfp_hook_{uuid.uuid4().hex[:6]}", "role": role})
    assert created.status_code == 200, created.text
    return created.json()["api_key"]


def _screen_payload(tag):
    return {
        "client_id": f"test_cfp_hk{tag}", "client_type": "PP",
        "client_first_name": "Paul", "client_last_name": f"Tranquillov{tag}",
        "client_dob": "1985-09-09", "client_gender": "M",
        "client_countries": {"nationality": ["FR"], "residence": [],
                             "birth_country": [], "registration_country": []},
    }


def test_hook_screening_requires_api_key(client):
    # Session JWT (humaine) -> 403 explicite : reserve aux comptes de service
    response = client.post("/api/hooks/screening",
                           json=_screen_payload(uuid.uuid4().hex[:6]))
    assert response.status_code == 403
    assert "clé" in response.json()["detail"].lower() or "cle" in response.json()["detail"].lower()


def test_hook_screening_with_api_key_and_idempotency(client):
    tag = uuid.uuid4().hex[:6]
    _upload_watchlist(client, f"Hooklistov{tag}")  # au moins une liste en prod
    full_key = _create_api_key(client)
    payload = _screen_payload(tag)
    body = json.dumps(payload)
    idem = f"test_cfp_idem_{uuid.uuid4().hex[:8]}"

    saved_override = app.dependency_overrides.pop(get_current_user)
    try:
        first = client.post(
            "/api/hooks/screening", content=body,
            headers={"X-API-Key": full_key, "Content-Type": "application/json",
                     "X-Idempotency-Key": idem})
        assert first.status_code == 200, first.text
        data = first.json()
        # Meme contrat de reponse que POST /api/screen (best_match + audit)
        assert "best_match" in data
        assert "audit_trail_id" in data
        assert "X-Idempotency-Replayed" not in first.headers

        # Retransmission : reponse d'origine rejouee a l'identique, sans re-criblage
        db = next(get_db())
        try:
            audits_before = db.query(AuditTrail).count()
        finally:
            db.close()
        replay = client.post(
            "/api/hooks/screening", content=body,
            headers={"X-API-Key": full_key, "Content-Type": "application/json",
                     "X-Idempotency-Key": idem})
        assert replay.status_code == 200
        assert replay.headers.get("X-Idempotency-Replayed") == "true"
        assert replay.json() == data
        db = next(get_db())
        try:
            assert db.query(AuditTrail).count() == audits_before  # aucune nouvelle decision
        finally:
            db.close()

        # Charge utile invalide -> 422 explicite
        bad = client.post("/api/hooks/screening", content="{\"client_type\": 42}",
                          headers={"X-API-Key": full_key, "Content-Type": "application/json"})
        assert bad.status_code == 422
    finally:
        app.dependency_overrides[get_current_user] = saved_override


def test_hook_hmac_signature_enforced(client, monkeypatch):
    tag = uuid.uuid4().hex[:6]
    full_key = _create_api_key(client)
    monkeypatch.setitem(config, "hooks", {"secret": "s3cret-partage"})
    body = json.dumps(_screen_payload(tag))
    good_sig = hmac.new(b"s3cret-partage", body.encode("utf-8"), hashlib.sha256).hexdigest()

    saved_override = app.dependency_overrides.pop(get_current_user)
    try:
        # Sans signature -> 401
        missing = client.post("/api/hooks/screening", content=body,
                              headers={"X-API-Key": full_key, "Content-Type": "application/json"})
        assert missing.status_code == 401
        # Signature fausse -> 401
        wrong = client.post("/api/hooks/screening", content=body,
                            headers={"X-API-Key": full_key, "Content-Type": "application/json",
                                     "X-Fiskr-Signature": "deadbeef"})
        assert wrong.status_code == 401
        # Signature valide -> 200
        ok = client.post("/api/hooks/screening", content=body,
                         headers={"X-API-Key": full_key, "Content-Type": "application/json",
                                  "X-Fiskr-Signature": good_sig})
        assert ok.status_code == 200, ok.text
    finally:
        app.dependency_overrides[get_current_user] = saved_override


def test_hook_client_upsert_creates_updates_and_logs(client):
    tag = uuid.uuid4().hex[:6]
    # Referentiel en production dans lequel upserter
    base = _upload_client_base(client, (
        "client_id,client_type,client_first_name,client_last_name,client_dob,"
        "client_gender,nationality\n"
        f"test_cfp_up{tag},PP,Ancien,Nom{tag},1970-01-01,M,FR\n"
    ))
    full_key = _create_api_key(client)

    saved_override = app.dependency_overrides.pop(get_current_user)
    try:
        # Creation d'une nouvelle fiche
        created = client.post(
            "/api/hooks/client-upsert",
            content=json.dumps({"client_id": f"test_cfp_new{tag}", "client_type": "PP",
                                "client_first_name": "Nina", "client_last_name": f"Nouvelle{tag}",
                                "client_dob": "1992-03-03"}),
            headers={"X-API-Key": full_key, "Content-Type": "application/json"})
        assert created.status_code == 200, created.text
        assert created.json()["operation"] == "created"
        assert created.json()["snapshot_id"] == base["snapshot_id"]

        # Mise a jour de la fiche existante
        updated = client.post(
            "/api/hooks/client-upsert",
            content=json.dumps({"client_id": f"test_cfp_up{tag}", "client_type": "PP",
                                "client_first_name": "Ancien", "client_last_name": f"Nom{tag}",
                                "client_dob": "1970-01-01", "client_email": "maj@example.test"}),
            headers={"X-API-Key": full_key, "Content-Type": "application/json"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["operation"] == "updated"
        assert "client_email" in updated.json()["changed_fields"]
    finally:
        app.dependency_overrides[get_current_user] = saved_override

    db = next(get_db())
    try:
        # Fiche creee dans le snapshot en production, compteur incremente
        row = db.query(ClientEntity).filter(
            ClientEntity.snapshot_id == base["snapshot_id"],
            ClientEntity.client_id == f"test_cfp_new{tag}").first()
        assert row is not None and row.client_first_name == "Nina"
        maj = db.query(ClientEntity).filter(
            ClientEntity.snapshot_id == base["snapshot_id"],
            ClientEntity.client_id == f"test_cfp_up{tag}").first()
        assert maj.client_email == "maj@example.test"
        snap = db.query(Snapshot).filter(Snapshot.snapshot_id == base["snapshot_id"]).first()
        assert snap.record_count == 2
        # Trace au journal des actions d'administration
        logs = db.query(AdminAuditLog).filter(
            AdminAuditLog.action == "CLIENT_UPSERT_HOOK",
            AdminAuditLog.target.like(f"%{tag}")).all()
        assert len(logs) == 2
    finally:
        db.close()

"""
Tests du perimetre par type de liste :
- restriction du criblage a un sous-ensemble de listes (screening_lists),
  avec tracabilite obligatoire dans le decision_tree du journal d'audit ;
- persistance de list_type sur les alertes, le journal d'audit et la liste
  blanche, et filtres list_type des endpoints ;
- compteurs legers /api/counters (badges de la barre laterale).
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Base, Alert, AlertEvent, AuditTrail, WhitelistPair
from fiskr.transactions import parse_iso20022_payment, screen_payment_message
from fiskr.blocking import generate_blocking_keys
from fiskr.config import config


def _override_user(username: str, role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_scope_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(WhitelistPair).filter(WhitelistPair.client_id.like("test_scope_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("reviewer1", "reviewer,user")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_scope_{uuid.uuid4().hex[:8]}"


def _putin_payload(client_id, screening_lists=None):
    payload = {
        "client_id": client_id,
        "client_type": "PP",
        "client_first_name": "Vladimir",
        "client_last_name": "Putin",
        "client_dob": "1952-10-07",
        "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    }
    if screening_lists is not None:
        payload["screening_lists"] = screening_lists
    return payload


def _audit_tree(audit_id):
    db = next(get_db())
    try:
        row = db.query(AuditTrail).filter(AuditTrail.id == audit_id).first()
        return row.decision_tree, row.list_type
    finally:
        db.close()


# ------------------ RESTRICTION DU CRIBLAGE ------------------

def test_default_screening_is_unrestricted(client):
    data = client.post("/api/screen", json=_putin_payload(_new_client_id())).json()
    assert data["screening_lists"] == "ALL"
    assert data["best_match"]["status"] == "ALERT"
    tree, _ = _audit_tree(data["audit_trail_id"])
    assert tree["screening_lists_restriction"] == "ALL"


def test_restriction_excludes_non_selected_lists(client):
    # La seed Putin est en WATCHLIST_OFAC : restreindre a PEP => aucun candidat
    cid = _new_client_id()
    data = client.post("/api/screen", json=_putin_payload(cid, ["WATCHLIST_PEP"])).json()
    assert data["screening_lists"] == ["WATCHLIST_PEP"]
    assert data["candidates_count"] == 0
    assert data["alert_id"] is None
    # Tracabilite : la restriction est persistee dans le journal immuable
    tree, _ = _audit_tree(data["audit_trail_id"])
    assert tree["screening_lists_restriction"] == ["WATCHLIST_PEP"]


def test_restriction_to_matching_list_still_alerts(client):
    cid = _new_client_id()
    data = client.post("/api/screen", json=_putin_payload(cid, ["WATCHLIST_OFAC"])).json()
    assert data["best_match"]["status"] == "ALERT"
    assert data["alert_id"] is not None
    tree, audit_list_type = _audit_tree(data["audit_trail_id"])
    assert tree["screening_lists_restriction"] == ["WATCHLIST_OFAC"]
    assert audit_list_type == "WATCHLIST_OFAC"
    # L'evenement de creation de l'alerte mentionne la restriction
    detail = client.get(f"/api/alerts/{data['alert_id']}").json()
    assert any("Criblage restreint" in (e["detail"] or "") for e in detail["events"])


def test_unknown_screening_list_rejected(client):
    response = client.post("/api/screen", json=_putin_payload(_new_client_id(), ["WATCHLIST_NOPE"]))
    assert response.status_code == 400
    assert "WATCHLIST_NOPE" in response.json()["detail"]


# ------------------ PERSISTANCE & FILTRES list_type ------------------

def test_list_type_persisted_and_filterable(client):
    cid = _new_client_id()
    data = client.post("/api/screen", json=_putin_payload(cid)).json()
    alert_id = data["alert_id"]
    assert alert_id is not None

    # Alerte : list_type expose et filtrable
    detail = client.get(f"/api/alerts/{alert_id}").json()
    assert detail["list_type"] == "WATCHLIST_OFAC"

    filtered = client.get("/api/alerts", params={"list_type": "WATCHLIST_OFAC", "page_size": 200}).json()
    assert all(i["list_type"] == "WATCHLIST_OFAC" for i in filtered["items"])
    other = client.get("/api/alerts", params={"list_type": "WATCHLIST_PEP", "page_size": 200}).json()
    assert alert_id not in [i["id"] for i in other["items"]]

    # Journal d'audit : list_type expose et filtrable
    history = client.get("/api/history", params={"list_type": "WATCHLIST_OFAC", "page_size": 200}).json()
    assert data["audit_trail_id"] in [i["id"] for i in history["items"]]
    assert all(i["list_type"] == "WATCHLIST_OFAC" for i in history["items"])

    unknown = client.get("/api/history", params={"list_type": "UNKNOWN", "page_size": 10})
    assert unknown.status_code == 200
    assert {"total", "page", "page_size", "items"} <= set(unknown.json().keys())


def test_whitelist_pair_derives_list_type(client):
    cid = _new_client_id()
    data = client.post("/api/screen", json=_putin_payload(cid)).json()
    entity_id = data["best_match"]["watchlist_entity"]["entity_id"]

    created = client.post("/api/whitelist", data={
        "client_id": cid,
        "watchlist_entity_id": entity_id,
        "justification": "Faux positif avéré (test périmètre listes).",
    }).json()
    assert created["list_type"] == "WATCHLIST_OFAC"

    listed = client.get("/api/whitelist", params={"list_type": "WATCHLIST_OFAC", "page_size": 200}).json()
    assert created["id"] in [p["id"] for p in listed["items"]]
    excluded = client.get("/api/whitelist", params={"list_type": "WATCHLIST_PEP", "page_size": 200}).json()
    assert created["id"] not in [p["id"] for p in excluded["items"]]


# ------------------ COMPTEURS DE BADGES ------------------

def test_counters_endpoint(client):
    response = client.get("/api/counters")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["open_alerts"], int)
    assert isinstance(data["pending_reviews"], int)


# ------------------ RESTRICTION AU FILTRAGE TRANSACTIONNEL ------------------

PACS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
 <FIToFICstmrCdtTrf>
  <GrpHdr><MsgId>SCOPE-PACS-1</MsgId><CreDtTm>2026-07-16T11:00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
  <CdtTrfTxInf>
   <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>
   <IntrBkSttlmAmt Ccy="EUR">100</IntrBkSttlmAmt>
   <Dbtr><Nm>Vladimir PUTIN</Nm><PstlAdr><Ctry>RU</Ctry></PstlAdr></Dbtr>
   <Cdtr><Nm>John Doe</Nm><PstlAdr><Ctry>US</Ctry></PstlAdr></Cdtr>
  </CdtTrfTxInf>
 </FIToFICstmrCdtTrf>
</Document>
"""

PUTIN_ENTITY = {
    "entity_id": "SCOPE-PUTIN", "entity_type": "I", "primary_name": "VLADIMIR PUTIN",
    "individual_name_parsed": {"first_name": "Vladimir", "last_name": "PUTIN", "maiden_name": ""},
    "aliases": {"high_priority": [], "low_priority": []},
    "dates_of_birth": ["1952-10-07"], "gender": "M",
    "countries": {"citizenship": ["RU"], "residence": [], "birth_country": [], "jurisdiction_country": []},
    "_list_type": "WATCHLIST_DGT",
}


@pytest.fixture
def isolated_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scope_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _index_of(entities):
    index = {}
    for ent in entities:
        for key in generate_blocking_keys(ent, config):
            index.setdefault(key, []).append(ent)
    return index


def test_transaction_screening_restriction(isolated_db):
    parsed = parse_iso20022_payment(PACS_XML.encode("utf-8"))
    index = _index_of([PUTIN_ENTITY])

    # Sans restriction : le donneur d'ordre matche l'entite DGT -> HIT
    hit = screen_payment_message(isolated_db, parsed, index, "v", "h", "tester")
    assert hit["verdict"] == "HIT"
    assert hit["screening_lists"] == "ALL"

    # Restreint a une liste qui n'est pas en cause -> PASS, restriction tracee
    passed = screen_payment_message(isolated_db, parsed, index, "v", "h", "tester",
                                    screening_lists=["WATCHLIST_PEP"])
    assert passed["verdict"] == "PASS"
    assert passed["screening_lists"] == ["WATCHLIST_PEP"]
    for party in passed["parties"]:
        row = isolated_db.query(AuditTrail).filter(AuditTrail.id == party["audit_id"]).first()
        assert row.decision_tree["screening_lists_restriction"] == ["WATCHLIST_PEP"]


def test_transaction_endpoint_rejects_unknown_list(client):
    response = client.post(
        "/api/transactions/screen",
        data={"screening_lists": "WATCHLIST_NOPE"},
        files={"file": ("payment.xml", PACS_XML.encode("utf-8"), "application/xml")},
    )
    assert response.status_code == 400
    assert "WATCHLIST_NOPE" in response.json()["detail"]

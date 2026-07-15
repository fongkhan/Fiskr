"""
Tests des items P3 : filtrage transactionnel ISO 20022 (pain.001 / pacs.008),
recherche adverse media (RSS) et narratifs d'alertes fondes sur le decision_tree.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.config import config
from fiskr.database import Base, Alert
from fiskr.blocking import generate_blocking_keys
from fiskr.transactions import parse_iso20022_payment, screen_payment_message
from fiskr.adverse_media import build_google_news_query, parse_rss_items, search_adverse_media
from fiskr.narrative import compose_deterministic_narrative, generate_alert_narrative


# ------------------ FIXTURES XML ISO 20022 ------------------

PAIN_001_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09">
 <CstmrCdtTrfInitn>
  <GrpHdr>
   <MsgId>MSG-001</MsgId><CreDtTm>2026-07-15T10:00:00</CreDtTm>
   <NbOfTxs>1</NbOfTxs><CtrlSum>1000.00</CtrlSum>
   <InitgPty><Nm>ACME Corp</Nm></InitgPty>
  </GrpHdr>
  <PmtInf>
   <PmtInfId>PMT-1</PmtInfId>
   <Dbtr><Nm>ACME Corp</Nm><PstlAdr><Ctry>FR</Ctry><AdrLine>1 rue de la Paix</AdrLine><AdrLine>75002 Paris</AdrLine></PstlAdr></Dbtr>
   <DbtrAcct><Id><IBAN>FR7630006000011234567890189</IBAN></Id></DbtrAcct>
   <DbtrAgt><FinInstnId><BICFI>BNPAFRPP</BICFI><Nm>BNP Paribas</Nm></FinInstnId></DbtrAgt>
   <CdtTrfTxInf>
    <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>
    <Amt><InstdAmt Ccy="EUR">1000.00</InstdAmt></Amt>
    <CdtrAgt><FinInstnId><BICFI>DEUTDEFF</BICFI></FinInstnId></CdtrAgt>
    <Cdtr><Nm>Igor PETROV</Nm><PstlAdr><Ctry>RU</Ctry></PstlAdr></Cdtr>
    <UltmtCdtr><Nm>Volga Shipping LLC</Nm></UltmtCdtr>
    <RmtInf><Ustrd>Invoice 42</Ustrd></RmtInf>
   </CdtTrfTxInf>
  </PmtInf>
 </CstmrCdtTrfInitn>
</Document>
"""

PACS_008_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
 <FIToFICstmrCdtTrf>
  <GrpHdr><MsgId>PACS-001</MsgId><CreDtTm>2026-07-15T11:00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
  <CdtTrfTxInf>
   <PmtId><EndToEndId>E2E-9</EndToEndId><TxId>TX-9</TxId></PmtId>
   <IntrBkSttlmAmt Ccy="USD">5000</IntrBkSttlmAmt>
   <Dbtr>
    <Nm>Vladimir PUTIN</Nm>
    <Id><PrvtId><DtAndPlcOfBirth><BirthDt>1952-10-07</BirthDt><CtryOfBirth>RU</CtryOfBirth></DtAndPlcOfBirth></PrvtId></Id>
    <PstlAdr><Ctry>RU</Ctry></PstlAdr>
   </Dbtr>
   <DbtrAgt><FinInstnId><BICFI>SABRRUMM</BICFI></FinInstnId></DbtrAgt>
   <Cdtr><Nm>John Doe</Nm><PstlAdr><Ctry>US</Ctry></PstlAdr></Cdtr>
   <CdtrAgt><FinInstnId><BICFI>CHASUS33</BICFI></FinInstnId></CdtrAgt>
  </CdtTrfTxInf>
 </FIToFICstmrCdtTrf>
</Document>
"""


# ------------------ PARSEUR ISO 20022 ------------------

def test_parse_pain_001():
    parsed = parse_iso20022_payment(PAIN_001_XML.encode("utf-8"))
    assert parsed["message_type"] == "pain.001"
    assert parsed["msg_id"] == "MSG-001"
    assert len(parsed["transactions"]) == 1

    tx = parsed["transactions"][0]
    assert tx["end_to_end_id"] == "E2E-1"
    assert tx["amount"] == "1000.00"
    assert tx["currency"] == "EUR"
    assert tx["remittance"] == "Invoice 42"

    by_role = {p["role_tag"]: p for p in tx["parties"]}
    assert by_role["InitgPty"]["name"] == "ACME Corp"
    assert by_role["Dbtr"]["country"] == "FR"
    assert "1 rue de la Paix" in by_role["Dbtr"]["address"]
    assert by_role["Cdtr"]["name"] == "Igor PETROV"
    assert by_role["Cdtr"]["country"] == "RU"
    assert by_role["UltmtCdtr"]["name"] == "Volga Shipping LLC"
    # Agents financiers : BIC + pays deduit
    assert by_role["DbtrAgt"]["bic"] == "BNPAFRPP"
    assert by_role["DbtrAgt"]["name"] == "BNP Paribas"
    assert by_role["DbtrAgt"]["is_agent"] is True
    assert by_role["CdtrAgt"]["name"] == "DEUTDEFF"  # pas de Nm -> BIC
    assert by_role["CdtrAgt"]["country"] == "DE"     # pays deduit du BIC


def test_parse_pacs_008():
    parsed = parse_iso20022_payment(PACS_008_XML.encode("utf-8"))
    assert parsed["message_type"] == "pacs.008"
    assert parsed["msg_id"] == "PACS-001"
    tx = parsed["transactions"][0]
    assert tx["amount"] == "5000"
    assert tx["currency"] == "USD"

    by_role = {p["role_tag"]: p for p in tx["parties"]}
    dbtr = by_role["Dbtr"]
    assert dbtr["name"] == "Vladimir PUTIN"
    assert dbtr["birth_date"] == "1952-10-07"
    assert dbtr["birth_country"] == "RU"


def test_parse_rejects_unknown_message():
    with pytest.raises(ValueError):
        parse_iso20022_payment(b"<Document><Foo/></Document>")
    with pytest.raises(ValueError):
        parse_iso20022_payment(b"not xml at all")


# ------------------ CRIBLAGE DES PARTIES ------------------

@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'p3_test.sqlite3'}")
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


PUTIN_ENTITY = {
    "entity_id": "TEST-PUTIN", "entity_type": "I", "primary_name": "VLADIMIR PUTIN",
    "individual_name_parsed": {"first_name": "Vladimir", "last_name": "PUTIN", "maiden_name": ""},
    "aliases": {"high_priority": [], "low_priority": []},
    "dates_of_birth": ["1952-10-07"], "gender": "M",
    "countries": {"citizenship": ["RU"], "residence": [], "birth_country": [], "jurisdiction_country": []},
    "_list_type": "WATCHLIST_DGT",
}


def test_transaction_screening_hit_opens_alert(db):
    parsed = parse_iso20022_payment(PACS_008_XML.encode("utf-8"))
    result = screen_payment_message(db, parsed, _index_of([PUTIN_ENTITY]), "vtest", "htest", "tester")

    assert result["verdict"] == "HIT"
    assert result["hits_count"] == 1

    by_name = {p["name"]: p for p in result["parties"]}
    hit = by_name["Vladimir PUTIN"]
    assert hit["status"] == "ALERT"
    assert hit["final_score"] >= 75.0
    assert hit["best_watchlist_id"] == "TEST-PUTIN"
    assert hit["list_type"] == "WATCHLIST_DGT"
    assert hit["alert_id"] is not None
    assert hit["audit_id"] is not None

    # L'alerte de travail est bien ouverte, rattachee au message
    alert = db.query(Alert).filter(Alert.id == hit["alert_id"]).first()
    assert alert is not None
    assert alert.status == "OPEN"
    assert alert.client_id.startswith("TXN:PACS-001:")

    # Le beneficiaire inconnu ne matche pas
    assert by_name["John Doe"]["status"] == "NO_MATCH"
    assert by_name["John Doe"]["alert_id"] is None


def test_transaction_screening_pass(db):
    parsed = parse_iso20022_payment(PAIN_001_XML.encode("utf-8"))
    # Index sur une entite sans rapport phonetique avec les parties du message
    other = dict(PUTIN_ENTITY, entity_id="TEST-X", primary_name="XYLOPHONE ZZYZX",
                 individual_name_parsed={"first_name": "Xylophone", "last_name": "ZZYZX", "maiden_name": ""})
    result = screen_payment_message(db, parsed, _index_of([other]), "vtest", "htest", "tester")
    assert result["verdict"] == "PASS"
    assert result["hits_count"] == 0
    assert db.query(Alert).count() == 0


# ------------------ ADVERSE MEDIA ------------------

RSS_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
 <title>Google News</title>
 <item>
  <title>Enquête pour blanchiment visant Jean Dupont</title>
  <link>https://example.com/article-1</link>
  <pubDate>Mon, 13 Jul 2026 08:00:00 GMT</pubDate>
  <source url="https://presse.example.com">La Presse</source>
 </item>
 <item>
  <title>Jean Dupont mis en cause dans une affaire de corruption</title>
  <link>https://example.com/article-2</link>
  <pubDate>Sun, 12 Jul 2026 08:00:00 GMT</pubDate>
 </item>
 <item>
  <title>Troisième article</title>
  <link>https://example.com/article-3</link>
 </item>
</channel></rss>
"""


def test_build_google_news_query():
    query = build_google_news_query("Jean Dupont", ["blanchiment", "money laundering"])
    assert '"Jean Dupont"' in query
    assert "blanchiment" in query
    assert '"money laundering"' in query


def test_parse_rss_items_respects_max():
    articles = parse_rss_items(RSS_FIXTURE, max_results=2)
    assert len(articles) == 2
    assert articles[0]["title"] == "Enquête pour blanchiment visant Jean Dupont"
    assert articles[0]["link"] == "https://example.com/article-1"
    assert articles[0]["source"] == "La Presse"


def test_search_adverse_media_with_fetcher():
    captured = {}

    def fake_fetcher(url):
        captured["url"] = url
        return RSS_FIXTURE

    result = search_adverse_media("Jean Dupont", fetcher=fake_fetcher)
    assert result["name"] == "Jean Dupont"
    assert result["provider"] == "google_news_rss"
    assert len(result["articles"]) == 3
    assert "news.google.com/rss/search" in captured["url"]

    with pytest.raises(ValueError):
        search_adverse_media("   ", fetcher=fake_fetcher)


# ------------------ NARRATIFS D'ALERTES ------------------

def _fake_alert(**overrides):
    base = dict(
        id=42, created_at=datetime(2026, 7, 15, 9, 30), client_name="Vladimir Poutine",
        client_id="C1", watchlist_name="VLADIMIR PUTIN", watchlist_entity_id="TEST-PUTIN",
        final_score=95.0, status="IN_PROGRESS", assigned_to="analyste1",
        decided_by=None, decided_at=None, decision_comment=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_audit(tree):
    return SimpleNamespace(decision_tree=tree, watchlist_version="Snapshot 2026-07-15")


def test_deterministic_narrative_fuzzy():
    tree = {
        "base_score": 88.0, "final_score": 95.0, "hard_match_triggered": False,
        "best_client_name": "Vladimir Poutine", "best_watchlist_name": "VLADIMIR PUTIN",
        "adjustments": {
            "dob": {"score": 15.0, "description": "Correspondance exacte de la date de naissance"},
            "gender": {"score": 0.0, "description": "N/A"},
            "geography": {"score": 10.0, "description": "Pays en correspondance (RU)"},
        },
        "cut_off_applied": 75.0,
        "watchlist_entity": {"_list_type": "WATCHLIST_DGT"},
    }
    text = compose_deterministic_narrative(_fake_alert(), _fake_audit(tree), [])
    assert "PROJET DE NARRATIF — Alerte n°42" in text
    assert "95.0 %" in text
    assert "88.0 %" in text
    assert "WATCHLIST_DGT" in text
    assert "Correspondance exacte de la date de naissance" in text
    assert "Seuil réglementaire appliqué : 75 %" in text
    assert "dépasse" in text
    assert "validation 4-yeux" in text  # decision humaine obligatoire


def test_deterministic_narrative_hard_match_and_decision():
    tree = {
        "hard_match_triggered": True,
        "hard_match_details": "Hard Match Priorité 2 : Passeport identique (750123456 - RU)",
        "cut_off_applied": 75.0,
        "watchlist_entity": {},
    }
    events = [SimpleNamespace(action="REDETECTED", timestamp=datetime(2026, 7, 15, 10, 0))]
    alert = _fake_alert(status="CLOSED_CONFIRMED", final_score=100.0,
                        decided_by="reviewer1", decided_at=datetime(2026, 7, 15, 12, 0),
                        decision_comment="Identité confirmée par pièce.")
    text = compose_deterministic_narrative(alert, _fake_audit(tree), events)
    assert "Passeport identique" in text
    assert "verrouillé à 100 %" in text
    assert "re-détectée 1 fois" in text
    assert "close — vrai positif confirmé" in text
    assert "reviewer1" in text


def test_generate_narrative_llm_disabled_falls_back():
    # narrative.llm_enabled est false par defaut : composeur deterministe
    text, llm_used = generate_alert_narrative(_fake_alert(), _fake_audit({"cut_off_applied": 75.0}), [])
    assert llm_used is False
    assert "PROJET DE NARRATIF" in text


# ------------------ ENDPOINTS ------------------

@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "role": "admin", "roles": ["admin"]
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_transaction_endpoint_rejects_bad_xml(client):
    response = client.post(
        "/api/transactions/screen",
        files={"file": ("payment.xml", b"<Document><Foo/></Document>", "application/xml")},
    )
    assert response.status_code == 400


def test_transaction_endpoint_pass(client):
    xml = PAIN_001_XML.replace("Igor PETROV", "Zzyzx Qwortan") \
                      .replace("ACME Corp", "Blorptech Vexatron") \
                      .replace("Volga Shipping LLC", "Qwyjibo Holdings")
    response = client.post(
        "/api/transactions/screen",
        files={"file": ("payment.xml", xml.encode("utf-8"), "application/xml")},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["verdict"] == "PASS"
    assert data["message"]["message_type"] == "pain.001"
    assert len(data["parties"]) >= 4


def test_adverse_media_endpoint(client, monkeypatch):
    monkeypatch.setattr(
        "fiskr.api.search_adverse_media",
        lambda name: {"name": name, "provider": "google_news_rss", "query": "q", "articles": []},
    )
    response = client.get("/api/adverse-media", params={"name": "Jean Dupont"})
    assert response.status_code == 200
    assert response.json()["name"] == "Jean Dupont"

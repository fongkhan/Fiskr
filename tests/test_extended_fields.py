"""
Tests des champs de donnees etendus (26 colonnes listes + 14 colonnes clients) :
- nouvelles cles de hard match : BIC/SWIFT, tax ID, adresse crypto, MMSI, call sign ;
- filtrage ISO 20022 : agent bancaire au BIC sanctionne -> hard match ;
- extraction OFAC des features structurees (crypto, BIC, contacts, navire, PM) ;
- ingestion CSV watchlist et CLIENT_BASE avec les colonnes etendues ;
- PATCH d'un champ etendu journalise ; recherche par champ etendu.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, Base, Snapshot, WatchlistEntity, WatchlistEntityChange, ClientEntity, Alert,
    AppSetting,
)
from fiskr.settings import SETTING_REQUIRE_APPROVAL
from fiskr.ingest import parse_ofac_advanced_xml
from fiskr.scoring import check_hard_matches
from fiskr.transactions import parse_iso20022_payment, screen_payment_message


# ====================================================================
# 1. HARD MATCHES UNITAIRES (scoring.check_hard_matches)
# ====================================================================

def test_hard_match_bic_exact_and_branch_prefix():
    # BIC 8 identique
    matched, reason = check_hard_matches({"client_bic": "KRASRUMM"}, {"bic_swift": "KRASRUMM"})
    assert matched and "BIC" in reason

    # BIC 11 du client vs BIC 8 liste : meme banque, agence differente -> match
    matched, reason = check_hard_matches({"client_bic": "KRASRUMM123"}, {"bic_swift": "KRASRUMM"})
    assert matched and "BIC" in reason

    # Casse et espaces normalises
    matched, _ = check_hard_matches({"client_bic": " krasrumm "}, {"bic_swift": "KRASRUMM"})
    assert matched


def test_hard_match_bic_rejects_invalid_or_different():
    # Longueur invalide (ni 8 ni 11) -> pas de hard match
    matched, _ = check_hard_matches({"client_bic": "KRASRU"}, {"bic_swift": "KRASRU"})
    assert not matched
    # Banques differentes
    matched, _ = check_hard_matches({"client_bic": "BNPAFRPP"}, {"bic_swift": "KRASRUMM"})
    assert not matched
    # BIC absent d'un cote
    matched, _ = check_hard_matches({"client_bic": "KRASRUMM"}, {})
    assert not matched


def test_hard_match_tax_id_normalized():
    matched, reason = check_hard_matches(
        {"client_tax_id": "inn 77-1234-5678"}, {"tax_id": "INN7712345678"})
    assert matched and "fiscal" in reason.lower()

    matched, _ = check_hard_matches({"client_tax_id": "INN999"}, {"tax_id": "INN7712345678"})
    assert not matched


def test_hard_match_crypto_wallet():
    wl = {"crypto_wallets": [
        {"currency": "XBT", "address": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"},
        {"currency": "ETH", "address": "0x7F367cC41522cE07553e823bf3be79A889DEbe1B"},
    ]}
    # Liste d'adresses cote client
    matched, reason = check_hard_matches(
        {"client_crypto_wallets": ["0x7F367cC41522cE07553e823bf3be79A889DEbe1B"]}, wl)
    assert matched and "crypto" in reason.lower()

    # Adresse unique en chaine
    matched, _ = check_hard_matches(
        {"client_crypto_wallets": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"}, wl)
    assert matched

    # Adresse differente -> pas de match
    matched, _ = check_hard_matches({"client_crypto_wallets": ["bc1qautreadresse"]}, wl)
    assert not matched


def test_hard_match_vessel_mmsi_and_call_sign():
    matched, reason = check_hard_matches(
        {"transaction_vessel_mmsi": "273-456-789"}, {"vessel_mmsi": "273456789"})
    assert matched and "MMSI" in reason

    matched, reason = check_hard_matches(
        {"transaction_vessel_call_sign": "ubxy7"}, {"vessel_call_sign": "UBXY7"})
    assert matched and "indicatif radio" in reason.lower()

    matched, _ = check_hard_matches(
        {"transaction_vessel_mmsi": "111111111"}, {"vessel_mmsi": "273456789"})
    assert not matched


# ====================================================================
# 2. EXTRACTION OFAC : FEATURES STRUCTUREES
# ====================================================================

EXTENDED_SDN_XML = """<?xml version="1.0" encoding="utf-8"?>
<Sanctions xmlns="https://www.un.org/sanctions/1.0">
  <ReferenceValueSets>
    <PartyTypeValues>
      <PartyType ID="3">Entity</PartyType>
      <PartyType ID="5">Vessel</PartyType>
    </PartyTypeValues>
    <PartySubTypeValues>
      <PartySubType ID="2" PartyTypeID="3">Entity</PartySubType>
      <PartySubType ID="3" PartyTypeID="5">Vessel</PartySubType>
    </PartySubTypeValues>
    <NamePartTypeValues>
      <NamePartType ID="1525">Entity Name</NamePartType>
      <NamePartType ID="1526">Vessel Name</NamePartType>
    </NamePartTypeValues>
    <AliasTypeValues>
      <AliasType ID="1403">Name</AliasType>
    </AliasTypeValues>
    <FeatureTypeValues>
      <FeatureType ID="920">Digital Currency Address - XBT</FeatureType>
      <FeatureType ID="921">SWIFT/BIC</FeatureType>
      <FeatureType ID="922">Tax ID No.</FeatureType>
      <FeatureType ID="923">D-U-N-S Number</FeatureType>
      <FeatureType ID="924">Website</FeatureType>
      <FeatureType ID="925">Email Address</FeatureType>
      <FeatureType ID="926">Phone Number</FeatureType>
      <FeatureType ID="927">Secondary sanctions risk:</FeatureType>
      <FeatureType ID="928">Organization Established Date</FeatureType>
      <FeatureType ID="929">Organization Type:</FeatureType>
      <FeatureType ID="930">Flag</FeatureType>
      <FeatureType ID="931">MSI</FeatureType>
      <FeatureType ID="932">Vessel Call Sign</FeatureType>
      <FeatureType ID="933">Vessel Type</FeatureType>
    </FeatureTypeValues>
    <SanctionsTypeValues>
      <SanctionsType ID="1">Program</SanctionsType>
    </SanctionsTypeValues>
  </ReferenceValueSets>
  <DistinctParties>
    <DistinctParty FixedRef="9101">
      <Profile ID="9101" PartySubTypeID="2">
        <Identity ID="81" FixedRef="9101" Primary="true">
          <Alias FixedRef="9101" AliasTypeID="1403" Primary="true" LowQuality="false">
            <DocumentedName ID="74" FixedRef="9101" DocNameStatusID="1">
              <DocumentedNamePart><NamePartValue NamePartGroupID="841">KRASNY BANK</NamePartValue></DocumentedNamePart>
            </DocumentedName>
          </Alias>
          <NamePartGroups>
            <MasterNamePartGroup><NamePartGroup ID="841" NamePartTypeID="1525"/></MasterNamePartGroup>
          </NamePartGroups>
        </Identity>
        <Feature ID="90" FeatureTypeID="920">
          <FeatureVersion ID="190"><VersionDetail>bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="91" FeatureTypeID="921">
          <FeatureVersion ID="191"><VersionDetail>KRASRUMM</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="92" FeatureTypeID="922">
          <FeatureVersion ID="192"><VersionDetail>7712345678</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="93" FeatureTypeID="923">
          <FeatureVersion ID="193"><VersionDetail>359304566</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="94" FeatureTypeID="924">
          <FeatureVersion ID="194"><VersionDetail>https://krasnybank.example</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="95" FeatureTypeID="925">
          <FeatureVersion ID="195"><VersionDetail>contact@krasnybank.example</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="96" FeatureTypeID="926">
          <FeatureVersion ID="196"><VersionDetail>+7 495 123-45-67</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="97" FeatureTypeID="927">
          <FeatureVersion ID="197"><VersionDetail>Ukraine-/Russia-Related Sanctions Regulations</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="98" FeatureTypeID="928">
          <FeatureVersion ID="198">
            <DatePeriod><Start><From><Year>1994</Year><Month>7</Month><Day>5</Day></From></Start></DatePeriod>
          </FeatureVersion>
        </Feature>
        <Feature ID="99" FeatureTypeID="929">
          <FeatureVersion ID="199"><VersionDetail>Private Company</VersionDetail></FeatureVersion>
        </Feature>
      </Profile>
    </DistinctParty>
    <DistinctParty FixedRef="9102">
      <Profile ID="9102" PartySubTypeID="3">
        <Identity ID="82" FixedRef="9102" Primary="true">
          <Alias FixedRef="9102" AliasTypeID="1403" Primary="true" LowQuality="false">
            <DocumentedName ID="75" FixedRef="9102" DocNameStatusID="1">
              <DocumentedNamePart><NamePartValue NamePartGroupID="851">VOLGA STAR</NamePartValue></DocumentedNamePart>
            </DocumentedName>
          </Alias>
          <NamePartGroups>
            <MasterNamePartGroup><NamePartGroup ID="851" NamePartTypeID="1526"/></MasterNamePartGroup>
          </NamePartGroups>
        </Identity>
        <Feature ID="100" FeatureTypeID="930">
          <FeatureVersion ID="200"><VersionDetail>Russia</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="101" FeatureTypeID="931">
          <FeatureVersion ID="201"><VersionDetail>273456789</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="102" FeatureTypeID="932">
          <FeatureVersion ID="202"><VersionDetail>UBXY7</VersionDetail></FeatureVersion>
        </Feature>
        <Feature ID="103" FeatureTypeID="933">
          <FeatureVersion ID="203"><VersionDetail>Crude Oil Tanker</VersionDetail></FeatureVersion>
        </Feature>
      </Profile>
    </DistinctParty>
  </DistinctParties>
  <SanctionsEntries>
    <SanctionsEntry ID="810" ProfileID="9101" ListID="91">
      <SanctionsMeasure ID="811" SanctionsTypeID="1"><Comment>UKRAINE-EO13662</Comment></SanctionsMeasure>
      <SanctionsMeasure ID="812" SanctionsTypeID="1"><Comment>RUSSIA-EO14024</Comment></SanctionsMeasure>
    </SanctionsEntry>
  </SanctionsEntries>
</Sanctions>"""


def test_ofac_extended_features_extracted(tmp_path):
    xml_file = tmp_path / "sdn_extended.xml"
    xml_file.write_text(EXTENDED_SDN_XML, encoding="utf-8")
    entities = {e["entity_id"]: e for e in parse_ofac_advanced_xml(str(xml_file))}
    assert len(entities) == 2

    bank = entities["9101"]
    assert bank["entity_type"] == "E"
    # Crypto : devise extraite du suffixe du type de feature
    assert bank["crypto_wallets"] == [
        {"currency": "XBT", "address": "bc1qxy2kgdygjrsqtzq2n0yrf2493p83kkfjhx0wlh"}]
    assert bank["bic_swift"] == "KRASRUMM"
    assert bank["tax_id"] == "7712345678"
    assert bank["duns_number"] == "359304566"
    assert bank["websites"] == ["https://krasnybank.example"]
    assert bank["email_addresses"] == ["contact@krasnybank.example"]
    assert bank["phone_numbers"] == ["+7 495 123-45-67"]
    assert "Ukraine-/Russia-Related" in bank["secondary_sanctions_risk"]
    # Date de creation portee en DatePeriod (structure des fichiers reels)
    assert bank["organization_established_date"] == "1994-07-05"
    assert bank["organization_type"] == "Private Company"
    # Programmes de sanctions en liste structuree
    assert bank["sanction_programs"] == ["UKRAINE-EO13662", "RUSSIA-EO14024"]

    vessel = entities["9102"]
    assert vessel["entity_type"] == "V"
    assert vessel["vessel_flag"] == "Russia"
    assert vessel["vessel_mmsi"] == "273456789"
    assert vessel["vessel_call_sign"] == "UBXY7"
    assert vessel["vessel_type"] == "Crude Oil Tanker"


# ====================================================================
# 3. FILTRAGE ISO 20022 : AGENT BANCAIRE AU BIC SANCTIONNE
# ====================================================================

PACS_008_SANCTIONED_AGENT = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
 <FIToFICstmrCdtTrf>
  <GrpHdr><MsgId>PACS-BIC-1</MsgId><CreDtTm>2026-07-20T11:00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
  <CdtTrfTxInf>
   <PmtId><EndToEndId>E2E-77</EndToEndId></PmtId>
   <IntrBkSttlmAmt Ccy="USD">9000</IntrBkSttlmAmt>
   <Dbtr><Nm>Honest Trading GmbH</Nm><PstlAdr><Ctry>DE</Ctry></PstlAdr></Dbtr>
   <DbtrAgt><FinInstnId><BICFI>KRASRUMM123</BICFI><Nm>Krasny Bank</Nm></FinInstnId></DbtrAgt>
   <Cdtr><Nm>John Doe</Nm><PstlAdr><Ctry>US</Ctry></PstlAdr></Cdtr>
   <CdtrAgt><FinInstnId><BICFI>CHASUS33</BICFI></FinInstnId></CdtrAgt>
  </CdtTrfTxInf>
 </FIToFICstmrCdtTrf>
</Document>
"""

KRASNY_ENTITY = {
    "entity_id": "TEST-KRASNY", "entity_type": "E", "primary_name": "KRASNY BANK",
    "individual_name_parsed": {"first_name": "", "last_name": "", "maiden_name": ""},
    "aliases": {"high_priority": [], "low_priority": []},
    "dates_of_birth": [], "gender": "U",
    "countries": {"citizenship": [], "residence": [], "birth_country": [], "jurisdiction_country": ["RU"]},
    "bic_swift": "KRASRUMM",
    "_list_type": "WATCHLIST_OFAC",
}


@pytest.fixture
def iso_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ext_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_filtering_agent_bic_hard_match(iso_db):
    parsed = parse_iso20022_payment(PACS_008_SANCTIONED_AGENT.encode("utf-8"))
    index = {"KEY": [KRASNY_ENTITY]}
    result = screen_payment_message(iso_db, parsed, index, "vtest", "htest", "tester")

    assert result["verdict"] == "HIT"
    by_name = {p["name"]: p for p in result["parties"]}
    agent = by_name["Krasny Bank"]
    assert agent["is_agent"] is True
    assert agent["bic"] == "KRASRUMM123"
    assert agent["status"] == "ALERT"
    # C'est bien le BIC qui force le match (score 100, drapeau hard match)
    assert agent["hard_match"] is True
    assert agent["final_score"] == 100.0
    assert agent["best_watchlist_id"] == "TEST-KRASNY"

    alert = iso_db.query(Alert).filter(Alert.id == agent["alert_id"]).first()
    assert alert is not None and alert.channel == "FILTERING"

    # Le donneur d'ordre honnete ne matche pas
    assert by_name["Honest Trading GmbH"]["status"] == "NO_MATCH"


# ====================================================================
# 4. API : INGESTION, RECHERCHE, CRIBLAGE, PATCH, CLIENT_BASE
# ====================================================================

def _override_user(role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "testeur", "full_name": "Testeur", "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        # Ne pas leguer le reglage d'homologation aux tests suivants
        db.query(AppSetting).filter(AppSetting.key == SETTING_REQUIRE_APPROVAL).delete(synchronize_session=False)
        test_snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_extfields_%")).all()
        snap_ids = [s.snapshot_id for s in test_snaps]
        if snap_ids:
            rows = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snap_ids)).all()
            pks = [r.id for r in rows]
            if pks:
                db.query(WatchlistEntityChange).filter(WatchlistEntityChange.entity_pk.in_(pks)).delete(synchronize_session=False)
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
            db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
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


def _upload_extended_entity(client, name, **extended):
    """Fiche listee en production via CSV avec colonnes etendues."""
    assert client.put("/api/settings/ingestion", json={"require_approval": False}).status_code == 200
    cols = list(extended.keys())
    header = "entity_id,entity_type,primary_name,nationality," + ",".join(cols)
    values = f"EU-{uuid.uuid4().hex[:10]},E,{name},RU," + ",".join(extended[c] for c in cols)
    response = client.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_extfields_{uuid.uuid4().hex[:8]}.csv", header + "\n" + values + "\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    found = client.get("/api/watchlist/db", params={"search": name}).json()
    assert found["total"] == 1
    return found["items"][0]


def test_csv_ingestion_extended_columns_and_field_search(client):
    marker = f"Extbankov {uuid.uuid4().hex[:6]}"
    bic = f"EXT{uuid.uuid4().hex[:5].upper()}"
    wallet = f"bc1q{uuid.uuid4().hex}"
    item = _upload_extended_entity(
        client, marker,
        bic_swift=bic, tax_id="INN-556677", duns_number="123456789",
        crypto_wallets=wallet, sanction_programs="UKR; BLR", listed_on="2022-02-28",
    )
    # Colonnes texte de listes decoupees sur « ; », crypto normalise en objets
    assert item["bic_swift"] == bic
    assert item["tax_id"] == "INN-556677"
    assert item["sanction_programs"] == ["UKR", "BLR"]
    assert item["crypto_wallets"] == [{"currency": "", "address": wallet}]
    assert item["listed_on"] == "2022-02-28"

    # Recherche ciblee par champ etendu (scalaire et JSON)
    by_bic = client.get("/api/watchlist/db", params={"search": bic, "search_field": "bic_swift"}).json()
    assert by_bic["total"] == 1 and by_bic["items"][0]["id"] == item["id"]
    by_wallet = client.get("/api/watchlist/db", params={"search": wallet, "search_field": "crypto_wallets"}).json()
    assert by_wallet["total"] == 1 and by_wallet["items"][0]["id"] == item["id"]
    # Champ inconnu -> 400
    assert client.get("/api/watchlist/db", params={"search": "x", "search_field": "invente"}).status_code == 400


def test_screening_client_bic_hard_match(client):
    marker = f"Bicbankov {uuid.uuid4().hex[:6]}"
    bic = f"BIC{uuid.uuid4().hex[:5].upper()}"
    _upload_extended_entity(client, marker, bic_swift=bic)

    data = client.post("/api/screen", json={
        "client_id": f"test-ext-{uuid.uuid4().hex[:8]}", "client_type": "PM",
        "client_company_name": marker.upper(),
        "client_bic": bic,
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": ["RU"]},
    }).json()
    best = data["best_match"]
    assert best["status"] == "ALERT"
    assert best["hard_match_triggered"] is True
    assert "BIC" in best["hard_match_details"]
    assert best["final_score"] == 100.0


def test_screening_client_crypto_hard_match(client):
    marker = f"Cryptov {uuid.uuid4().hex[:6]}"
    wallet = f"bc1q{uuid.uuid4().hex}"
    _upload_extended_entity(client, marker, crypto_wallets=wallet)

    data = client.post("/api/screen", json={
        "client_id": f"test-ext-{uuid.uuid4().hex[:8]}", "client_type": "PM",
        "client_company_name": marker.upper(),
        "client_crypto_wallets": [wallet],
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": ["RU"]},
    }).json()
    best = data["best_match"]
    assert best["status"] == "ALERT"
    assert best["hard_match_triggered"] is True
    assert "crypto" in best["hard_match_details"].lower()


def test_patch_extended_fields_journaled(client):
    marker = f"Patchextov {uuid.uuid4().hex[:6]}"
    item = _upload_extended_entity(client, marker, bic_swift="OLDBICXX")

    response = client.patch(f"/api/watchlist/entity/{item['id']}", json={
        "bic_swift": "NEWBICXX",
        "vessel_mmsi": "273456789",
        "crypto_wallets": [{"currency": "XBT", "address": "bc1qpatchtest"}],
        "sanction_programs": ["UKR"],
    })
    assert response.status_code == 200, response.text
    data = response.json()
    assert sorted(data["changed_fields"]) == ["bic_swift", "crypto_wallets", "sanction_programs", "vessel_mmsi"]
    assert data["entity"]["bic_swift"] == "NEWBICXX"
    assert data["entity"]["crypto_wallets"] == [{"currency": "XBT", "address": "bc1qpatchtest"}]

    changes = client.get(f"/api/watchlist/entity/{item['id']}/changes").json()["items"]
    by_field = {c["field"]: c for c in changes}
    assert by_field["bic_swift"]["old_value"] == "OLDBICXX"
    assert by_field["bic_swift"]["new_value"] == "NEWBICXX"
    assert "bc1qpatchtest" in by_field["crypto_wallets"]["new_value"]


def test_client_base_extended_kyc_columns(client):
    marker = f"test_extfields_{uuid.uuid4().hex[:8]}"
    header = (
        "client_id,client_type,client_first_name,client_last_name,nationality,"
        "client_iban,client_bic,client_tax_id,client_phone,client_email,client_website,"
        "client_crypto_wallets,client_risk_rating,client_pep_flag,client_segment,"
        "client_activity_sector,client_activity_countries,client_relationship_start,client_status"
    )
    row = (
        f"CUST-EXT-1,PP,Igor,Extov,RU,"
        f"FR7630006000011234567890189,BNPAFRPP,FR-TAX-123,+33612345678,igor@example.org,https://extov.example,"
        f"bc1qwalleta; bc1qwalletb,high,oui,Retail,"
        f"Commerce de gros,\"FR, RU\",2019-05-01,active"
    )
    response = client.post(
        "/api/ingest",
        data={"file_type": "CLIENT_BASE"},
        files={"file": (f"{marker}.csv", header + "\n" + row + "\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    snap_id = response.json()["snapshot_id"]

    db = next(get_db())
    try:
        row_db = db.query(ClientEntity).filter(ClientEntity.snapshot_id == snap_id).first()
        assert row_db is not None
        assert row_db.client_iban == "FR7630006000011234567890189"
        assert row_db.client_bic == "BNPAFRPP"
        assert row_db.client_tax_id == "FR-TAX-123"
        assert row_db.client_phone == "+33612345678"
        assert row_db.client_email == "igor@example.org"
        assert row_db.client_website == "https://extov.example"
        assert row_db.client_crypto_wallets == ["bc1qwalleta", "bc1qwalletb"]
        assert row_db.client_risk_rating == "HIGH"
        assert row_db.client_pep_flag is True
        assert row_db.client_segment == "Retail"
        assert row_db.client_activity_sector == "Commerce de gros"
        assert row_db.client_activity_countries == ["FR", "RU"]
        assert row_db.client_relationship_start == "2019-05-01"
        assert row_db.client_status == "ACTIVE"
    finally:
        db.close()

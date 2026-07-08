import json
import pytest
from fastapi.testclient import TestClient

from fiskr.ssie import (
    parse_ssie_xml,
    discover_reference_types,
    merge_ssie_selectors,
    DEFAULT_SSIE_SELECTORS,
)
from fiskr.api import app
from fiskr.auth import get_current_user


# XML generique au format reference-croise (structure OFAC Advanced simplifiee)
MOCK_SSIE_XML = """<?xml version="1.0" encoding="utf-8"?>
<Sanctions xmlns="http://tempuri.org">
    <ReferenceValueList>
        <ReferenceValue ID="10">Gender</ReferenceValue>
        <ReferenceValue ID="11">Date of Birth</ReferenceValue>
        <ReferenceValue ID="12">Nationality</ReferenceValue>
        <ReferenceValue ID="13">Passport Number</ReferenceValue>
        <ReferenceValue ID="14">Website</ReferenceValue>
        <ReferenceValue ID="15">Vessel IMO Number</ReferenceValue>
        <ReferenceValue ID="16">Alias</ReferenceValue>
    </ReferenceValueList>
    <DistinctParty ID="777">
        <Name>Viktor Orlov</Name>
        <Feature FeatureTypeID="10">Male</Feature>
        <Feature FeatureTypeID="11">12/04/1960</Feature>
        <Feature FeatureTypeID="12">RU</Feature>
        <Feature FeatureTypeID="13">P1234567</Feature>
        <Feature FeatureTypeID="14">http://orlov-holdings.example</Feature>
        <Feature FeatureTypeID="16">Victor ORLOFF</Feature>
    </DistinctParty>
    <DistinctParty ID="778">
        <Name>KHATLON STAR</Name>
        <Feature FeatureTypeID="15">IMO 9741215</Feature>
    </DistinctParty>
</Sanctions>
"""

# Meme contenu logique mais nomenclature de balises differente (type SWIFT SLD)
MOCK_SSIE_XML_CUSTOM_TAGS = """<?xml version="1.0" encoding="utf-8"?>
<SanctionsExport>
    <CodeDictionary>
        <CodeEntry code="G1">Gender</CodeEntry>
        <CodeEntry code="N1">Nationality</CodeEntry>
    </CodeDictionary>
    <EntitiesList>
        <Listed code="SLD-001" Name="Maria Petrova">
            <Carac CaracTypeCode="G1">Female</Carac>
            <Carac CaracTypeCode="N1">BY</Carac>
        </Listed>
    </EntitiesList>
</SanctionsExport>
"""

CUSTOM_SELECTORS = {
    "reference_root_tag": ".//CodeDictionary",
    "reference_item_tag": "CodeEntry",
    "entity_root_tag": ".//Listed",
    "entity_feature_tag": "Carac",
    "mapping_id_attr": "code",
    "mapping_link_attr": "CaracTypeCode",
}


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "admin", "role": "admin"}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_ssie_reference_discovery(tmp_path):
    xml_file = tmp_path / "ssie_mock.xml"
    xml_file.write_text(MOCK_SSIE_XML, encoding="utf-8")

    references = discover_reference_types(str(xml_file), merge_ssie_selectors())

    assert references["10"] == "Gender"
    assert references["13"] == "Passport Number"
    assert len(references) == 7


def test_ssie_pipeline_default_selectors(tmp_path):
    xml_file = tmp_path / "ssie_mock.xml"
    xml_file.write_text(MOCK_SSIE_XML, encoding="utf-8")

    entities = list(parse_ssie_xml(str(xml_file)))
    assert len(entities) == 2

    individual = entities[0]
    assert individual["entity_id"] == "777"
    assert individual["entity_type"] == "I"
    assert individual["primary_name"] == "Viktor Orlov"
    assert individual["individual_name_parsed"]["first_name"] == "Viktor"
    assert individual["individual_name_parsed"]["last_name"] == "Orlov"
    assert individual["gender"] == "M"
    # Date normalisee DD/MM/YYYY -> YYYY-MM-DD
    assert individual["dates_of_birth"] == ["1960-04-12"]
    assert individual["countries"]["citizenship"] == ["RU"]
    assert individual["passport_documents"][0]["number"] == "P1234567"
    assert "Victor ORLOFF" in individual["aliases"]["high_priority"]
    # Caracteristique non mappee conservee dans additional_informations
    assert "Website: http://orlov-holdings.example" in individual["additional_informations"]
    assert individual["origin"] == "OFAC_ADVANCED_v1"

    vessel = entities[1]
    assert vessel["entity_id"] == "778"
    assert vessel["entity_type"] == "V"
    assert vessel["imo_number"] == "9741215"


def test_ssie_pipeline_custom_selectors(tmp_path):
    """L'agnosticisme structurel : memes donnees, nomenclature de balises differente."""
    xml_file = tmp_path / "ssie_custom.xml"
    xml_file.write_text(MOCK_SSIE_XML_CUSTOM_TAGS, encoding="utf-8")

    entities = list(parse_ssie_xml(str(xml_file), selectors=CUSTOM_SELECTORS, source_format="SWIFT_SLD_v1"))
    assert len(entities) == 1

    ent = entities[0]
    assert ent["entity_id"] == "SLD-001"
    assert ent["primary_name"] == "Maria Petrova"
    assert ent["entity_type"] == "I"
    assert ent["gender"] == "F"
    assert ent["countries"]["citizenship"] == ["BY"]
    assert ent["origin"] == "SWIFT_SLD_v1"


def test_merge_ssie_selectors_partial_override():
    merged = merge_ssie_selectors({"entity_root_tag": ".//Listed", "unknown_key": "ignored"})
    assert merged["entity_root_tag"] == ".//Listed"
    assert merged["reference_item_tag"] == DEFAULT_SSIE_SELECTORS["reference_item_tag"]
    assert "unknown_key" not in merged


def test_api_ingest_ssie_snapshot(client, tmp_path):
    """Import de bout en bout via /api/ingest avec selecteurs personnalises."""
    files = {"file": ("ssie_custom_import.xml", MOCK_SSIE_XML_CUSTOM_TAGS, "application/xml")}
    data = {
        "file_type": "WATCHLIST_SSIE",
        "ssie_selectors": json.dumps(CUSTOM_SELECTORS),
        "ssie_source_format": "SWIFT_SLD_v1",
    }
    response = client.post("/api/ingest", data=data, files=files)
    assert response.status_code == 200
    payload = response.json()
    assert payload["record_count"] == 1
    assert payload["snapshot_id"]

    # Le snapshot SSIE doit apparaitre dans l'historique
    snaps = client.get("/api/snapshots").json()
    ssie_snaps = [s for s in snaps if s["file_type"] == "WATCHLIST_SSIE"]
    assert any(s["snapshot_id"] == payload["snapshot_id"] and s["status"] == "READY" for s in ssie_snaps)


def test_api_ingest_ssie_invalid_selectors(client):
    files = {"file": ("ssie_bad.xml", MOCK_SSIE_XML, "application/xml")}
    data = {"file_type": "WATCHLIST_SSIE", "ssie_selectors": "{not valid json"}
    response = client.post("/api/ingest", data=data, files=files)
    assert response.status_code == 400
    assert "ssie_selectors" in response.json()["detail"]

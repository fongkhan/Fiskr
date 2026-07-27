"""
Connecteurs americains ajoutes : OFAC Non-SDN et Consolidated Screening List.

Ces deux sources comblent des trous distincts. Le fichier Non-SDN porte les
regimes SANS gel total des avoirs (sanctions sectorielles SSI en tete), absents
de la SDN. La CSL apporte le CONTROLE DES EXPORTATIONS (BIS, Departement
d'Etat), absent de toutes les autres sources branchees.

Reserve, comme pour SECO : l'environnement de developpement n'a pas d'acces
reseau, donc rien n'a pu etre confronte au fichier reel. Le connecteur Non-SDN
ne prend cependant aucun risque de format — il reutilise le parseur SDN, deja
teste contre la structure reelle dans tests/test_ofac_structure.py.
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, WatchlistEntity
from fiskr.ingest import (
    parse_ofac_advanced_xml, parse_ofac_consolidated_xml, parse_csl_json,
    CSL_DEFAULT_EXCLUDED_SOURCES,
)
from fiskr.sync import (
    run_ofac_nonsdn_sync, run_csl_sync, get_sync_config, _csl_source_config,
)
from fiskr.settings import SYNC_SOURCES


# Structure « Advanced » minimale, identique entre SDN et Non-SDN.
OFAC_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Sanctions xmlns="http://www.un.org/sanctions/1.0">
  <ReferenceValueSets>
    <PartySubTypeValues>
      <PartySubType ID="4" PartySubTypeID="4" PartyTypeID="1">Individual</PartySubType>
    </PartySubTypeValues>
  </ReferenceValueSets>
  <DistinctParties>
    <DistinctParty FixedRef="55511">
      <Profile ID="55511" PartySubTypeID="4">
        <Identity ID="90001" Primary="true">
          <Alias FixedRef="55511" AliasTypeID="1403" Primary="true" LowQuality="false">
            <DocumentedName ID="70001">
              <DocumentedNamePart>
                <NamePartValue NamePartGroupID="1">SECTORAL</NamePartValue>
              </DocumentedNamePart>
              <DocumentedNamePart>
                <NamePartValue NamePartGroupID="2">BANK OJSC</NamePartValue>
              </DocumentedNamePart>
            </DocumentedName>
          </Alias>
        </Identity>
      </Profile>
    </DistinctParty>
  </DistinctParties>
</Sanctions>
"""

CSL_PAYLOAD = {
    "sources_used": [{"source": "Entity List (EL) - Bureau of Industry and Security"}],
    "results": [
        {
            "id": "01c9a2b3",
            "source": "Entity List (EL) - Bureau of Industry and Security",
            "type": "Entity",
            "name": "SHENZHEN PRECISION OPTICS CO LTD",
            "alt_names": ["Shenzhen Precision Optics", "SPO Ltd"],
            "addresses": [
                {"address": "12 Nanshan Rd", "city": "Shenzhen", "state": "Guangdong",
                 "postal_code": "518000", "country": "China"},
                {"address": "Unit 4, Kwai Chung", "city": "Hong Kong", "country": "Hong Kong"},
            ],
            "programs": ["EAR"],
            "federal_register_notice": "85 FR 44159",
            "start_date": "2020-07-22",
            "license_requirement": "Presumption of denial",
            "license_policy": "Case-by-case",
            "source_list_url": "https://www.bis.doc.gov/entities",
            "remarks": "Acquisition of US-origin items for military end use.",
        },
        {
            "id": "77aa11",
            "source": "Denied Persons List (DPL) - Bureau of Industry and Security",
            "type": "Individual",
            "name": "John Archibald DOE",
            "alt_names": [],
            "dates_of_birth": ["circa 1971"],
            "places_of_birth": ["Houston, United States"],
            "citizenships": ["United States"],
            "ids": [
                {"type": "Passport", "number": "X9911223", "country": "United States",
                 "expiration_date": "2029-04-30"},
                {"type": "National ID No.", "number": "556677", "country": "United States"},
                {"type": "Docket Number", "number": "20-BIS-0042"},
            ],
            "start_date": "2021-03-11",
            "end_date": "2031-03-11",
            "title": "Managing director",
        },
        {
            "id": "vessel-1",
            "source": "Specially Designated Nationals (SDN) - Treasury Department",
            "type": "Vessel",
            "name": "MV DOUBLON",
            "call_sign": "V7AB2",
            "vessel_type": "Crude Oil Tanker",
            "vessel_flag": "Panama",
            "gross_tonnage": "58000",
        },
        {"id": "", "source": "Entity List (EL)", "type": "Entity", "name": "SANS IDENTIFIANT"},
    ],
}


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'us_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def csl_file(tmp_path):
    path = tmp_path / "csl.json"
    path.write_text(json.dumps(CSL_PAYLOAD), encoding="utf-8")
    return str(path)


def _fetcher(content):
    return lambda url, dest: Path(dest).write_text(content, encoding="utf-8")


# ------------------ OFAC NON-SDN ------------------

def test_nonsdn_reuses_the_sdn_parser_but_isolates_its_id_space(tmp_path):
    """
    Le format est identique : ce qui doit differer, c'est l'espace
    d'identifiants. `entity_id` sert de cle aux alertes et a la liste blanche ;
    une collision entre SDN et Non-SDN ferait fusionner deux fiches.
    """
    path = tmp_path / "cons.xml"
    path.write_text(OFAC_XML, encoding="utf-8")

    sdn = list(parse_ofac_advanced_xml(str(path)))
    nonsdn = list(parse_ofac_consolidated_xml(str(path)))
    assert len(sdn) == len(nonsdn) == 1

    assert str(sdn[0]["entity_id"]) == "55511"
    assert nonsdn[0]["entity_id"] == "NONSDN-55511"
    assert nonsdn[0]["origin"] == "OFAC Non-SDN Consolidated"
    # Tout le reste est rigoureusement le meme travail d'extraction
    assert nonsdn[0]["primary_name"] == sdn[0]["primary_name"] == "SECTORAL BANK OJSC"


def test_nonsdn_sync_lifecycle(db):
    report = run_ofac_nonsdn_sync(db, fetcher=_fetcher(OFAC_XML))
    assert report.status == "SUCCESS"
    assert report.source == "OFACNONSDN"
    assert report.added_count == 1

    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_OFAC_NONSDN"
    listed = db.query(WatchlistEntity).first()
    assert listed.entity_id == "NONSDN-55511"

    assert run_ofac_nonsdn_sync(db, fetcher=_fetcher(OFAC_XML)).status == "NO_CHANGE"


def test_nonsdn_is_off_by_default_and_schedulable():
    assert get_sync_config()["ofac_nonsdn"]["enabled"] is False
    assert "ofac_nonsdn" in SYNC_SOURCES


# ------------------ CONSOLIDATED SCREENING LIST ------------------

def test_csl_skips_the_lists_already_fetched_at_their_source(csl_file):
    """
    L'agregat contient la SDN, que Fiskr recupere deja aupres de l'OFAC :
    la charger ici doublerait chaque alerte.
    """
    entities = {e["entity_id"]: e for e in parse_csl_json(csl_file)}
    assert "CSL-vessel-1" not in entities          # SDN ecartee par defaut
    assert set(entities) == {"CSL-01c9a2b3", "CSL-77aa11"}

    # Exclusion explicitement vide = tout charger, navire SDN compris
    everything = {e["entity_id"] for e in parse_csl_json(csl_file, excluded_sources=())}
    assert "CSL-vessel-1" in everything

    # Filtre libre : on peut aussi ecarter une liste BIS
    without_bis = {e["entity_id"] for e in parse_csl_json(
        csl_file, excluded_sources=("Bureau of Industry",))}
    assert without_bis == {"CSL-vessel-1"}


def test_csl_maps_an_export_control_entity(csl_file):
    optics = {e["entity_id"]: e for e in parse_csl_json(csl_file)}["CSL-01c9a2b3"]
    assert optics["entity_type"] == "E"
    assert optics["primary_name"] == "SHENZHEN PRECISION OPTICS CO LTD"
    assert optics["aliases"]["high_priority"] == ["Shenzhen Precision Optics", "SPO Ltd"]
    assert optics["address"].startswith("12 Nanshan Rd")
    assert optics["alternative_addresses"] == ["Unit 4, Kwai Chung, Hong Kong, Hong Kong"]
    assert optics["city"] == "Shenzhen"
    assert optics["sanction_programs"] == ["EAR"]
    assert optics["listed_on"] == "2020-07-22"
    assert optics["designating_state"] == "US"
    # La liste qui designe est le « pourquoi » de l'alerte : elle est conservee
    assert optics["designation_reasons"].startswith("Entity List (EL)")
    assert optics["official_reference"] == "85 FR 44159 (maj 2020-07-22)"
    # Le contexte reglementaire propre au controle des exportations est garde
    assert "Presumption of denial" in optics["additional_informations"]
    assert "bis.doc.gov" in optics["additional_informations"]


def test_csl_maps_an_individual_with_partial_date_and_documents(csl_file):
    doe = {e["entity_id"]: e for e in parse_csl_json(csl_file)}["CSL-77aa11"]
    assert doe["entity_type"] == "I"
    # « circa 1971 » n'est pas une date ISO : l'annee est retenue, au 1er janvier
    assert doe["dates_of_birth"] == ["1971-01-01"]
    assert doe["place_of_birth"] == "Houston, United States"
    assert doe["countries"]["citizenship"] == ["US"]
    assert doe["passport_documents"] == [
        {"number": "X9911223", "issuing_country": "US", "expiration_date": "2029-04-30"}
    ]
    assert doe["national_id_documents"] == [{"number": "556677", "issuing_country": "US"}]
    assert doe["other_registration_ids"] == [
        {"id_type": "docket number", "number": "20-BIS-0042"}
    ]
    assert doe["delisted_on"] == "2031-03-11"


def test_csl_maps_vessel_attributes_when_the_source_is_kept(csl_file):
    vessel = {e["entity_id"]: e for e in
              parse_csl_json(csl_file, excluded_sources=())}["CSL-vessel-1"]
    assert vessel["entity_type"] == "V"
    assert vessel["vessel_call_sign"] == "V7AB2"
    assert vessel["vessel_flag"] == "Panama"
    assert vessel["vessel_tonnage"] == "58000"


def test_csl_ignores_rows_without_a_usable_key(csl_file):
    """Une fiche sans identifiant n'a pas de cle de delta : elle est ecartee."""
    names = [e["primary_name"] for e in parse_csl_json(csl_file, excluded_sources=())]
    assert "SANS IDENTIFIANT" not in names


def test_csl_tolerates_missing_and_oddly_typed_keys(tmp_path):
    """Robustesse de format : aucune cle n'est supposee presente."""
    path = tmp_path / "maigre.json"
    path.write_text(json.dumps({"results": [
        {"id": "x1", "name": "MINIMAL CORP"},
        {"id": "x2", "name": "CHAINES", "alt_names": "A; B",
         "programs": "EAR; ITAR", "addresses": ["Une adresse en clair"]},
        "pas un objet",
    ]}), encoding="utf-8")
    entities = {e["entity_id"]: e for e in parse_csl_json(str(path), excluded_sources=())}
    assert set(entities) == {"CSL-x1", "CSL-x2"}
    assert entities["CSL-x1"]["entity_type"] == "E"        # type absent -> entite
    assert entities["CSL-x1"]["dates_of_birth"] == []
    assert entities["CSL-x2"]["aliases"]["high_priority"] == ["A", "B"]
    assert entities["CSL-x2"]["sanction_programs"] == ["EAR", "ITAR"]
    assert entities["CSL-x2"]["address"] == "Une adresse en clair"


def test_csl_exclusion_config_distinguishes_absent_from_empty():
    """`exclude_sources` absent reprend le defaut ; explicitement vide = tout."""
    assert _csl_source_config({})["exclude_sources"] == tuple(CSL_DEFAULT_EXCLUDED_SOURCES)
    assert _csl_source_config({"exclude_sources": []})["exclude_sources"] == ()
    assert _csl_source_config({"exclude_sources": "A; B"})["exclude_sources"] == ("A", "B")
    assert _csl_source_config({})["enabled"] is False
    assert "csl" in SYNC_SOURCES


def test_csl_sync_lifecycle_applies_the_configured_exclusions(db):
    payload = json.dumps(CSL_PAYLOAD)
    report = run_csl_sync(db, fetcher=_fetcher(payload))
    assert report.status == "SUCCESS"
    assert report.source == "CSL"
    # 4 fiches dans le fichier : une SDN ecartee, une sans identifiant ecartee
    assert report.added_count == 2

    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_CSL"
    optics = db.query(WatchlistEntity).filter(
        WatchlistEntity.entity_id == "CSL-01c9a2b3").first()
    assert optics is not None
    # Les colonnes etendues arrivent bien jusqu'en base par la synchronisation
    assert optics.official_reference == "85 FR 44159 (maj 2020-07-22)"
    assert optics.sanction_programs == ["EAR"]
    assert optics.designating_state == "US"

    assert run_csl_sync(db, fetcher=_fetcher(payload)).status == "NO_CHANGE"


# ------------------ BOUT EN BOUT ------------------

@pytest.fixture
def api():
    from fastapi.testclient import TestClient
    from fiskr.api import app
    from fiskr.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin_us", "full_name": "admin_us",
        "role": "admin", "roles": ["admin"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_end_to_end_csl_upload_then_screen(api):
    """Import par l'API reelle puis criblage : la touche porte la liste CSL."""
    import uuid
    marker = uuid.uuid4().hex[:6].upper()
    payload = json.loads(json.dumps(CSL_PAYLOAD))
    payload["results"][0]["name"] = f"SHENZHEN PRECISION OPTICS {marker}"
    payload["results"][0]["id"] = f"csl-{marker}"

    response = api.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_CSL"},
        files={"file": (f"csl_{marker}.json", json.dumps(payload), "application/json")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["record_count"] == 2

    result = api.post("/api/screen", json={
        "client_id": f"test_csl_{marker}",
        "client_type": "PM",
        "client_company_name": f"SHENZHEN PRECISION OPTICS {marker}",
        "client_countries": {"nationality": [], "residence": [],
                             "birth_country": [], "registration_country": ["CN"]},
        "screening_lists": ["WATCHLIST_CSL"],
    })
    assert result.status_code == 200, result.text
    best = result.json()["best_match"]
    assert best is not None, result.json()
    assert best["status"] == "ALERT"
    assert best["watchlist_entity"]["_list_type"] == "WATCHLIST_CSL"
    assert best["watchlist_entity"]["entity_id"] == f"CSL-csl-{marker}"

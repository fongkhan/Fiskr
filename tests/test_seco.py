"""
Connecteur SECO (liste consolidee suisse).

Deux voies produisent le meme schema pivot : l'export officiel SESAM de la
Confederation (XML) et le jeu `ch_seco_sanctions` agrege par OpenSanctions
(targets.simple.csv). Les tests couvrent les deux, plus le cycle de
synchronisation et le transport des champs etendus jusqu'en base.

Reserve assumee : le parseur XML est ecrit d'apres le schema publie et valide
sur ce jeu d'essai ; il n'a pas pu etre confronte au fichier reel (acces reseau
ferme). Ces tests verrouillent donc le CONTRAT du parseur, ce qui est ce qu'ils
peuvent verrouiller.
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, WatchlistEntity
from fiskr.ingest import parse_seco_xml, parse_seco_opensanctions_csv
from fiskr.sync import run_seco_sync, get_sync_config, _seco_source_config
from fiskr.settings import SYNC_SOURCES


SECO_XML = """<?xml version="1.0" encoding="UTF-8"?>
<export xmlns="http://www.seco.admin.ch/sanctions" date="2026-07-20">
  <sanctions-program>
    <program-key>UKR</program-key>
    <sanctions-set ssid="1">
      <version-date>2026-06-30</version-date>
      <origin>EU</origin>
    </sanctions-set>
    <sanctions-set-name lang="deu">Verordnung ueber Massnahmen (SR 946.231.176.72)</sanctions-set-name>
    <sanctions-set-name lang="fra">Ordonnance instituant des mesures en lien avec la situation en Ukraine (RS 946.231.176.72)</sanctions-set-name>
  </sanctions-program>
  <sanctions-program>
    <program-key>SOM</program-key>
    <sanctions-set ssid="2"><origin>UN</origin></sanctions-set>
    <sanctions-set-name lang="fra">Ordonnance Somalie (RS 946.231.169.4)</sanctions-set-name>
  </sanctions-program>

  <target ssid="1000" sanctions-set-id="1">
    <individual ssid="1001">
      <identity ssid="1002" main="true">
        <name name-type="primary-name" quality="good">
          <name-part name-part-type="family-name"><value>IVANOV</value></name-part>
          <name-part name-part-type="given-name"><value>Ivan</value></name-part>
          <name-part name-part-type="title"><value>Colonel</value></name-part>
        </name>
        <day-month-year year="1970" month="3" day="12"/>
        <place-of-birth>
          <location>Moscou</location>
          <country iso-code="RU">Russie</country>
        </place-of-birth>
        <nationality><country iso-code="RU">Russie</country></nationality>
        <address>
          <address-details>Rue Tverskaia 12</address-details>
          <zip-code>101000</zip-code>
          <location>Moscou</location>
          <country iso-code="RU">Russie</country>
        </address>
        <identification-document document-type="passport">
          <number>75 1234567</number>
          <issuer code="RU">Russie</issuer>
          <expiry-date><day-month-year year="2030" month="12" day="31"/></expiry-date>
        </identification-document>
        <gender>M</gender>
      </identity>
      <identity ssid="1003" main="false">
        <name name-type="alias" quality="low">
          <name-part name-part-type="whole-name"><value>Vanya Ivanoff</value></name-part>
        </name>
      </identity>
      <justification lang="fra">Soutien materiel a des actions compromettant l'integrite territoriale.</justification>
      <other-information lang="fra">Ancien vice-ministre.</other-information>
      <modification modification-type="added" effective-date="2022-03-04" publication-date="2022-03-04"/>
      <modification modification-type="modified" effective-date="2024-11-18"/>
    </individual>
  </target>

  <target ssid="2000" sanctions-set-id="2">
    <entity ssid="2001">
      <identity ssid="2002" main="true">
        <name name-type="primary-name">
          <name-part name-part-type="whole-name"><value>ALPHA TRADING LLC</value></name-part>
        </name>
        <address>
          <address-details>Airport Road 4</address-details>
          <location>Mogadiscio</location>
          <country iso-code="SO">Somalie</country>
        </address>
        <address>
          <address-details>Boite postale 77</address-details>
          <location>Dubai</location>
          <country iso-code="AE">Emirats arabes unis</country>
        </address>
        <identification-document document-type="commercial-register">
          <number>CH-660.1.234.567-8</number>
        </identification-document>
      </identity>
      <identity ssid="2003" main="false">
        <name name-type="alias"><name-part name-part-type="whole-name"><value>Alpha Trading Ltd</value></name-part></name>
      </identity>
      <modification modification-type="added" effective-date="2019-05-02"/>
    </entity>
  </target>

  <target ssid="3000" sanctions-set-id="1">
    <object ssid="3001" object-type="vessel">
      <identity ssid="3002" main="true">
        <name name-type="primary-name"><name-part name-part-type="whole-name"><value>MV NORTHERN STAR</value></name-part></name>
      </identity>
      <modification modification-type="added" effective-date="2023-02-25"/>
    </object>
  </target>

  <target ssid="4000" sanctions-set-id="1">
    <individual ssid="4001">
      <identity ssid="4002" main="true">
        <name name-type="primary-name">
          <name-part name-part-type="family-name"><value>PETROVA</value></name-part>
          <name-part name-part-type="given-name"><value>Olga</value></name-part>
        </name>
        <day-month-year year="1985"/>
        <identification-document document-type="id-card">
          <number>AB998877</number>
          <issuer code="BY">Belarus</issuer>
          <expiry-date><day-month-year year="2028" month="6" day="1"/></expiry-date>
        </identification-document>
        <gender>W</gender>
      </identity>
    </individual>
  </target>
</export>
"""

SECO_OS_CSV = (
    "id,schema,name,aliases,birth_date,countries,addresses,identifiers,sanctions,dataset\n"
    "ch-seco-1,Person,Ivan Ivanov,Vanya Ivanoff,1970-03-12,ru,\"Moscou, Russie\",P75123,UKR,ch_seco_sanctions\n"
    "ch-seco-2,Company,Alpha Trading LLC,Alpha Trading Ltd,,so,Mogadiscio,,SOM,ch_seco_sanctions\n"
)


@pytest.fixture
def seco_file(tmp_path):
    path = tmp_path / "seco.xml"
    path.write_text(SECO_XML, encoding="utf-8")
    return str(path)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'seco_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _fetcher(content):
    return lambda url, dest: Path(dest).write_text(content, encoding="utf-8")


# ------------------ PARSEUR XML OFFICIEL (SESAM) ------------------

def test_seco_xml_maps_the_three_target_kinds(seco_file):
    entities = {e["entity_id"]: e for e in parse_seco_xml(seco_file)}
    assert set(entities) == {"SECO-1000", "SECO-2000", "SECO-3000", "SECO-4000"}
    assert entities["SECO-1000"]["entity_type"] == "I"
    assert entities["SECO-2000"]["entity_type"] == "E"
    # <object object-type="vessel"> devient un navire, pas une entite generique
    assert entities["SECO-3000"]["entity_type"] == "V"


def test_seco_xml_composes_name_and_keeps_document_order_as_alias(seco_file):
    ivanov = {e["entity_id"]: e for e in parse_seco_xml(seco_file)}["SECO-1000"]
    # Le fichier ecrit le patronyme en premier ; le moteur attend l'ordre inverse
    assert ivanov["primary_name"] == "Ivan IVANOV"
    assert ivanov["individual_name_parsed"] == {
        "first_name": "Ivan", "last_name": "IVANOV", "maiden_name": ""
    }
    # ...et la graphie du document reste cherchable comme alias
    assert "IVANOV Ivan" in ivanov["aliases"]["high_priority"]
    # Le titre n'entre pas dans le nom, il part dans sa propre colonne
    assert "Colonel" not in ivanov["primary_name"]
    assert ivanov["title"] == "Colonel"
    # quality="low" degrade l'alias en priorite basse
    assert ivanov["aliases"]["low_priority"] == ["Vanya Ivanoff"]


def test_seco_xml_does_not_confuse_document_validity_with_birth_date(seco_file):
    """La date d'expiration d'un passeport n'est pas une date de naissance."""
    entities = {e["entity_id"]: e for e in parse_seco_xml(seco_file)}
    assert entities["SECO-1000"]["dates_of_birth"] == ["1970-03-12"]
    assert entities["SECO-1000"]["passport_documents"] == [
        {"number": "75 1234567", "issuing_country": "RU", "expiration_date": "2030-12-31"}
    ]
    # Annee seule -> 1er janvier ; l'expiration 2028 reste hors des dates de naissance
    assert entities["SECO-4000"]["dates_of_birth"] == ["1985-01-01"]
    assert entities["SECO-4000"]["national_id_documents"] == [
        {"number": "AB998877", "issuing_country": "BY"}
    ]


def test_seco_xml_resolves_legal_basis_and_listing_dates(seco_file):
    entities = {e["entity_id"]: e for e in parse_seco_xml(seco_file)}
    ivanov = entities["SECO-1000"]
    # Base legale suisse resolue depuis le bloc <sanctions-program> (1re passe)
    assert ivanov["sanction_programs"] == ["UKR"]
    assert "RS 946.231.176.72" in ivanov["official_reference"]
    # La reference porte la DERNIERE date d'acte, l'inscription la PREMIERE
    assert ivanov["official_reference"].endswith("(maj 2024-11-18)")
    assert ivanov["listed_on"] == "2022-03-04"
    # La Suisse transpose : l'autorite d'origine est conservee
    assert ivanov["designating_state"] == "EU"
    assert entities["SECO-2000"]["designating_state"] == "UN"
    assert entities["SECO-2000"]["sanction_programs"] == ["SOM"]
    # Aucun bloc <modification> : pas de date inventee
    assert entities["SECO-4000"]["listed_on"] is None


def test_seco_xml_extracts_geography_and_documents(seco_file):
    entities = {e["entity_id"]: e for e in parse_seco_xml(seco_file)}
    ivanov = entities["SECO-1000"]
    assert ivanov["countries"]["citizenship"] == ["RU"]
    assert ivanov["countries"]["birth_country"] == ["RU"]
    assert ivanov["place_of_birth"] == "Moscou, Russie"
    assert ivanov["city"] == "Moscou"
    assert ivanov["gender"] == "M"
    # « W » (weiblich) de la version allemande du fichier
    assert entities["SECO-4000"]["gender"] == "F"

    alpha = entities["SECO-2000"]
    assert alpha["address"].startswith("Airport Road 4")
    assert alpha["alternative_addresses"] == ["Boite postale 77, Dubai, Emirats arabes unis"]
    assert sorted(alpha["countries"]["jurisdiction_country"]) == ["AE", "SO"]
    assert alpha["other_registration_ids"] == [
        {"id_type": "commercial-register", "number": "CH-660.1.234.567-8"}
    ]


def test_seco_xml_ignores_targets_without_a_usable_name(tmp_path):
    path = tmp_path / "vide.xml"
    path.write_text(
        '<?xml version="1.0"?><export>'
        '<target ssid="9000"><individual ssid="9001">'
        '<identity main="true"><name name-type="primary-name"/></identity>'
        '</individual></target></export>',
        encoding="utf-8"
    )
    assert list(parse_seco_xml(str(path))) == []


# ------------------ VOIE OPENSANCTIONS ------------------

def test_seco_opensanctions_csv_uses_its_own_namespace(tmp_path):
    """Meme lecteur que le dataset PEP, mais prefixe et provenance distincts :
    une fiche SECO ne doit jamais se confondre avec une fiche PEP."""
    path = tmp_path / "seco.csv"
    path.write_text(SECO_OS_CSV, encoding="utf-8")
    entities = {e["entity_id"]: e for e in parse_seco_opensanctions_csv(str(path))}
    assert set(entities) == {"SECO-ch-seco-1", "SECO-ch-seco-2"}
    ivanov = entities["SECO-ch-seco-1"]
    assert ivanov["primary_name"] == "Ivan Ivanov"
    assert ivanov["entity_type"] == "I"
    assert ivanov["dates_of_birth"] == ["1970-03-12"]
    assert ivanov["origin"] == "OpenSanctions SECO (CH)"
    assert ivanov["designation_reasons"] == "Sanctions suisses (SECO)"
    assert entities["SECO-ch-seco-2"]["entity_type"] == "E"


def test_pep_parser_is_unchanged_by_the_shared_reader(tmp_path):
    """Non-regression : la generalisation du lecteur ne deplace pas le PEP."""
    from fiskr.ingest import parse_pep_targets_csv
    path = tmp_path / "pep.csv"
    path.write_text(
        "id,schema,name,aliases,birth_date,countries,addresses,identifiers,sanctions,dataset\n"
        "os-1,Person,Jean Dupont,,1960-05-01,fr,Paris,,Maire,peps\n",
        encoding="utf-8"
    )
    entity = list(parse_pep_targets_csv(str(path)))[0]
    assert entity["entity_id"] == "PEP-os-1"
    assert entity["origin"] == "OpenSanctions PEP"
    assert entity["designation_reasons"] == "Personne Politiquement Exposée (PEP)"


# ------------------ CONFIGURATION ------------------

def test_seco_format_selects_the_matching_default_url():
    """Basculer de format suffit a changer de source : l'URL suit."""
    xml_cfg = _seco_source_config({"enabled": True})
    assert xml_cfg["format"] == "xml" and "sesam.search.admin.ch" in xml_cfg["url"]

    os_cfg = _seco_source_config({"enabled": True, "format": "opensanctions"})
    assert "ch_seco_sanctions" in os_cfg["url"]

    # Une URL explicite prime toujours, et un format inconnu retombe sur XML
    pinned = _seco_source_config({"format": "n-importe-quoi", "url": "https://exemple/x.xml"})
    assert pinned["format"] == "xml" and pinned["url"] == "https://exemple/x.xml"


def test_seco_is_off_by_default_and_schedulable():
    assert get_sync_config()["seco"]["enabled"] is False
    assert "seco" in SYNC_SOURCES


# ------------------ CYCLE DE SYNCHRONISATION ------------------

def test_seco_sync_lifecycle(db):
    report = run_seco_sync(db, fetcher=_fetcher(SECO_XML))
    assert report.status == "SUCCESS"
    assert report.source == "SECO"
    assert report.added_count == 4

    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_SECO"
    assert snap.record_count == 4

    # Deduplication par empreinte : le meme fichier ne recree pas de snapshot
    assert run_seco_sync(db, fetcher=_fetcher(SECO_XML)).status == "NO_CHANGE"


def test_seco_opensanctions_sync_uses_the_flat_reader(db, monkeypatch):
    """Le meme runner change de lecteur quand `format` bascule."""
    import fiskr.sync as sync_module

    real = sync_module.get_sync_config

    def as_opensanctions():
        cfg = real()
        cfg["seco"] = {"enabled": True, "format": "opensanctions", "url": "https://exemple/x.csv"}
        return cfg

    monkeypatch.setattr(sync_module, "get_sync_config", as_opensanctions)
    report = run_seco_sync(db, fetcher=_fetcher(SECO_OS_CSV))
    assert report.status == "SUCCESS"
    assert report.added_count == 2
    listed = db.query(WatchlistEntity).filter(
        WatchlistEntity.entity_id == "SECO-ch-seco-1"
    ).first()
    assert listed is not None and listed.origin == "OpenSanctions SECO (CH)"


def test_sync_persists_extended_columns(db):
    """
    Les colonnes etendues extraites par les parseurs officiels doivent arriver
    en base par le chemin SYNCHRONISATION, pas seulement par l'upload manuel.
    """
    run_seco_sync(db, fetcher=_fetcher(SECO_XML))
    ivanov = db.query(WatchlistEntity).filter(
        WatchlistEntity.entity_id == "SECO-1000"
    ).first()
    assert ivanov is not None
    assert ivanov.official_reference is not None
    assert "RS 946.231.176.72" in ivanov.official_reference
    assert ivanov.sanction_programs == ["UKR"]
    assert ivanov.listed_on == "2022-03-04"
    assert ivanov.designating_state == "EU"
    assert ivanov.title == "Colonel"


# ------------------ BOUT EN BOUT : IMPORT PUIS CRIBLAGE ------------------

def _admin_client():
    from fastapi.testclient import TestClient
    from fiskr.api import app
    from fiskr.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin_seco", "full_name": "admin_seco",
        "role": "admin", "roles": ["admin"],
    }
    return TestClient(app)


@pytest.fixture
def api():
    from fiskr.api import app
    with _admin_client() as c:
        yield c
    app.dependency_overrides.clear()


def test_end_to_end_upload_then_screen(api):
    """
    Le connecteur ne sert a rien si le moteur ne peut pas cribler ce qu'il
    produit : on importe le fichier par l'API reelle, puis on crible un client
    homonyme et on verifie que la touche porte bien la liste suisse.
    """
    import uuid as _uuid
    marker = _uuid.uuid4().hex[:6].upper()
    xml = SECO_XML.replace("IVANOV", f"IVANOV{marker}").replace(
        "Vanya Ivanoff", f"Vanya Ivanoff{marker}")

    response = api.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_SECO"},
        files={"file": (f"seco_{marker}.xml", xml, "application/xml")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["record_count"] == 4

    # La fiche est consultable et porte sa base legale suisse
    found = api.get("/api/watchlist/db", params={"search": f"IVANOV{marker}"}).json()
    assert found["total"] >= 1
    entity = found["items"][0]
    assert "RS 946.231.176.72" in (entity.get("official_reference") or "")

    result = api.post("/api/screen", json={
        "client_id": f"test_seco_{marker}",
        "client_type": "PP",
        "client_first_name": "Ivan",
        "client_last_name": f"IVANOV{marker}",
        "client_dob": "1970-03-12",
        "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [],
                             "birth_country": [], "registration_country": []},
        # Le test verifie que la liste SUISSE intercepte : on restreint le
        # perimetre a cette liste. Sans cela, un homonyme laisse par un autre
        # test dans la base de developpement peut remporter le best_match et
        # faire echouer un test qui, seul, passe — exactement le piege de
        # marqueurs a prefixe commun deja rencontre sur test_watchlist_db.
        "screening_lists": ["WATCHLIST_SECO"],
    })
    assert result.status_code == 200, result.text
    best = result.json()["best_match"]
    assert best is not None, result.json()
    assert best["status"] == "ALERT"
    listed = best["watchlist_entity"]
    assert f"IVANOV{marker}".lower() in listed["primary_name"].lower()
    assert listed["_list_type"] == "WATCHLIST_SECO"
    assert listed["entity_id"] == "SECO-1000"

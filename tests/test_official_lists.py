"""
Tests des connecteurs de listes consolidees officielles :
- UE FSF (webgate FSD) : XML consolide faisant autorite, remplace la liste EU
- ONU : liste consolidee du Conseil de securite (scsanctions.un.org)
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot
from fiskr.ingest import parse_eu_fsf_xml, parse_un_consolidated_xml
from fiskr.sync import run_eu_fsf_sync, run_un_sync


EU_FSF_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<export xmlns="http://eu.europa.ec/fpi/fsd/export" generationDate="2026-07-14T08:00:00">
  <sanctionEntity designationDetails="" unitedNationId="QDi.430" euReferenceNumber="EU.27.28" logicalId="13">
    <regulation regulationType="amendment" organisationType="council" publicationDate="2022-02-23" numberTitle="269/2014 (OJ L78)" programme="UKR" logicalId="4402">
      <publicationUrl>http://eur-lex.europa.eu/some-act</publicationUrl>
    </regulation>
    <subjectType code="person" classificationCode="P"/>
    <nameAlias firstName="Sergei" middleName="" lastName="IVANOV" wholeName="Sergei IVANOV" nameLanguage="EN" gender="M" title="" function="Minister of Defence" strong="true" logicalId="18"/>
    <nameAlias firstName="" middleName="" lastName="" wholeName="Sergueï Ivanov" nameLanguage="FR" gender="" title="" function="" strong="false" logicalId="19"/>
    <citizenship region="" countryIso2Code="RU" countryDescription="RUSSIAN FEDERATION" logicalId="20"/>
    <birthdate circa="false" calendarType="GREGORIAN" city="Leningrad" zipCode="" birthdate="1965-03-12" day="12" month="3" year="1965" region="" place="" countryIso2Code="RU" countryDescription="RUSSIAN FEDERATION" logicalId="21"/>
    <identification diplomatic="true" knownExpired="false" identificationTypeCode="passport" identificationTypeDescription="National passport" number="750123456" countryIso2Code="RU" countryDescription="RUSSIAN FEDERATION" logicalId="22"/>
    <address city="Moscow" street="12 Tverskaya Street" poBox="" zipCode="125009" region="" place="" countryIso2Code="RU" countryDescription="RUSSIAN FEDERATION" logicalId="23"/>
    <remark>Senior official of the Russian Federation government</remark>
  </sanctionEntity>
  <sanctionEntity designationDetails="" unitedNationId="" euReferenceNumber="EU.30.77" logicalId="14">
    <regulation regulationType="amendment" organisationType="council" numberTitle="765/2006 (OJ L134)" programme="BLR" logicalId="4403">
      <publicationUrl>http://eur-lex.europa.eu/other-act</publicationUrl>
    </regulation>
    <subjectType code="enterprise" classificationCode="E"/>
    <nameAlias firstName="" middleName="" lastName="" wholeName="ZARYA CORP" nameLanguage="EN" gender="" title="" function="" strong="true" logicalId="24"/>
    <address city="Minsk" street="1 Independence Avenue" poBox="" zipCode="" region="" place="" countryIso2Code="BY" countryDescription="BELARUS" logicalId="25"/>
  </sanctionEntity>
</export>"""

UN_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<CONSOLIDATED_LIST dateGenerated="2026-07-14T08:00:00">
  <INDIVIDUALS>
    <INDIVIDUAL>
      <DATAID>6908555</DATAID>
      <VERSIONNUM>1</VERSIONNUM>
      <FIRST_NAME>IGOR</FIRST_NAME>
      <SECOND_NAME>PETROV</SECOND_NAME>
      <THIRD_NAME/>
      <UN_LIST_TYPE>Al-Qaida</UN_LIST_TYPE>
      <REFERENCE_NUMBER>QDi.430</REFERENCE_NUMBER>
      <LISTED_ON>2016-08-14</LISTED_ON>
      <NAME_ORIGINAL_SCRIPT>Игорь Петров</NAME_ORIGINAL_SCRIPT>
      <COMMENTS1>Financier of terrorist operations.</COMMENTS1>
      <DESIGNATION><VALUE>Treasurer</VALUE></DESIGNATION>
      <NATIONALITY><VALUE>Russian Federation</VALUE></NATIONALITY>
      <LIST_TYPE><VALUE>UN List</VALUE></LIST_TYPE>
      <INDIVIDUAL_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>Igor Petrovitch</ALIAS_NAME></INDIVIDUAL_ALIAS>
      <INDIVIDUAL_ALIAS><QUALITY>Low</QUALITY><ALIAS_NAME>IP</ALIAS_NAME></INDIVIDUAL_ALIAS>
      <INDIVIDUAL_ADDRESS><COUNTRY>Syrian Arab Republic</COUNTRY></INDIVIDUAL_ADDRESS>
      <INDIVIDUAL_DATE_OF_BIRTH><TYPE_OF_DATE>EXACT</TYPE_OF_DATE><DATE>1965-03-12</DATE></INDIVIDUAL_DATE_OF_BIRTH>
      <INDIVIDUAL_PLACE_OF_BIRTH><CITY>Aleppo</CITY><COUNTRY>Syrian Arab Republic</COUNTRY></INDIVIDUAL_PLACE_OF_BIRTH>
      <INDIVIDUAL_DOCUMENT><TYPE_OF_DOCUMENT>Passport</TYPE_OF_DOCUMENT><NUMBER>750123456</NUMBER><ISSUING_COUNTRY>Russian Federation</ISSUING_COUNTRY></INDIVIDUAL_DOCUMENT>
      <SORT_KEY/>
    </INDIVIDUAL>
  </INDIVIDUALS>
  <ENTITIES>
    <ENTITY>
      <DATAID>110000</DATAID>
      <FIRST_NAME>ZARYA HOLDING</FIRST_NAME>
      <UN_LIST_TYPE>ISIL (Da'esh) and Al-Qaida</UN_LIST_TYPE>
      <REFERENCE_NUMBER>QDe.001</REFERENCE_NUMBER>
      <ENTITY_ALIAS><QUALITY>Good</QUALITY><ALIAS_NAME>Zarya Corp</ALIAS_NAME></ENTITY_ALIAS>
      <ENTITY_ADDRESS><STREET>1 Red Square</STREET><CITY>Moscow</CITY><COUNTRY>Russian Federation</COUNTRY></ENTITY_ADDRESS>
    </ENTITY>
  </ENTITIES>
</CONSOLIDATED_LIST>"""


@pytest.fixture
def db(tmp_path):
    """Session SQLAlchemy isolee (SQLite temporaire) pour ne pas toucher la base de dev."""
    engine = create_engine(f"sqlite:///{tmp_path / 'lists_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def make_fetcher(content: str):
    def fetcher(url, dest_path):
        Path(dest_path).write_text(content, encoding="utf-8")
    return fetcher


# ------------------ PARSEUR EU FSF ------------------

def test_parse_eu_fsf_mapping(tmp_path):
    xml_file = tmp_path / "fsf.xml"
    xml_file.write_text(EU_FSF_SAMPLE_XML, encoding="utf-8")
    entities = {e["entity_id"]: e for e in parse_eu_fsf_xml(str(xml_file))}
    assert len(entities) == 2

    person = entities["EUFSF-EU.27.28"]
    assert person["entity_type"] == "I"
    assert person["primary_name"] == "Sergei IVANOV"
    assert person["individual_name_parsed"]["first_name"] == "Sergei"
    assert person["individual_name_parsed"]["last_name"] == "IVANOV"
    assert person["gender"] == "M"
    assert person["dates_of_birth"] == ["1965-03-12"]
    assert person["countries"]["citizenship"] == ["RU"]
    assert person["countries"]["birth_country"] == ["RU"]
    assert person["place_of_birth"].startswith("Leningrad")
    assert person["designation"] == "Minister of Defence"
    assert person["designation_reasons"] == "UKR"  # programme du reglement
    assert "269/2014" in person["additional_informations"]
    assert "QDi.430" in person["additional_informations"]  # reference ONU croisee
    assert person["passport_documents"][0] == {"number": "750123456", "issuing_country": "RU", "expiration_date": None}
    # Alias non-strong -> priorite basse
    assert "Sergueï Ivanov" in person["aliases"]["low_priority"]

    enterprise = entities["EUFSF-EU.30.77"]
    assert enterprise["entity_type"] == "E"
    assert enterprise["countries"]["jurisdiction_country"] == ["BY"]
    assert enterprise["designation_reasons"] == "BLR"


# ------------------ PARSEUR ONU ------------------

def test_parse_un_consolidated_mapping(tmp_path):
    xml_file = tmp_path / "un.xml"
    xml_file.write_text(UN_SAMPLE_XML, encoding="utf-8")
    entities = {e["entity_id"]: e for e in parse_un_consolidated_xml(str(xml_file))}
    assert len(entities) == 2

    person = entities["UN-QDi.430"]
    assert person["entity_type"] == "I"
    assert person["primary_name"] == "IGOR PETROV"
    assert person["dates_of_birth"] == ["1965-03-12"]
    # Pays anglais normalises en ISO2 (cles de blocking)
    assert person["countries"]["citizenship"] == ["RU"]
    assert person["countries"]["birth_country"] == ["SY"]
    assert person["place_of_birth"] == "Aleppo, Syrian Arab Republic"
    assert person["designation"] == "Treasurer"
    assert person["designation_reasons"] == "Al-Qaida"
    # Script original + alias Good en priorite haute, alias Low en basse
    assert "Игорь Петров" in person["aliases"]["high_priority"]
    assert "Igor Petrovitch" in person["aliases"]["high_priority"]
    assert "IP" in person["aliases"]["low_priority"]
    assert person["passport_documents"][0]["issuing_country"] == "RU"

    entity = entities["UN-QDe.001"]
    assert entity["entity_type"] == "E"
    assert entity["countries"]["jurisdiction_country"] == ["RU"]
    assert "Zarya Corp" in entity["aliases"]["high_priority"]


# ------------------ SYNCS ------------------

def test_un_sync_lifecycle(db):
    report1 = run_un_sync(db, fetcher=make_fetcher(UN_SAMPLE_XML))
    assert report1.status == "SUCCESS"
    assert report1.source == "UN"
    assert report1.added_count == 2
    snap1 = db.query(Snapshot).filter(Snapshot.snapshot_id == report1.snapshot_id).first()
    assert snap1.file_type == "WATCHLIST_UN"
    assert snap1.status == "READY"

    # Meme fichier -> NO_CHANGE
    report2 = run_un_sync(db, fetcher=make_fetcher(UN_SAMPLE_XML))
    assert report2.status == "NO_CHANGE"

    # Fichier modifie -> delta + supersede
    modified = UN_SAMPLE_XML.replace("<FIRST_NAME>ZARYA HOLDING</FIRST_NAME>", "<FIRST_NAME>ZARYA GROUP</FIRST_NAME>")
    report3 = run_un_sync(db, fetcher=make_fetcher(modified))
    assert report3.status == "SUCCESS"
    assert report3.modified_count == 1
    db.refresh(snap1)
    assert snap1.status == "SUPERSEDED"


def test_eu_fsf_sync_requires_token(db):
    # URL par defaut avec {token} et token vide -> erreur explicite sans telechargement
    report = run_eu_fsf_sync(db, fetcher=make_fetcher(EU_FSF_SAMPLE_XML))
    assert report.status == "ERROR"
    assert "token" in report.message.lower()
    assert db.query(Snapshot).count() == 0


def test_eu_fsf_sync_with_token_replaces_eu_list(db, monkeypatch):
    from fiskr import sync as sync_module
    cfg = sync_module.get_sync_config()
    cfg["eu_fsf"]["token"] = "demo-user"
    monkeypatch.setattr(sync_module, "get_sync_config", lambda: cfg)

    report = run_eu_fsf_sync(db, fetcher=make_fetcher(EU_FSF_SAMPLE_XML))
    assert report.status == "SUCCESS"
    assert report.source == "EUFSF"
    assert report.added_count == 2
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    # Partage le type WATCHLIST_EU : fait autorite sur la liste scrapee du JO
    assert snap.file_type == "WATCHLIST_EU"
    assert snap.status == "READY"

    # Mode homologation : le snapshot suivant attend le pointage
    from fiskr.database import AppSetting
    from fiskr.settings import SETTING_REQUIRE_APPROVAL
    db.add(AppSetting(key=SETTING_REQUIRE_APPROVAL, value=True))
    db.commit()
    modified = EU_FSF_SAMPLE_XML.replace('wholeName="ZARYA CORP"', 'wholeName="ZARYA GROUP"')
    report2 = run_eu_fsf_sync(db, fetcher=make_fetcher(modified))
    assert report2.status == "PENDING_REVIEW"
    db.refresh(snap)
    assert snap.status == "READY"  # la production reste servie par le snapshot approuve

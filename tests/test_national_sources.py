"""
Connecteurs nationaux : Canada (SEMA) et Australie (DFAT).

Ces deux listes sont publiees sous forme de TABLEAU, pas de schema structure,
et leurs intitules de colonnes bougent d'une version a l'autre — la canadienne
existe en plus en deux langues. Les lecteurs cherchent donc chaque colonne par
sa forme normalisee (sans casse, sans accents, sans separateurs) et acceptent
plusieurs libelles. Les tests ci-dessous verrouillent cette tolerance, qui est
la propriete la plus fragile des deux connecteurs.

Reserve identique aux autres connecteurs de ce lot : ecrits d'apres le format
publie, valides sur un jeu d'essai, pas contre le fichier reel (acces reseau
ferme dans l'environnement de developpement).
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, WatchlistEntity
from fiskr.ingest import (
    parse_canada_sema_csv, parse_dfat_consolidated, _table_get, _table_key,
)
from fiskr.sync import run_canada_sync, run_dfat_sync, get_sync_config
from fiskr.settings import SYNC_SOURCES


CANADA_EN = (
    "Country,Item,Schedule,LastName,GivenName,Aliases,DateOfBirth,Entity,Title,DateOfListing\n"
    "Russia,12,1,IVANOV,Ivan,Vanya Ivanoff;I. Ivanov,1970-03-12,,Colonel,2022-03-04\n"
    "Russia,13,1,,,,,ALPHA TRADING LLC,,2022-03-04\n"
    "Iran,,,,,,,,,\n"
)

# Meme contenu, telecharge depuis la page francophone du gouvernement canadien.
CANADA_FR = (
    "Pays,Article,Annexe,Nom,Prénom,Pseudonymes,Date de naissance,Entité,Titre,Date d'inscription\n"
    "Russie,12,1,IVANOV,Ivan,Vanya Ivanoff,1970-03-12,,Colonel,2022-03-04\n"
    "Russie,13,1,,,,,ALPHA TRADING LLC,,2022-03-04\n"
)

DFAT_CSV = (
    "Reference,Name of Individual or Entity,Type,Name Type,Date of Birth,Place of Birth,"
    "Citizenship,Address,Additional Information,Listing Information,Committees,Control Date\n"
    "1001,Kim Jong Chol,Individual,Primary Name,1971-06-12,Pyongyang,North Korea,"
    "Pyongyang DPRK,Fils de,UNSC 1718,DPRK,10/08/2017\n"
    "1001,KIM Jong-chul,Individual,aka,,,,,,,,\n"
    "1001,Kim Jong Chul,Individual,aka,,,,Autre adresse,,,,\n"
    "2002,Alpha Shipping Co,Entity,Primary Name,,,,Panama City,,Autonomous,,01/02/2020\n"
    ",Sans reference,Entity,Primary Name,,,,,,,,\n"
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'nat_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _file(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _fetcher(content):
    return lambda url, dest: Path(dest).write_text(content, encoding="utf-8")


# ------------------ LECTURE TOLERANTE ------------------

def test_column_lookup_ignores_case_accents_and_separators():
    assert _table_key("Date of Birth") == _table_key("date_of_birth") == "dateofbirth"
    assert _table_key("Date de naissance") == _table_key("DateDeNaissance")
    row = {"Date of Birth": "1970-01-01", " Entité ": "ACME"}
    assert _table_get(row, "DateOfBirth") == "1970-01-01"
    assert _table_get(row, "Entity", "Entite") == "ACME"
    # Une colonne vide n'ecrase pas le repli sur le libelle suivant
    assert _table_get({"A": "", "B": "valeur"}, "A", "B") == "valeur"
    assert _table_get({}, "Absente") == ""


# ------------------ CANADA (SEMA) ------------------

def test_canada_maps_individuals_and_entities(tmp_path):
    entities = {e["entity_id"]: e
                for e in parse_canada_sema_csv(_file(tmp_path, "ca.csv", CANADA_EN))}
    # La ligne sans nom ni entite n'a rien a cribler : elle est ecartee
    assert set(entities) == {"CA-1-12", "CA-1-13"}

    ivanov = entities["CA-1-12"]
    assert ivanov["entity_type"] == "I"
    assert ivanov["primary_name"] == "Ivan IVANOV"
    assert ivanov["individual_name_parsed"] == {
        "first_name": "Ivan", "last_name": "IVANOV", "maiden_name": ""}
    # Alias declares + graphie patronyme-d'abord, comme pour SECO
    assert ivanov["aliases"]["high_priority"] == ["Vanya Ivanoff", "I. Ivanov", "IVANOV Ivan"]
    assert ivanov["dates_of_birth"] == ["1970-03-12"]
    assert ivanov["countries"]["citizenship"] == ["RU"]
    assert ivanov["designating_state"] == "CA"
    assert ivanov["listed_on"] == "2022-03-04"

    alpha = entities["CA-1-13"]
    assert alpha["entity_type"] == "E"
    assert alpha["primary_name"] == "ALPHA TRADING LLC"
    # Le pays d'une personne morale est une juridiction, pas une nationalite
    assert alpha["countries"]["jurisdiction_country"] == ["RU"]
    assert alpha["countries"]["citizenship"] == []


def test_canada_reads_the_french_edition_identically(tmp_path):
    """Un telechargement depuis la page francophone ne doit pas donner une
    liste vide : c'est le meme fichier, avec d'autres intitules."""
    english = {e["entity_id"]: e["primary_name"]
               for e in parse_canada_sema_csv(_file(tmp_path, "en.csv", CANADA_EN))}
    french = {e["entity_id"]: e["primary_name"]
              for e in parse_canada_sema_csv(_file(tmp_path, "fr.csv", CANADA_FR))}
    assert french == english


def test_canada_rebuilds_a_stable_key_from_the_regulatory_reference(tmp_path):
    """
    Le fichier ne porte aucun identifiant technique. Sans cle stable, chaque
    publication paraitrait remplacer integralement la precedente et le delta
    serait illisible : la reference reglementaire en tient lieu.
    """
    entities = {e["entity_id"]: e
                for e in parse_canada_sema_csv(_file(tmp_path, "ca.csv", CANADA_EN))}
    assert entities["CA-1-12"]["official_reference"] == "Annexe 1 article 12 (maj 2022-03-04)"

    # Deux designes sur la meme annexe/article gardent des cles distinctes
    doublon = (
        "Country,Item,Schedule,LastName,GivenName,Entity\n"
        "Russia,7,2,PREMIER,Alpha,\n"
        "Russia,7,2,SECOND,Beta,\n"
    )
    ids = [e["entity_id"] for e in parse_canada_sema_csv(_file(tmp_path, "d.csv", doublon))]
    assert ids == ["CA-2-7", "CA-2-7.2"]

    # Sans annexe ni article, la cle derive du nom — et reste STABLE
    sans_ref = "Country,LastName,GivenName\nIran,NOMSEUL,Ali\n"
    first = [e["entity_id"] for e in parse_canada_sema_csv(_file(tmp_path, "s1.csv", sans_ref))]
    second = [e["entity_id"] for e in parse_canada_sema_csv(_file(tmp_path, "s2.csv", sans_ref))]
    assert first == second and first[0].startswith("CA-")


def test_canada_sync_lifecycle(db):
    report = run_canada_sync(db, fetcher=_fetcher(CANADA_EN))
    assert report.status == "SUCCESS"
    assert report.source == "CANADA"
    assert report.added_count == 2
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_CANADA"
    listed = db.query(WatchlistEntity).filter(
        WatchlistEntity.entity_id == "CA-1-12").first()
    assert listed.official_reference == "Annexe 1 article 12 (maj 2022-03-04)"
    assert run_canada_sync(db, fetcher=_fetcher(CANADA_EN)).status == "NO_CHANGE"


# ------------------ AUSTRALIE (DFAT) ------------------

def test_dfat_groups_repeated_rows_by_reference(tmp_path):
    entities = {e["entity_id"]: e
                for e in parse_dfat_consolidated(_file(tmp_path, "au.csv", DFAT_CSV))}
    assert "AU-1001" in entities and "AU-2002" in entities

    kim = entities["AU-1001"]
    assert kim["entity_type"] == "I"
    assert kim["primary_name"] == "Kim Jong Chol"
    # Les 2 lignes « aka » du meme groupe deviennent des alias, pas des fiches
    assert kim["aliases"]["high_priority"] == ["KIM Jong-chul", "Kim Jong Chul"]
    assert kim["dates_of_birth"] == ["1971-06-12"]
    assert kim["place_of_birth"] == "Pyongyang"
    assert kim["countries"]["citizenship"] == ["KP"]
    # Une adresse portee par une ligne alias rejoint le groupe
    assert kim["address"] == "Pyongyang DPRK"
    assert kim["alternative_addresses"] == ["Autre adresse"]
    # Date australienne JJ/MM/AAAA
    assert kim["listed_on"] == "2017-08-10"
    # Comite onusien present -> la mesure vient de l'ONU, transposee
    assert kim["designating_state"] == "UN"
    assert kim["sanction_programs"] == ["DPRK"]

    alpha = entities["AU-2002"]
    assert alpha["entity_type"] == "E"
    # Pas de comite : designation australienne autonome
    assert alpha["designating_state"] == "AU"


def test_dfat_gives_a_stable_key_to_a_row_without_reference(tmp_path):
    entities = list(parse_dfat_consolidated(_file(tmp_path, "au.csv", DFAT_CSV)))
    orphan = [e for e in entities if e["primary_name"] == "Sans reference"]
    assert len(orphan) == 1
    again = list(parse_dfat_consolidated(_file(tmp_path, "au2.csv", DFAT_CSV)))
    assert orphan[0]["entity_id"] == [
        e for e in again if e["primary_name"] == "Sans reference"][0]["entity_id"]


def test_dfat_reads_an_xlsx_workbook_or_says_what_is_missing(tmp_path):
    """La voie XLSX est optionnelle : soit elle marche, soit elle dit quoi
    installer — jamais une pile d'appels."""
    from fiskr.ingest import XLSX_AVAILABLE
    path = tmp_path / "au.xlsx"
    if XLSX_AVAILABLE:
        import openpyxl
        wb = openpyxl.Workbook()
        sheet = wb.active
        sheet.append(["Liste consolidée DFAT — bandeau de titre"])   # ligne parasite
        sheet.append(["Reference", "Name of Individual or Entity", "Type",
                      "Name Type", "Control Date"])
        sheet.append(["3003", "Beta Holdings", "Entity", "Primary Name", "05/06/2021"])
        wb.save(path)
        entities = list(parse_dfat_consolidated(str(path)))
        assert [e["entity_id"] for e in entities] == ["AU-3003"]
        assert entities[0]["listed_on"] == "2021-06-05"
    else:
        path.write_bytes(b"pas un vrai classeur")
        with pytest.raises(RuntimeError, match="openpyxl"):
            list(parse_dfat_consolidated(str(path)))


def test_dfat_sync_lifecycle(db):
    report = run_dfat_sync(db, fetcher=_fetcher(DFAT_CSV))
    assert report.status == "SUCCESS"
    assert report.source == "DFAT"
    assert report.added_count == 3
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_DFAT"
    assert run_dfat_sync(db, fetcher=_fetcher(DFAT_CSV)).status == "NO_CHANGE"


# ------------------ CONFIGURATION ------------------

def test_both_sources_are_off_by_default_and_schedulable():
    cfg = get_sync_config()
    assert cfg["canada"]["enabled"] is False
    assert cfg["dfat"]["enabled"] is False
    assert "canada" in SYNC_SOURCES and "dfat" in SYNC_SOURCES


# ------------------ BOUT EN BOUT ------------------

@pytest.fixture
def api():
    from fastapi.testclient import TestClient
    from fiskr.api import app
    from fiskr.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin_nat", "full_name": "admin_nat",
        "role": "admin", "roles": ["admin"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_end_to_end_canada_upload_then_screen(api):
    import uuid
    marker = uuid.uuid4().hex[:6].upper()
    # Patronyme volontairement eloigne de ceux des autres jeux d'essai :
    # deux marqueurs a prefixe commun se ressemblent assez pour se voler
    # le best_match en repli fuzzy.
    csv_content = CANADA_EN.replace("IVANOV", f"TREMBLAY{marker}")

    response = api.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_CANADA"},
        files={"file": (f"canada_{marker}.csv", csv_content, "text/csv")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["record_count"] == 2

    result = api.post("/api/screen", json={
        "client_id": f"test_ca_{marker}",
        "client_type": "PP",
        "client_first_name": "Ivan",
        "client_last_name": f"TREMBLAY{marker}",
        "client_dob": "1970-03-12",
        "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [],
                             "birth_country": [], "registration_country": []},
        "screening_lists": ["WATCHLIST_CANADA"],
    })
    assert result.status_code == 200, result.text
    best = result.json()["best_match"]
    assert best is not None and best["status"] == "ALERT"
    assert best["watchlist_entity"]["_list_type"] == "WATCHLIST_CANADA"


# ------------------ CANADA : L'EXPORT XML (le CSV a ete retire) ------------------

CANADA_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<data-set>
 <record>
  <Country>Belarus / Bélarus</Country>
  <LastName>Atabekov</LastName>
  <GivenName>Khazalbek Bakhtibekovich</GivenName>
  <DateOfBirthOrShipBuildDate>1965-04-29</DateOfBirthOrShipBuildDate>
  <Schedule>1, Part 1</Schedule>
  <Item>1</Item>
  <DateOfListing>2020-09-28</DateOfListing>
 </record>
 <record>
  <Country>Russia / Russie</Country>
  <EntityOrShip>Testovaya Kompaniya OOO</EntityOrShip>
  <Schedule>1, Part 2</Schedule>
  <Item>7</Item>
  <DateOfListing>2022-03-15</DateOfListing>
 </record>
 <record>
  <Country>Russia / Russie</Country>
  <EntityOrShip>MV Test Vessel</EntityOrShip>
  <ShipIMONumber>9123456</ShipIMONumber>
  <Schedule>1, Part 3</Schedule>
  <Item>2</Item>
  <DateOfListing>2023-06-01</DateOfListing>
 </record>
</data-set>
"""


def test_canada_xml_export_maps_persons_entities_and_ships(tmp_path):
    """Affaires mondiales Canada a RETIRE son CSV (404 constaté en production).
    L'export XML — un tableau plat d'enregistrements — doit produire les mêmes
    fiches, y compris les personnes morales et les navires que le CSV
    n'exposait pas (colonne EntityOrShip, numéro OMI)."""
    entities = list(parse_canada_sema_csv(_file(tmp_path, "ca.xml", CANADA_XML)))
    assert len(entities) == 3
    by_type = {}
    for e in entities:
        by_type.setdefault(e["entity_type"], []).append(e)
    assert len(by_type["I"]) == 1 and len(by_type["E"]) == 2

    person = by_type["I"][0]
    assert person["primary_name"] == "Khazalbek Bakhtibekovich Atabekov"
    # L'intitulé XML de la date de naissance diffère de celui du CSV
    assert person["dates_of_birth"] == ["1965-04-29"]
    assert person["listed_on"] == "2020-09-28"

    ship = next(e for e in by_type["E"] if e["primary_name"] == "MV Test Vessel")
    assert ship["imo_number"] == "9123456"


def test_canada_xml_and_csv_agree_on_the_same_content(tmp_path):
    """Le passage du CSV au XML ne doit RIEN changer aux fiches produites :
    sans quoi le delta de la première synchronisation annoncerait un
    remplacement intégral de la liste."""
    csv_text = ("Country,LastName,GivenName,DateOfBirth,Schedule,Item,DateOfListing\n"
                "Belarus / Bélarus,Atabekov,Khazalbek Bakhtibekovich,1965-04-29,"
                "\"1, Part 1\",1,2020-09-28\n")
    xml_only = list(parse_canada_sema_csv(_file(tmp_path, "one.xml", CANADA_XML)))[0]
    csv_only = list(parse_canada_sema_csv(_file(tmp_path, "one.csv", csv_text)))[0]
    for champ in ("entity_id", "primary_name", "entity_type", "dates_of_birth",
                  "listed_on", "countries", "official_reference"):
        assert xml_only[champ] == csv_only[champ], champ

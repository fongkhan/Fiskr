import pytest
from datetime import date
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

from fiskr.database import Base, Snapshot, WatchlistEntity, SyncReport
from fiskr.sync import (
    extract_daily_acts,
    scrape_act_entities,
    run_ofac_sync,
    run_eurlex_sync,
    send_report_email,
    _detect_entity_type,
    _stable_eu_entity_id,
)
from fiskr.api import app
from fiskr.auth import get_current_user


# ------------------ FIXTURES ------------------

@pytest.fixture
def db(tmp_path):
    """Session SQLAlchemy isolee (SQLite temporaire) pour ne pas toucher la base de dev."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sync_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "admin", "role": "admin"}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_ofac_xml(entities):
    """Genere un mock OFAC Advanced XML: [(id, first, last, birth_year), ...]"""
    parties = ""
    for pid, first, last, year in entities:
        parties += f"""
    <DistinctParty ID="{pid}">
        <Profile>
            <PartySubType PartyTypeID="151"/>
            <Identity>
                <DocumentedName DocNameStatusID="1">
                    <DocumentedNamePart NamePartTypeID="1360"><Value>{first}</Value></DocumentedNamePart>
                    <DocumentedNamePart NamePartTypeID="1361"><Value>{last}</Value></DocumentedNamePart>
                </DocumentedName>
            </Identity>
            <Feature FeatureTypeID="8">
                <FeatureVersion>
                    <DatePeriod><Start><From>
                        <Year>{year}</Year><Month>01</Month><Day>01</Day>
                    </From></Start></DatePeriod>
                </FeatureVersion>
            </Feature>
            <Location>
                <LocationType>citizenship</LocationType>
                <LocationCountry CountryISO2="RU"/>
            </Location>
        </Profile>
    </DistinctParty>"""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Sanctions xmlns="http://tempuri.org">{parties}
</Sanctions>
"""


def make_fetcher(xml_content: str):
    def fetcher(url, dest_path):
        Path(dest_path).write_text(xml_content, encoding="utf-8")
    return fetcher


MOCK_DAILY_OJ_HTML = """
<html><body>
<div class="daily-acts">
    <a href="./legal-content/FR/TXT/HTML/?uri=OJ:L_2026_1234">R&egrave;glement (UE) 2026/1234 du Conseil concernant des mesures restrictives eu &eacute;gard &agrave; la situation en Exemplie</a>
    <a href="./legal-content/FR/TXT/HTML/?uri=OJ:L_2026_5678">R&egrave;glement (UE) 2026/5678 relatif aux droits de douane sur les bananes</a>
</div>
</body></html>
"""

MOCK_ACT_HTML = """
<html><body>
<h1>ANNEXE</h1>
<table>
<tr><th>Nom</th><th>Informations d'identification</th><th>Motifs</th><th>Date de l'inscription</th></tr>
<tr><td>Igor PETROV</td><td>N&eacute; le 12.03.1965 ; nationalit&eacute; : russe</td><td>Personne soutenant le regime</td><td>08.07.2026</td></tr>
<tr><td>ZARYA HOLDING</td><td>Entit&eacute; enregistr&eacute;e &agrave; Moscou, soci&eacute;t&eacute; de transport</td><td>Appui logistique</td><td>08.07.2026</td></tr>
<tr><td>VOLGA STAR</td><td>Navire, IMO 9876543</td><td>Transport de p&eacute;trole brut</td><td>08.07.2026</td></tr>
</table>
</body></html>
"""


# ------------------ SCRAPING EUR-LEX ------------------

def test_extract_daily_acts_filters_keyword():
    base = "https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?ojDate=08072026"
    acts = extract_daily_acts(MOCK_DAILY_OJ_HTML, base)

    assert len(acts) == 1
    assert "mesures restrictives" in acts[0]["title"]
    assert acts[0]["url"].startswith("https://eur-lex.europa.eu/")
    assert "legal-content" in acts[0]["url"]


def test_scrape_act_entities_types_and_identifiers():
    entities = scrape_act_entities(MOCK_ACT_HTML, "Reglement test", "http://act.example")
    by_name = {e["primary_name"]: e for e in entities}

    assert "Igor PETROV" in by_name
    individual = by_name["Igor PETROV"]
    assert individual["entity_type"] == "I"
    assert individual["dates_of_birth"] == ["1965-03-12"]
    assert individual["individual_name_parsed"]["first_name"] == "Igor"
    # La colonne "Motifs" de l'annexe est conservee dans designation_reasons
    assert individual["designation_reasons"] == "Personne soutenant le regime"

    assert by_name["ZARYA HOLDING"]["entity_type"] == "E"
    assert by_name["ZARYA HOLDING"]["designation_reasons"] == "Appui logistique"

    vessel = by_name["VOLGA STAR"]
    assert vessel["entity_type"] == "V"
    assert vessel["imo_number"] == "9876543"

    # ID stable et deterministe pour le delta inter-jours
    assert individual["entity_id"] == _stable_eu_entity_id("Igor PETROV")


def test_scrape_act_excludes_transliteration_header():
    # L'en-tete "Noms (translitteration en caracteres latins)" des annexes ne doit
    # pas devenir une fiche (bug observe sur le JO du 08/06/2026)
    html = """
    <html><body><table>
    <tr><td>Noms (translitt&eacute;ration en caract&egrave;res latins)</td><td>Noms</td><td>Informations d'identification</td></tr>
    <tr><td>Mohammad AKBARZADEH</td><td>&#1605;&#1581;&#1605;&#1583;</td><td>N&eacute; le 01.01.1980</td></tr>
    </table></body></html>
    """
    entities = scrape_act_entities(html, "Acte test", "http://act.example")
    names = {e["primary_name"] for e in entities}
    assert names == {"Mohammad AKBARZADEH"}


def test_eurlex_sync_long_act_title_clamped_to_column(db):
    # Les titres d'actes EUR-Lex depassent 255 caracteres : la colonne origin
    # (VARCHAR(255)) doit etre tronquee au lieu de faire echouer l'INSERT
    long_title = ("Décision (PESC) 2026/1226 du Conseil du 8 juin 2026 modifiant la décision (PESC) 2023/1532 "
                  "concernant des mesures restrictives en raison du soutien militaire apporté par l'Iran à des "
                  "groupes armés et entités au Moyen-Orient et dans la région de la mer Rouge, ainsi que des "
                  "actions imputables à l'Iran qui compromettent la liberté de navigation au Moyen-Orient "
                  "et dans la région de la mer Rouge")
    assert len(long_title) > 255
    daily_html = f'<html><body><a href="./legal-content/FR/TXT/?uri=OJ:L_202601226">{long_title}</a></body></html>'
    act_html = """
    <html><body><table>
    <tr><th>Nom</th><th>Informations d'identification</th><th>Motifs</th></tr>
    <tr><td>Mohammad AKBARZADEH</td><td>N&eacute; le 01.01.1980</td><td>Soutien logistique</td></tr>
    </table></body></html>
    """
    report = run_eurlex_sync(db, for_date=date(2026, 6, 8), http_get=make_http_get(daily_html, act_html))

    assert report.status == "SUCCESS"
    assert report.added_count == 1
    ent = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == report.snapshot_id).first()
    assert ent.primary_name == "MOHAMMAD AKBARZADEH"
    assert ent.designation_reasons == "Soutien logistique"
    assert len(ent.origin) <= 255
    assert ent.origin.startswith("EUR-Lex - Décision (PESC) 2026/1226")


def test_scrape_act_cleans_language_mentions_and_headers():
    # Bugs observes sur les JO de juin 2026 : suffixes "(en russe : ...)" tronques,
    # en-tetes "Lieu d'enregistrement" / "Motifs de l'inscription sur une liste",
    # formules juridiques ("Sont geles tous les fonds...") et noms non latins
    html = """
    <html><body><table>
    <tr><th>Nom</th><th>Informations d'identification</th><th>Motifs</th></tr>
    <tr><td>Kirill FEDOROV (en russe : &#1050;&#1080;&#1088;&#1080;&#1083;&#1083;)</td><td>N&eacute; le 27.10.1998</td><td>Propagandiste</td></tr>
    <tr><td>Anton USOV en russe : &#1040;&#1085;&#1090;&#1086;&#1085; &#1059;&#1057;&#1054;&#1042;</td><td>N&eacute; le 03.04.1981</td><td>Cadre</td></tr>
    <tr><td>Lieu d&rsquo;enregistrement</td><td>Moscou</td><td>-</td></tr>
    <tr><td>Motifs de l&rsquo;inscription sur une liste</td><td>-</td><td>-</td></tr>
    <tr><td>Sont gel&eacute;s tous les fonds</td><td>-</td><td>-</td></tr>
    <tr><td>&#1056;&#1091;&#1089;&#1090;&#1072;&#1082;&#1090;</td><td>Entit&eacute; russe</td><td>-</td></tr>
    </table></body></html>
    """
    entities = scrape_act_entities(html, "Acte test", "http://act.example")
    names = {e["primary_name"] for e in entities}
    assert "Kirill FEDOROV" in names
    assert "Anton USOV" in names
    assert all("en russe" not in n.lower() for n in names)
    assert all(not n.lower().startswith(("lieu", "motifs", "sont")) for n in names)


def test_detect_entity_type_word_boundaries():
    # "SHIPPING" ne doit pas etre confondu avec le mot "ship"
    assert _detect_entity_type("ZARYA SHIPPING LLC societe de transport") == "E"
    assert _detect_entity_type("Navire petrolier, IMO 1234567") == "V"
    assert _detect_entity_type("Ne le 01.01.1970 a Moscou") == "I"


# ------------------ SYNC OFAC (REMPLACEMENT + DELTA) ------------------

def test_ofac_sync_first_run_then_no_change_then_delta(db):
    v1 = make_ofac_xml([("100", "Ivan", "Volkov", "1960"), ("200", "Piotr", "Sokolov", "1970")])

    # 1er run : import initial
    report1 = run_ofac_sync(db, fetcher=make_fetcher(v1))
    assert report1.status == "SUCCESS"
    assert report1.added_count == 2
    assert report1.removed_count == 0
    snap1 = db.query(Snapshot).filter(Snapshot.snapshot_id == report1.snapshot_id).first()
    assert snap1.status == "READY"
    assert snap1.record_count == 2

    # 2e run : fichier identique -> aucun changement
    report2 = run_ofac_sync(db, fetcher=make_fetcher(v1))
    assert report2.status == "NO_CHANGE"
    assert report2.snapshot_id is None

    # 3e run : 100 modifie (annee de naissance), 200 supprime, 300 ajoute
    v2 = make_ofac_xml([("100", "Ivan", "Volkov", "1961"), ("300", "Anna", "Orlova", "1980")])
    report3 = run_ofac_sync(db, fetcher=make_fetcher(v2))
    assert report3.status == "SUCCESS"
    assert report3.added_count == 1
    assert report3.modified_count == 1
    assert report3.removed_count == 1

    details = report3.delta_report["details"]
    assert details["added"][0]["id"] == "300"
    assert details["removed"][0]["id"] == "200"
    assert details["modified"][0]["id"] == "100"
    assert any("dates_of_birth" in c for c in details["modified"][0]["changes_detected"])

    # Remplacement applique : l'ancien snapshot est SUPERSEDED, seul le nouveau est actif
    db.refresh(snap1)
    assert snap1.status == "SUPERSEDED"
    active = db.query(Snapshot).filter(Snapshot.file_type == "WATCHLIST_OFAC", Snapshot.status == "READY").all()
    assert [s.snapshot_id for s in active] == [report3.snapshot_id]


# ------------------ SYNC EUR-LEX (FUSION INCREMENTALE) ------------------

def make_http_get(daily_html: str, act_html: str):
    def http_get(url):
        if "legal-content" in url:
            return act_html
        return daily_html
    return http_get


def test_eurlex_sync_no_publication(db):
    html_without_measures = "<html><body><a href='./x'>Reglement sur les fromages</a></body></html>"
    report = run_eurlex_sync(db, for_date=date(2026, 7, 8), http_get=make_http_get(html_without_measures, ""))

    assert report.status == "NO_PUBLICATION"
    assert db.query(Snapshot).count() == 0


def test_eurlex_sync_scrape_then_incremental_merge(db):
    # Jour 1 : 3 listes extraits de l'acte
    report1 = run_eurlex_sync(db, for_date=date(2026, 7, 8), http_get=make_http_get(MOCK_DAILY_OJ_HTML, MOCK_ACT_HTML))
    assert report1.status == "SUCCESS"
    assert report1.added_count == 3
    assert report1.delta_report["acts"][0]["title"].startswith("Règlement (UE) 2026/1234")

    # Jour 2 : nouvel acte avec 1 nouveau liste + 1 deja connu (ligne identique)
    act_day2 = """
    <html><body><table>
    <tr><th>Nom</th><th>Informations d'identification</th><th>Motifs</th><th>Date de l'inscription</th></tr>
    <tr><td>Igor PETROV</td><td>N&eacute; le 12.03.1965 ; nationalit&eacute; : russe</td><td>Personne soutenant le regime</td><td>08.07.2026</td></tr>
    <tr><td>DIMA KUZNETSOV</td><td>N&eacute; le 05.05.1985</td><td>Financement du regime</td><td>09.07.2026</td></tr>
    </table></body></html>
    """
    report2 = run_eurlex_sync(db, for_date=date(2026, 7, 9), http_get=make_http_get(MOCK_DAILY_OJ_HTML, act_day2))
    assert report2.status == "SUCCESS"
    # Fusion : PETROV inchange, KUZNETSOV ajoute, ZARYA/VOLGA reconduits (pas de suppression)
    assert report2.added_count == 1
    assert report2.removed_count == 0
    assert report2.modified_count == 0

    new_snapshot_entities = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == report2.snapshot_id
    ).all()
    names = {e.primary_name for e in new_snapshot_entities}
    assert len(new_snapshot_entities) == 4
    assert any("KUZNETSOV" in n for n in names)
    assert any("VOLGA" in n for n in names)

    # L'ancien snapshot EU est remplace dans le cache actif
    snap1 = db.query(Snapshot).filter(Snapshot.snapshot_id == report1.snapshot_id).first()
    assert snap1.status == "SUPERSEDED"


# ------------------ EMAIL ------------------

def test_send_report_email_skipped_without_smtp(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SYNC_EMAIL_TO", raising=False)
    report = SyncReport(source="OFAC", status="SUCCESS", added_count=1, modified_count=0, removed_count=0)
    assert send_report_email(report) is False


# ------------------ API ------------------

def test_api_sync_run_invalid_source(client):
    response = client.post("/api/sync/run", json={"source": "INTERPOL"})
    assert response.status_code == 400


def test_api_sync_config_and_reports(client):
    cfg = client.get("/api/sync/config")
    assert cfg.status_code == 200
    data = cfg.json()
    assert "ofac" in data and "eurlex" in data
    assert "email_configured" in data

    reports = client.get("/api/sync/reports")
    assert reports.status_code == 200
    assert isinstance(reports.json(), list)


def test_api_sync_run_eurlex_no_publication(client, monkeypatch):
    # JO du jour sans acte "mesures restrictives" : rapport NO_PUBLICATION, aucun snapshot cree
    monkeypatch.setattr("fiskr.sync.http_get_text", lambda url, timeout=60.0: "<html><body>Rien aujourd'hui</body></html>")
    response = client.post("/api/sync/run", json={"source": "EURLEX", "date": "2026-07-09"})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "NO_PUBLICATION"
    assert data["source"] == "EURLEX"

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
from tests.conftest import wait_for_job


def _run_sync(client, payload, timeout=60.0):
    """
    Declenche une synchronisation (202 + jeton), attend la fin du job et
    retourne le rapport publie sur le jeton. La synchronisation ne rend plus son
    rapport dans la reponse du POST : elle travaille en tache de fond pour ne
    pas immobiliser l'application pendant plusieurs minutes.
    """
    response = client.post("/api/sync/run", json=payload)
    assert response.status_code == 202, response.text
    token = response.json()["job_token"]
    state = wait_for_job(client, token, timeout=timeout)
    assert state["status"] == "DONE", state
    detail = client.get(f"/api/progress?id={token}")
    assert detail.status_code == 200, detail.text
    report = detail.json()["result"]
    assert report is not None, "le job s'est terminé sans rapport publié"
    return report


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


# Journal Officiel en version anglaise (edition de reference)
MOCK_DAILY_OJ_HTML = """
<html><body>
<div class="daily-acts">
    <a href="./legal-content/EN/TXT/HTML/?uri=OJ:L_2026_1234">Council Regulation (EU) 2026/1234 concerning restrictive measures in view of the situation in Examplia</a>
    <a href="./legal-content/EN/TXT/HTML/?uri=OJ:L_2026_5678">Council Regulation (EU) 2026/5678 on customs duties applicable to bananas</a>
</div>
</body></html>
"""

MOCK_ACT_HTML = """
<html><body>
<h1>ANNEX</h1>
<table>
<tr><th>Name</th><th>Identifying information</th><th>Reasons</th><th>Date of listing</th></tr>
<tr><td>Igor PETROV</td><td>Born on 12.3.1965; nationality: Russian</td><td>Person supporting the regime</td><td>8.7.2026</td></tr>
<tr><td>ZARYA HOLDING</td><td>Entity registered in Moscow, transport company</td><td>Logistics support</td><td>8.7.2026</td></tr>
<tr><td>VOLGA STAR</td><td>Vessel, IMO 9876543</td><td>Transport of crude oil</td><td>8.7.2026</td></tr>
</table>
</body></html>
"""


def stub_pdf_fetcher(url, dest_path):
    Path(dest_path).write_bytes(b"%PDF-1.4 mock official act " + url.encode("utf-8"))


# ------------------ SCRAPING EUR-LEX ------------------

def test_extract_daily_acts_filters_keyword():
    base = "https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?ojDate=08072026"
    acts = extract_daily_acts(MOCK_DAILY_OJ_HTML, base)

    assert len(acts) == 1
    assert "restrictive measures" in acts[0]["title"]
    assert acts[0]["url"].startswith("https://eur-lex.europa.eu/")
    assert "legal-content" in acts[0]["url"]


def test_scrape_act_entities_types_and_identifiers():
    entities = scrape_act_entities(MOCK_ACT_HTML, "Regulation test", "http://act.example")
    by_name = {e["primary_name"]: e for e in entities}

    assert "Igor PETROV" in by_name
    individual = by_name["Igor PETROV"]
    assert individual["entity_type"] == "I"
    assert individual["dates_of_birth"] == ["1965-03-12"]
    assert individual["individual_name_parsed"]["first_name"] == "Igor"
    # La colonne "Reasons" de l'annexe est conservee dans designation_reasons
    assert individual["designation_reasons"] == "Person supporting the regime"

    assert by_name["ZARYA HOLDING"]["entity_type"] == "E"
    assert by_name["ZARYA HOLDING"]["designation_reasons"] == "Logistics support"

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


def test_eurlex_sync_long_act_title_clamped_to_column(db, tmp_path):
    # Les titres d'actes EUR-Lex depassent 255 caracteres : la colonne origin
    # (VARCHAR(255)) doit etre tronquee au lieu de faire echouer l'INSERT
    long_title = ("Council Decision (CFSP) 2026/1226 of 8 June 2026 amending Decision (CFSP) 2023/1532 "
                  "concerning restrictive measures in view of Iran's military support for armed groups and "
                  "entities in the Middle East and the Red Sea region, as well as actions attributable to "
                  "Iran undermining the freedom of navigation in the Middle East and the Red Sea region "
                  "and the stability of the region as a whole")
    assert len(long_title) > 255
    daily_html = f'<html><body><a href="./legal-content/EN/TXT/?uri=OJ:L_202601226">{long_title}</a></body></html>'
    act_html = """
    <html><body><table>
    <tr><th>Name</th><th>Identifying information</th><th>Reasons</th></tr>
    <tr><td>Mohammad AKBARZADEH</td><td>Born on 1.1.1980</td><td>Logistical support</td></tr>
    </table></body></html>
    """
    report = run_eurlex_sync(db, for_date=date(2026, 6, 8), mode="extract",
                             http_get=make_http_get(daily_html, act_html),
                             pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path / "archives")

    assert report.status == "SUCCESS"
    assert report.added_count == 1
    ent = db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == report.snapshot_id).first()
    assert ent.primary_name == "MOHAMMAD AKBARZADEH"
    assert ent.designation_reasons == "Logistical support"
    assert len(ent.origin) <= 255
    assert ent.origin.startswith("EUR-Lex - Council Decision (CFSP) 2026/1226")


def test_scrape_act_cleans_language_mentions_and_headers():
    # Bugs observes sur les JO de juin 2026 : suffixes "(en russe : ...)" tronques,
    # en-tetes "Lieu d'enregistrement" / "Motifs de l'inscription sur une liste",
    # formules juridiques ("Sont geles tous les fonds...") et noms non latins
    html = """
    <html><body><table>
    <tr><th>Nom</th><th>Informations d'identification</th><th>Motifs</th></tr>
    <tr><td>Kirill FEDOROV (en russe : &#1050;&#1080;&#1088;&#1080;&#1083;&#1083;)</td><td>N&eacute; le 27.10.1998</td><td>Propagandiste</td></tr>
    <tr><td>Anton USOV en russe : &#1040;&#1085;&#1090;&#1086;&#1085; &#1059;&#1057;&#1054;&#1042;</td><td>N&eacute; le 03.04.1981</td><td>Cadre</td></tr>
    <tr><td>Maria Vladimirovna DUDKO (Russian: &#1052;&#1072;&#1088;&#1080;&#1103;)</td><td>Born on 12.4.1985</td><td>Director of a public relations agency</td></tr>
    <tr><td>EN L series</td><td>-</td><td>-</td></tr>
    <tr><td>Regulation (EU) 2016/44 should therefore be amended</td><td>-</td><td>-</td></tr>
    <tr><td>Lieu d&rsquo;enregistrement</td><td>Moscou</td><td>-</td></tr>
    <tr><td>Motifs de l&rsquo;inscription sur une liste</td><td>-</td><td>-</td></tr>
    <tr><td>Sont gel&eacute;s tous les fonds</td><td>-</td><td>-</td></tr>
    <tr><td>&#1056;&#1091;&#1089;&#1090;&#1072;&#1082;&#1090;</td><td>Entit&eacute; russe</td><td>-</td></tr>
    <tr><td>&laquo;&nbsp;Corps des gardiens de la r&eacute;volution (IRGC)&nbsp;&raquo;</td><td>La mention suivante est remplac&eacute;e par le texte suivant</td><td>-</td></tr>
    </table></body></html>
    """
    entities = scrape_act_entities(html, "Acte test", "http://act.example")
    names = {e["primary_name"] for e in entities}
    assert "Kirill FEDOROV" in names
    assert "Anton USOV" in names
    # Syntaxe anglaise "(Russian: ...)" egalement nettoyee
    assert "Maria Vladimirovna DUDKO" in names
    assert all("russian" not in n.lower() and "en russe" not in n.lower() for n in names)
    # En-tetes de mise en page et considerants anglais exclus
    assert all(not n.lower().startswith(("lieu", "motifs", "sont", "regulation", "en l series")) for n in names)
    # Les instructions d'amendement citant du texte de liste sont ignorees
    assert all("gardiens" not in n.lower() for n in names)
    # Le decoupage prenoms multiples / nom de famille est applique
    fedorov = next(e for e in entities if e["primary_name"] == "Kirill FEDOROV")
    assert fedorov["individual_name_parsed"] == {"first_name": "Kirill", "last_name": "FEDOROV", "maiden_name": ""}


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


def test_eurlex_sync_scrape_then_incremental_merge(db, tmp_path):
    archive_dir = tmp_path / "archives"
    # Jour 1 : 3 listes extraits de l'acte
    report1 = run_eurlex_sync(db, for_date=date(2026, 7, 8), mode="extract",
                              http_get=make_http_get(MOCK_DAILY_OJ_HTML, MOCK_ACT_HTML),
                              pdf_fetcher=stub_pdf_fetcher, archive_dir=archive_dir)
    assert report1.status == "SUCCESS"
    assert report1.added_count == 3
    act = report1.delta_report["acts"][0]
    assert act["title"].startswith("Council Regulation (EU) 2026/1234")
    # Le PDF officiel (valeur probante en audit) est archive avec son empreinte
    assert act["pdf_file"] and (archive_dir / act["pdf_file"]).exists()
    assert len(act["pdf_sha256"]) == 64

    # Jour 2 : nouvel acte avec 1 nouveau liste + 1 deja connu (ligne identique)
    act_day2 = """
    <html><body><table>
    <tr><th>Name</th><th>Identifying information</th><th>Reasons</th><th>Date of listing</th></tr>
    <tr><td>Igor PETROV</td><td>Born on 12.3.1965; nationality: Russian</td><td>Person supporting the regime</td><td>8.7.2026</td></tr>
    <tr><td>DIMA KUZNETSOV</td><td>Born on 5.5.1985</td><td>Financing of the regime</td><td>9.7.2026</td></tr>
    </table></body></html>
    """
    report2 = run_eurlex_sync(db, for_date=date(2026, 7, 9), mode="extract",
                              http_get=make_http_get(MOCK_DAILY_OJ_HTML, act_day2),
                              pdf_fetcher=stub_pdf_fetcher, archive_dir=archive_dir)
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
    """Refus SYNCHRONE : aucun job ne part sur une source inconnue."""
    response = client.post("/api/sync/run", json={"source": "INTERPOL"})
    assert response.status_code == 400


def test_api_sync_run_invalid_date_is_refused_synchronously(client):
    """Meme exigence pour une date malformee : 400 immediat, pas de 202."""
    response = client.post("/api/sync/run", json={"source": "EURLEX", "date": "09/07/2026"})
    assert response.status_code == 400


def test_api_sync_run_refuses_a_second_run_of_the_same_source(client, monkeypatch):
    """
    Une source deja en cours ne se relance pas : deux ingestions concurrentes
    de la meme liste se marcheraient dessus. L'exclusivite est portee par le
    dedupe_key de la file de travaux (ligne QUEUED/RUNNING en base) : elle vaut
    donc entre TOUS les processus, pas seulement dans celui qui a lance la sync.
    """
    from fiskr.database import SessionLocal, Job

    session = SessionLocal()
    try:
        live = Job(token="sync:ofac", kind="sync", label="Synchronisation OFAC",
                   params={}, status="RUNNING", dedupe_key="sync:ofac",
                   created_by="test")
        session.add(live)
        session.commit()
        live_id = live.id
    finally:
        session.close()
    try:
        response = client.post("/api/sync/run", json={"source": "OFAC"})
        assert response.status_code == 409
    finally:
        session = SessionLocal()
        try:
            session.query(Job).filter(Job.id == live_id).delete()
            session.commit()
        finally:
            session.close()


def test_api_sync_run_answers_202_without_waiting(client, monkeypatch):
    """
    Le POST rend la main tout de suite : il ne doit PAS attendre la fin du
    cycle. On le prouve en rendant le cycle lent — la reponse arrive avant.
    Force le mode `thread` : la suite tourne en `eager` (execution inline),
    qui attend par construction — c'est le mode de production qu'on teste ici.
    """
    import time as _time
    from fiskr import api as api_module

    monkeypatch.setenv("FISKR_JOBS_MODE", "thread")

    def _slow_sync(db, **kwargs):
        _time.sleep(1.5)
        return SyncReport(source="OFAC", status="NO_CHANGE", added_count=0,
                          modified_count=0, removed_count=0)

    monkeypatch.setitem(api_module._SYNC_RUNNERS, "ofac", _slow_sync)
    started = _time.monotonic()
    response = client.post("/api/sync/run", json={"source": "OFAC"})
    elapsed = _time.monotonic() - started
    assert response.status_code == 202, response.text
    assert elapsed < 1.0, f"la requête a attendu le cycle ({elapsed:.2f} s)"
    state = wait_for_job(client, response.json()["job_token"])
    assert state["status"] == "DONE", state


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
    data = _run_sync(client, {"source": "EURLEX", "date": "2026-07-09"})
    assert data["status"] == "NO_PUBLICATION"
    assert data["source"] == "EURLEX"


# ------------------ MODE HOMOLOGATION (STAGING) ------------------

def _enable_staging(db):
    from fiskr.database import AppSetting
    from fiskr.settings import SETTING_REQUIRE_APPROVAL
    db.add(AppSetting(key=SETTING_REQUIRE_APPROVAL, value=True))
    db.commit()


def test_ofac_sync_staging_keeps_previous_live(db):
    # v1 en production (mode inactif)
    v1 = make_ofac_xml([("100", "Ivan", "Volkov", "1960")])
    report1 = run_ofac_sync(db, fetcher=make_fetcher(v1))
    assert report1.status == "SUCCESS"
    snap1 = db.query(Snapshot).filter(Snapshot.snapshot_id == report1.snapshot_id).first()

    # Mode homologation actif : v2 attend un pointage, v1 reste en production
    _enable_staging(db)
    v2 = make_ofac_xml([("100", "Ivan", "Volkov", "1961"), ("300", "Anna", "Orlova", "1980")])
    report2 = run_ofac_sync(db, fetcher=make_fetcher(v2))
    assert report2.status == "PENDING_REVIEW"
    assert report2.added_count == 1  # delta calcule malgre l'attente

    snap2 = db.query(Snapshot).filter(Snapshot.snapshot_id == report2.snapshot_id).first()
    assert snap2.status == "PENDING_REVIEW"
    db.refresh(snap1)
    assert snap1.status == "READY"  # non supersede tant que v2 n'est pas approuve


def test_ofac_sync_staging_hash_dedup_on_pending(db):
    _enable_staging(db)
    v1 = make_ofac_xml([("100", "Ivan", "Volkov", "1960")])
    report1 = run_ofac_sync(db, fetcher=make_fetcher(v1))
    assert report1.status == "PENDING_REVIEW"

    # Re-sync du meme fichier : pas de doublon pending quotidien
    report2 = run_ofac_sync(db, fetcher=make_fetcher(v1))
    assert report2.status == "NO_CHANGE"
    pending = db.query(Snapshot).filter(Snapshot.status == "PENDING_REVIEW").all()
    assert len(pending) == 1


def test_eurlex_sync_staging_merge_base_includes_pending(db, tmp_path):
    _enable_staging(db)
    archive_dir = tmp_path / "archives"

    # Jour 1 : 3 listes -> snapshot pending
    report1 = run_eurlex_sync(db, for_date=date(2026, 7, 8), mode="extract",
                              http_get=make_http_get(MOCK_DAILY_OJ_HTML, MOCK_ACT_HTML),
                              pdf_fetcher=stub_pdf_fetcher, archive_dir=archive_dir)
    assert report1.status == "PENDING_REVIEW"

    # Jour 2 : nouvel acte -> le pending du jour 2 reconduit les entites du pending du jour 1
    act_day2 = """
    <html><body><table>
    <tr><th>Name</th><th>Identifying information</th><th>Reasons</th><th>Date of listing</th></tr>
    <tr><td>DIMA KUZNETSOV</td><td>Born on 5.5.1985</td><td>Financing of the regime</td><td>9.7.2026</td></tr>
    </table></body></html>
    """
    report2 = run_eurlex_sync(db, for_date=date(2026, 7, 9), mode="extract",
                              http_get=make_http_get(MOCK_DAILY_OJ_HTML, act_day2),
                              pdf_fetcher=stub_pdf_fetcher, archive_dir=archive_dir)
    assert report2.status == "PENDING_REVIEW"

    day2_entities = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == report2.snapshot_id
    ).all()
    names = {e.primary_name for e in day2_entities}
    assert len(day2_entities) == 4  # 1 nouveau + 3 reconduits du pending jour 1
    assert any("KUZNETSOV" in n for n in names)
    assert any("PETROV" in n for n in names)

    # Le pending du jour 1 n'est pas supersede (decision humaine explicite)
    snap1 = db.query(Snapshot).filter(Snapshot.snapshot_id == report1.snapshot_id).first()
    assert snap1.status == "PENDING_REVIEW"


# ------------------ FIABILITE RESEAU (retries transport, UA, echecs visibles) ------------------

import fiskr.sync as sync_mod
from fiskr.sync import (
    _with_retries, _RetryableHTTP, download_to_file, http_get_text, get_sync_config,
)


def _zero_backoff_config(monkeypatch):
    """Configuration reseau sans attente entre tentatives (tests instantanes)."""
    cfg = get_sync_config()
    cfg["network"]["backoff_seconds"] = 0
    monkeypatch.setattr(sync_mod, "get_sync_config", lambda: cfg)
    return cfg


def test_network_config_defaults():
    net = get_sync_config()["network"]
    assert net["retries"] >= 1
    assert net["timeout_seconds"] > 0
    assert net["download_timeout_seconds"] > 0
    assert net["backoff_seconds"] >= 0
    assert net["user_agent"]  # UA navigateur : les portails filtrent l'UA httpx


def test_with_retries_recovers_from_transport_errors():
    import httpx
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] < 3:
            # L'erreur exacte du bug signale : elle sortait sans reprise
            raise httpx.ConnectError("connection refused")
        return "ok"

    assert _with_retries(op, "https://eur-lex.europa.eu/x", retries=3, backoff=0) == "ok"
    assert calls["n"] == 3


def test_with_retries_exhausts_then_raises_runtime():
    import httpx

    def op():
        raise httpx.ConnectError("network is down")

    with pytest.raises(RuntimeError) as exc:
        _with_retries(op, "https://eur-lex.europa.eu/x", retries=2, backoff=0)
    assert "3 tentatives" in str(exc.value)


class _FauxFlux:
    """
    Double de reponse en streaming : http_get_text lit le corps par blocs pour
    le borner (TAILLE_MAX_PAGE), donc le double doit se comporter comme un
    `client.stream(...)` httpx et pas comme un `client.get(...)`.
    """

    def __init__(self, status_code, corps, charset_encoding=None, headers=None):
        self.status_code = status_code
        self._corps = corps.encode("utf-8") if isinstance(corps, str) else corps
        self.charset_encoding = charset_encoding
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, chunk_size=65536):
        for i in range(0, len(self._corps), chunk_size):
            yield self._corps[i:i + chunk_size]


def _client_qui_sert(reponses):
    """Client double dont `stream` rend les reponses de `reponses` dans l'ordre
    (une fonction, ou une liste consommee appel apres appel)."""
    compteur = {"n": 0}

    class FauxClient:
        def stream(self, method, url, timeout=None, headers=None):
            compteur["n"] += 1
            r = reponses(compteur["n"]) if callable(reponses) else reponses[compteur["n"] - 1]
            if isinstance(r, Exception):
                raise r
            return r

    return FauxClient(), compteur


def test_http_get_text_retries_transport_then_succeeds(monkeypatch):
    import httpx
    _zero_backoff_config(monkeypatch)

    def reponses(n):
        if n < 3:
            return httpx.ConnectError("connection reset by peer")
        return _FauxFlux(200, "<html>Journal Officiel</html>")

    client, calls = _client_qui_sert(reponses)
    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: client)
    assert http_get_text("https://eur-lex.europa.eu/oj") == "<html>Journal Officiel</html>"
    assert calls["n"] == 3


def test_http_get_text_404_fails_immediately_without_retry(monkeypatch):
    _zero_backoff_config(monkeypatch)
    client, calls = _client_qui_sert(lambda n: _FauxFlux(404, "not found"))
    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: client)
    with pytest.raises(RuntimeError):
        http_get_text("https://eur-lex.europa.eu/absent")
    assert calls["n"] == 1  # erreur deterministe : aucune reprise inutile


def test_http_get_text_empty_200_is_retried(monkeypatch):
    # Anti-robot EUR-Lex : 200 a corps vide, puis la vraie page
    _zero_backoff_config(monkeypatch)
    client, calls = _client_qui_sert(
        lambda n: _FauxFlux(200, "" if n == 1 else "<html>page</html>"))
    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: client)
    assert http_get_text("https://eur-lex.europa.eu/oj") == "<html>page</html>"
    assert calls["n"] == 2

def test_download_to_file_sends_browser_user_agent(monkeypatch, tmp_path):
    import httpx
    _zero_backoff_config(monkeypatch)
    captured = {}

    class FakeStream:
        status_code = 200
        headers = {"content-length": "4"}

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size):
            yield b"data"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_stream(method, url, timeout=None, follow_redirects=None, headers=None):
        captured["headers"] = headers or {}
        return FakeStream()

    monkeypatch.setattr(httpx, "stream", fake_stream)
    dest = tmp_path / "acte.pdf"
    download_to_file("https://eur-lex.europa.eu/doc.pdf", dest, retries=0)
    assert dest.read_bytes() == b"data"
    assert captured["headers"].get("User-Agent")  # anti-robot : UA explicite


def test_download_to_file_retries_transient_status(monkeypatch, tmp_path):
    import httpx
    _zero_backoff_config(monkeypatch)
    calls = {"n": 0}

    class FakeStream:
        def __init__(self, status):
            self.status_code = status
            self.headers = {}

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size):
            yield b"pdfok"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_stream(method, url, timeout=None, follow_redirects=None, headers=None):
        calls["n"] += 1
        return FakeStream(503 if calls["n"] == 1 else 200)

    monkeypatch.setattr(httpx, "stream", fake_stream)
    dest = tmp_path / "acte2.pdf"
    download_to_file("https://eur-lex.europa.eu/doc2.pdf", dest, retries=2)
    assert calls["n"] == 2
    assert dest.read_bytes() == b"pdfok"


# Journal avec DEUX actes "mesures restrictives" pour tester l'echec partiel
MOCK_DAILY_OJ_HTML_2ACTS = """
<html><body>
<div class="daily-acts">
    <a href="./legal-content/EN/TXT/HTML/?uri=OJ:L_2026_1111">Council Regulation (EU) 2026/1111 concerning restrictive measures against Examplia</a>
    <a href="./legal-content/EN/TXT/HTML/?uri=OJ:L_2026_2222">Council Regulation (EU) 2026/2222 concerning restrictive measures against Otheria</a>
</div>
</body></html>
"""


def _flaky_getter(failing_fragment):
    """Getter qui echoue (erreur reseau simulee) pour les URLs contenant le
    fragment. NB : les URLs d'actes sont resolues RELATIVEMENT a la page du JO
    (elles contiennent aussi daily-view) — seul ojDate identifie le sommaire."""
    def getter(url, timeout=60.0):
        if "ojDate" in url:
            return MOCK_DAILY_OJ_HTML_2ACTS
        if failing_fragment and failing_fragment in url:
            raise RuntimeError("Echec apres 4 tentatives (connexion)")
        return MOCK_ACT_HTML
    return getter


def test_eurlex_partial_failure_is_success_with_visible_failures(db, tmp_path):
    report = run_eurlex_sync(
        db, for_date=date(2026, 7, 10), mode="extract",
        http_get=_flaky_getter("L_2026_2222"),
        pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path,
    )
    # Un acte sur deux scrape : la sync aboutit mais l'anomalie est VISIBLE
    assert report.status == "SUCCESS"
    assert "inaccessibles" in report.message
    failures = (report.delta_report or {}).get("fetch_failures") or []
    assert len(failures) == 1
    assert "L_2026_2222" in failures[0]["url"]


def test_eurlex_total_failure_is_error_not_no_change(db, tmp_path):
    report = run_eurlex_sync(
        db, for_date=date(2026, 7, 11), mode="extract",
        http_get=_flaky_getter("legal-content"),  # tous les actes en echec
        pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path,
    )
    # Panne reseau totale : ERROR (jamais un faux NO_CHANGE rassurant)
    assert report.status == "ERROR"
    assert "erreurs de connexion" in report.message
    failures = (report.delta_report or {}).get("fetch_failures") or []
    assert len(failures) == 2


# ------------------ EUR-LEX EN SIGNAL D'ALERTE PRECOCE (mode par defaut) ------------------

def test_eurlex_alert_mode_signals_without_inventing_designations(db, tmp_path, monkeypatch):
    """
    Le mode par defaut signale l'acte et n'ecrit AUCUNE fiche : les
    designations viennent de la liste consolidee (EUFSF), qui fait autorite.
    C'est la garantie centrale du mode alerte — ne jamais deduire une identite
    d'un texte juridique par expression reguliere.
    """
    from fiskr import sync as sync_module
    calls = []
    monkeypatch.setattr(sync_module, "scrape_act_entities",
                        lambda *a, **k: calls.append(a) or [])

    report = run_eurlex_sync(
        db, mode="alert", for_date=date(2026, 7, 8),
        http_get=make_http_get(MOCK_DAILY_OJ_HTML, MOCK_ACT_HTML),
        pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path,
    )

    assert report.status == "SUCCESS"
    assert calls == [], "le mode alerte ne doit jamais scraper les annexes"
    # Aucun snapshot, donc aucune fiche listee creee
    assert report.snapshot_id is None
    assert db.query(WatchlistEntity).count() == 0
    assert db.query(Snapshot).count() == 0
    # L'acte detecte est restitue, avec son titre
    acts = (report.delta_report or {}).get("acts") or []
    assert len(acts) == 1 and "restrictive measures" in acts[0]["title"]
    assert (report.delta_report or {}).get("mode") == "alert"


def test_eurlex_alert_mode_archives_the_official_pdf(db, tmp_path):
    """La valeur probante est conservee : le PDF officiel reste archive avec
    son empreinte, c'est lui qui fait foi devant un auditeur."""
    report = run_eurlex_sync(
        db, mode="alert", for_date=date(2026, 7, 8),
        http_get=make_http_get(MOCK_DAILY_OJ_HTML, MOCK_ACT_HTML),
        pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path,
    )
    acts = (report.delta_report or {}).get("acts") or []
    assert acts[0]["pdf_file"], "le PDF officiel doit etre archive"
    assert len(acts[0]["pdf_sha256"]) == 64
    assert (tmp_path / acts[0]["pdf_file"]).exists()


def test_eurlex_alert_mode_warns_when_consolidated_source_is_off(db, tmp_path):
    """
    Sans EUFSF actif, ce signal ne debouche sur rien : la liste UE se
    perimerait en silence. Le rapport doit le DIRE — un trou de couverture
    tacite est le pire des deux mondes.
    """
    report = run_eurlex_sync(
        db, mode="alert", for_date=date(2026, 7, 8),
        http_get=make_http_get(MOCK_DAILY_OJ_HTML, MOCK_ACT_HTML),
        pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path,
    )
    assert "EUFSF" in report.message and "désactivée" in report.message
    assert (report.delta_report or {}).get("eu_fsf_enabled") is False


def test_eurlex_alert_mode_stays_quiet_without_acts(db, tmp_path):
    html_without_measures = '<html><body><a href="/x">Regulation on bananas</a></body></html>'
    report = run_eurlex_sync(
        db, mode="alert", for_date=date(2026, 7, 8),
        http_get=make_http_get(html_without_measures, ""),
        pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path,
    )
    assert report.status == "NO_PUBLICATION"


def test_eurlex_alert_mode_makes_one_request_for_the_daily_page(db, tmp_path):
    """Une requete pour la page du jour, et c'est tout : le mode extract en
    faisait une par acte en plus. Moins solliciter, c'est moins se faire
    limiter par le portail."""
    seen = []

    def counting_getter(url):
        seen.append(url)
        return MOCK_DAILY_OJ_HTML

    run_eurlex_sync(db, mode="alert", for_date=date(2026, 7, 8), http_get=counting_getter,
                    pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path)
    assert len(seen) == 1, f"une seule requete attendue, obtenu {seen}"


# ------------------ COUCHE RESEAU : RETRY-AFTER + REQUETES CONDITIONNELLES ------------------

def test_retry_after_accepts_seconds_and_http_dates():
    from fiskr.sync import parse_retry_after
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after("  45  ") == 45.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("pas une valeur") is None
    # Une date deja passee ne doit jamais produire une attente negative
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    # Une date future donne un delai positif
    from email.utils import format_datetime
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    future = format_datetime(_dt.now(_tz.utc) + _td(seconds=60))
    assert 50 <= parse_retry_after(future) <= 61


def test_retry_after_is_honoured_and_capped(monkeypatch):
    """Le serveur decide de l'attente — mais un Retry-After delirant ne doit
    pas immobiliser un job de synchronisation."""
    from fiskr import sync as sync_module

    # sync.py importe `time` DANS _with_retries : c'est le module reel qu'il
    # faut instrumenter, pas un attribut de fiskr.sync (qui n'existe pas).
    import time as _time
    slept = []
    monkeypatch.setattr(_time, "sleep", lambda s: slept.append(s))

    attempts = {"n": 0}

    def failing():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise sync_module._RetryableHTTP("HTTP 429", retry_after=90.0)
        if attempts["n"] == 2:
            raise sync_module._RetryableHTTP("HTTP 429", retry_after=99999.0)
        return "ok"

    assert sync_module._with_retries(failing, "http://x", retries=3, backoff=2.0) == "ok"
    # 1re attente : le delai du serveur (90) prime sur le backoff local (2)
    assert slept[0] == 90.0
    # 2e : plafonnee, sans quoi le job dormirait des heures
    assert slept[1] == sync_module.MAX_RETRY_AFTER_SECONDS


def test_retryable_http_carries_the_server_delay():
    from fiskr.sync import _retryable_from_response

    class _Resp:
        status_code = 429
        headers = {"retry-after": "30"}

    err = _retryable_from_response(_Resp())
    assert err.retry_after == 30.0
    assert "Retry-After: 30s" in str(err)


def test_conditional_request_sends_validators_and_handles_304(db, tmp_path, monkeypatch):
    """
    Une source inchangee doit repondre 304 et ne rien faire telecharger :
    c'est autant de sollicitations en moins d'un portail officiel.
    """
    from fiskr import sync as sync_module

    sent_headers = {}

    class _FakeStream:
        status_code = 304
        headers = {}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_bytes(self, chunk_size=None): return iter(())

    def fake_stream(method, url, **kwargs):
        sent_headers.update(kwargs.get("headers") or {})
        return _FakeStream()

    # Meme motif : httpx est importe dans download_to_file
    import httpx as _httpx
    monkeypatch.setattr(_httpx, "stream", fake_stream)

    dest = tmp_path / "unused.xml"
    outcome = sync_module.download_to_file(
        "http://source.example/list.xml", dest,
        validators={"etag": 'W/"abc123"', "last_modified": "Wed, 01 Jul 2026 10:00:00 GMT"})

    assert sent_headers.get("If-None-Match") == 'W/"abc123"'
    assert sent_headers.get("If-Modified-Since") == "Wed, 01 Jul 2026 10:00:00 GMT"
    assert outcome["not_modified"] is True
    assert not dest.exists(), "un 304 ne doit rien ecrire sur disque"


def test_validators_round_trip_through_the_database(db):
    """Les validateurs vivent en base : le demon travailleur, les processus
    API et une relance manuelle partagent le meme etat de fraicheur."""
    from fiskr.sync import stored_validators, remember_validators

    assert stored_validators(db, "OFAC") == {}
    remember_validators(db, "OFAC", 'W/"v1"', "Wed, 01 Jul 2026 10:00:00 GMT")
    assert stored_validators(db, "OFAC") == {
        "etag": 'W/"v1"', "last_modified": "Wed, 01 Jul 2026 10:00:00 GMT"}
    # Une source qui cesse d'annoncer des validateurs perd les anciens :
    # mieux vaut retelecharger que conditionner sur un validateur perime
    remember_validators(db, "OFAC", None, None)
    assert stored_validators(db, "OFAC") == {}
    # Cloisonnement par source
    remember_validators(db, "UN", 'W/"un1"', None)
    assert stored_validators(db, "UN") == {"etag": 'W/"un1"'}
    assert stored_validators(db, "OFAC") == {}


def test_custom_headers_reach_the_request(tmp_path, monkeypatch):
    """
    Les en-tetes d'authentification (sync.<source>.auth_headers) partent avec
    la requete et PRIMENT sur les en-tetes navigateur : c'est la porte
    d'entree des fournisseurs a cle d'API (Authorization: Bearer...).
    """
    from fiskr import sync as sync_module

    sent_headers = {}

    class _FakeStream:
        status_code = 200
        headers = {"content-length": "2"}

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_bytes(self, chunk_size=None): return iter((b"ok",))

    def fake_stream(method, url, **kwargs):
        sent_headers.update(kwargs.get("headers") or {})
        return _FakeStream()

    import httpx as _httpx
    monkeypatch.setattr(_httpx, "stream", fake_stream)

    sync_module.download_to_file(
        "http://premium.example/feed.xml", tmp_path / "feed.xml",
        headers={"Authorization": "Bearer secret-123", "User-Agent": "Fiskr-Premium"})

    assert sent_headers.get("Authorization") == "Bearer secret-123"
    # L'en-tete passe en argument PRIME sur celui du navigateur
    assert sent_headers.get("User-Agent") == "Fiskr-Premium"


def test_opensanctions_config_carries_auth_headers(monkeypatch):
    """Les auth_headers declares en config arrivent jusqu'au cycle generique
    pour les sources du registre — le jour d'un flux sous cle, tout est deja
    cable, il ne manque que le contrat."""
    from fiskr import sync as sync_module

    monkeypatch.setitem(
        sync_module.config.setdefault("sync", {}), "adb",
        {"enabled": False, "auth_headers": {"Authorization": "Bearer os-key"}})
    cfg = sync_module.get_sync_config()
    assert cfg["adb"]["auth_headers"] == {"Authorization": "Bearer os-key"}


def test_sync_reports_server_filters(client):
    """
    Les filtres serveur `source` et `status` portent sur TOUT l'historique
    (borné par `limit`), pas sur la seule page affichée : c'est pourquoi ils
    sont côté serveur et non côté client.
    """
    from fiskr.database import SessionLocal, SyncReport
    db = SessionLocal()
    made = []
    try:
        for src, st in [("OFAC", "SUCCESS"), ("OFAC", "ERROR"), ("UN", "SUCCESS")]:
            r = SyncReport(source=src, status=st, trigger="MANUAL",
                           added_count=0, modified_count=0, removed_count=0)
            db.add(r); db.flush(); made.append(r.id)
        db.commit()

        by_source = client.get("/api/sync/reports?source=OFAC").json()
        assert by_source and all(r["source"] == "OFAC" for r in by_source)

        by_status = client.get("/api/sync/reports?status=ERROR").json()
        assert by_status and all(r["status"] == "ERROR" for r in by_status)

        both = client.get("/api/sync/reports?source=OFAC&status=SUCCESS").json()
        assert both and all(r["source"] == "OFAC" and r["status"] == "SUCCESS" for r in both)
    finally:
        db.query(SyncReport).filter(SyncReport.id.in_(made)).delete(synchronize_session=False)
        db.commit(); db.close()

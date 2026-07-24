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
    report = run_eurlex_sync(db, for_date=date(2026, 6, 8), http_get=make_http_get(daily_html, act_html),
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
    report1 = run_eurlex_sync(db, for_date=date(2026, 7, 8), http_get=make_http_get(MOCK_DAILY_OJ_HTML, MOCK_ACT_HTML),
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
    report2 = run_eurlex_sync(db, for_date=date(2026, 7, 9), http_get=make_http_get(MOCK_DAILY_OJ_HTML, act_day2),
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
    report1 = run_eurlex_sync(db, for_date=date(2026, 7, 8), http_get=make_http_get(MOCK_DAILY_OJ_HTML, MOCK_ACT_HTML),
                              pdf_fetcher=stub_pdf_fetcher, archive_dir=archive_dir)
    assert report1.status == "PENDING_REVIEW"

    # Jour 2 : nouvel acte -> le pending du jour 2 reconduit les entites du pending du jour 1
    act_day2 = """
    <html><body><table>
    <tr><th>Name</th><th>Identifying information</th><th>Reasons</th><th>Date of listing</th></tr>
    <tr><td>DIMA KUZNETSOV</td><td>Born on 5.5.1985</td><td>Financing of the regime</td><td>9.7.2026</td></tr>
    </table></body></html>
    """
    report2 = run_eurlex_sync(db, for_date=date(2026, 7, 9), http_get=make_http_get(MOCK_DAILY_OJ_HTML, act_day2),
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


def test_http_get_text_retries_transport_then_succeeds(monkeypatch):
    import httpx
    _zero_backoff_config(monkeypatch)
    calls = {"n": 0}

    class FakeResponse:
        status_code = 200
        text = "<html>Journal Officiel</html>"

    class FakeClient:
        def get(self, url, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("connection reset by peer")
            return FakeResponse()

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: FakeClient())
    assert http_get_text("https://eur-lex.europa.eu/oj") == "<html>Journal Officiel</html>"
    assert calls["n"] == 3


def test_http_get_text_404_fails_immediately_without_retry(monkeypatch):
    _zero_backoff_config(monkeypatch)
    calls = {"n": 0}

    class FakeResponse:
        status_code = 404
        text = "not found"

    class FakeClient:
        def get(self, url, timeout=None):
            calls["n"] += 1
            return FakeResponse()

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: FakeClient())
    with pytest.raises(RuntimeError):
        http_get_text("https://eur-lex.europa.eu/absent")
    assert calls["n"] == 1  # erreur deterministe : aucune reprise inutile


def test_http_get_text_empty_200_is_retried(monkeypatch):
    # Anti-robot EUR-Lex : 200 a corps vide, puis la vraie page
    _zero_backoff_config(monkeypatch)
    calls = {"n": 0}

    class FakeClient:
        def get(self, url, timeout=None):
            calls["n"] += 1

            class R:
                status_code = 200
                text = "" if calls["n"] == 1 else "<html>page</html>"
            return R()

    monkeypatch.setattr(sync_mod, "_get_shared_client", lambda: FakeClient())
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
        db, for_date=date(2026, 7, 10),
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
        db, for_date=date(2026, 7, 11),
        http_get=_flaky_getter("legal-content"),  # tous les actes en echec
        pdf_fetcher=stub_pdf_fetcher, archive_dir=tmp_path,
    )
    # Panne reseau totale : ERROR (jamais un faux NO_CHANGE rassurant)
    assert report.status == "ERROR"
    assert "erreurs de connexion" in report.message
    failures = (report.delta_report or {}).get("fetch_failures") or []
    assert len(failures) == 2

"""
Robustesse operationnelle du cahier de tests :
- serialisation des jobs lourds (jamais deux backtests RUNNING en meme temps :
  claim_next les enchaine au lieu de les cumuler — deux passes simultanees ont
  deja epuise la RAM d'une production) ;
- panel d'auto-backtest genere automatiquement quand aucun n'existe (cause la
  plus frequente d'un cahier de tests « qui ne demarre pas » apres une sync) ;
- rapport de backtest : mode delta par defaut (memes chiffres, une seule passe
  complete), mode full conserve avec une regle candidate ;
- EUR-Lex : official_reference porte l'acte, repli PDF fonctionnel (pypdf
  simule) et gracieux sans pypdf, mode extract par defaut.
"""
import sys
import types
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from fiskr import jobs
from fiskr.database import get_db, Job, Snapshot, WatchlistEntity
from fiskr.sync import scrape_act_entities, _scrape_archived_pdf, get_sync_config


@pytest.fixture()
def db():
    session = next(get_db())
    yield session
    session.rollback()
    try:
        session.query(Job).filter(Job.token.like("test-btops-%")).delete(synchronize_session=False)
        session.query(WatchlistEntity).filter(
            WatchlistEntity.snapshot_id.like("test-btops-%")).delete(synchronize_session=False)
        session.query(Snapshot).filter(
            Snapshot.snapshot_id.like("test-btops-%")).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


# ------------------ SERIALISATION DES JOBS LOURDS ------------------

def test_claim_next_serializes_heavy_kinds(db):
    running = Job(token=f"test-btops-{uuid.uuid4().hex[:6]}", kind="backtest",
                  status="RUNNING", heartbeat_at=datetime.utcnow())
    queued_bt = Job(token=f"test-btops-{uuid.uuid4().hex[:6]}", kind="backtest",
                    status="QUEUED")
    queued_other = Job(token=f"test-btops-{uuid.uuid4().hex[:6]}", kind="import",
                       status="QUEUED", priority=200)   # priorite PIRE que le backtest
    db.add_all([running, queued_bt, queued_other])
    db.commit()

    # Un backtest tourne : le backtest en file est saute, l'import (pourtant
    # moins prioritaire) passe — la file ne se fige pas derriere le lourd.
    claimed_id = jobs.claim_next(db, claimer="test-btops")
    assert claimed_id == queued_other.id

    # Le backtest RUNNING se termine : le backtest en file devient eligible
    db.query(Job).filter(Job.id == running.id).update({"status": "DONE"})
    db.commit()
    assert jobs.claim_next(db, claimer="test-btops") == queued_bt.id


def test_serial_kind_busy_helper(db):
    other = Job(token=f"test-btops-{uuid.uuid4().hex[:6]}", kind="backtest",
                status="RUNNING", heartbeat_at=datetime.utcnow())
    db.add(other)
    db.commit()
    assert jobs._serial_kind_busy(db, "backtest") is True
    assert jobs._serial_kind_busy(db, "backtest", exclude_job_id=other.id) is False
    assert jobs._serial_kind_busy(db, "import") is False   # genre non serialise


# ------------------ PANEL D'AUTO-BACKTEST AUTO-GENERE ------------------

def test_auto_backtest_panel_generated_when_missing(db):
    from fiskr.tasks import _resolve_auto_backtest_panel
    from fiskr.backtest import TEST_PANEL_FILE_TYPE

    # Une petite liste en production garantit une matiere premiere au generateur
    sid = f"test-btops-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=sid, file_type="WATCHLIST_UN",
                    file_name="test-btops.xml", file_hash=uuid.uuid4().hex,
                    record_count=1, uploaded_at=datetime.utcnow(), status="READY"))
    db.add(WatchlistEntity(
        snapshot_id=sid, entity_id=f"test-btops-{uuid.uuid4().hex[:6]}",
        entity_type="I", primary_name="Btops Panel Seed",
        individual_name_parsed={"first_name": "Btops", "last_name": "Seed", "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=[], countries={}, entity_checksum="x" * 8))
    db.commit()

    panel_id = _resolve_auto_backtest_panel(db)
    # Peu importe qu'un panel existait deja (autres tests) ou qu'il vienne
    # d'etre genere : l'automatisme ne doit JAMAIS s'abstenir faute de panel
    # tant qu'une production existe.
    assert panel_id is not None
    panel = db.query(Snapshot).filter(Snapshot.snapshot_id == panel_id).first()
    assert panel is not None
    assert panel.file_type == TEST_PANEL_FILE_TYPE
    assert panel.status == "READY" and (panel.record_count or 0) > 0


# ------------------ EUR-LEX : REFERENCE PROBANTE + REPLI PDF ------------------

_ACT_TABLE_HTML = """
<html><body><table>
<tr><td>Nom</td><td>Informations d'identification</td><td>Motifs</td></tr>
<tr><td>Viktor BTOPSOV</td><td>born 12.3.1970 in Minsk</td><td>Association with the regime</td></tr>
</table></body></html>
"""


def test_scraped_entities_carry_official_reference():
    entities = scrape_act_entities(
        _ACT_TABLE_HTML,
        "Council Implementing Regulation (EU) 2026/999",
        "http://eur-lex.example/act")
    assert entities, "l'annexe tabulaire doit produire au moins une fiche"
    assert all(e["official_reference"] == "Council Implementing Regulation (EU) 2026/999"
               for e in entities)


def test_pdf_fallback_extracts_from_archived_pdf(tmp_path, monkeypatch):
    # pypdf simule : le repli doit fonctionner sans dependance reelle
    class _FakePage:
        def extract_text(self):
            return "ANNEX  1. Ivan BTOPSKI (born 01.01.1975), member of the council; 2. Olga BTOPSKA, adviser;"

    class _FakeReader:
        def __init__(self, path):
            assert Path(path).exists()
            self.pages = [_FakePage()]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=_FakeReader))

    pdf_path = tmp_path / "act.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    act = {"url": "http://eur-lex.example/act", "title": "Regulation (EU) 2026/1000",
           "pdf_file": "act.pdf"}
    entities = _scrape_archived_pdf(act, tmp_path)
    names = {e["primary_name"] for e in entities}
    assert "Ivan BTOPSKI" in names
    assert all("(extrait du PDF)" in e["origin"] for e in entities)
    assert all(e["official_reference"] == "Regulation (EU) 2026/1000" for e in entities)


def test_pdf_fallback_graceful_without_pypdf(tmp_path, monkeypatch):
    # Sans pypdf : le repli s'abstient sans lever (l'acte reste en echec visible)
    monkeypatch.setitem(sys.modules, "pypdf", None)   # import pypdf -> ImportError
    pdf_path = tmp_path / "act.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")
    act = {"url": "u", "title": "t", "pdf_file": "act.pdf"}
    assert _scrape_archived_pdf(act, tmp_path) == []


def test_eurlex_default_mode_is_extract():
    assert get_sync_config()["eurlex"]["mode"] == "extract"


# ------------------ EXTRACTEUR HTML DES LISTES D'ALERTE (HK SFC) ------------------

def test_html_table_extractor_ignores_scripts_and_nested_tables(tmp_path):
    """La page SFC embarque du JavaScript et des tableaux de mise en page :
    ni l'un ni l'autre ne doivent devenir des « noms » de fiches."""
    from fiskr.ingest import parse_hk_sfc_alert_list

    html = """
    <html><body>
    <table><tr><td>layout</td><td>
        <table>
            <tr><th>Name</th><th>Website</th><th>Date</th></tr>
            <tr><td>Alpha Fake Broker Limited<script>var config = {%s};</script></td>
                <td>www.alpha-fake.example</td><td>2026-05-01</td></tr>
            <tr><td>Beta Clone Securities</td><td>www.beta-clone.example</td><td>2026-05-02</td></tr>
            <tr><td>%s</td><td>www.junk.example</td><td>2026-05-03</td></tr>
        </table>
    </td></tr></table>
    </body></html>
    """ % ('"x": "' + "A" * 3000 + '"', "Texte éditorial " * 40)
    path = tmp_path / "sfc.html"
    path.write_text(html, encoding="utf-8")

    entities = list(parse_hk_sfc_alert_list(str(path)))
    names = [e["primary_name"] for e in entities]
    assert "Alpha Fake Broker Limited" in names, names
    assert "Beta Clone Securities" in names
    # Aucun nom démesuré : ni script avalé, ni ligne éditoriale
    assert all(len(n) <= 200 for n in names), [len(n) for n in names]
    assert len(entities) == 2

"""
Listes d'alerte de regulateurs (HK SFC, AMF) et exclusions de bailleurs
(Banque mondiale).

Ces trois listes ne sont PAS des listes de sanctions, et la distinction n'est
pas cosmetique : une touche n'emporte aucune obligation de gel. Chacune recoit
donc son propre type de liste, donc son propre seuil et ses propres
statistiques, au lieu d'etre versee dans le flux des gels d'avoirs.

Les listes de regulateurs partagent un lecteur : elles sont publiees sous forme
de tableau (HTML le plus souvent), leurs intitules de colonnes n'ont aucune
convention commune, et elles ne portent aucun identifiant technique. C'est
cette tolerance que les tests verrouillent.

Reserve identique au reste du lot : ecrits d'apres les formats publies, valides
sur des jeux d'essai, pas contre les fichiers reels (acces reseau ferme).
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, WatchlistEntity
from fiskr.ingest import (
    parse_hk_sfc_alert_list, parse_amf_blacklist, parse_worldbank_debarred_json,
    _read_html_table_rows, _alert_stable_id,
)
from fiskr.sync import (
    run_hk_sfc_sync, run_amf_sync, run_worldbank_sync, get_sync_config,
    _alert_list_suffix,
)
from fiskr.settings import SYNC_SOURCES


# Page telle qu'un regulateur la publie : un tableau de mise en page,
# puis le tableau de donnees.
SFC_HTML = """<html><body>
<table><tr><td>Accueil</td><td>Contact</td></tr></table>
<table>
  <tr><th>Name</th><th>Website</th><th>Type</th><th>Date of publication</th><th>Remarks</th></tr>
  <tr><td>Golden Dragon Capital Ltd</td><td>golden-dragon.example; gd-capital.example</td>
      <td>Entity</td><td>2025-11-04</td><td>Suspected unlicensed activity</td></tr>
  <tr><td></td><td>fake-hsbc.example</td><td>Entity</td><td>2026-01-15</td>
      <td>Impersonation of a licensed corporation</td></tr>
  <tr><td>Chan Tai Man</td><td></td><td>Individual</td><td>2026-02-02</td><td>Unlicensed dealing</td></tr>
</table></body></html>"""

AMF_CSV = (
    "Dénomination,Site internet,Catégorie,Date de publication,Motif\n"
    "Trading Alpha SAS,trading-alpha.example,Personne morale,2025-06-01,Offre non autorisée\n"
    "Jean DUPONT,,Personne physique,2025-06-02,Démarchage illicite\n"
    "Gamma Ltd,gamma.example,,2025-06-03,\n"
)

WORLDBANK_JSON = {
    "response": {
        "ZPROCSUPP": [
            {
                "SUPP_ID": "12345",
                "SUPP_NAME": "Beta Construction Ltd",
                "COUNTRY_NAME": "Kenya",
                "SUPP_CITY": "Nairobi",
                "SUPP_ADDR": "PO Box 1, Nairobi",
                "DEBAR_FROM_DATE": "2023-04-01",
                "DEBAR_TO_DATE": "2026-04-01",
                "DEBAR_REASON": "Fraudulent practice",
            },
            {"SUPP_NAME": "", "COUNTRY_NAME": "Sans nom, ignorée"},
            {"SUPP_NAME": "Gamma Engineering", "DEBAR_FROM_DATE": "2024-01-15"},
        ]
    }
}


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'alert_test.sqlite3'}")
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


# ------------------ EXTRACTION DU TABLEAU D'UNE PAGE ------------------

def test_html_reader_picks_the_data_table_not_the_layout_one(tmp_path):
    rows = list(_read_html_table_rows(_file(tmp_path, "p.html", SFC_HTML)))
    assert len(rows) == 3
    assert rows[0]["Name"] == "Golden Dragon Capital Ltd"
    # Le tableau de navigation, plus court, est ignore
    assert "Accueil" not in json.dumps(rows, ensure_ascii=False)


def test_url_extension_selects_the_reader():
    assert _alert_list_suffix("https://x/liste.json") == ".json"
    assert _alert_list_suffix("https://x/liste.csv?v=2") == ".csv"
    assert _alert_list_suffix("https://x/liste.xlsx") == ".xlsx"
    # Sans extension, la forme la plus repandue de ces listes : une page web
    assert _alert_list_suffix("https://www.sfc.hk/en/alert-list") == ".html"


# ------------------ HONG KONG SFC ------------------

def test_hk_sfc_maps_an_alert_page(tmp_path):
    entities = list(parse_hk_sfc_alert_list(_file(tmp_path, "sfc.html", SFC_HTML)))
    assert len(entities) == 3
    by_name = {e["primary_name"]: e for e in entities}

    dragon = by_name["Golden Dragon Capital Ltd"]
    assert dragon["entity_type"] == "E"
    # Les domaines supplementaires restent cherchables comme alias faibles :
    # un virement libelle au nom du site doit ressortir.
    assert dragon["aliases"]["low_priority"] == ["gd-capital.example"]
    assert dragon["websites"] == ["golden-dragon.example", "gd-capital.example"]
    assert dragon["listed_on"] == "2025-11-04"
    assert dragon["designating_state"] == "HK"
    assert dragon["official_reference"] == "SFC Hong Kong — Alert List (maj 2025-11-04)"
    # Le libelle dit que c'est une MISE EN GARDE, pas un gel
    assert "Mise en garde" in dragon["designation_reasons"]
    assert "SFC" in dragon["designation_reasons"]

    # Une ligne sans nom mais avec un site : le site fait office d'identite,
    # sans quoi la fiche serait perdue.
    assert "fake-hsbc.example" in by_name
    assert by_name["Chan Tai Man"]["entity_type"] == "I"


def test_hk_sfc_keys_are_stable_across_publications(tmp_path):
    """Sans identifiant technique, deux publications identiques doivent donner
    les memes cles — sinon chaque delta serait un remplacement complet."""
    first = [e["entity_id"] for e in parse_hk_sfc_alert_list(_file(tmp_path, "a.html", SFC_HTML))]
    second = [e["entity_id"] for e in parse_hk_sfc_alert_list(_file(tmp_path, "b.html", SFC_HTML))]
    assert first == second
    assert all(k.startswith("HKSFC-") for k in first)
    # Deux homonymes sur deux domaines restent deux fiches distinctes
    assert _alert_stable_id("ACME", "a.example") != _alert_stable_id("ACME", "b.example")


def test_hk_sfc_sync_lifecycle(db):
    report = run_hk_sfc_sync(db, fetcher=_fetcher(SFC_HTML))
    assert report.status == "SUCCESS"
    assert report.source == "HKSFC"
    assert report.added_count == 3
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_HK_SFC"
    assert run_hk_sfc_sync(db, fetcher=_fetcher(SFC_HTML)).status == "NO_CHANGE"


# ------------------ AMF ------------------

def test_amf_does_not_confuse_personne_morale_with_a_natural_person(tmp_path):
    """
    « Personne morale » CONTIENT « person » : chercher ce fragment classait
    toute société française en personne physique. Le type d'entité est une
    composante de la clé de blocking — l'erreur écarterait des candidats au
    lieu de se voir.
    """
    entities = {e["primary_name"]: e
                for e in parse_amf_blacklist(_file(tmp_path, "amf.csv", AMF_CSV))}
    assert entities["Trading Alpha SAS"]["entity_type"] == "E"
    assert entities["Jean DUPONT"]["entity_type"] == "I"
    # Catégorie absente : personne morale par défaut (le cas le plus fréquent)
    assert entities["Gamma Ltd"]["entity_type"] == "E"


def test_amf_reads_french_column_headings(tmp_path):
    alpha = {e["primary_name"]: e
             for e in parse_amf_blacklist(_file(tmp_path, "amf.csv", AMF_CSV))}["Trading Alpha SAS"]
    assert alpha["websites"] == ["trading-alpha.example"]
    assert alpha["listed_on"] == "2025-06-01"
    assert alpha["designating_state"] == "FR"
    assert alpha["official_reference"] == "AMF — listes noires (maj 2025-06-01)"
    assert "Offre non autorisée" in alpha["additional_informations"]
    assert "AMF" in alpha["designation_reasons"]


def test_amf_sync_lifecycle(db):
    report = run_amf_sync(db, fetcher=_fetcher(AMF_CSV))
    assert report.status == "SUCCESS"
    assert report.source == "AMF"
    assert report.added_count == 3
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_AMF"


# ------------------ BANQUE MONDIALE ------------------

def test_worldbank_maps_a_debarment_with_its_end_date(tmp_path):
    entities = {e["entity_id"]: e for e in parse_worldbank_debarred_json(
        _file(tmp_path, "wb.json", json.dumps(WORLDBANK_JSON)))}
    # La ligne sans nom est ecartee : rien a cribler
    assert set(entities) == {"WB-12345", *(k for k in entities if k != "WB-12345")}
    assert len(entities) == 2

    beta = entities["WB-12345"]
    assert beta["entity_type"] == "E"
    assert beta["primary_name"] == "Beta Construction Ltd"
    assert beta["country"] == "Kenya"
    assert beta["city"] == "Nairobi"
    assert beta["listed_on"] == "2023-04-01"
    # Une exclusion a une FIN : c'est une radiation programmee, pas du texte
    assert beta["delisted_on"] == "2026-04-01"
    assert "Fraudulent practice" in beta["additional_informations"]
    assert beta["other_registration_ids"] == [
        {"id_type": "WorldBankSupplierId", "number": "12345"}]


def test_worldbank_tolerates_a_different_json_envelope(tmp_path):
    """L'enveloppe varie selon le point d'entree : la liste est cherchee, pas
    supposee a un chemin fixe."""
    plain = [{"SUPP_ID": "9", "SUPP_NAME": "Direct List Co"}]
    nested = {"a": {"b": {"c": plain}}}
    for label, payload in (("liste nue", plain), ("imbriquee", nested)):
        entities = list(parse_worldbank_debarred_json(
            _file(tmp_path, f"{label}.json", json.dumps(payload))))
        assert [e["entity_id"] for e in entities] == ["WB-9"], label


def test_worldbank_sync_lifecycle(db):
    payload = json.dumps(WORLDBANK_JSON)
    report = run_worldbank_sync(db, fetcher=_fetcher(payload))
    assert report.status == "SUCCESS"
    assert report.source == "WORLDBANK"
    assert report.added_count == 2
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == "WATCHLIST_WORLDBANK"
    listed = db.query(WatchlistEntity).filter(
        WatchlistEntity.entity_id == "WB-12345").first()
    assert listed.delisted_on == "2026-04-01"
    assert run_worldbank_sync(db, fetcher=_fetcher(payload)).status == "NO_CHANGE"


# ------------------ CONFIGURATION ------------------

def test_the_three_sources_are_off_by_default_and_schedulable():
    cfg = get_sync_config()
    for key in ("hk_sfc", "amf", "worldbank"):
        assert cfg[key]["enabled"] is False, key
        assert key in SYNC_SOURCES, key


def test_each_list_keeps_its_own_type_so_it_can_be_thresholded_apart():
    """
    Le point de conception qui compte : une mise en garde et un gel des avoirs
    ne partagent pas le meme type de liste, donc pas le meme seuil.
    """
    from fiskr.api import WATCHLIST_FILE_TYPES
    for file_type in ("WATCHLIST_HK_SFC", "WATCHLIST_AMF", "WATCHLIST_WORLDBANK"):
        assert file_type in WATCHLIST_FILE_TYPES
    assert len({"WATCHLIST_HK_SFC", "WATCHLIST_AMF", "WATCHLIST_WORLDBANK",
                "WATCHLIST_OFAC"}) == 4


# ------------------ BOUT EN BOUT ------------------

@pytest.fixture
def api():
    from fastapi.testclient import TestClient
    from fiskr.api import app
    from fiskr.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin_alert", "full_name": "admin_alert",
        "role": "admin", "roles": ["admin"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_end_to_end_hk_sfc_upload_then_screen(api):
    import uuid
    marker = uuid.uuid4().hex[:6].upper()
    page = SFC_HTML.replace("Golden Dragon Capital Ltd", f"Golden Dragon Capital {marker} Ltd")

    response = api.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_HK_SFC"},
        files={"file": (f"sfc_{marker}.html", page, "text/html")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["record_count"] == 3

    result = api.post("/api/screen", json={
        "client_id": f"test_sfc_{marker}",
        "client_type": "PM",
        "client_company_name": f"Golden Dragon Capital {marker} Ltd",
        "client_countries": {"nationality": [], "residence": [],
                             "birth_country": [], "registration_country": ["HK"]},
        "screening_lists": ["WATCHLIST_HK_SFC"],
    })
    assert result.status_code == 200, result.text
    best = result.json()["best_match"]
    assert best is not None and best["status"] == "ALERT"
    assert best["watchlist_entity"]["_list_type"] == "WATCHLIST_HK_SFC"


# ------------------ ROBUSTESSE DE LECTURE ------------------

def test_format_is_detected_from_content_not_from_the_file_name(tmp_path):
    """
    Les régulateurs servent leur liste à une URL SANS extension. Se fier au nom
    du fichier ferait lire un CSV comme une page web, et l'import ressortirait
    à zéro fiche — c'est-à-dire silencieusement, sous les traits d'une liste
    vide plutôt que d'une erreur.
    """
    # Contenu CSV dans un fichier nommé .html
    menteur = _file(tmp_path, "liste.html", AMF_CSV)
    assert len(list(parse_amf_blacklist(menteur))) == 3

    # Contenu HTML dans un fichier nommé .csv
    inverse = _file(tmp_path, "liste.csv", SFC_HTML)
    assert len(list(parse_hk_sfc_alert_list(inverse))) == 3

    # Contenu JSON dans un fichier sans extension du tout
    plat = _file(tmp_path, "alertlist", json.dumps(
        [{"Name": "Delta Ltd", "Website": "delta.example", "Type": "Entity"}]))
    entities = list(parse_hk_sfc_alert_list(plat))
    assert [e["primary_name"] for e in entities] == ["Delta Ltd"]


def test_a_record_without_a_country_is_reachable_through_the_wildcard(tmp_path):
    """
    COUNTRY_ISO est une composante de la clé de blocking : une fiche sans pays
    tombe dans la partition « pays inconnu », que ne rejoint AUCUN client ayant
    un pays. Ces listes n'en publiant presque jamais, elles seraient
    structurellement inatteignables.

    La correction est au niveau du MOTEUR, pas du parseur : le client interroge
    aussi la variante « pays inconnu » de ses propres clés. Un premier jet
    remplissait la juridiction du régulateur dans la fiche ; c'était une
    rustine, et elle RESTREIGNAIT l'atteignabilité — une entité signalée par la
    SFC n'aurait été visible que des clients hongkongais, alors qu'un courtier
    frauduleux vise des victimes partout.
    """
    from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys
    from fiskr.config import config

    dragon = list(parse_hk_sfc_alert_list(_file(tmp_path, "sfc.html", SFC_HTML)))[0]
    # Aucune géographie inventée : la source n'en publie pas
    assert dragon["countries"]["jurisdiction_country"] == []
    # ...mais l'autorité qui signale est conservée, elle
    assert dragon["designating_state"] == "HK"

    listed_keys = set(generate_blocking_keys(dragon, config))
    client = {
        "client_id": "c1", "client_type": "PM",
        "client_company_name": dragon["primary_name"],
        # Client FRANÇAIS : c'est le cas qui compte
        "client_countries": {"registration_country": ["FR"]},
    }
    assert not listed_keys & set(generate_blocking_keys(client, config))
    assert listed_keys & set(lookup_blocking_keys(client, config))

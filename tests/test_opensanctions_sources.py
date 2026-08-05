"""
Sources du registre OpenSanctions (fiskr/sources.py) : onze listes publiques
branchees sur le lecteur targets.simple.csv commun (celui de PEP et de la voie
de secours SECO).

Ce que ces tests verrouillent, c'est le REGISTRE : chaque source declaree doit
etre entierement operationnelle (config, runner, alias d'API, type de liste,
planification, import manuel) sans qu'aucun code par-source n'existe. Un test
parametre sur le registre entier garantit qu'une source ajoutee demain herite
des memes garanties sans nouveau test a ecrire.

Reserve identique aux autres connecteurs : ecrits d'apres le format publie
(targets.simple.csv est commun a tous les datasets du fournisseur), valides
sur un jeu d'essai, pas contre les fichiers reels (acces reseau ferme). La
sonde tools/diagnostic_sources.py verifie les slugs depuis le serveur.
"""
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, WatchlistEntity, WATCHLIST_FILE_TYPES
from fiskr.sources import (
    OPENSANCTIONS_SOURCES, OPENSANCTIONS_BY_KEY, OPENSANCTIONS_BY_FILE_TYPE,
    opensanctions_default_url,
)
from fiskr.sync import OPENSANCTIONS_RUNNERS, get_sync_config
from fiskr.settings import SYNC_SOURCES


# Jeu d'essai au format targets.simple.csv — les colonnes que le lecteur
# exploite : id, schema, name, aliases, birth_date, countries, addresses,
# identifiers, sanctions, phones, emails, first_seen (multi-valeurs par ;)
TARGETS_CSV = (
    "id,schema,name,aliases,birth_date,countries,addresses,identifiers,"
    "sanctions,phones,emails,first_seen\n"
    "Q100,Person,Viktor Test,V. Test;Vik Test,1969-05-12,ru;by,"
    "12 Test Street Minsk,PASS-123,Programme Alpha,,v@test.example,2023-04-01\n"
    "Q200,Company,Testovaya Kompaniya OOO,,,ru,,INN-7701234567,"
    "Programme Beta,,,2024-01-15\n"
)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'os_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _fetcher(content):
    return lambda url, dest: Path(dest).write_text(content, encoding="utf-8")


# ------------------ CYCLE COMPLET, PARAMETRE SUR LE REGISTRE ------------------

@pytest.mark.parametrize("run_key", sorted(OPENSANCTIONS_BY_KEY))
def test_each_registry_source_syncs_to_a_ready_snapshot(db, run_key):
    """
    La garantie centrale : CHAQUE source du registre, sans code propre,
    deroule le cycle complet — snapshot READY, entites au schema pivot,
    prefixe d'identifiant et provenance de la source.
    """
    src = OPENSANCTIONS_BY_KEY[run_key]
    report = OPENSANCTIONS_RUNNERS[run_key](db, fetcher=_fetcher(TARGETS_CSV))

    assert report.status == "SUCCESS", report.message
    assert report.source == src.source
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.file_type == src.file_type
    assert snap.status == "READY"
    assert snap.record_count == 2

    person = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == snap.snapshot_id,
        WatchlistEntity.entity_type == "I").first()
    assert person.entity_id == f"{src.id_prefix}-Q100"
    # La quality gate normalise la casse a la persistance
    assert person.primary_name.upper() == "VIKTOR TEST"
    assert person.origin == src.origin
    assert person.designation_reasons == src.designation_reasons
    assert person.dates_of_birth == ["1969-05-12"]
    assert "RU" in (person.countries or {}).get("citizenship", [])

    company = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == snap.snapshot_id,
        WatchlistEntity.entity_type == "E").first()
    assert company.entity_id == f"{src.id_prefix}-Q200"

    # Idempotence : le meme contenu ne cree pas un second snapshot
    assert OPENSANCTIONS_RUNNERS[run_key](db, fetcher=_fetcher(TARGETS_CSV)).status == "NO_CHANGE"


# ------------------ LA CHECKLIST EXECUTABLE DU REGISTRE ------------------

def test_registry_sources_are_off_by_default_and_schedulable():
    cfg = get_sync_config()
    for key in OPENSANCTIONS_BY_KEY:
        assert cfg[key]["enabled"] is False, key
        assert key in SYNC_SOURCES, key
        # URL vide en config -> derivee du dataset declare
        assert cfg[key]["url"] == opensanctions_default_url(
            OPENSANCTIONS_BY_KEY[key].dataset)


def test_registry_sources_are_wired_into_the_api():
    """Alias de POST /api/sync/run, runners de la file, import manuel :
    tout se derive du registre — c'est ce que ce test fige."""
    from fiskr.api import _SYNC_RUNNERS, _SYNC_SOURCE_ALIASES

    for key, src in OPENSANCTIONS_BY_KEY.items():
        assert _SYNC_RUNNERS[key] is OPENSANCTIONS_RUNNERS[key]
        assert _SYNC_SOURCE_ALIASES[src.source] == (key, src.source)


def test_each_source_keeps_its_own_list_type_so_it_can_be_thresholded_apart():
    """
    Meme point de conception que HK SFC/AMF/Banque mondiale : une exclusion
    de bailleur, une designation nationale et une sanction ukrainienne ne
    partagent ni le meme risque ni le meme seuil — chaque source garde son
    type de liste.
    """
    types = [s.file_type for s in OPENSANCTIONS_SOURCES]
    assert len(set(types)) == len(types), "types de liste dupliqués"
    for file_type in types:
        assert file_type in WATCHLIST_FILE_TYPES
        assert OPENSANCTIONS_BY_FILE_TYPE[file_type].file_type == file_type


def test_schema_bounds_hold_for_every_registry_entry():
    """SyncReport.source est VARCHAR(20), les colonnes list_type VARCHAR(30) :
    un depassement casserait a l'INSERT, pas a la revue de code."""
    for src in OPENSANCTIONS_SOURCES:
        assert len(src.source) <= 20, src.source
        assert len(src.file_type) <= 30, src.file_type
        assert src.run_key.islower(), src.run_key
        assert src.source.isupper(), src.source


def test_manual_upload_reaches_the_same_parser(db, tmp_path, monkeypatch):
    """L'import manuel d'un fichier targets.simple.csv sous un type du
    registre passe par le meme lecteur parametre que la synchronisation."""
    from fiskr.ingest import parse_opensanctions_simple_csv

    src = OPENSANCTIONS_SOURCES[0]
    path = tmp_path / "targets.simple.csv"
    path.write_text(TARGETS_CSV, encoding="utf-8")
    entities = list(parse_opensanctions_simple_csv(
        str(path), id_prefix=src.id_prefix, origin=src.origin,
        designation_reasons=src.designation_reasons))
    assert [e["entity_id"] for e in entities] == [
        f"{src.id_prefix}-Q100", f"{src.id_prefix}-Q200"]


def test_error_message_of_unknown_source_lists_registry_aliases():
    """Le 400 de POST /api/sync/run enumere desormais les alias DERIVES :
    une source ajoutee au registre y apparait sans mise a jour manuelle."""
    from fastapi.testclient import TestClient
    from fiskr.api import app
    from fiskr.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "role": "admin", "roles": ["admin"]}
    try:
        with TestClient(app) as client:
            response = client.post("/api/sync/run", json={"source": "INCONNUE"})
            assert response.status_code == 400
            for src in OPENSANCTIONS_SOURCES:
                assert src.source in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()


# ------------------ REPUBLICATION AU CONTENU IDENTIQUE ------------------

def _reordered_csv():
    # Même contenu, octets différents : lignes de données inversées (le hash
    # change, aucune fiche ne diffère — la republication quotidienne type)
    lines = TARGETS_CSV.strip().split("\n")
    return "\n".join([lines[0], lines[2], lines[1]]) + "\n"


def test_metadata_only_republish_skips_homologation(db):
    """Republication au contenu identique (hash différent par les seules
    métadonnées) : NO_CHANGE, snapshot archivé, production intacte — plus
    d'homologation ni de cahier de tests pour un non-événement (vu en
    production sur les sources OpenSanctions)."""
    run_key = sorted(OPENSANCTIONS_BY_KEY)[0]
    src = OPENSANCTIONS_BY_KEY[run_key]
    run = OPENSANCTIONS_RUNNERS[run_key]

    first = run(db, fetcher=_fetcher(TARGETS_CSV))
    assert first.status == "SUCCESS"

    second = run(db, fetcher=_fetcher(_reordered_csv()))
    assert second.status == "NO_CHANGE"
    assert "identique" in (second.message or "")

    discarded = db.query(Snapshot).filter(
        Snapshot.snapshot_id == second.snapshot_id).first()
    assert discarded is not None and discarded.status == "SUPERSEDED"
    ready = db.query(Snapshot).filter(Snapshot.file_type == src.file_type,
                                      Snapshot.status == "READY").all()
    assert [s.snapshot_id for s in ready] == [first.snapshot_id]


def test_metadata_only_republish_skips_homologation_in_staging_mode(db):
    """Même garantie en mode homologation : le non-événement n'entre pas en
    file d'attente de pointage humain (premier import, lui, y entre)."""
    from fiskr.settings import set_setting, SETTING_REQUIRE_APPROVAL
    run_key = sorted(OPENSANCTIONS_BY_KEY)[0]
    run = OPENSANCTIONS_RUNNERS[run_key]

    assert run(db, fetcher=_fetcher(TARGETS_CSV)).status == "SUCCESS"
    set_setting(db, SETTING_REQUIRE_APPROVAL, True)
    db.commit()

    second = run(db, fetcher=_fetcher(_reordered_csv()))
    assert second.status == "NO_CHANGE"
    assert db.query(Snapshot).filter(
        Snapshot.status == "PENDING_REVIEW").count() == 0


# ------------------ COHÉRENCE REGISTRE <-> FRONT ------------------

def _app_js() -> str:
    return (Path(__file__).resolve().parents[1] / "fiskr" / "static" / "app.js") \
        .read_text(encoding="utf-8")


def test_every_list_type_has_a_front_label():
    """Un type de liste sans libellé s'affiche en brut (« WATCHLIST_AE_TERROR »)
    dans les tableaux et les sélecteurs. Le registre étant la source de vérité,
    tout type qu'il déclare doit avoir son libellé côté écran."""
    from fiskr.database import WATCHLIST_FILE_TYPES
    src = _app_js()
    sans_libelle = [t for t in WATCHLIST_FILE_TYPES if f"{t}:" not in src]
    assert not sans_libelle, "Types sans libellé dans LIST_TYPE_LABELS : " + ", ".join(sans_libelle)


def test_every_registry_source_is_offered_on_the_sources_screen():
    """Une source branchée mais absente du catalogue front est injoignable :
    ni bouton de synchronisation, ni planification."""
    src = _app_js()
    absentes = [s.run_key for s in OPENSANCTIONS_SOURCES
                if f'key: "{s.run_key}"' not in src]
    assert not absentes, "Sources absentes du catalogue front : " + ", ".join(absentes)


def test_registry_datasets_are_unique():
    """Deux entrées sur le même jeu de données produiraient deux listes
    jumelles — et deux fois le même criblage."""
    datasets = [s.dataset for s in OPENSANCTIONS_SOURCES]
    doublons = {d for d in datasets if datasets.count(d) > 1}
    assert not doublons, f"Jeux OpenSanctions déclarés deux fois : {doublons}"
    cles = [s.run_key for s in OPENSANCTIONS_SOURCES]
    assert len(cles) == len(set(cles))
    types = [s.file_type for s in OPENSANCTIONS_SOURCES]
    assert len(types) == len(set(types))

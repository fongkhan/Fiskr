"""
L'aperçu montre ce que l'import écrit — une seule table de colonnes.

Constaté dans un vrai navigateur : une liste UE au format CSV, colonne
`birth_date`. L'aperçu affichait la date « retenue » pour chaque ligne (c'était
le seul champ de date que proposait l'assistant de correspondance) ; l'import,
lui, ne lisait que `dates_of_birth` ou `dob`. Résultat : 25 fiches en
production, 25 dates de naissance vides, et chaque alerte disait « Pas de
comparaison DOB » — le criblage perdait son premier discriminant d'homonymes
sous un aperçu qui affirmait l'inverse.

Deux listes de colonnes tenues à la main avaient divergé. Il n'en reste qu'une
(`api._CHAMPS_CSV`), que l'aperçu, son assistant et l'import lisent tous par
`_normaliser_ligne_csv`. Ces tests font passer le même fichier par l'aperçu
PUIS par l'import, et comparent.
"""
import inspect
import json
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, AppSetting, Snapshot, WatchlistEntity
from fiskr.settings import SETTING_REQUIRE_APPROVAL


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "test_accord", "full_name": "test_accord",
        "role": "admin", "roles": ["admin"],
    }
    db = next(get_db())
    avant = db.query(AppSetting).filter(AppSetting.key == SETTING_REQUIRE_APPROVAL).first()
    valeur_avant = None if avant is None else avant.value
    db.close()
    c = TestClient(app)
    # En homologation : le lot reste hors production (aucun effet sur les
    # listes que lisent les autres tests), mais ses fiches sont bien écrites.
    assert c.put("/api/settings/ingestion", json={"require_approval": True}).status_code == 200
    yield c
    app.dependency_overrides.pop(get_current_user, None)
    db = next(get_db())
    try:
        snaps = [s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_name.like("test_accord_%")).all()]
        if snaps:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snaps)).delete(
                synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(snaps)).delete(
                synchronize_session=False)
        ligne = db.query(AppSetting).filter(AppSetting.key == SETTING_REQUIRE_APPROVAL).first()
        if valeur_avant is None and ligne is not None:
            db.delete(ligne)
        elif ligne is not None:
            ligne.value = valeur_avant
        db.commit()
    finally:
        db.close()


def _csv(entete_date="birth_date", entete_id="entity_id"):
    return (f"{entete_id},entity_type,primary_name,nationality,{entete_date}\n"
            f"ACC-{uuid.uuid4().hex[:6]},I,Vladimir Vladimirovich PUTIN,RU,1952-10-07\n")


def _apercu(client, contenu):
    r = client.post("/api/ingest/preview", data={"file_type": "WATCHLIST_EU", "delimiter": ","},
                    files={"file": ("essai.csv", contenu, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _importer(client, contenu):
    r = client.post("/api/ingest", data={"file_type": "WATCHLIST_EU", "delimiter": ","},
                    files={"file": (f"test_accord_{uuid.uuid4().hex[:8]}.csv", contenu, "text/csv")})
    assert r.status_code in (200, 202), r.text
    snapshot_id = r.json()["snapshot_id"]
    db = next(get_db())
    try:
        return db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == snapshot_id).all()
    finally:
        db.close()


# ------------------------------------------------------------ l'incident

def test_la_date_que_l_apercu_montre_est_celle_que_l_import_ecrit(client):
    """Le cas exact du navigateur : colonne `birth_date`."""
    contenu = _csv("birth_date")
    compris = _apercu(client, contenu)["lignes"][0]["compris"]
    assert compris.get("dates_of_birth") == "1952-10-07", \
        "l'aperçu doit montrer la date sous le champ que le moteur lit"
    fiches = _importer(client, contenu)
    assert len(fiches) == 1
    assert fiches[0].dates_of_birth == ["1952-10-07"], \
        "l'import écrivait [] : 25 fiches sans date sous un aperçu qui l'affichait"


@pytest.mark.parametrize("entete", ["dates_of_birth", "dob", "birth_date"])
def test_chaque_en_tete_de_date_accepte_l_est_par_l_apercu_et_par_l_import(client, entete):
    """L'écart jouait dans les deux sens : `dob` était lu par l'import et
    montré vide par l'aperçu."""
    contenu = _csv(entete)
    assert _apercu(client, contenu)["lignes"][0]["compris"]["dates_of_birth"] == "1952-10-07"
    assert _importer(client, contenu)[0].dates_of_birth == ["1952-10-07"]


def test_un_identifiant_en_colonne_id_est_montre_et_ecrit(client):
    contenu = _csv(entete_id="id")
    attendu = contenu.split("\n")[1].split(",")[0]
    assert _apercu(client, contenu)["lignes"][0]["compris"]["entity_id"] == attendu
    assert _importer(client, contenu)[0].entity_id == attendu


# --------------------------------------------------------- une seule table

def test_les_champs_proposes_derivent_de_la_table_unique():
    for file_type, champs in api._CHAMPS_CSV.items():
        assert api._APERCU_CHAMPS[file_type] == tuple(champs)
    assert "birth_date" not in api._APERCU_CHAMPS["WATCHLIST_EU"], \
        "`birth_date` est un en-tête accepté, pas un champ du moteur"


def test_l_apercu_et_l_import_passent_par_la_meme_normalisation():
    """Un second chemin de lecture, et les deux listes divergeraient de nouveau."""
    assert "_normaliser_ligne_csv(file_type" in inspect.getsource(api.preview_ingest)
    assert inspect.getsource(api._ingest_parse_and_finalize).count(
        "_normaliser_ligne_csv(file_type") == 2, "liste au format CSV ET référentiel clients"


def test_une_correspondance_ne_vise_que_des_champs_que_l_import_lit(client):
    """L'assistant acceptait `birth_date` comme cible : l'import l'ignorait."""
    r = client.post("/api/ingest/preview",
                    data={"file_type": "WATCHLIST_EU", "delimiter": ",",
                          "column_mapping": json.dumps({"birth_date": "birth_date"})},
                    files={"file": ("essai.csv", _csv("birth_date"), "text/csv")})
    assert r.status_code == 400 and "inconnu" in r.json()["detail"].lower()

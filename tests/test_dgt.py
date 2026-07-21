"""
Tests du connecteur DGT : registre national des gels des avoirs (Direction
generale du Tresor, API publique ENGEL). Parseur JSON -> schema pivot, et
synchronisation complete (delta, dedup par hash, supersede, mode homologation).
"""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, WatchlistEntity
from fiskr.ingest import parse_dgt_gels_json
from fiskr.sync import run_dgt_sync


DGT_SAMPLE_JSON = """{
  "Publications": {
    "DatePublication": "2026-07-11 12:00:00",
    "PublicationDetail": [
      {
        "IdRegistre": 4963,
        "Nature": "Personne physique",
        "Nom": "PETROV",
        "RegistreDetail": [
          {"TypeChamp": "PRENOM", "Valeur": [{"Prenom": "Igor"}]},
          {"TypeChamp": "SEXE", "Valeur": [{"Sexe": "Masculin"}]},
          {"TypeChamp": "DATE_DE_NAISSANCE", "Valeur": [{"Jour": "12", "Mois": "3", "Annee": "1965"}]},
          {"TypeChamp": "LIEU_DE_NAISSANCE", "Valeur": [{"Lieu": "Moscou", "Pays": "Russie"}]},
          {"TypeChamp": "NATIONALITE", "Valeur": [{"Pays": "Russe"}]},
          {"TypeChamp": "ALIAS", "Valeur": [{"Alias": "Igor Petrovitch"}, {"Alias": "I. Petrov"}]},
          {"TypeChamp": "TITRE", "Valeur": [{"Titre": "Ministre"}]},
          {"TypeChamp": "ADRESSE_PP", "Valeur": [{"Adresse": "12 rue Tverskaya, Moscou", "Pays": "Russie"}]},
          {"TypeChamp": "PASSEPORT", "Valeur": [{"NumeroPasseport": "750123456", "CommentairePasseport": "passeport diplomatique"}]},
          {"TypeChamp": "MOTIFS", "Valeur": [{"Motifs": "Soutien matériel au régime."}]},
          {"TypeChamp": "FONDEMENT_JURIDIQUE", "Valeur": [{"FondementJuridique": "3", "FondementJuridiqueLabel": "Règlement (UE) 269/2014"}]},
          {"TypeChamp": "REFERENCE_UE", "Valeur": [{"ReferenceUe": "UE.4721.83"}]}
        ]
      },
      {
        "IdRegistre": 5100,
        "Nature": "Personne morale",
        "Nom": "ZARYA HOLDING",
        "RegistreDetail": [
          {"TypeChamp": "ADRESSE_PM", "Valeur": [{"Adresse": "1 place Rouge, Moscou", "Pays": "Russie"}]},
          {"TypeChamp": "IDENTIFICATION", "Valeur": [{"Identification": "INN 7712345678", "CommentaireIdentification": "numéro fiscal"}]},
          {"TypeChamp": "MOTIFS", "Valeur": [{"Motifs": "Financement d'activités sanctionnées."}]}
        ]
      },
      {
        "IdRegistre": 5200,
        "Nature": "Navire",
        "Nom": "VOLGA STAR",
        "RegistreDetail": [
          {"TypeChamp": "MOTIFS", "Valeur": [{"Motifs": "Transport de pétrole brut."}]}
        ]
      }
    ]
  }
}"""


@pytest.fixture
def db(tmp_path):
    """Session SQLAlchemy isolee (SQLite temporaire) pour ne pas toucher la base de dev."""
    engine = create_engine(f"sqlite:///{tmp_path / 'dgt_test.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def make_fetcher(json_content: str):
    def fetcher(url, dest_path):
        Path(dest_path).write_text(json_content, encoding="utf-8")
    return fetcher


# ------------------ PARSEUR ------------------

def test_parse_dgt_registry_mapping(tmp_path):
    json_file = tmp_path / "registre.json"
    json_file.write_text(DGT_SAMPLE_JSON, encoding="utf-8")
    entities = {e["entity_id"]: e for e in parse_dgt_gels_json(str(json_file))}

    assert len(entities) == 3

    # Personne physique -> I, avec identite complete
    pp = entities["DGT-4963"]
    assert pp["entity_type"] == "I"
    assert pp["primary_name"] == "Igor PETROV"
    assert pp["individual_name_parsed"]["first_name"] == "Igor"
    assert pp["individual_name_parsed"]["last_name"] == "PETROV"
    assert pp["gender"] == "M"
    assert pp["dates_of_birth"] == ["1965-03-12"]
    assert pp["place_of_birth"] == "Moscou, Russie"
    # Pays normalises en ISO2 pour croiser les cles de blocking clients
    assert pp["countries"]["citizenship"] == ["RU"]
    assert pp["countries"]["birth_country"] == ["RU"]
    assert "Igor Petrovitch" in pp["aliases"]["high_priority"]
    assert pp["designation"] == "Ministre"
    assert pp["designation_reasons"] == "Soutien matériel au régime."
    assert "269/2014" in pp["additional_informations"]
    assert pp["passport_documents"][0]["number"] == "750123456"
    assert pp["address"].startswith("12 rue Tverskaya")
    assert pp["origin"] == "DGT Registre national des gels"
    # Reference officielle : reference UE + date de publication du registre
    assert pp["official_reference"] == "UE.4721.83 (maj 2026-07-11)"

    # Personne morale -> E, identification en autres registres
    pm = entities["DGT-5100"]
    assert pm["entity_type"] == "E"
    assert pm["official_reference"] is None  # aucune reference UE/ONU sur cette fiche
    assert pm["other_registration_ids"] == [{"id_type": "Identification", "number": "INN 7712345678"}]
    assert pm["countries"]["jurisdiction_country"] == ["RU"]

    # Navire -> V
    assert entities["DGT-5200"]["entity_type"] == "V"


# ------------------ SYNC (DELTA, DEDUP, SUPERSEDE) ------------------

def test_dgt_sync_first_run_then_no_change_then_delta(db):
    report1 = run_dgt_sync(db, fetcher=make_fetcher(DGT_SAMPLE_JSON))
    assert report1.status == "SUCCESS"
    assert report1.source == "DGT"
    assert report1.added_count == 3
    snap1 = db.query(Snapshot).filter(Snapshot.snapshot_id == report1.snapshot_id).first()
    assert snap1.file_type == "WATCHLIST_DGT"
    assert snap1.status == "READY"
    assert snap1.record_count == 3

    # Meme fichier -> NO_CHANGE
    report2 = run_dgt_sync(db, fetcher=make_fetcher(DGT_SAMPLE_JSON))
    assert report2.status == "NO_CHANGE"

    # v2 : le navire est radie, une nouvelle personne apparait
    data = json.loads(DGT_SAMPLE_JSON)
    details = data["Publications"]["PublicationDetail"]
    details = [d for d in details if d["IdRegistre"] != 5200]
    details.append({
        "IdRegistre": 6000,
        "Nature": "Personne physique",
        "Nom": "KUZNETSOVA",
        "RegistreDetail": [
            {"TypeChamp": "PRENOM", "Valeur": [{"Prenom": "Dina"}]},
            {"TypeChamp": "SEXE", "Valeur": [{"Sexe": "Féminin"}]}
        ]
    })
    data["Publications"]["PublicationDetail"] = details
    report3 = run_dgt_sync(db, fetcher=make_fetcher(json.dumps(data, ensure_ascii=False)))
    assert report3.status == "SUCCESS"
    assert report3.added_count == 1
    assert report3.removed_count == 1

    # Remplacement applique : seul le nouveau snapshot reste actif
    db.refresh(snap1)
    assert snap1.status == "SUPERSEDED"
    active = db.query(Snapshot).filter(Snapshot.file_type == "WATCHLIST_DGT", Snapshot.status == "READY").all()
    assert [s.snapshot_id for s in active] == [report3.snapshot_id]

    new_entity = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == report3.snapshot_id,
        WatchlistEntity.entity_id == "DGT-6000"
    ).first()
    assert new_entity is not None
    assert new_entity.entity_type == "I"
    assert new_entity.gender == "F"


def test_dgt_sync_staging_keeps_previous_live(db):
    from fiskr.database import AppSetting
    from fiskr.settings import SETTING_REQUIRE_APPROVAL

    # v1 en production (mode homologation inactif)
    report1 = run_dgt_sync(db, fetcher=make_fetcher(DGT_SAMPLE_JSON))
    snap1 = db.query(Snapshot).filter(Snapshot.snapshot_id == report1.snapshot_id).first()

    # Mode homologation actif : v2 attend un pointage, v1 reste en production
    db.add(AppSetting(key=SETTING_REQUIRE_APPROVAL, value=True))
    db.commit()
    data = json.loads(DGT_SAMPLE_JSON)
    data["Publications"]["PublicationDetail"][0]["Nom"] = "PETROV-MODIFIE"
    report2 = run_dgt_sync(db, fetcher=make_fetcher(json.dumps(data, ensure_ascii=False)))
    assert report2.status == "PENDING_REVIEW"
    snap2 = db.query(Snapshot).filter(Snapshot.snapshot_id == report2.snapshot_id).first()
    assert snap2.status == "PENDING_REVIEW"
    db.refresh(snap1)
    assert snap1.status == "READY"

    # Re-sync du meme fichier pending -> pas de doublon quotidien
    report3 = run_dgt_sync(db, fetcher=make_fetcher(json.dumps(data, ensure_ascii=False)))
    assert report3.status == "NO_CHANGE"

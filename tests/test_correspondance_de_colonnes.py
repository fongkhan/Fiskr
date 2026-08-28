"""
Assistant de correspondance de colonnes.

L'aperçu du lot précédent DIAGNOSTIQUE le mal (« aucun champ attendu
reconnu ») ; il laissait l'utilisateur renommer les colonnes de son fichier.
Cet assistant le soigne : on associe soi-même « nom » à `client_last_name`,
on revoit l'aperçu, on importe — et la correspondance est retenue pour les
fichiers de la même forme.

Le piège de ce lot était dans le code existant. Le lecteur CSV portait déjà
un paramètre `mapping_dict` — sans aucun appelant — dont la sémantique ne
gardait QUE les champs mappés : une correspondance partielle, exactement ce
que produit un assistant, aurait fait disparaître en silence toutes les
autres colonnes. Le câbler tel quel aurait créé la perte de données qu'il
prétend éviter. Le premier test de ce fichier tient cette sémantique.
"""
import json
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app, _empreinte_des_entetes
from fiskr.auth import get_current_user
from fiskr.database import get_db, CsvColumnMapping
from fiskr.ingest import parse_csv_file

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")
MAL_NOMME = "id,type,prenom,nom,pays\nC1,PP,Jean,Dupont,FR\nC2,PP,Marie,Martin,FR\n"
CORRESPONDANCE = {"client_id": "id", "client_type": "type",
                  "client_first_name": "prenom", "client_last_name": "nom",
                  "nationality": "pays"}


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "test_map", "full_name": "test_map",
        "role": "admin", "roles": ["admin"],
    }
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)
    db = next(get_db())
    db.query(CsvColumnMapping).filter(CsvColumnMapping.created_by == "test_map").delete(
        synchronize_session=False)
    db.commit()
    db.close()


def _apercu(client, contenu=MAL_NOMME, mapping=None, file_type="CLIENT_BASE", delimiter=","):
    donnees = {"file_type": file_type, "delimiter": delimiter}
    if mapping is not None:
        donnees["column_mapping"] = json.dumps(mapping)
    return client.post("/api/ingest/preview", data=donnees,
                       files={"file": ("essai.csv", contenu, "text/csv")})


# ------------------------------------------------- la sémantique du lecteur

def test_une_correspondance_partielle_ne_perd_aucune_colonne(tmp_path):
    """LE piège du lot : la version précédente du lecteur ne gardait que les
    champs mappés. Associer « nom » aurait fait disparaître l'identifiant, la
    nationalité et tout le reste — sans un mot."""
    fichier = tmp_path / "c.csv"
    fichier.write_text("id,nom,pays\nC1,Dupont,FR\n", encoding="utf-8")
    ligne = next(parse_csv_file(str(fichier), mapping_dict={"client_last_name": "nom"}))
    assert ligne["client_last_name"] == "Dupont", "le champ mappé est rempli"
    assert ligne["id"] == "C1" and ligne["pays"] == "FR", \
        "les colonnes non associées restent : une aide ne doit pas amputer le fichier"


def test_une_source_vide_ne_recouvre_pas_la_colonne_d_origine(tmp_path):
    fichier = tmp_path / "c.csv"
    fichier.write_text("client_last_name,pays\nDupont,FR\n", encoding="utf-8")
    ligne = next(parse_csv_file(str(fichier), mapping_dict={"client_last_name": ""}))
    assert ligne["client_last_name"] == "Dupont"


# ----------------------------------------------------- l'aperçu, avec mapping

def test_la_correspondance_repare_l_apercu(client):
    sans = _apercu(client).json()
    assert sans["aucun_champ_reconnu"] is True and sans["acceptees"] == 0
    avec = _apercu(client, mapping=CORRESPONDANCE).json()
    assert avec["aucun_champ_reconnu"] is False
    assert avec["acceptees"] == 2 and avec["rejected_count"] == 0
    assert avec["lignes"][0]["compris"]["client_last_name"] == "Dupont"
    assert avec["correspondance_appliquee"] == CORRESPONDANCE


def test_l_apercu_annonce_les_champs_que_l_import_lit(client):
    """Sans cette liste, l'assistant devrait recopier les champs côté client —
    une seconde source de vérité, vouée à diverger."""
    data = _apercu(client).json()
    assert set(data["champs_attendus"]) == set(api._APERCU_CHAMPS["CLIENT_BASE"])


def test_un_champ_cible_inconnu_est_refuse(client):
    r = _apercu(client, mapping={"colonne_bidon": "id"})
    assert r.status_code == 400 and "inconnu" in r.json()["detail"].lower()


def test_une_colonne_source_absente_est_refusee(client):
    """Elle remplirait le champ de vide, et l'utilisateur croirait avoir
    corrigé le problème : c'est la panne que l'assistant doit éviter."""
    r = _apercu(client, mapping={"client_last_name": "colonne_absente"})
    assert r.status_code == 400 and "absente" in r.json()["detail"].lower()


def test_une_correspondance_illisible_est_refusee(client):
    r = client.post("/api/ingest/preview",
                    data={"file_type": "CLIENT_BASE", "delimiter": ",",
                          "column_mapping": "pas du json"},
                    files={"file": ("c.csv", MAL_NOMME, "text/csv")})
    assert r.status_code == 400


def test_ne_pas_associer_un_champ_est_un_choix_valide(client):
    data = _apercu(client, mapping={**CORRESPONDANCE, "client_dob": ""}).json()
    assert "client_dob" not in data["correspondance_appliquee"]
    assert data["acceptees"] == 2


# -------------------------------------------------------------- la mémoire

def test_l_empreinte_ne_depend_ni_de_l_ordre_ni_de_la_casse():
    assert _empreinte_des_entetes(["id", "nom"]) == _empreinte_des_entetes(["NOM", " id "])
    assert _empreinte_des_entetes(["id", "nom"]) != _empreinte_des_entetes(["id", "prenom"])


def test_la_memoire_est_proposee_pour_la_meme_forme_de_fichier(client):
    db = next(get_db())
    empreinte = _empreinte_des_entetes(["id", "type", "prenom", "nom", "pays"])
    db.add(CsvColumnMapping(file_type="CLIENT_BASE", headers_fingerprint=empreinte,
                            headers_sample="id, type, prenom, nom, pays",
                            mapping=CORRESPONDANCE, created_by="test_map"))
    db.commit()
    db.close()
    data = _apercu(client).json()
    assert data["correspondance_memorisee"] == CORRESPONDANCE
    # ... et JAMAIS appliquée d'elle-même : l'utilisateur voit ce qu'il accepte
    assert data["correspondance_appliquee"] == {}
    assert data["aucun_champ_reconnu"] is True


def test_la_memoire_ne_deborde_pas_sur_une_autre_forme(client):
    db = next(get_db())
    db.add(CsvColumnMapping(file_type="CLIENT_BASE",
                            headers_fingerprint=_empreinte_des_entetes(["id", "type", "prenom", "nom", "pays"]),
                            mapping=CORRESPONDANCE, created_by="test_map"))
    db.commit()
    db.close()
    autre = "reference,categorie,patronyme\nX1,PP,Durand\n"
    data = _apercu(client, contenu=autre).json()
    assert data["correspondance_memorisee"] is None, \
        "une correspondance devinée et appliquée à un autre fichier serait le défaut même"


def test_la_memoire_ne_s_ecrit_qu_apres_un_import_abouti():
    """Retenir une correspondance saisie mais jamais éprouvée reviendrait à
    reproposer plus tard une erreur que personne n'a vue."""
    src = open(os.path.join(os.path.dirname(STATIC), "api.py"), encoding="utf-8").read()
    corps = src[src.index("def _ingest_parse_and_finalize"):]
    appel = corps.index("_memoriser_correspondance(")
    fin_ok = corps.index("progress_registry.finish(progress_id)")
    assert fin_ok < appel, "la mémorisation doit suivre la fin normale de l'import"
    apercu = src[src.index("async def preview_ingest"):src.index("async def preview_ingest") + 4000]
    assert "_memoriser_correspondance" not in apercu, "l'aperçu n'écrit rien"


# -------------------------------------------------- la chaîne jusqu'à l'import

def test_la_correspondance_traverse_l_endpoint_le_job_et_le_lecteur():
    """Une correspondance validée dans l'aperçu mais perdue en route ferait
    écrire à l'import autre chose que ce que l'aperçu a montré."""
    src = open(os.path.join(os.path.dirname(STATIC), "api.py"), encoding="utf-8").read()
    assert "column_mapping: Optional[str] = Form(None)" in src
    assert '"column_mapping": correspondance or None,' in src, "transmise au job"
    assert src.count("mapping_dict=column_mapping or None") == 2, \
        "les deux lectures CSV de l'import doivent la porter"
    taches = open(os.path.join(os.path.dirname(STATIC), "tasks.py"), encoding="utf-8").read()
    assert "column_mapping=None" in taches and "column_mapping=column_mapping" in taches


def test_la_correspondance_est_validee_avant_toute_ecriture():
    """Un refus ne doit pas laisser un instantané orphelin derrière lui."""
    src = open(os.path.join(os.path.dirname(STATIC), "api.py"), encoding="utf-8").read()
    corps = src[src.index("def ingest_snapshot("):]
    corps = corps[:corps.index("\n@app.")] if "\n@app." in corps else corps
    validation = corps.index("_valider_correspondance(file_type, column_mapping)")
    creation = corps.index("Snapshot(")
    assert validation < creation, "valider AVANT de créer l'instantané"


# ------------------------------------------------------------------ le frontal

def test_l_assistant_propose_sans_appliquer():
    src = _lire("app.js")
    fn = src[src.index("function _rendreAssistant"):]
    fn = fn[:fn.index("\nfunction ")]
    assert "appliquerCorrespondanceMemorisee()" in fn
    assert "correspondance_memorisee" in fn
    # Ouvert d'office quand rien n'est reconnu : c'est là qu'il sert
    assert "aucun_champ_reconnu ?" in fn and 'open' in fn


def test_la_correspondance_suit_le_fichier_jusqu_a_l_import():
    src = _lire("app.js")
    assert 'formData.append("column_mapping", JSON.stringify(_correspondanceCourante))' in src
    assert 'donnees.append("column_mapping", JSON.stringify(_correspondanceCourante))' in src
    # Changer de fichier ou de type l'oublie : elle visait d'autres colonnes
    assert src.count("_correspondanceCourante = {};") >= 3

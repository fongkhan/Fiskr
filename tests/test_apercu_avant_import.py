"""
Voir ce que l'import a compris, avant de l'écrire — et savoir ce qu'il écarte.

Le défaut visé n'est pas une erreur : c'est un SUCCÈS trompeur. On téléverse
un référentiel clients dont la colonne du nom s'appelle « nom » et non
« client_last_name » ; l'import ne lève rien, le Quality Gate écarte les
lignes une à une, et l'écran annonce « instantané importé avec succès ». La
liste en production est vide ou amputée, et le criblage répond « aucune
correspondance » sans jamais se plaindre.

Deux réponses, testées ici :

1. **L'aperçu** fait passer les premières lignes par le VRAI lecteur CSV et le
   VRAI Quality Gate, montre ce qui a été compris, et n'écrit rien.
2. **L'import dit ce qu'il écarte.** Le compte des lignes refusées et leurs
   motifs — écrits par le Quality Gate lui-même, jamais reformulés — sortent
   avec le résultat, au lieu de disparaître dans un `continue`.
"""
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app
from fiskr.auth import get_current_user

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")

BON = ("client_id,client_type,client_first_name,client_last_name,nationality\n"
       "C1,PP,Jean,Dupont,FR\n"
       "C2,PP,Marie,Martin,FR\n")
MAL_NOMME = ("id,type,prenom,nom,pays\n"
             "C1,PP,Jean,Dupont,FR\n"
             "C2,PP,Marie,Martin,FR\n")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "test_apercu", "full_name": "test_apercu",
        "role": "admin", "roles": ["admin"],
    }
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


def _apercu(client, contenu, file_type="CLIENT_BASE", delimiter=","):
    return client.post("/api/ingest/preview",
                       data={"file_type": file_type, "delimiter": delimiter},
                       files={"file": ("essai.csv", contenu, "text/csv")})


# ------------------------------------------------------------------ l'aperçu

def test_un_fichier_bien_nomme_est_compris(client):
    data = _apercu(client, BON).json()
    assert data["acceptees"] == 2 and data["rejected_count"] == 0
    assert data["aucun_champ_reconnu"] is False
    assert data["lignes"][0]["compris"]["client_last_name"] == "Dupont"
    assert data["colonnes_du_fichier"][0] == "client_id"


def test_un_en_tete_qui_ne_correspond_pas_est_denonce(client):
    """Le cas central : l'import « réussirait » sans rien importer d'utile."""
    data = _apercu(client, MAL_NOMME).json()
    assert data["aucun_champ_reconnu"] is True
    assert data["acceptees"] == 0 and data["rejected_count"] == 2
    assert data["lignes"][0]["compris"]["client_last_name"] == "", \
        "la valeur « Dupont » est dans le fichier, mais pas là où le moteur la lit"


def test_un_mauvais_separateur_se_voit_aussi(client):
    data = _apercu(client, "client_id;client_type;client_last_name\nC1;PP;Dupont\n").json()
    assert data["aucun_champ_reconnu"] is True


def test_les_motifs_viennent_du_quality_gate_et_ne_sont_pas_reformules(client):
    """Les messages nomment la règle et la colonne attendue : les réécrire
    ferait une seconde source de vérité, vouée à diverger de la règle."""
    data = _apercu(client, MAL_NOMME).json()
    assert data["rejected_reasons"], "aucun motif remonté"
    assert any(re.match(r"Rule_[A-Z]\d+", m) for m in data["rejected_reasons"]), \
        data["rejected_reasons"]


def test_l_apercu_s_arrete_a_dix_lignes(client):
    beaucoup = "client_id,client_type,client_first_name,client_last_name\n" + "".join(
        f"C{i},PP,Jean,Dupont{i}\n" for i in range(50))
    data = _apercu(client, beaucoup).json()
    assert data["lignes_examinees"] == api._APERCU_LIGNES == 10


def test_l_apercu_n_ecrit_rien(client):
    """Ni instantané, ni fiche, ni fichier temporaire laissé derrière."""
    from fiskr.database import get_db, Snapshot
    from fiskr.config import PROJECT_ROOT
    db = next(get_db())
    avant = db.query(Snapshot).count()
    _apercu(client, BON)
    _apercu(client, MAL_NOMME)
    assert db.query(Snapshot).count() == avant, "un aperçu a créé un instantané"
    db.close()
    temp = PROJECT_ROOT / "temp_ingestion"
    if temp.exists():
        assert not list(temp.glob("apercu_*")), "fichier d'aperçu laissé sur le disque"


def test_l_apercu_refuse_les_formats_sans_choix_de_colonnes(client):
    """Les sources officielles branchées ont un lecteur dédié et un format
    publié par l'émetteur : proposer un aperçu y laisserait croire qu'il y a
    un choix à faire là où il n'y en a pas."""
    r = _apercu(client, BON, file_type="WATCHLIST_OFAC")
    assert r.status_code == 400
    assert "CSV" in r.json()["detail"]


def test_un_fichier_illisible_ne_leve_pas_une_erreur_serveur(client):
    r = client.post("/api/ingest/preview",
                    data={"file_type": "CLIENT_BASE", "delimiter": ","},
                    files={"file": ("binaire.csv", b"\xff\xfe\x00\x01", "application/octet-stream")})
    assert r.status_code == 400, r.status_code


# --------------------------------------------- ce que l'import écarte, il le dit

def test_le_compteur_de_rejets_retient_les_motifs_sans_les_multiplier():
    compteur = api._CompteurDeRejets()
    for _ in range(3):
        compteur.ajouter({"errors": ["Rule_B01: Champ Nom Principal Vide"]})
    compteur.ajouter({"errors": ["Rule_B02: Type d'Entité Invalide"]})
    rapport = compteur.rapport()
    assert rapport["rejected_count"] == 4
    assert rapport["rejected_reasons"] == ["Rule_B01: Champ Nom Principal Vide",
                                           "Rule_B02: Type d'Entité Invalide"]


def test_les_motifs_retenus_sont_bornes():
    """Un fichier de cent mille lignes fausses ne doit pas faire grossir la
    réponse à proportion : cinq motifs distincts suffisent à diagnostiquer."""
    compteur = api._CompteurDeRejets()
    for i in range(50):
        compteur.ajouter({"errors": [f"Rule_X{i:02d}: motif {i}"]})
    assert compteur.rapport()["rejected_count"] == 50
    assert len(compteur.rapport()["rejected_reasons"]) == api._CompteurDeRejets._MOTIFS_RETENUS


def test_tous_les_rejets_de_l_import_sont_comptes():
    """Dérivé de la source : chaque `continue` du Quality Gate dans l'import
    doit être précédé d'un `rejets.ajouter`. Un site oublié rendrait le compte
    faux — donc rassurant à tort, ce qui est pire que pas de compte du tout."""
    chemin = os.path.join(os.path.dirname(STATIC), "api.py")
    with open(chemin, encoding="utf-8") as f:
        lignes = f.readlines()
    debut = next(i for i, l in enumerate(lignes) if "_ingest_parse_and_finalize" in l and "def " in l)
    fin = next(i for i, l in enumerate(lignes[debut + 1:], debut + 1)
               if l.startswith("def ") or l.startswith("@app"))
    corps = lignes[debut:fin]
    sites = [i for i, l in enumerate(corps) if 'if not report["is_valid"]:' in l]
    assert sites, "aucun site de rejet trouvé — l'extraction a-t-elle cassé ?"
    for i in sites:
        suivantes = "".join(corps[i + 1:i + 3])
        assert "rejets.ajouter(report)" in suivantes, \
            f"site de rejet non compté, ligne {debut + i + 1} du fichier"


def test_la_charge_utile_de_l_import_porte_le_compte():
    src = open(os.path.join(os.path.dirname(STATIC), "api.py"), encoding="utf-8").read()
    assert "**rejets.rapport()," in src, "le résultat de l'import doit porter le compte des rejets"


# ------------------------------------------------------------------ le frontal

def test_le_frontal_annonce_les_lignes_ecartees():
    src = _lire("app.js")
    assert "data.rejected_count" in src
    assert "rejected_reasons" in src


def test_l_alarme_precede_le_tableau():
    """L'alarme est la seule lecture qui change ce que l'utilisateur doit
    faire dans la seconde qui suit : elle passe avant le détail."""
    src = _lire("app.js")
    fn = src[src.index("function rendreApercuImport"):]
    fn = fn[:fn.index("\n}")]
    assert fn.index("alarme") < fn.index("table-container")
    assert "aucun_champ_reconnu" in fn


def test_le_bouton_d_apercu_ne_s_offre_que_pour_les_types_couverts():
    src = _lire("app.js")
    assert 'const TYPES_AVEC_APERCU = ["CLIENT_BASE", "WATCHLIST_EU"]' in src
    assert "TYPES_AVEC_APERCU.includes(fileType)" in src
    # Le frontal et le serveur doivent couvrir les MÊMES types
    serveur = open(os.path.join(os.path.dirname(STATIC), "api.py"), encoding="utf-8").read()
    m = re.search(r'_APERCU_TYPES = \(([^)]*)\)', serveur)
    assert m
    types_serveur = set(re.findall(r'"([A-Z_]+)"', m.group(1)))
    assert types_serveur == {"CLIENT_BASE", "WATCHLIST_EU"}, types_serveur
    page = _lire("index.html")
    assert 'id="apercu-ingest-btn"' in page and 'onclick="apercuAvantImport()"' in page
    assert 'id="apercu-import"' in page

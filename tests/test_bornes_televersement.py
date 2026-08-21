"""
Bornes de taille sur les depots de fichiers.

Aucun endpoint de televersement n'etait plafonne : un fichier lu en entier en
memoire ou recopie sur le disque sans limite suffisait a epuiser l'un ou
l'autre. La boite CFT surveillee etant alimentee par un systeme amont, l'entree
n'est pas toujours celle d'un operateur attentif.

Ces tests verifient les trois proprietes attendues :
  1. sous le plafond, le depot passe et le contenu arrive intact ;
  2. au-dela, un 413 est leve — et rien de partiel ne reste sur le disque ;
  3. tout endpoint acceptant un UploadFile passe par un des helpers bornes
     (garde-fou contre le prochain endpoint ecrit sans plafond).
"""
import ast
import asyncio
import io
import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException

from fiskr.api import (
    TAILLE_MAX_TELEVERSEMENT, lire_televersement, copier_televersement,
    copier_televersement_vers,
)

RACINE = Path(__file__).resolve().parent.parent


class _FauxUpload:
    """Imite l'UploadFile de Starlette : .file synchrone, .read() asynchrone."""

    def __init__(self, contenu: bytes, filename="fichier.bin"):
        self.file = io.BytesIO(contenu)
        self.filename = filename

    async def read(self, taille=-1):
        return self.file.read(taille)


# ------------------ LECTURE EN MEMOIRE ------------------

def test_lecture_sous_le_plafond_rend_le_contenu_intact():
    contenu = b"MSG" * 5000
    recu = asyncio.run(lire_televersement(_FauxUpload(contenu), "message"))
    assert recu == contenu


def test_lecture_au_dela_du_plafond_leve_413():
    plafond = TAILLE_MAX_TELEVERSEMENT["message"]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(lire_televersement(_FauxUpload(b"\0" * (plafond + 1)), "message"))
    assert exc.value.status_code == 413
    assert "trop volumineux" in exc.value.detail


def test_lecture_exactement_au_plafond_passe():
    plafond = TAILLE_MAX_TELEVERSEMENT["message"]
    recu = asyncio.run(lire_televersement(_FauxUpload(b"\0" * plafond), "message"))
    assert len(recu) == plafond


# ------------------ RECOPIE SUR LE DISQUE ------------------

def test_copie_sous_le_plafond_ecrit_tout_et_alimente_le_hachage(tmp_path):
    contenu = b"ligne\n" * 10000
    cible = tmp_path / "depot.csv"
    hacheur = hashlib.sha256()
    vus = []
    with open(cible, "wb") as sortie:
        ecrits = copier_televersement(_FauxUpload(contenu), sortie, "clients",
                                      hasher=hacheur, progres=vus.append)
    assert ecrits == len(contenu)
    assert cible.read_bytes() == contenu
    assert hacheur.hexdigest() == hashlib.sha256(contenu).hexdigest()
    assert vus and vus[-1] == len(contenu)


def test_copie_au_dela_du_plafond_ne_laisse_pas_de_fichier_partiel(tmp_path, monkeypatch):
    monkeypatch.setitem(TAILLE_MAX_TELEVERSEMENT, "piece", 4 * 1024 * 1024)
    cible = tmp_path / "piece.bin"
    with pytest.raises(HTTPException) as exc:
        with open(cible, "wb") as sortie:
            copier_televersement(_FauxUpload(b"\0" * (6 * 1024 * 1024)), sortie, "piece")
    assert exc.value.status_code == 413
    # Le refus ne doit pas laisser occuper la place qu'on refusait d'accorder.
    assert cible.stat().st_size == 0


def test_variante_vers_supprime_le_fichier_refuse(tmp_path, monkeypatch):
    monkeypatch.setitem(TAILLE_MAX_TELEVERSEMENT, "piece", 1024)
    cible = tmp_path / "justificatif.pdf"
    with pytest.raises(HTTPException) as exc:
        copier_televersement_vers(cible, _FauxUpload(b"\0" * 4096), "piece")
    assert exc.value.status_code == 413
    assert not cible.exists()


def test_variante_vers_ecrit_le_fichier_accepte(tmp_path):
    contenu = b"%PDF-1.4 justificatif"
    cible = tmp_path / "justificatif.pdf"
    assert copier_televersement_vers(cible, _FauxUpload(contenu), "piece") == len(contenu)
    assert cible.read_bytes() == contenu


def test_les_plafonds_sont_ordonnes_par_nature():
    # Une liste officielle est volumineuse par construction (SDN_ADVANCED.XML
    # pese 126 Mo), une piece jointe d'alerte ne l'est pas.
    assert (TAILLE_MAX_TELEVERSEMENT["message"]
            < TAILLE_MAX_TELEVERSEMENT["piece"]
            < TAILLE_MAX_TELEVERSEMENT["clients"]
            < TAILLE_MAX_TELEVERSEMENT["liste"])


# ------------------ GARDE-FOU : TOUT UPLOADFILE EST BORNE ------------------

_HELPERS = {"lire_televersement", "copier_televersement", "copier_televersement_vers"}


def _endpoints_avec_upload():
    """Rend (nom, borne) pour chaque fonction d'endpoint prenant un UploadFile."""
    arbre = ast.parse((RACINE / "fiskr" / "api.py").read_text())
    trouves = []
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = noeud.args
        annotations = [a.annotation for a in list(args.args) + list(args.kwonlyargs)]
        prend_fichier = any(
            isinstance(a, ast.Name) and a.id == "UploadFile"
            or isinstance(a, ast.Subscript) and ast.unparse(a).find("UploadFile") >= 0
            for a in annotations if a is not None
        )
        if not prend_fichier:
            continue
        appels = {
            n.func.id for n in ast.walk(noeud)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        trouves.append((noeud.name, bool(appels & _HELPERS)))
    return trouves


def test_tous_les_endpoints_de_televersement_sont_bornes():
    endpoints = _endpoints_avec_upload()
    assert len(endpoints) >= 6, f"Detection cassee : {endpoints}"
    sans_borne = [nom for nom, borne in endpoints if not borne]
    assert not sans_borne, (
        "Ces endpoints acceptent un fichier sans plafond de taille : "
        + ", ".join(sans_borne)
    )


def test_aucune_recopie_brute_ne_subsiste():
    source = (RACINE / "fiskr" / "api.py").read_text()
    assert "copyfileobj(file.file" not in source, (
        "Une recopie non bornee a ete reintroduite : passer par "
        "copier_televersement_vers()."
    )

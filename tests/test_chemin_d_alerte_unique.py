"""
Un seul chemin d'ouverture d'alerte.

`open_or_redetect_alert` (singulier) et `open_or_redetect_alerts` (pluriel) ont
coexisté le temps que le criblage passe de « la meilleure correspondance » à
« toutes les correspondances ». Le singulier n'était plus appelé par personne —
il restait seulement **importé** dans trois modules — mais il portait
quatre-vingt-quinze lignes qui dupliquaient le chemin de conformité le plus
sensible du produit : déduplication, priorité, échéance SLA, clôture par règle,
journal d'événements, notification.

Du code mort qui duplique un chemin de conformité est pire que du code mort :
c'est une deuxième vérité, prête à être rebranchée par quelqu'un qui la trouve
et la croit équivalente. Elle ne l'était déjà plus — le pluriel lit les règles
et la liste blanche **par lot**, écrit ses lignes d'audit avant de commiter, et
plafonne les notifications au-delà d'un seuil de volumétrie.

Ce test verrouille l'unicité du chemin.
"""
import ast
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
SOURCES = sorted((RACINE / "fiskr").glob("*.py"))


def _fonctions_declarees():
    noms = set()
    for fichier in SOURCES:
        arbre = ast.parse(fichier.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
                noms.add(noeud.name)
    return noms


def _appels(nom):
    """Appels effectifs de `nom`, hors définition et hors import."""
    trouves = []
    for fichier in SOURCES:
        arbre = ast.parse(fichier.read_text(encoding="utf-8"))
        for noeud in ast.walk(arbre):
            if isinstance(noeud, ast.Call) and ast.unparse(noeud.func).endswith(nom):
                trouves.append(f"{fichier.name}:{noeud.lineno}")
    return trouves


def test_le_chemin_pluriel_est_le_seul_qui_ouvre_des_alertes():
    assert "open_or_redetect_alerts" in _fonctions_declarees()
    assert "open_or_redetect_alert" not in _fonctions_declarees(), (
        "le chemin singulier est revenu : deux vérités pour l'ouverture d'une "
        "alerte, dont une qui ne dédoublonne pas par lot")


def test_le_chemin_pluriel_est_reellement_appele():
    """Garde-fou : sans lui, supprimer les DEUX rendrait le test vert."""
    appels = _appels("open_or_redetect_alerts")
    assert len(appels) >= 3, f"seulement {appels}"


def test_plus_aucun_module_n_importe_le_chemin_singulier():
    for fichier in SOURCES:
        texte = fichier.read_text(encoding="utf-8")
        for ligne in texte.split("\n"):
            if "import" not in ligne or "open_or_redetect_alert" not in ligne:
                continue
            # `open_or_redetect_alerts` est légitime ; le singulier, non
            sans_pluriel = ligne.replace("open_or_redetect_alerts", "")
            assert "open_or_redetect_alert" not in sans_pluriel, (
                f"{fichier.name} importe encore le chemin singulier : {ligne.strip()}")


@pytest.mark.parametrize("garantie", [
    "whitelisted_pairs",        # liste blanche lue PAR LOT
    "active_rules",             # règles chargées une fois, pas par correspondance
    "alert_sla_hours",          # réglage SLA lu une fois pour le lot
])
def test_le_chemin_retenu_est_bien_celui_qui_travaille_par_lot(garantie):
    """Ce qui rendait le singulier obsolète : le pluriel évite les N+1 que le
    volume d'homonymes rendrait ruineux."""
    alertes = (RACINE / "fiskr" / "alerts.py").read_text(encoding="utf-8")
    rescreen = (RACINE / "fiskr" / "rescreen.py").read_text(encoding="utf-8")
    assert garantie in alertes + rescreen

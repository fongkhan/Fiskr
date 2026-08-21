"""
Plafond de LECTURE : ce qu'un connecteur sans lecture en flux peut tenir.

Le plafond de **dépôt** ne suffit pas. Trois connecteurs officiels sont au
format JSON — registre national des gels (DGT), Consolidated Screening List
américaine, exclusions de la Banque mondiale — et la bibliothèque standard
n'offre pas de lecture en flux : `json.load` construit l'arbre entier. Or
l'arbre pèse **plus** que le fichier, et le facteur dépend du contenu. Mesuré
avec `tracemalloc` :

| Contenu | Facteur | 512 Mo donneraient |
|---|---:|---:|
| entrées CSL réalistes | ×4,0 | 2,0 Go |
| chaînes courtes distinctes | ×6,0 | 3,0 Go |
| objets minuscules `{"a":1}` | ×15,1 | 7,5 Go |
| listes vides `[]` | ×16,1 | 8,0 Go |

Sur un hébergement mutualisé le processus meurt, et sous Passenger c'est le
worker web entier qui tombe. 64 Mo laisse plus de trois fois la marge du plus
gros fichier réel (la CSL de trade.gov pèse une vingtaine de mégaoctets) et
borne le pire cas adverse à environ un gigaoctet.

Deux lecteurs qui n'avaient pas besoin de ce plafond ont été mis en flux : le
ConList britannique (`f.read().splitlines()` matérialisait un demi-gigaoctet de
texte) et les pages HTML d'alerte (`HTMLParser.feed` accepte des morceaux).
"""
import json

import pytest

from fiskr import ingest
from fiskr.ingest import (FichierTropVolumineux, TAILLE_MAX_LECTURE_BLOC,
                          charger_json_borne, parse_ofsi_conlist_csv,
                          _verifie_taille_bloc)


# ------------------ LE PLAFOND ------------------

def test_un_fichier_ordinaire_passe(tmp_path):
    fichier = tmp_path / "petit.json"
    fichier.write_text(json.dumps({"results": [{"id": "A"}]}))
    assert charger_json_borne(str(fichier)) == {"results": [{"id": "A"}]}


def test_un_fichier_au_dela_du_plafond_est_refuse_avant_lecture(tmp_path, monkeypatch):
    fichier = tmp_path / "gros.json"
    fichier.write_text("[" + ",".join(['{"a":1}'] * 500) + "]")

    def _interdit(*a, **k):
        raise AssertionError("le fichier a été lu malgré le refus")

    monkeypatch.setattr(ingest.json, "load", _interdit)
    with pytest.raises(FichierTropVolumineux) as exc:
        charger_json_borne(str(fichier), plafond=1024)
    assert exc.value.plafond == 1024
    assert "Mo" in str(exc.value)
    # Le message dit quoi faire, pas seulement que c'est refusé
    assert "flux" in str(exc.value)


def test_le_plafond_laisse_de_la_marge_au_plus_gros_fichier_reel():
    """La CSL de trade.gov pèse une vingtaine de mégaoctets : le plafond doit
    lui laisser de la marge, sinon il casse un import légitime."""
    assert TAILLE_MAX_LECTURE_BLOC >= 3 * 20 * 1024 * 1024


def test_un_fichier_absent_ne_leve_pas_ici(tmp_path):
    """Le contrôle de taille ne doit pas voler son erreur au lecteur : c'est
    lui qui dira « fichier introuvable », avec le bon message."""
    _verifie_taille_bloc(str(tmp_path / "inexistant.json"))


# ------------------ LES TROIS CONNECTEURS JSON L'EMPRUNTENT ------------------

@pytest.mark.parametrize("parseur", [
    "parse_dgt_gels_json", "parse_csl_json", "parse_worldbank_debarred_json",
])
def test_les_connecteurs_json_passent_par_le_lecteur_borne(parseur, tmp_path, monkeypatch):
    fichier = tmp_path / "liste.json"
    fichier.write_text("{}")
    appels = []
    vrai = ingest.charger_json_borne
    monkeypatch.setattr(ingest, "charger_json_borne",
                        lambda *a, **k: appels.append(a[0]) or vrai(*a, **k))
    list(getattr(ingest, parseur)(str(fichier)))
    assert appels == [str(fichier)], f"{parseur} lit le JSON sans passer par la borne"


def test_aucune_lecture_json_brute_ne_subsiste():
    """Garde-fou : le motif supprimé ne doit pas revenir par une autre porte."""
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "fiskr" / "ingest.py").read_text()
    # La seule occurrence tolérée est celle DANS charger_json_borne
    assert source.count("json.load(") == 1, (
        "une lecture JSON non bornée a été réintroduite : passer par "
        "charger_json_borne()."
    )


# ------------------ LE ConList EST LU EN FLUX ------------------

_PREAMBULE = "Last Updated:,15/07/2026,,,,,,,,,,,,,,,,,,,\n"
_ENTETE = ("Name 6,Name 1,Name 2,Name 3,Name 4,Name 5,Title,DOB,Town of Birth,"
           "Country of Birth,Nationality,Position,Address 1,Address 2,Address 3,"
           "Post/Zip Code,Country,Other Information,Group Type,Alias Type,Regime,"
           "Group ID,Name Non-Latin Script,Passport Number,NI Number,Listed On\n")
_LIGNE = ("PETROV,Igor,,,,,Gen,12/03/1965,Moscow,Russia,Russian,Minister,12 Tverskaya,"
          ",,125009,Russia,Officiel,Individual,Primary name,Russia,10001,,750123456,"
          "AB123456C,14/08/2016\n")


def test_le_conlist_est_lu_sans_materialiser_le_fichier(tmp_path, monkeypatch):
    fichier = tmp_path / "ConList.csv"
    fichier.write_text(_PREAMBULE + _ENTETE + _LIGNE, encoding="utf-8")

    vrai_open = open

    class _FluxSurveille:
        def __init__(self, flux):
            self._flux = flux

        def read(self, *a):
            if not a:
                raise AssertionError("read() sans borne : le fichier est matérialisé")
            return self._flux.read(*a)

        def __getattr__(self, nom):
            return getattr(self._flux, nom)

        def __iter__(self):
            return iter(self._flux)

        def __enter__(self):
            self._flux.__enter__()
            return self

        def __exit__(self, *a):
            return self._flux.__exit__(*a)

    def _open_surveille(chemin, *a, **k):
        return _FluxSurveille(vrai_open(chemin, *a, **k))

    monkeypatch.setattr("builtins.open", _open_surveille)
    entites = list(parse_ofsi_conlist_csv(str(fichier)))
    assert len(entites) == 1
    assert entites[0]["entity_id"] == "OFSI-10001"


def test_le_preambule_est_toujours_saute(tmp_path):
    """Le format OFSI place une ou plusieurs lignes de préambule avant
    l'en-tête : les sauter est ce que la lecture en flux devait préserver."""
    fichier = tmp_path / "ConList.csv"
    fichier.write_text(_PREAMBULE * 3 + _ENTETE + _LIGNE, encoding="utf-8")
    entites = list(parse_ofsi_conlist_csv(str(fichier)))
    assert [e["primary_name"] for e in entites] == ["Igor PETROV"]


def test_un_conlist_sans_preambule_passe_aussi(tmp_path):
    fichier = tmp_path / "ConList.csv"
    fichier.write_text(_ENTETE + _LIGNE, encoding="utf-8")
    assert len(list(parse_ofsi_conlist_csv(str(fichier)))) == 1


# ------------------ LES PAGES HTML SONT ALIMENTÉES PAR BLOCS ------------------

def test_le_lecteur_html_alimente_le_parseur_par_morceaux(tmp_path):
    from fiskr.ingest import _read_html_table_rows
    lignes = "".join(f"<tr><td>SOCIETE {i}</td><td>https://x{i}.example</td></tr>"
                     for i in range(300))
    page = f"<html><body><table><tr><th>Nom</th><th>Site</th></tr>{lignes}</table></body></html>"
    fichier = tmp_path / "alerte.html"
    fichier.write_text(page, encoding="utf-8")
    rendu = list(_read_html_table_rows(str(fichier)))
    assert len(rendu) == 300
    assert rendu[0]["Nom"] == "SOCIETE 0"
    assert rendu[-1]["Site"] == "https://x299.example"

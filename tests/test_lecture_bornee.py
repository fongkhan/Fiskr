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
    """Le plafond doit laisser de la marge aux fichiers lus d'un bloc, sinon il
    casse un import légitime. Réécrit le 25 novembre 2026, et le dit : la CSL
    de trade.gov, qui servait d'étalon (« une vingtaine de mégaoctets »), pèse
    désormais 108 Mo — sa synchronisation échouait chaque nuit en production
    sur ce plafond. Elle se lit maintenant en flux (cf. plus bas) ; le
    plafond reste celui des connecteurs JSON encore lus d'un bloc."""
    assert TAILLE_MAX_LECTURE_BLOC >= 3 * 20 * 1024 * 1024


def test_un_fichier_absent_ne_leve_pas_ici(tmp_path):
    """Le contrôle de taille ne doit pas voler son erreur au lecteur : c'est
    lui qui dira « fichier introuvable », avec le bon message."""
    _verifie_taille_bloc(str(tmp_path / "inexistant.json"))


# ------------------ LES TROIS CONNECTEURS JSON L'EMPRUNTENT ------------------

@pytest.mark.parametrize("parseur", [
    "parse_dgt_gels_json", "parse_worldbank_debarred_json",
])
def test_les_connecteurs_json_passent_par_le_lecteur_borne(parseur, tmp_path, monkeypatch):
    """(La CSL n'y est plus : elle se lit en flux, cf. plus bas.)"""
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


# ------------------ LA CSL EST LUE EN FLUX ------------------

def _csl(n, avant=None):
    """Une CSL synthétique : chaînes à échapper, crochets dans les valeurs,
    nombres, et des clés avant `results` comme dans le vrai fichier."""
    lignes = [{"id": f"csl-{i}", "name": f"Société \"{i}\" [export] {{x}}",
               "type": "Entity", "source": "Entity List (EL) - Bureau of Industry and Security",
               "alt_names": [f"Alias, {i}"], "score": i * 1.5, "remarks": None}
              for i in range(n)]
    return dict(avant or {"total": n, "sources_used": [{"source": "EL", "n": n}]}, results=lignes)


@pytest.mark.parametrize("taille_bloc", [1, 7, 64, 4096])
def test_le_lecteur_en_flux_rend_exactement_ce_que_rend_json_load(tmp_path, taille_bloc):
    """Des blocs d'un octet à quelques Ko : chaque élément coupé par la fin
    d'un bloc doit être relu entier, y compris un nombre (12 pour 123)."""
    import json as _json
    fichier = tmp_path / "csl.json"
    donnees = _csl(40)
    fichier.write_text(_json.dumps(donnees, ensure_ascii=False, indent=1), encoding="utf-8")
    lus = list(ingest.iterer_tableau_json(str(fichier), "results", taille_bloc=taille_bloc))
    assert lus == donnees["results"]


def test_le_lecteur_en_flux_lit_un_tableau_racine_et_ignore_une_cle_absente(tmp_path):
    import json as _json
    racine = tmp_path / "racine.json"
    racine.write_text(_json.dumps([1, 22, 333, {"a": [4]}]), encoding="utf-8")
    assert list(ingest.iterer_tableau_json(str(racine), taille_bloc=2)) == [1, 22, 333, {"a": [4]}]
    sans = tmp_path / "sans.json"
    sans.write_text(_json.dumps({"total": 0, "autre": [1, 2]}), encoding="utf-8")
    assert list(ingest.iterer_tableau_json(str(sans), "results")) == []


def test_un_json_tronque_leve_au_lieu_de_rendre_une_liste_partielle(tmp_path):
    import json as _json
    fichier = tmp_path / "tronque.json"
    texte = _json.dumps(_csl(5))
    fichier.write_text(texte[: len(texte) // 2], encoding="utf-8")
    with pytest.raises(ValueError):
        list(ingest.iterer_tableau_json(str(fichier), "results", taille_bloc=16))


def test_une_csl_au_dela_du_plafond_de_lecture_en_bloc_est_lue(tmp_path, monkeypatch):
    """La situation de production : un fichier plus gros que le plafond des
    lecteurs en bloc. Il est lu en entier, sans jamais construire l'arbre."""
    import json as _json
    fichier = tmp_path / "csl.json"
    fichier.write_text(_json.dumps(_csl(300)), encoding="utf-8")
    monkeypatch.setattr(ingest, "TAILLE_MAX_LECTURE_BLOC", 1024)
    monkeypatch.setattr(ingest.json, "load",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("arbre entier construit")))
    fiches = list(ingest.parse_csl_json(str(fichier), excluded_sources=()))
    assert len(fiches) == 300
    assert fiches[7]["entity_id"].endswith("csl-7")


@pytest.mark.parametrize("taille_bloc", [3, 64, 4096])
def test_un_element_demesure_est_saute_sans_etre_construit(tmp_path, taille_bloc):
    """
    La CSL réelle du 24 novembre 2026 : une entrée de l'Entity List sans nom
    dont le champ `addresses` pèse 79 Mo (la suivante : 20 Ko). Lue, elle
    tenait ~300 Mo de mémoire pour une fiche écartée faute de nom. Elle est
    sautée — chaînes, échappements et crochets compris — et signalée ; ses
    voisines sont lues intactes.
    """
    import json as _json
    geante = {"id": "x", "name": "", "addresses": [
        {"address": f'rue "{i}" [{i}] {{{i}}} \\ fin', "city": "é" * 3} for i in range(400)]}
    donnees = {"total": 3, "results": [{"id": "a", "name": "Avant"}, geante,
                                       {"id": "b", "name": 'Après ["] \\'}]}
    fichier = tmp_path / "csl.json"
    fichier.write_text(_json.dumps(donnees, ensure_ascii=False), encoding="utf-8")
    sautes = []
    lus = list(ingest.iterer_tableau_json(str(fichier), "results", taille_bloc=taille_bloc,
                                          taille_max_element=2000, si_ecarte=sautes.append))
    assert lus == [donnees["results"][0], donnees["results"][2]]
    assert len(sautes) == 1 and sautes[0] >= len(_json.dumps(geante, ensure_ascii=False)) - 2


def test_un_element_ordinaire_n_est_jamais_saute(tmp_path):
    import json as _json
    fichier = tmp_path / "csl.json"
    fichier.write_text(_json.dumps(_csl(50)), encoding="utf-8")
    sautes = []
    lus = list(ingest.iterer_tableau_json(str(fichier), "results", taille_bloc=128,
                                          si_ecarte=sautes.append))
    assert len(lus) == 50 and sautes == []

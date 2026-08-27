"""
Comparer deux versions d'une fiche listée, à l'homologation.

Le delta savait déjà dire « aliases.high_priority a changé » et posait côte à
côte deux blocs JSON. Sur une fiche à quinze alias, repérer à l'œil celui qui
est ENTRÉ tient de l'exercice de vision — or c'est exactement l'écart qui
élargit ou rétrécit la couverture du criblage. Le réviseur qui approuve une
liste doit pouvoir dire ce qu'il approuve.

Ce lot compare élément par élément et dit ce qui entre, ce qui sort, ce qui
reste. La logique est écrite en JavaScript ; ces tests l'exécutent vraiment
(Node), plutôt que de vérifier que le fichier contient les bonnes lettres :
une comparaison fausse passerait n'importe quelle inspection textuelle.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _extraire(nom_fonction, source=None):
    """Découpe une fonction de app.js à l'accolade fermante de son bloc."""
    src = source if source is not None else _lire("app.js")
    debut = src.index(f"function {nom_fonction}(")
    profondeur, i = 0, src.index("{", debut)
    for k in range(i, len(src)):
        if src[k] == "{":
            profondeur += 1
        elif src[k] == "}":
            profondeur -= 1
            if profondeur == 0:
                return src[debut:k + 1]
    raise AssertionError(f"fonction {nom_fonction} non terminée")


_NODE = shutil.which("node") or shutil.which("nodejs")
besoin_node = pytest.mark.skipif(_NODE is None, reason="node absent de l'environnement")


def _executer(fonctions, appel):
    """Exécute des fonctions de app.js dans Node et renvoie le résultat JSON."""
    programme = "\n".join(fonctions) + f"\nconsole.log(JSON.stringify({appel}));"
    sortie = subprocess.run([_NODE, "-e", programme], capture_output=True, text=True, timeout=30)
    assert sortie.returncode == 0, sortie.stderr
    return json.loads(sortie.stdout)


# ------------------------------------------------- la comparaison, exécutée

@besoin_node
def test_le_diff_de_listes_dit_ce_qui_entre_et_ce_qui_sort():
    r = _executer([_extraire("_diffDeListes")],
                  '_diffDeListes(["Ivanov", "Ivanoff"], ["Ivanov", "Ivanow"])')
    assert r["entrees"] == ["Ivanow"]
    assert r["sorties"] == ["Ivanoff"]
    assert r["gardees"] == ["Ivanov"]


@besoin_node
def test_un_alias_en_double_qui_disparait_compte_pour_une_sortie():
    """Les doublons comptent : une fiche qui perd l'un de ses deux « Ivanov »
    a bien perdu une entrée — un diff par ensembles ne le verrait pas."""
    r = _executer([_extraire("_diffDeListes")],
                  '_diffDeListes(["Ivanov", "Ivanov"], ["Ivanov"])')
    assert r["sorties"] == ["Ivanov"]
    assert r["entrees"] == []
    assert len(r["gardees"]) == 1


@besoin_node
def test_le_meme_contenu_dans_un_autre_ordre_n_est_pas_un_ecart():
    r = _executer([_extraire("_diffDeListes")],
                  '_diffDeListes(["A", "B"], ["B", "A"])')
    assert r["entrees"] == [] and r["sorties"] == []


@besoin_node
def test_le_chemin_pointe_resout_la_valeur_imbriquee():
    """`changes_detected` donne des chemins pointés (« aliases.high_priority »)
    tandis que `before`/`after` portent la valeur de la clé RACINE : sans
    résolution du chemin, la comparaison porterait sur la mauvaise valeur."""
    fn = [_extraire("_valeurAuChemin")]
    avant = '{"aliases": {"high_priority": ["A"], "low_priority": []}}'
    assert _executer(fn, f'_valeurAuChemin({avant}, "aliases.high_priority")') == ["A"]
    # Un chemin qui ne mène nulle part rend `undefined` sans jeter. On compare
    # côté JavaScript : `JSON.stringify(undefined)` ne produit rien du tout,
    # donc le vérifier depuis Python confondrait « undefined » et « planté ».
    assert _executer(fn, f'_valeurAuChemin({avant}, "countries.residence") === undefined') is True
    assert _executer(fn, '_valeurAuChemin(null, "aliases.high_priority") === undefined') is True
    assert _executer(fn, f'_valeurAuChemin({avant}, "aliases.high_priority.0.x") === undefined') is True


@besoin_node
def test_un_alias_ajoute_se_lit_dans_le_rendu():
    """Le cas réel : la synchronisation ajoute un alias à une fiche listée.
    Le rendu doit montrer l'alias entrant, marqué, et le compte."""
    src = _lire("app.js")
    fonctions = [
        "const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');",
        _extraire("_valeurAuChemin", src),
        _extraire("_texteDeValeur", src),
        _extraire("_diffDeListes", src),
        _extraire("construireDiffDeFiche", src),
    ]
    entree = json.dumps({
        "id": "EU-1", "primary_name": "Ivan IVANOV",
        "changes_detected": ["aliases.high_priority"],
        "before": {"aliases": {"high_priority": ["Ivan Ivanov"], "low_priority": []}},
        "after": {"aliases": {"high_priority": ["Ivan Ivanov", "I. Ivanov"], "low_priority": []}},
    })
    html = _executer(fonctions, f"construireDiffDeFiche({entree})")
    assert "I. Ivanov" in html
    assert 'class="entree"' in html, "l'alias entrant doit être marqué comme entrant"
    assert "1 entrée(s), 0 sortie(s), 1 inchangée(s)." in html
    assert "low_priority" not in html, "un champ inchangé n'a rien à faire dans la comparaison"


@besoin_node
def test_un_champ_scalaire_s_affiche_avant_apres_et_le_vide_se_dit():
    src = _lire("app.js")
    fonctions = [
        "const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;');",
        _extraire("_valeurAuChemin", src),
        _extraire("_texteDeValeur", src),
        _extraire("_diffDeListes", src),
        _extraire("construireDiffDeFiche", src),
    ]
    entree = json.dumps({
        "id": "EU-2", "primary_name": "Société X",
        "changes_detected": ["is_deceased", "individual_name_parsed.maiden_name"],
        "before": {"is_deceased": False, "individual_name_parsed": {"maiden_name": None}},
        "after": {"is_deceased": True, "individual_name_parsed": {"maiden_name": "Petrova"}},
    })
    html = _executer(fonctions, f"construireDiffDeFiche({entree})")
    assert "diff-avant" in html and "diff-apres" in html
    assert "∅" in html, "une valeur absente doit se dire, pas s'afficher vide"
    assert "Petrova" in html


@besoin_node
def test_le_rendu_neutralise_le_contenu_des_fiches():
    """Les noms et alias viennent des listes officielles : contenu tiers."""
    src = _lire("app.js")
    fonctions = [
        "const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');",
        _extraire("_valeurAuChemin", src),
        _extraire("_texteDeValeur", src),
        _extraire("_diffDeListes", src),
        _extraire("construireDiffDeFiche", src),
    ]
    entree = json.dumps({
        "id": "X", "primary_name": "X",
        "changes_detected": ["aliases.high_priority"],
        "before": {"aliases": {"high_priority": []}},
        "after": {"aliases": {"high_priority": ["<script>alert(1)</script>"]}},
    })
    html = _executer(fonctions, f"construireDiffDeFiche({entree})")
    assert "<script>" not in html and "&lt;script&gt;" in html


# --------------------------------------------------------- l'insertion réelle

def test_les_deux_ecrans_partagent_le_meme_rendu():
    """Le dossier figé n'affichait que le NOM des champs touchés : « ce que le
    réviseur avait sous les yeux » s'arrêtait avant l'essentiel. Les deux
    écrans passent maintenant par le même rendu — un seul endroit à corriger
    le jour où la comparaison s'affine."""
    src = _lire("app.js")
    assert src.count("_lignesModifiees(") >= 3, "défini une fois, appelé par les deux écrans"
    assert '_lignesModifiees(modified, "review")' in src
    assert '_lignesModifiees(items, "historique")' in src
    # L'ancien rendu, deux blocs JSON dans une cellule, ne doit plus traîner
    assert "JSON.stringify(before)" not in src


def test_le_bouton_ne_transporte_pas_de_donnee_dans_l_attribut():
    """Un nom de listé dans un onclick, c'est du contenu tiers dans du code :
    l'indice suffit, la fiche reste dans le registre côté script."""
    src = _lire("app.js")
    lignes = _extraire("_lignesModifiees", src)
    assert re.search(r"ouvrirDiffDeFiche\('\$\{escapeHtml\(contexte\)\}', \$\{i\}\)", lignes), \
        "le bouton doit passer un contexte et un indice, jamais la fiche elle-même"
    assert "_diffsDeFiches[contexte] = items" in lignes


def test_la_modale_existe_et_se_ferme_comme_les_autres():
    page = _lire("index.html")
    assert 'id="diff-fiche-modal"' in page
    for ident in ("diff-fiche-titre", "diff-fiche-corps"):
        assert f'id="{ident}"' in page, ident
    bloc = page[page.index('id="diff-fiche-modal"'):]
    bloc = bloc[:bloc.index("<div id=\"app-dialog\"")]
    assert 'class="close-modal"' in bloc and "aria-label=\"Fermer\"" in bloc


def test_l_entree_et_la_sortie_ne_se_distinguent_pas_que_par_la_couleur():
    """Huit pour cent des hommes perçoivent mal le rouge et le vert : sur un
    écran qui dit ce qui entre et ce qui sort d'une liste de sanctions, la
    couleur seule ne peut pas porter l'information."""
    src = _lire("app.js")
    lignes = _extraire("construireDiffDeFiche", src)
    assert '"−"' in lignes and '"+"' in lignes, "le signe doit accompagner la couleur"
    css = _lire("styles.css")
    assert ".diff-signe" in css and ".diff-liste li.entree" in css and ".diff-liste li.sortie" in css


def test_les_libelles_du_diff_sont_traduits():
    dico = _lire("i18n.js")
    for cle in ("Comparer", "Comparaison de la fiche", "Avant", "Après",
                "Mêmes entrées, ordre différent."):
        assert f'"{cle}"' in dico, cle
    assert re.search(r"\(\\d\+\) entrée\\\(s\\\)", dico), \
        "la ligne de comptage doit passer par une règle composée"

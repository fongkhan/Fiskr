"""
Une modale affirme `aria-modal="true"` : rien d'autre n'existe tant qu'elle est
ouverte. Au clavier, l'affirmation était fausse douze fois sur douze — mesuré
dans un navigateur réel : aucune modale ne faisait entrer le focus, et la
tabulation repartait aussitôt dans la page du dessous.

Deux règles tiennent le reste, et les deux se vérifient sur la source :

* **Une modale se ferme d'une seule façon.** Échap et le clic sur le fond
  doivent rejouer la commande de fermeture DÉCLARÉE dans le balisage — celle
  que porte déjà la croix —, sinon ils ferment sans le ménage qui va avec.
  `closeAlertModal` remet l'alerte courante à zéro ; Échap, qui posait
  directement la classe `hidden`, laissait un identifiant d'alerte périmé
  derrière lui.
* **Ce qu'on croit focalisable doit l'être.** `[href]` tout court attrape les
  `<use href="#icone">` des SVG : ils satisfont le sélecteur et refusent le
  focus. Deux modales sur onze restaient ainsi hors d'atteinte, sans la
  moindre erreur pour le dire.
"""
import os
import re

import pytest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fiskr", "static")

# La modale générique à Promise se ferme par ses propres boutons (résolution de
# la promesse) : elle ne porte pas de croix, et c'est voulu.
SANS_CROIX = {"app-dialog"}


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def index():
    return _lire("index.html")


@pytest.fixture(scope="module")
def app_js():
    return _lire("app.js")


def _modales(index):
    """Les vraies modales : classe « modal » exactement, pas « modal-content »."""
    return re.findall(r'<div id="([\w-]+)" class="modal(?: hidden)?"', index)


def _corps(app_js, nom):
    debut = app_js.index(f"function {nom}(")
    return app_js[debut:app_js.index("\n}", debut)]


def _code_seul(corps):
    """Le code sans ses commentaires : une garde qui lit le commentaire
    expliquant le défaut y retrouverait le défaut."""
    return "\n".join(re.sub(r"//.*$", "", ligne) for ligne in corps.splitlines())


def test_chaque_modale_declare_sa_commande_de_fermeture(index):
    """
    `fermerModale` ne tient pas de table : elle DÉRIVE la marche à suivre du
    balisage, en rejouant la croix. Une modale livrée sans croix retomberait
    donc sur un simple masquage — et perdrait le ménage qui accompagne sa
    fermeture.
    """
    manquantes = []
    for id_modale in _modales(index):
        if id_modale in SANS_CROIX:
            continue
        bloc = index[index.index(f'<div id="{id_modale}" class="modal'):]
        bloc = bloc[:bloc.index("</div>\n", bloc.index("</h2>") if "</h2>" in bloc[:2000] else 0) + 2000]
        if not re.search(r'class="close-modal"[^>]*onclick=|onclick=[^>]*class="close-modal"', bloc):
            manquantes.append(id_modale)
    assert not manquantes, (
        "Modales sans commande de fermeture déclarée dans le balisage :\n  "
        + "\n  ".join(manquantes))


def test_echap_et_le_fond_passent_par_la_commande_declaree(app_js):
    """
    Garde de source. Deux chemins de fermeture qui divergent, c'est un ménage
    fait dans un cas et pas dans l'autre : le défaut ne se voit qu'une fois
    l'état périmé réutilisé, très loin de là.
    """
    corps = _code_seul(_corps(app_js, "initA11y"))
    assert corps.count("fermerModale(") == 2, (
        "Échap et le clic sur le fond doivent tous deux passer par fermerModale")
    assert 'classList.add("hidden")' not in corps, (
        "initA11y ferme une modale à la main : le ménage de la commande "
        "déclarée (remise à zéro de l'alerte courante, style en ligne de la "
        "modale d'audit) serait sauté")


def test_le_selecteur_focalisable_ne_ramasse_pas_les_icones(app_js):
    """
    `[href]` sans balise attrape `<use href="#i-share-2">`. Le focus part alors
    sur un élément qui ne le prend pas : il reste dehors, la modale s'ouvre
    quand même, et rien ne signale que le clavier est resté derrière.
    """
    debut = app_js.index("const _SELECTEUR_FOCALISABLE")
    selecteur = app_js[debut:app_js.index(";", debut)]
    assert "a[href]" in selecteur
    assert not re.search(r"(?<![\w\]])\[href\]", selecteur), (
        "sélecteur trop large : il attraperait les <use href> des SVG")


def test_l_entree_dans_la_modale_est_verifiee_et_non_supposee(app_js):
    """
    Appeler `focus()` ne garantit pas que le focus ait bougé. Le code doit le
    CONSTATER et se rabattre sur la modale elle-même — c'est exactement ce qui
    manquait quand le sélecteur ramassait une icône.
    """
    corps = _code_seul(_corps(app_js, "_entrerDansLaModale"))
    assert "m.contains(document.activeElement)" in corps
    assert 'setAttribute("tabindex", "-1")' in corps


def test_la_visibilite_d_une_modale_ne_se_lit_pas_dans_offsetParent(app_js):
    """
    Une modale est en `position: fixed` : son `offsetParent` est nul même
    grande ouverte. Le test qui s'y fiait déclarait les douze modales fermées
    en permanence — donc n'ouvrait jamais rien.
    """
    corps = _code_seul(_corps(app_js, "_modaleVisible"))
    assert "getComputedStyle" in corps
    assert "offsetParent" not in corps


def test_le_clic_sur_le_fond_n_est_pose_qu_une_fois(app_js):
    """
    Il y avait deux implémentations : celle d'`initA11y`, pour les douze
    modales, et une affectation `window.onclick` qui n'en couvrait que quatre
    et les fermait autrement. Deux copies d'une même règle finissent toujours
    par diverger — et `window.onclick` écrase en prime tout autre gestionnaire.
    """
    assert "window.onclick" not in _code_seul(app_js), (
        "seconde implémentation du clic sur le fond : initA11y la pose déjà "
        "pour toutes les modales")

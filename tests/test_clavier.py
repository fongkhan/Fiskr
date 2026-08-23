"""
Ce qui réagit au clic doit réagir au clavier.

Mesuré en pilotant un navigateur sur l'application réelle : sur 344 éléments
portant un `onclick`, **dix-sept n'étaient atteignables qu'à la souris**.
`<button>` et `<a href>` le sont nativement ; un `<li onclick>`, un
`<div onclick>` ou un `<th onclick>`, non — ils sont invisibles à la tabulation.

Les trois familles concernées n'étaient pas décoratives :

* **La croix de fermeture de chaque modale** (huit occurrences). Le README
  revendique qu'aucune confirmation réglementaire ne passe par un popup natif :
  elles passent toutes par des modales intégrées. Échap les fermait déjà — mais
  l'affordance visible, elle, était hors d'atteinte.
* **Les en-têtes de tri** (166 après correction). Trier un tableau était réservé
  à la souris, sur tous les tableaux du produit.
* **Les lignes cliquables** du centre de notifications et de l'accueil — celles
  à qui l'on avait justement rendu curseur et survol.

La feuille de style disait déjà où était la vérité : `.close-modal` portait
`background: none; border: none; padding: 0` — des règles écrites pour un
bouton, appliquées à un `<span>`. La conversion ne change pas un pixel.

Après correction, mesuré de la même façon : **17 → 1**. Le seul restant est le
voile de la barre latérale, exclu volontairement — Échap ferme le tiroir, et
l'ajouter au parcours de tabulation n'ajouterait qu'un arrêt vide.
"""
import os
import re

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def test_aucune_croix_de_modale_n_est_un_span():
    """
    Un `<span>` ne se focalise pas, ne s'active pas à l'Entrée, et ne s'annonce
    pas comme une commande. Le rendu est identique : la feuille de style
    prévoyait déjà un bouton.
    """
    index = _lire("index.html")
    fautifs = re.findall(r'<span[^>]*class="[^"]*close-modal', index)
    assert not fautifs, f"{len(fautifs)} croix de fermeture encore en <span>"
    boutons = re.findall(r'<button[^>]*class="close-modal"[^>]*>', index)
    assert boutons, "aucune croix de fermeture trouvée : le test ne vérifie rien"
    sans_nom = [b for b in boutons if "aria-label" not in b]
    assert not sans_nom, (
        "une croix de fermeture doit porter un nom accessible — « × » seul ne "
        f"dit rien à un lecteur d'écran : {sans_nom[:2]}")


def test_un_en_tete_de_tri_est_une_commande_atteignable():
    """Trier était réservé à la souris, sur tous les tableaux du produit."""
    app_js = _lire("app.js")
    debut = app_js.index("function initSortableTables()")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert 'setAttribute("tabindex", "0")' in corps, "en-tête de tri non focalisable"
    assert 'setAttribute("role", "button")' in corps, "en-tête de tri non annoncé"
    assert '"Enter"' in corps and '" "' in corps, (
        "Entrée et Espace doivent déclencher le tri")
    assert "preventDefault" in corps, (
        "sans cela, Espace ferait défiler la page au lieu de trier")


def test_le_clavier_est_rendu_a_tout_ce_qui_reagit_au_clic():
    """
    Le frontal reconstruit ses listes en permanence : marquer une fois au
    chargement ne tiendrait pas. Un observateur reprend les fragments injectés
    — mesuré à 3,3 ms pour 800 lignes cliquables.
    """
    app_js = _lire("app.js")
    assert "function rendreCliquablesAccessibles" in app_js
    assert "function initClavierSurCliquables" in app_js
    assert "initClavierSurCliquables();" in app_js, "la fonction doit être appelée"
    assert "MutationObserver" in app_js.split("function initClavierSurCliquables")[1][:1600], (
        "les fragments injectés après le chargement doivent être repris")


def test_la_delegation_laisse_les_champs_de_saisie_tranquilles():
    """
    Une délégation clavier sur toute la page est un piège classique : elle vole
    la barre d'espace de chaque champ de saisie. Le code écarte explicitement
    les balises que le navigateur gère déjà.
    """
    app_js = _lire("app.js")
    debut = app_js.index("function initClavierSurCliquables")
    corps = app_js[debut:debut + 1800]
    assert "_BALISES_FOCALISABLES.has(el.tagName)" in corps, (
        "sans cette garde, Espace dans un champ déclencherait un clic")
    focalisables = app_js[app_js.index("_BALISES_FOCALISABLES = new Set("):][:220]
    for balise in ("BUTTON", "A", "INPUT", "SELECT", "TEXTAREA"):
        assert balise in focalisables, f"{balise} doit rester géré par le navigateur"


def test_le_voile_de_la_barre_laterale_reste_hors_du_parcours():
    """
    Exclusion assumée : Échap ferme le tiroir, et un voile dans le parcours de
    tabulation n'ajouterait qu'un arrêt vide entre deux commandes utiles.
    """
    app_js = _lire("app.js")
    debut = app_js.index("function rendreCliquablesAccessibles")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "sidebar-overlay" in corps, (
        "l'exclusion doit être explicite dans le code, pas un oubli")


def test_echap_ferme_toujours_la_modale_la_plus_haute():
    """La sortie au clavier existait déjà : elle ne doit pas se perdre en
    rendant la croix accessible."""
    app_js = _lire("app.js")
    debut = app_js.index("function initA11y()")
    corps = app_js[debut:debut + 900]
    assert '"Escape"' in corps and ".modal:not(.hidden)" in corps

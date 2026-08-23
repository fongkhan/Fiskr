"""
Un panneau d'onglet doit vivre DANS son onglet.

Signalé en production comme « un gros souci d'affichage ». Le diagnostic, mené
en pilotant un navigateur sur l'application réelle : quatre panneaux —
Ajout Manuel, Sources, Homologation, Historique — n'étaient enfants d'aucune
section. Le navigateur les avait remontés directement sous `<body>`, à cause
d'un `</div>` excédentaire qui refermait `#sec-watchlist-mgmt` trop tôt.

Trois conséquences, toutes visibles à l'écran :

* **Ils s'affichaient sur tous les onglets.** Hors de toute `.tab-content`,
  ils échappaient à `display: none` — Criblage, Audit, Paramètres, peu importe.
* **Ils s'empilaient.** `switchSubTab` désactive les panneaux par une requête
  PORTÉE à la section (`section.querySelectorAll`) mais active par identifiant
  GLOBAL : un panneau hors section n'était donc jamais désactivé, seulement
  activé. Mesuré : trois à quatre panneaux rendus en même temps, une page de
  6 424 px au lieu de 4 451.
* **Ils passaient sous la barre latérale.** Hors de `.main-content`, ils
  perdaient sa `margin-left: 280px` et démarraient à x = 0, sous une barre
  `position: fixed` opaque à 95 %.

L'asymétrie de `switchSubTab` — désactivation portée, activation globale — est
ce qui a transformé une faute de balisage en empilement. Ces tests tiennent la
structure, seule chose qui garantisse que les deux moitiés parlent du même
ensemble de panneaux.
"""
import os

import pytest

from html.parser import HTMLParser

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")

# Éléments sans fermeture : HTML void, plus ceux de SVG utilisés par les icônes.
_SANS_FERMETURE = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
    "path", "use", "circle", "rect", "polyline", "polygon", "line", "ellipse", "stop",
}


class _Arbre(HTMLParser):
    """Reconstruit l'ascendance de chaque panneau, et relève les fautes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pile = []
        self.panneaux = []      # (id, section, dans_main)
        self.sections = []      # (id, dans_main)
        self.fautes = []

    def handle_starttag(self, tag, attrs):
        if tag in _SANS_FERMETURE:
            return
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        self.pile.append((tag, a.get("id"), classes))
        dans_main = any("main-content" in c for _, _, c in self.pile[:-1])
        if "sub-tab-content" in classes:
            section = next((i for _, i, c in reversed(self.pile[:-1]) if "tab-content" in c), None)
            self.panneaux.append((a.get("id"), section, dans_main))
        elif "tab-content" in classes:
            self.sections.append((a.get("id"), dans_main))

    def handle_endtag(self, tag):
        if tag in _SANS_FERMETURE:
            return
        for i in range(len(self.pile) - 1, -1, -1):
            if self.pile[i][0] == tag:
                if i != len(self.pile) - 1:
                    sautes = ", ".join(f"<{t} id={i2}>" for t, i2, _ in self.pile[i + 1:][:3])
                    self.fautes.append(
                        f"ligne {self.getpos()[0]} : </{tag}> ferme en sautant {sautes}")
                del self.pile[i:]
                return
        self.fautes.append(f"ligne {self.getpos()[0]} : </{tag}> sans ouverture correspondante")


def _analyse(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        arbre = _Arbre()
        arbre.feed(f.read())
    return arbre


@pytest.fixture(scope="module")
def index():
    return _analyse("index.html")


def test_chaque_panneau_vit_dans_une_section(index):
    """
    Un panneau hors section échappe à `.tab-content { display: none }` : il
    s'affiche sur TOUS les onglets, et `switchSubTab` — qui désactive par une
    requête portée à la section — ne peut plus l'éteindre.
    """
    orphelins = [p for p, section, _ in index.panneaux if section is None]
    assert not orphelins, (
        "Panneaux de sous-onglet hors de toute section : ils s'afficheront sur "
        "tous les onglets et ne pourront plus être désactivés.\n  "
        + "\n  ".join(str(o) for o in orphelins))


def test_tout_le_contenu_vit_dans_la_zone_principale(index):
    """
    Hors de `.main-content`, un panneau perd sa `margin-left` et démarre à
    x = 0 — c'est-à-dire sous la barre latérale, qui est `position: fixed`.
    """
    dehors = [p for p, _, dans_main in index.panneaux if not dans_main]
    dehors += [s for s, dans_main in index.sections if not dans_main]
    assert not dehors, (
        "Éléments hors de .main-content : ils passeront sous la barre "
        "latérale.\n  " + "\n  ".join(str(d) for d in dehors))


def test_le_balisage_est_equilibre(index):
    """
    La cause première : un `</div>` excédentaire. Le navigateur ne proteste
    pas — il referme la section et remonte la suite sous `<body>`, ce qui rend
    le défaut invisible à la lecture du fichier.
    """
    assert not index.fautes, (
        "Balises mal appariées :\n  " + "\n  ".join(index.fautes[:10]))
    assert not index.pile, (
        "Balises restées ouvertes : "
        + ", ".join(f"<{t} id={i}>" for t, i, _ in index.pile[:5]))


def test_une_seule_vue_active_par_section_dans_le_balisage_livre(index):
    """La page livrée doit s'ouvrir sur UN panneau par section, pas sur une
    pile — c'est l'état de départ dont dépend tout le reste."""
    from collections import Counter

    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    import re
    actifs = Counter()
    for balise in re.findall(r'<div id="sub-sec-[^"]+" class="[^"]*"', html):
        if re.search(r'class="[^"]*\bactive\b', balise):
            panneau = re.search(r'id="(sub-sec-[^"]+)"', balise).group(1)
            section = next(s for p, s, _ in index.panneaux if p == panneau)
            actifs[section] += 1
    trop = {s: n for s, n in actifs.items() if n != 1}
    assert not trop, f"sections ouvrant sur autre chose qu'un seul panneau : {trop}"


def test_le_corpus_verifie_est_reel(index):
    """Garde-fou : si l'analyse se cassait, les tests ci-dessus passeraient sur
    du vide."""
    assert len(index.panneaux) >= 30, f"{len(index.panneaux)} panneau(x) analysé(s)"
    assert len(index.sections) >= 8, f"{len(index.sections)} section(s) analysée(s)"


def test_la_page_de_connexion_est_equilibree_aussi():
    """Même garde sur l'autre page servie."""
    login = _analyse("login.html")
    assert not login.fautes, "\n".join(login.fautes[:5])
    assert not login.pile

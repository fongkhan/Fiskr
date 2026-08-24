"""
Ce qu'un écran PROPOSE doit exister.

Trois défauts se ressemblent et se trouvent de la même façon, en confrontant
le balisage au code plutôt qu'en cliquant :

* une commande écrite dans le balisage qui appelle une fonction absente — le
  bouton est là, il ne fait rien, et rien ne le dit ;
* une fonction que plus personne n'appelle — un chemin doublé, figé, qui
  continue d'être maintenu et de tromper la lecture. La modale « Mon Profil &
  Sécurité » était dans ce cas : l'onglet « Mon compte » avait repris tout son
  travail, la modale n'était plus ouverte par personne, et son formulaire est
  resté en place le temps qu'on le remarque ;
* un identifiant que le code va chercher et que le balisage n'a plus.

Aucun de ces trois-là ne se voit à l'exécution : le navigateur ne signale rien,
la fonction absente ne lève qu'au clic, et `getElementById` rend simplement
`null`.
"""
import os
import re

import pytest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fiskr", "static")

# Ce que le navigateur fournit lui-même : ni le balisage ni app.js n'ont à les définir.
_FOURNIS_PAR_LE_NAVIGATEUR = {
    "if", "for", "while", "switch", "catch", "return", "typeof", "function", "new", "this",
    "alert", "confirm", "prompt", "parseInt", "parseFloat", "Number", "String", "Boolean",
    "Array", "Object", "JSON", "Math", "Date", "event", "setTimeout", "setInterval",
    "encodeURIComponent", "decodeURIComponent", "fetch", "console",
}

_APPEL = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*\(")
_HANDLER = re.compile(
    r'\bon(?:click|change|submit|input|keyup|keydown|keypress|blur|focus)\s*=\s*"([^"]*)"')


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _pages():
    return sorted(n for n in os.listdir(STATIC) if n.endswith(".html"))


def _scripts():
    return sorted(n for n in os.listdir(STATIC) if n.endswith(".js"))


@pytest.fixture(scope="module")
def sources():
    return {n: _lire(n) for n in _pages() + _scripts()}


def _noms_definis(sources):
    noms = set()
    for nom, src in sources.items():
        noms |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", src))
        noms |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", src))
        noms |= set(re.findall(r"\bwindow\.([A-Za-z_$][\w$]*)\s*=", src))
    return noms


def test_chaque_commande_du_balisage_appelle_une_fonction_qui_existe(sources):
    """
    Un `onclick="faireQuelqueChose()"` dont la fonction n'existe pas produit une
    commande morte : le bouton se dessine, se survole, se clique, et lève une
    ReferenceError que personne ne lit. C'est arrivé sur l'écran de backtest.
    """
    definis = _noms_definis(sources) | _FOURNIS_PAR_LE_NAVIGATEUR
    fautes = []
    for page in _pages():
        for expression in _HANDLER.findall(sources[page]):
            for appelee in _APPEL.findall(expression):
                if appelee not in definis:
                    fautes.append(f"{page} : « {expression.strip()[:70]} » appelle {appelee}(), introuvable")
    assert not fautes, "Commandes mortes :\n  " + "\n  ".join(sorted(set(fautes)))


def test_aucune_fonction_de_app_js_n_est_devenue_inatteignable(sources):
    """
    Une fonction que rien n'appelle est un chemin figé : il ne s'exécute plus,
    mais il se lit, se maintient et se copie. Quand un écran en remplace un
    autre, c'est ainsi que l'ancien survit — entier, invisible et faux.

    La règle vaut telle quelle, sans liste d'exceptions : au moment où elle est
    écrite, aucune des fonctions d'`app.js` n'est orpheline.
    """
    app = sources["app.js"]
    ailleurs = "".join(src for nom, src in sources.items() if nom != "app.js")
    orphelines = []
    for m in re.finditer(r"^\s*(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", app, re.M):
        nom, position = m.group(1), m.start(1)
        motif = re.compile(r"(?<![\w$.])" + re.escape(nom) + r"\b")
        if any(o.start() != position for o in motif.finditer(app)):
            continue
        if motif.search(ailleurs):
            continue
        orphelines.append(nom)
    assert not orphelines, (
        "Fonctions que plus personne n'appelle (chemin doublé ou affordance "
        "perdue) :\n  " + "\n  ".join(orphelines))


def test_chaque_identifiant_cherche_par_le_code_existe_dans_le_balisage(sources):
    """
    `getElementById("panneau-x")` sur un identifiant disparu rend `null`. Selon
    la ligne suivante, cela donne une erreur au clic ou — pire — un silence :
    la fonction s'exécute jusqu'au bout sans rien faire.
    """
    app = sources["app.js"]
    connus = set()
    for src in sources.values():
        connus |= set(re.findall(r'\bid="([^"\'{}$`]+)"', src))
        connus |= set(re.findall(r"\bid='([^\"'{}$`]+)'", src))
    connus |= set(re.findall(r'\.id\s*=\s*["\']([\w-]+)["\']', app))
    # Identifiants fabriqués par gabarit : `id="edit-ent-${champ}"`. On retient
    # le préfixe, seule chose que la source connaisse d'avance.
    prefixes = tuple(re.findall(r'\bid="([\w-]*)\$\{', app))

    cherches = set(re.findall(r'getElementById\(\s*["\']([\w-]+)["\']\s*\)', app))
    cherches |= set(re.findall(r'querySelector(?:All)?\(\s*["\']#([\w-]+)["\']\s*\)', app))
    manquants = sorted(
        i for i in cherches
        if i not in connus and not (prefixes and i.startswith(prefixes)))
    assert not manquants, (
        "Identifiants cherchés par app.js et absents du balisage :\n  " + "\n  ".join(manquants))

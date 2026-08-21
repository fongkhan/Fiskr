"""
La documentation est une table copiee a la main.

C'est exactement la classe de defaut que ce depot poursuit ailleurs : une
valeur ecrite une fois a cote du code, une source qui evolue, et plus personne
pour s'apercevoir que les deux ont diverge. Un chemin d'endpoint faux dans un
guide d'integration ne fait pas planter Fiskr — il fait perdre une demi-journee
a l'integrateur qui le suit, et il donne le sentiment que le reste du document
n'est pas fiable non plus.

Ces tests DERIVENT la verification de ce que le code produit reellement :
la table des routes de FastAPI, et le contenu du depot.

Portee volontairement limitee a ce qu'un lecteur utilise pour AGIR (README et
Documentation/). CHANGELOG.md en est exclu : c'est un journal historique, une
entree ancienne a le droit de citer un endpoint depuis retire.
"""

import os
import re

DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _documents():
    doc_dir = os.path.join(DEPOT, "Documentation")
    fichiers = [os.path.join(DEPOT, "README.md")]
    for racine, _, noms in os.walk(doc_dir):
        fichiers.extend(os.path.join(racine, n) for n in sorted(noms) if n.endswith(".md"))
    return [f for f in fichiers if os.path.exists(f)]


def _routes_reelles():
    from fiskr.api import app
    reelles = set()
    for route in app.routes:
        methodes = (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}
        for m in methodes:
            reelles.add((m, _normalise(route.path)))
    return reelles


def _normalise(chemin: str) -> str:
    """Le NOM d'un parametre de chemin n'engage personne : `/{id}` et
    `/{entity_pk}` designent la meme route. Sa PLACE, elle, engage."""
    return re.sub(r"\{[^}]+\}", "{}", chemin).rstrip("/") or "/"


_AVEC_METHODE = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+`?(/api/[A-Za-z0-9_\-/{}.:]*)")
_SANS_METHODE = re.compile(r"`(/api/[A-Za-z0-9_\-/{}]*)`")


def test_chaque_endpoint_cite_avec_sa_methode_existe():
    reelles = _routes_reelles()
    assert len(reelles) > 100, "table de routes suspecte : la derivation a echoue"
    fautes = []
    for doc in _documents():
        texte = open(doc, encoding="utf-8").read()
        for methode, chemin in _AVEC_METHODE.findall(texte):
            chemin = chemin.rstrip("`.,;:)")
            if (methode, _normalise(chemin)) not in reelles:
                fautes.append(f"{os.path.relpath(doc, DEPOT)} : {methode} {chemin}")
    assert not fautes, (
        "Endpoints documentes qui n'existent pas — un integrateur qui suit le "
        "guide recoit un 404 :\n  " + "\n  ".join(sorted(set(fautes))))


def test_chaque_chemin_d_api_cite_seul_existe():
    """Les chemins cites sans methode (`/api/...`) doivent exister sous AU MOINS
    une methode : le document parle bien de quelque chose."""
    chemins_reels = {chemin for _, chemin in _routes_reelles()}
    fautes = []
    for doc in _documents():
        texte = open(doc, encoding="utf-8").read()
        for chemin in _SANS_METHODE.findall(texte):
            if _normalise(chemin) not in chemins_reels:
                fautes.append(f"{os.path.relpath(doc, DEPOT)} : {chemin}")
    assert not fautes, "Chemins d'API documentes qui n'existent pas :\n  " + "\n  ".join(sorted(set(fautes)))


_FICHIER_DU_DEPOT = re.compile(
    r"`((?:fiskr|tests|tools|Documentation|scripts)/[A-Za-z0-9_\-/.]+\.[a-z]{2,4})`")


def test_chaque_fichier_du_depot_cite_existe():
    """Renommer un module sans relire la documentation laisse derriere soi des
    renvois vers des fichiers qui n'existent plus."""
    fautes = []
    for doc in _documents():
        for chemin in _FICHIER_DU_DEPOT.findall(open(doc, encoding="utf-8").read()):
            if not os.path.exists(os.path.join(DEPOT, chemin)):
                fautes.append(f"{os.path.relpath(doc, DEPOT)} : {chemin}")
    assert not fautes, "Fichiers cites qui n'existent pas :\n  " + "\n  ".join(sorted(set(fautes)))


_LIEN_INTERNE = re.compile(r"\]\((?!https?:|#|mailto:)([^)#]+)")


def test_aucun_renvoi_interne_mort():
    """Les documents se citent entre eux ; un renvoi mort coupe le fil que le
    lecteur suivait."""
    fautes = []
    for doc in _documents():
        base = os.path.dirname(doc)
        for cible in _LIEN_INTERNE.findall(open(doc, encoding="utf-8").read()):
            if not os.path.exists(os.path.normpath(os.path.join(base, cible.strip()))):
                fautes.append(f"{os.path.relpath(doc, DEPOT)} -> {cible}")
    assert not fautes, "Renvois internes morts :\n  " + "\n  ".join(sorted(set(fautes)))


def test_la_verification_porte_sur_un_corpus_reel():
    """Garde-fou : si la collecte des documents se casse, les tests ci-dessus
    passeraient sur un corpus vide sans rien verifier du tout."""
    docs = _documents()
    assert len(docs) >= 10, f"corpus documentaire suspect : {len(docs)} fichier(s)"
    cites = 0
    for doc in docs:
        texte = open(doc, encoding="utf-8").read()
        cites += len(_AVEC_METHODE.findall(texte)) + len(_SANS_METHODE.findall(texte))
    assert cites >= 50, f"seulement {cites} endpoint(s) cite(s) : la collecte est cassee"

_LIEN_ABSOLU_LOCAL = re.compile(r"file:///[a-zA-Z]:/|file:///home/|file:///Users/")


def test_aucun_renvoi_vers_le_disque_de_son_auteur():
    """
    Un lien `file:///e:/Program Files/git/Fiskr/...` ne mene nulle part pour
    quiconque n'est pas devant cette machine-la, et il publie au passage
    l'arborescence locale de celui qui a redige le document. Ces renvois
    doivent etre relatifs au depot.
    """
    fautes = []
    for doc in _documents():
        for i, ligne in enumerate(open(doc, encoding="utf-8"), 1):
            if _LIEN_ABSOLU_LOCAL.search(ligne):
                fautes.append(f"{os.path.relpath(doc, DEPOT)}:{i}")
    assert not fautes, (
        "Renvois vers un chemin absolu de machine locale :\n  " + "\n  ".join(fautes))

def test_le_compte_de_tests_annonce_par_le_readme_est_juste():
    """
    Le README annonce la taille de la suite. Ce nombre est une table recopiee a
    la main comme une autre : il annoncait 153 pendant que la suite en comptait
    dix fois plus. Il se derive donc de `tests/`.
    """
    import ast

    dossier = os.path.join(DEPOT, "tests")
    fonctions, fichiers = 0, 0
    for nom in sorted(os.listdir(dossier)):
        if not (nom.startswith("test_") and nom.endswith(".py")):
            continue
        fichiers += 1
        with open(os.path.join(dossier, nom), encoding="utf-8") as f:
            arbre = ast.parse(f.read())
        fonctions += sum(
            1 for n in ast.walk(arbre)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_"))

    assert fonctions > 500, f"comptage suspect : {fonctions} fonction(s)"
    with open(os.path.join(DEPOT, "README.md"), encoding="utf-8") as f:
        # Blancs normalises : la phrase peut etre coupee sur deux lignes par
        # l'habillage du document sans cesser d'etre la meme phrase.
        readme = re.sub(r"\s+", " ", f.read())

    # Ecrit avec une espace fine ou insecable selon la typographie du document
    attendu = [f"{fonctions:,}".replace(",", sep) for sep in (" ", "\u202f", "\u00a0", "")]
    assert any(f"**{a} fonctions de test**" in readme for a in attendu), (
        f"Le README doit annoncer **{fonctions:,} fonctions de test** "
        f"(reparties sur {fichiers} fichiers) — mettez la phrase a jour.".replace(",", " "))
    assert f"{fichiers} fichiers" in readme, (
        f"Le README doit annoncer {fichiers} fichiers de test.")


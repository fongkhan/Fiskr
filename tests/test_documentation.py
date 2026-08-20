"""
Documentation : les liens tiennent, l'index est complet, rien ne ment.

Une documentation qui renvoie vers un fichier renommé est pire qu'une
documentation absente : elle a l'air fiable. Ces tests sont là parce que le
dépôt en portait les trois symptômes classiques — un lien vers un document
renommé, des chemins absolus d'un poste de développement
(`file:///e:/Program Files/...`), et des notes de travail décrivant une
interface qui n'existe plus, rangées à côté des documents de référence.

Ils ne jugent pas le contenu. Ils vérifient ce qui se vérifie mécaniquement :
tout lien mène quelque part, tout document est référencé, et les archives sont
signalées comme telles.
"""
import re
from pathlib import Path

import pytest

RACINE = Path(".")
DOCS = Path("Documentation")
INDEX = DOCS / "README.md"
ARCHIVES = DOCS / "archives"


def _markdowns():
    """Tous les documents, sauf le CHANGELOG (historique : il cite des
    chemins d'époque, c'est son rôle)."""
    fichiers = [p for p in RACINE.glob("*.md") if p.name != "CHANGELOG.md"]
    fichiers += sorted(DOCS.rglob("*.md"))
    return fichiers


def _liens(md: Path):
    texte = md.read_text(encoding="utf-8")
    for lien in re.findall(r"\]\(([^)]+)\)", texte):
        yield lien.strip()


def test_l_index_existe_et_range_par_question():
    assert INDEX.exists(), "Documentation/README.md manquant"
    texte = INDEX.read_text(encoding="utf-8")
    assert "Je veux" in texte, "l'index doit être classé par question, pas par nom de fichier"
    # La nature de chaque document est annoncee : un releve date ne se lit pas
    # comme une reference tenue a jour
    for nature in ("Référence", "Parcours", "Relevé", "Étude"):
        assert nature in texte, nature


def test_chaque_document_est_reference_par_l_index():
    """Un document que l'index ne cite pas est un document que personne ne
    trouve."""
    cite = set(re.findall(r"\]\(([^)#]+\.md)\)", INDEX.read_text(encoding="utf-8")))
    cite = {c.split("/")[-1] for c in cite}
    oublies = [p.name for p in DOCS.glob("*.md")
               if p.name != "README.md" and p.name not in cite]
    assert not oublies, f"documents absents de l'index : {oublies}"


def test_aucun_lien_interne_ne_pointe_dans_le_vide():
    casses = []
    for md in _markdowns():
        if ARCHIVES in md.parents:
            continue  # les archives citent des chemins d'epoque, c'est assume
        for lien in _liens(md):
            if lien.startswith(("http://", "https://", "mailto:", "#")):
                continue
            cible = (md.parent / lien.split("#")[0].replace("%20", " "))
            if not cible.exists():
                casses.append(f"{md} -> {lien}")
    assert not casses, "liens cassés :\n" + "\n".join(casses)


def test_aucun_chemin_absolu_de_poste_de_developpement():
    """`file:///e:/Program Files/git/Fiskr/...` ne mène nulle part pour un
    lecteur, et trahit une note recopiée sans relecture."""
    fautifs = []
    for md in _markdowns():
        if ARCHIVES in md.parents:
            continue
        if re.search(r"file:///|[A-Za-z]:\\\\", md.read_text(encoding="utf-8")):
            fautifs.append(str(md))
    assert not fautifs, f"chemins absolus dans : {fautifs}"


def test_les_ancres_du_sommaire_du_README_existent():
    """Un sommaire dont les ancres ne résolvent pas est un sommaire décoratif.
    Les titres portent des emoji, dont le rendu en ancre varie d'un moteur
    Markdown à l'autre : le README pose donc des ancres explicites."""
    texte = Path("README.md").read_text(encoding="utf-8")
    posees = set(re.findall(r'<a id="([^"]+)"></a>', texte))
    citees = set(re.findall(r"\]\(#([^)]+)\)", texte))
    assert citees, "le README doit avoir un sommaire"
    assert citees <= posees, f"ancres introuvables : {sorted(citees - posees)}"


def test_les_archives_se_declarent_comme_telles():
    """Une note périmée rangée à côté d'un document de référence se lit comme
    une référence. Elle doit dire ce qu'elle est."""
    assert ARCHIVES.exists()
    note = ARCHIVES / "README.md"
    assert note.exists(), "Documentation/archives/README.md manquant"
    texte = note.read_text(encoding="utf-8")
    assert "pas comme référence" in texte or "pas une référence" in texte
    # Chaque fichier archive est explique
    for fichier in ARCHIVES.iterdir():
        if fichier.name == "README.md":
            continue
        assert fichier.name in texte, f"{fichier.name} archivé sans explication"


def test_la_documentation_ne_contient_pas_de_code_executable():
    """Un `.py` de 46 Ko dans le dossier documentation, c'est un fichier que
    personne ne lit et que personne ne maintient. Les outils vivent dans
    `tools/`, les archives disent qu'elles sont des archives."""
    egares = [str(p) for p in DOCS.glob("*.py")]
    assert not egares, f"code dans la documentation : {egares}"


def test_le_README_renvoie_vers_le_guide_et_vers_l_index():
    """Les trois portes d'entrée sont distinctes et doivent être annoncées :
    le guide intégré pour se servir du produit, le README pour l'installer et
    l'exploiter, la documentation pour comprendre ses décisions."""
    texte = Path("README.md").read_text(encoding="utf-8")
    assert "Documentation/README.md" in texte
    assert "Guide" in texte and "guide" in texte

"""
Conformité du schéma pivot : aucune clé émise dans le vide.

Chaque parseur de source officielle produit un dictionnaire au « schéma
pivot », que `build_watchlist_entity` reprend clé par clé avec `item.get(...)`.
Une clé mal orthographiée ne lève rien : le champ est simplement absent de la
fiche persistée, donc absent du criblage. C'est arrivé — `build_watchlist_entity`
porte encore deux replis de lecture (`adress`, `additional_info`) qui sont la
trace de fautes de frappe passées, côté import CSV.

Ces trois blocs de construction se ressemblent beaucoup d'un parseur à l'autre
(le détecteur de duplication les signale), mais ce sont des **déclarations de
schéma** : chaque valeur vient de variables locales à la source. Les fusionner
derrière un constructeur à trente arguments n'enlèverait pas une ligne et
retirerait la seule chose qui rend ces blocs relisibles — voir le champ à côté
de sa provenance. Le vrai risque n'est pas la répétition, c'est la clé qui ne
mène nulle part : c'est ce que ce test verrouille, statiquement et pour tous
les parseurs à la fois.
"""
import ast
import re
from pathlib import Path

from fiskr.sync import EXTENDED_ENTITY_FIELDS

INGEST = Path("fiskr/ingest.py").read_text(encoding="utf-8")
SYNC = Path("fiskr/sync.py").read_text(encoding="utf-8")

# Clés que la persistance lit dans le rapport qualité et non dans l'item brut
# (`report["cleansed_name"]`, `["cleansed_aliases"]`, `["resolved_gender"]`).
VIA_RAPPORT_QUALITE = {"primary_name", "aliases", "gender"}


def _cles_emises() -> dict:
    """Clés des dictionnaires pivot rendus par les fonctions `parse_*`."""
    emises: dict = {}
    for fn in ast.walk(ast.parse(INGEST)):
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("parse_"):
            continue
        for node in ast.walk(fn):
            if not isinstance(node, (ast.Yield, ast.Return)):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            for cle in node.value.keys:
                if isinstance(cle, ast.Constant) and isinstance(cle.value, str):
                    emises.setdefault(cle.value, set()).add(fn.name)
    return emises


def _cles_lues() -> set:
    """Clés que `build_watchlist_entity` (et les champs étendus) reprennent."""
    return (set(re.findall(r'item\.get\(\s*["\']([^"\']+)["\']', SYNC))
            | set(EXTENDED_ENTITY_FIELDS)
            | VIA_RAPPORT_QUALITE)


def test_l_analyse_voit_bien_les_parseurs():
    """Garde-fou du test lui-même : s'il ne trouve plus rien (refonte du
    fichier, parseurs déplacés), il passerait à vide sans rien vérifier."""
    emises = _cles_emises()
    assert len(emises) > 30, f"seulement {len(emises)} clés pivot trouvées"
    parseurs = {p for sources in emises.values() for p in sources}
    assert len(parseurs) >= 8, f"seulement {len(parseurs)} parseurs analysés"
    assert "entity_id" in emises and "primary_name" in emises


def test_aucune_cle_pivot_ne_tombe_dans_le_vide():
    """Une clé qu'aucun consommateur ne lit est une donnée perdue en silence :
    la source la fournit, la fiche persistée ne l'a pas, le criblage non plus."""
    lues = _cles_lues()
    orphelines = {cle: sorted(sources) for cle, sources in _cles_emises().items()
                  if cle not in lues}
    assert not orphelines, (
        "clés pivot qu'aucun consommateur ne lit (faute de frappe ou champ à "
        f"brancher dans build_watchlist_entity) : {orphelines}")

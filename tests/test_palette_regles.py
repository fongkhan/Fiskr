"""
La palette de l'éditeur de règles doit refléter le contexte réel.

L'écran des règles anti-faux positifs offre au clic les clés de `ctx`
(`FP_CTX_KEYS`), leurs sous-clés (`FP_CTX_SUBKEYS`) et des modèles de code
(`FP_RULE_SNIPPETS`). Ces trois tables étaient recopiées à la main, et elles
avaient dérivé :

* **cinq clés manquaient** — `perimeter`, `hits_count`, `hit_rank`,
  `corroboration`, `rarity` — c'est-à-dire précisément celles qui existent pour
  qu'une règle puisse raisonner sur la volumétrie, la corroboration et la
  banalité du nom. Un auteur de règle ne pouvait pas les découvrir ;
* deux sous-clés d'`entity` **n'existaient pas** : `programs` et
  `designation_date` (les vraies colonnes sont `sanction_programs` et
  `listed_on`). Une règle écrite depuis ces chips lisait toujours `None` ;
* un modèle livré lisait `adjustments["country_penalty"]`, qui n'existe pas :
  la comparaison valait `0 <= -10`, donc **la règle ne se déclenchait jamais**.

C'est le pire défaut possible sur cet écran : une règle silencieusement inerte
ne signale rien — ni à son auteur, ni à son valideur, ni au contrôleur.

Ces tests dérivent la vérité du moteur plutôt que de la recopier.
"""
import re
from pathlib import Path

import pytest

from fiskr.database import ClientEntity, WatchlistEntity
from fiskr.fprules import build_filtering_ctx, build_screening_ctx, run_rule

RACINE = Path(__file__).resolve().parent.parent
APP_JS = (RACINE / "fiskr" / "static" / "app.js").read_text(encoding="utf-8")


def _cles_palette():
    bloc = re.search(r"const FP_CTX_KEYS = \[(.*?)\n\];", APP_JS, re.S)
    assert bloc, "FP_CTX_KEYS introuvable"
    return [m for m in re.findall(r'key:\s*"([a-z_]+)"', bloc.group(1))]


def _sous_cles():
    bloc = re.search(r"const FP_CTX_SUBKEYS = \{(.*?)\n\};", APP_JS, re.S)
    assert bloc, "FP_CTX_SUBKEYS introuvable"
    sortie = {}
    for nom, corps in re.findall(r"(\w+):\s*\[(.*?)\]", bloc.group(1), re.S):
        sortie[nom] = re.findall(r'"([a-z_]+)"', corps)
    return sortie


def _snippets():
    bloc = re.search(r"const FP_RULE_SNIPPETS = \[(.*?)\n\];", APP_JS, re.S)
    assert bloc, "FP_RULE_SNIPPETS introuvable"
    return re.findall(r'\["([^"]+)",\s*`(.*?)`\]', bloc.group(1), re.S)


def _ctx_criblage():
    return build_screening_ctx(
        {"client_id": "C1", "client_first_name": "IVAN", "client_last_name": "IVANOV",
         "client_dob": "1970-01-01", "client_countries": {"nationality": ["RU"]}},
        {"entity_id": "E1", "primary_name": "IVAN IVANOV", "_list_type": "WATCHLIST_PEP"},
        {"final_score": 92.0, "base_score": 88.0,
         "best_client_name": "IVAN IVANOV", "best_watchlist_name": "IVAN IVANOV",
         "adjustments": {"dob": {"score": 15.0, "description": ""},
                         "gender": {"score": 0.0, "description": ""},
                         "geography": {"score": -10.0, "description": ""}}},
        hits_count=40, hit_rank=3)


def _ctx_filtrage():
    return build_filtering_ctx(
        {"name": "IVAN IVANOV", "country": "RU", "bic": "", "roles": ["Bénéficiaire"],
         "is_agent": False, "address": "", "birth_date": ""},
        {"entity_id": "E1", "primary_name": "IVAN IVANOV", "_list_type": "WATCHLIST_OFAC"},
        {"final_score": 92.0, "base_score": 88.0, "adjustments": {},
         "best_client_name": "IVAN IVANOV", "best_watchlist_name": "IVAN IVANOV"},
        {"message_type": "pain.001", "msg_id": "M1"}, "TXN:M1:0")


# ------------------ LA PALETTE COUVRE LE CONTEXTE RÉEL ------------------

def test_la_detection_trouve_bien_les_trois_tables():
    assert len(_cles_palette()) >= 14
    assert set(_sous_cles()) >= {"party", "entity", "client"}
    assert len(_snippets()) >= 6


@pytest.mark.parametrize("fabrique", [_ctx_criblage, _ctx_filtrage])
def test_toute_cle_du_contexte_est_offerte_au_clic(fabrique):
    manquantes = set(fabrique()) - set(_cles_palette())
    assert not manquantes, (
        f"clés de ctx absentes de la palette — un auteur de règle ne peut pas "
        f"les découvrir : {sorted(manquantes)}")


def test_aucune_cle_offerte_n_est_absente_du_contexte():
    """L'inverse : une clé offerte qui n'existe pas produit une règle inerte."""
    connues = set(_ctx_criblage()) | set(_ctx_filtrage())
    fantomes = set(_cles_palette()) - connues
    assert not fantomes, f"clés offertes mais absentes du ctx : {sorted(fantomes)}"


def test_les_cles_de_volumetrie_et_de_rarete_sont_offertes():
    """Elles existent précisément pour être utilisées par des règles."""
    palette = set(_cles_palette())
    assert {"perimeter", "hits_count", "hit_rank", "corroboration", "rarity"} <= palette


# ------------------ LES SOUS-CLÉS EXISTENT VRAIMENT ------------------

def test_les_sous_cles_de_corroboration_et_de_rarete_existent():
    ctx = _ctx_criblage()
    for bloc in ("corroboration", "rarity", "adjustments"):
        declarees = set(_sous_cles().get(bloc, []))
        reelles = set(ctx[bloc])
        assert declarees <= reelles, (
            f"sous-clés de {bloc} inexistantes : {sorted(declarees - reelles)}")


def test_les_sous_cles_d_entite_sont_des_colonnes():
    """`entity` porte les colonnes de la base : une sous-clé qui n'en est pas
    une lit toujours `None`."""
    colonnes = {c.name for c in WatchlistEntity.__table__.columns} | {"_list_type"}
    declarees = set(_sous_cles().get("entity", []))
    assert declarees <= colonnes, (
        f"sous-clés d'entity qui ne sont pas des colonnes : "
        f"{sorted(declarees - colonnes)}")


def test_les_sous_cles_de_client_sont_des_colonnes():
    colonnes = {c.name for c in ClientEntity.__table__.columns}
    declarees = set(_sous_cles().get("client", []))
    assert declarees <= colonnes, (
        f"sous-clés de client qui ne sont pas des colonnes : "
        f"{sorted(declarees - colonnes)}")


def test_les_sous_cles_de_partie_existent():
    declarees = set(_sous_cles().get("party", []))
    assert declarees <= set(_ctx_filtrage()["party"])


# ------------------ LE SQUELETTE DE RÈGLE ÉNUMÈRE LE CONTEXTE ------------------

def test_le_squelette_de_regle_nomme_toutes_les_cles():
    """
    `RULE_TEMPLATE` est la première chose que lit un auteur de règle : sa
    docstring énumère les clés disponibles. Deux y manquaient — `perimeter` et
    `rarity` — donc les deux leviers destinés précisément aux règles.
    """
    from fiskr.fprules import RULE_TEMPLATE
    connues = set(_ctx_criblage()) | set(_ctx_filtrage())
    absentes = [c for c in connues if c not in RULE_TEMPLATE]
    assert not absentes, (
        f"clés du ctx absentes du squelette de règle : {sorted(absentes)}")


def test_le_squelette_compile():
    from fiskr.fprules import RULE_TEMPLATE, compile_rule
    assert compile_rule(RULE_TEMPLATE) is not None


# ------------------ LES MODÈLES LIVRÉS S'EXÉCUTENT ------------------

def _code(corps: str) -> str:
    """Un modèle est un CORPS de fonction : on l'enveloppe comme l'éditeur."""
    lignes = [l[1:] if l.startswith(" ") else l for l in corps.split("\n")]
    return "def rule(ctx):\n" + "\n".join(" " + l for l in lignes if l.strip())


@pytest.mark.parametrize("nom,corps", _snippets())
def test_chaque_modele_livre_rend_un_booleen(nom, corps):
    resultat, erreur = run_rule(_code(corps), _ctx_criblage())
    assert erreur is None, f"modèle « {nom} » en erreur : {erreur}"
    assert isinstance(resultat, bool)


def test_le_modele_d_ajustement_pays_se_declenche_vraiment():
    """Le défaut qui a motivé ce fichier : il lisait `country_penalty`, qui
    n'existe pas — la comparaison valait `0 <= -10`, toujours fausse."""
    modele = next(c for n, c in _snippets() if "ajustement pays" in n)
    ctx = _ctx_criblage()
    ctx["final_score"] = 80.0          # sous le seuil du modèle
    ctx["adjustments"]["geography"]["score"] = -10.0
    resultat, erreur = run_rule(_code(modele), ctx)
    assert erreur is None
    assert resultat is True, "le modèle ne se déclenche jamais"

    ctx["adjustments"]["geography"]["score"] = 10.0
    assert run_rule(_code(modele), ctx)[0] is False


def test_le_modele_de_rarete_respecte_le_perimetre_sanction():
    modele = next(c for n, c in _snippets() if "répandus" in n)
    ctx = _ctx_criblage()
    ctx["rarity"] = {"disponible": True, "sans_token_commun": False,
                     "nom_repandu": True}
    ctx["corroboration"]["corroborated"] = False
    assert run_rule(_code(modele), ctx)[0] is True
    ctx["hard_match"] = True
    assert run_rule(_code(modele), ctx)[0] is False


def test_le_modele_nom_seul_ne_touche_pas_le_perimetre_sanction():
    modele = next(c for n, c in _snippets() if "sans élément identifiant" in n)
    ctx = _ctx_criblage()
    ctx["corroboration"]["name_only"] = True
    ctx["hits_count"] = 40
    ctx["perimeter"] = "HORS_SANCTION"
    assert run_rule(_code(modele), ctx)[0] is True
    ctx["perimeter"] = "SANCTION"
    assert run_rule(_code(modele), ctx)[0] is False

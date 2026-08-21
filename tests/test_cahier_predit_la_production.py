"""
Cahier de tests : il doit prédire la production, pas autre chose.

Depuis que le criblage conserve **toutes** les correspondances au-dessus du
seuil, deux écarts s'étaient ouverts entre le cahier de tests et ce que la
production fait réellement :

1. **Le volume annoncé.** Le cahier comptait des paires `(client, listé)` — une
   par client, la meilleure correspondance. C'est la bonne mesure pour un
   *taux d'interception*, mais ce n'est pas le nombre d'alertes que la
   production ouvrira : elle en ouvre une **par correspondance**. Un client
   homonyme d'un nom courant en porte des centaines. Le rapport donne
   désormais les deux chiffres, distinctement nommés.

2. **Le comportement des règles**, plus grave. `build_screening_ctx` recevait
   ses valeurs par défaut dans le cahier, donc `hits_count` y valait toujours
   **1**. Une règle volumétrique — « au-delà de N correspondances… » — ne se
   déclenchait donc jamais pendant le cahier, alors qu'elle se déclenche en
   production. Le cahier annonçait un écart de taux d'interception calculé
   avec des règles qui ne s'appliquaient pas. Il reçoit maintenant la même
   volumétrie que la production.
"""
import inspect

import pytest

from fiskr import screenpool


def _fiche(n, nom="MOHAMMED ALI", list_type="WATCHLIST_PEP"):
    return {"entity_id": f"CP-{n}", "entity_type": "I", "primary_name": nom,
            "aliases": {"high_priority": [], "low_priority": []},
            "dates_of_birth": [], "gender": "U",
            "countries": {"citizenship": [], "residence": [],
                          "birth_country": [], "jurisdiction_country": []},
            "_list_type": list_type}


class _IndexUnique(dict):
    def __init__(self, fiches):
        super().__init__()
        self._fiches = fiches

    def get(self, cle, defaut=None):
        return self._fiches


CLIENT = {"client_id": "C-1", "client_type": "PP",
          "client_first_name": "MOHAMMED", "client_last_name": "ALI",
          "client_gender": "U"}


def _cfg():
    from fiskr.config import config
    return dict(config)


def test_le_cahier_compte_les_correspondances_et_pas_seulement_les_clients():
    """Douze homonymes = douze alertes en production, un client intercepté."""
    index = _IndexUnique([_fiche(i) for i in range(12)])
    resultat = screenpool.screen_one(CLIENT, index, _cfg(), set(), [])
    assert resultat is not None
    categorie, detail = resultat
    assert categorie == "alert"
    assert detail["hits"] == 12, "le compte des correspondances est faux"

    agg = screenpool.new_partial()
    screenpool.apply_outcome(agg, resultat)
    fusion = screenpool.merge_partials([agg])
    assert fusion["alerts"] == 1, "un seul client intercepté"
    assert fusion["hits"] == 12, "douze alertes seront ouvertes"


def test_les_deux_chiffres_repondent_a_deux_questions():
    """Le taux d'interception porte sur les clients ; le volume de travail sur
    les correspondances. Les confondre fait sous-estimer la charge d'un facteur
    égal au nombre d'homonymes."""
    index = _IndexUnique([_fiche(i) for i in range(30)])
    agg = screenpool.new_partial()
    for numero in range(3):
        client = dict(CLIENT, client_id=f"C-{numero}")
        screenpool.apply_outcome(
            agg, screenpool.screen_one(client, index, _cfg(), set(), []))
    fusion = screenpool.merge_partials([agg])
    assert fusion["alerts"] == 3
    assert fusion["hits"] == 90


def test_une_regle_volumetrique_se_declenche_dans_le_cahier(monkeypatch):
    """Le cœur du sujet : sans la volumétrie dans le contexte, cette règle ne
    se déclenchait jamais pendant le cahier — et se déclenchait en production.
    Le cahier annonçait donc un écart calculé avec des règles inertes."""

    class _Regle:
        id, name, version = 1, "volumetrie", 1
        code = ("def rule(ctx):\n"
                "    return ctx['hits_count'] >= 10\n")
        perimeters = None

    index = _IndexUnique([_fiche(i) for i in range(12)])
    categorie, detail = screenpool.screen_one(
        CLIENT, index, _cfg(), set(), [_Regle()])
    assert categorie == "rule", "la règle volumétrique n'a pas vu la volumétrie"
    assert detail["rule_name"] == "volumetrie"

    # ... et sur un petit périmètre, elle ne se déclenche pas
    petit = _IndexUnique([_fiche(i) for i in range(3)])
    categorie, _ = screenpool.screen_one(CLIENT, petit, _cfg(), set(), [_Regle()])
    assert categorie == "alert"


def test_le_rang_de_la_meilleure_correspondance_vaut_un():
    """Une règle qui garde « les N premières » doit voir la meilleure au
    rang 1, comme en production."""

    class _Regle:
        id, name, version = 2, "rang", 1
        code = "def rule(ctx):\n    return ctx['hit_rank'] != 1\n"
        perimeters = None

    index = _IndexUnique([_fiche(i) for i in range(12)])
    categorie, _ = screenpool.screen_one(CLIENT, index, _cfg(), set(), [_Regle()])
    assert categorie == "alert", "la meilleure correspondance doit être de rang 1"


def test_un_client_en_liste_blanche_compte_quand_meme_ses_correspondances():
    """Sinon le volume annoncé baisserait dès qu'une paire est blanchie, alors
    que les autres correspondances du même client existent toujours."""
    index = _IndexUnique([_fiche(i) for i in range(12)])
    resultat = screenpool.screen_one(CLIENT, index, _cfg(),
                                     {("C-1", "CP-0")}, [])
    categorie, detail = resultat
    assert categorie == "whitelisted"
    agg = screenpool.new_partial()
    screenpool.apply_outcome(agg, resultat)
    # Les compteurs publiés dérivent des sorts retenus (un par client) :
    # c'est `finalize` qui les rend, jamais l'accumulateur brut.
    compte = screenpool.finalize(agg)
    assert compte["hits"] == 12
    assert compte["whitelisted_suppressed"] == 1
    assert compte["alerts"] == 0, "un client blanchi n'est pas intercepté"


def test_le_contexte_de_regle_du_cahier_est_celui_de_la_production():
    """Garde-fou : si `screen_one` cesse de transmettre la volumétrie, les
    règles redeviennent inertes dans le cahier sans que rien n'échoue."""
    code = inspect.getsource(screenpool.screen_one)
    assert "hits_count=hits" in code
    assert "hit_rank=1" in code


def test_le_rapport_porte_les_deux_chiffres():
    from fiskr import backtest
    code = inspect.getsource(backtest.run_backtest)
    assert '"hits": current.get("hits", 0)' in code
    assert '"hits": candidate.get("hits", 0)' in code
    assert '"interception_rate_pct": _rate(current["alerts"])' in code

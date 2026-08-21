"""
Index de blocage du canal filtrage : construit une fois, pas à chaque message.

`screen_payment_message` reconstruisait l'index à CHAQUE message : aplatir
l'index de criblage, dédupliquer par `entity_id`, puis régénérer les clés de
blocage de tout l'univers. Mesuré sur un corpus aux proportions de la
production (832 470 fiches) : **17,0 s** de génération de clés et **1,7 s**
d'aplatissement — dix-neuf secondes payées dans la requête HTTP, sur le canal
dont l'exigence première est le temps de réponse.

L'index est désormais mémorisé par processus. Ces tests fixent les deux
propriétés qui rendent une mémorisation acceptable sur un chemin de
conformité :

  1. elle est RÉUTILISÉE quand rien n'a changé ;
  2. elle est JETÉE dès que quoi que ce soit change les clés — empreinte des
     listes, layout du canal, capacités du moteur, équivalences.

Un index périmé ferait manquer des fiches : c'est un faux négatif silencieux,
la seule erreur que ce produit ne peut pas se permettre.
"""
import pytest

from fiskr import capabilities as caps
from fiskr import transactions
from fiskr.settings import blocking_config_for


@pytest.fixture(autouse=True)
def index_au_repos():
    transactions.invalider_index_de_filtrage()
    yield
    transactions.invalider_index_de_filtrage()


def _univers(nb=40):
    fiches = [{
        "entity_id": f"E{i:04d}",
        "primary_name": f"IVAN IVANOV{'X' * (i % 7)}",
        "entity_type": "I",
        "country": "RU",
        "_list_type": "WATCHLIST_OFAC" if i % 2 else "WATCHLIST_PEP",
        "aliases": {"high_priority": [], "low_priority": []},
    } for i in range(nb)]
    # Une fiche apparait sous PLUSIEURS cles de l'index de criblage : la
    # deduplication par entity_id doit la ramener a une seule.
    return {"K1": fiches[: nb // 2], "K2": fiches, "K3": fiches[nb // 2:]}


def _cfg(layout=("PHONETIC_FIRST",)):
    return blocking_config_for(list(layout), channel="FILTERING")


def _compte_constructions(monkeypatch):
    appels = []
    vrai = transactions._filtering_index

    def espion(entities, filtering_cfg, allowed_lists=None):
        appels.append(len(entities))
        return vrai(entities, filtering_cfg, allowed_lists)

    monkeypatch.setattr(transactions, "_filtering_index", espion)
    return appels


# ------------------ RÉUTILISATION ------------------

def test_l_index_n_est_construit_qu_une_fois(monkeypatch):
    appels = _compte_constructions(monkeypatch)
    univers, cfg = _univers(), _cfg()
    for _ in range(5):
        transactions.index_de_filtrage(univers, cfg, "hash-A")
    assert len(appels) == 1, f"{len(appels)} constructions au lieu d'une"


def test_les_fiches_sont_dedupliquees_avant_l_index(monkeypatch):
    appels = _compte_constructions(monkeypatch)
    transactions.index_de_filtrage(_univers(40), _cfg(), "hash-A")
    # 40 fiches distinctes, présentes 80 fois au total dans l'index de criblage
    assert appels == [40]


def test_l_index_rendu_est_le_meme_objet(monkeypatch):
    univers, cfg = _univers(), _cfg()
    premier = transactions.index_de_filtrage(univers, cfg, "hash-A")
    assert transactions.index_de_filtrage(univers, cfg, "hash-A") is premier


# ------------------ INVALIDATION ------------------

def test_un_changement_de_listes_jette_l_index(monkeypatch):
    appels = _compte_constructions(monkeypatch)
    univers, cfg = _univers(), _cfg()
    transactions.index_de_filtrage(univers, cfg, "hash-A")
    transactions.index_de_filtrage(univers, cfg, "hash-B")
    assert len(appels) == 2


def test_un_changement_de_layout_jette_l_index(monkeypatch):
    appels = _compte_constructions(monkeypatch)
    univers = _univers()
    transactions.index_de_filtrage(univers, _cfg(("PHONETIC_FIRST",)), "hash-A")
    transactions.index_de_filtrage(
        univers, _cfg(("COUNTRY_ISO", "PHONETIC_FIRST")), "hash-A")
    assert len(appels) == 2


def test_un_changement_de_capacites_jette_l_index(monkeypatch):
    """Les capacités décident des clés : couper la phonétique ou la
    translittération change l'index de fond en comble."""
    appels = _compte_constructions(monkeypatch)
    univers, cfg = _univers(), _cfg()
    transactions.index_de_filtrage(univers, cfg, "hash-A")
    with caps.use_context("FILTERING", []):
        transactions.index_de_filtrage(univers, cfg, "hash-A")
    assert len(appels) == 2


def test_l_invalidation_explicite_force_la_reconstruction(monkeypatch):
    appels = _compte_constructions(monkeypatch)
    univers, cfg = _univers(), _cfg()
    transactions.index_de_filtrage(univers, cfg, "hash-A")
    transactions.invalider_index_de_filtrage()
    transactions.index_de_filtrage(univers, cfg, "hash-A")
    assert len(appels) == 2


# ------------------ RESTRICTION DE LISTES ------------------

def test_la_restriction_ne_reconstruit_plus_l_index(monkeypatch):
    """Restreindre les listes ne doit pas coûter dix-neuf secondes : la
    restriction s'applique à la sélection des candidats."""
    appels = _compte_constructions(monkeypatch)
    univers, cfg = _univers(), _cfg()
    index = transactions.index_de_filtrage(univers, cfg, "hash-A")
    partie = {"name": "IVAN IVANOV", "country": "RU", "bic": "",
              "birth_date": "", "birth_country": ""}
    transactions._party_candidates(partie, index, cfg, ["WATCHLIST_OFAC"])
    transactions._party_candidates(partie, index, cfg, None)
    assert len(appels) == 1


def test_la_restriction_ecarte_exactement_les_memes_fiches():
    """
    Filtrer à la sélection doit donner le MÊME ensemble que filtrer à la
    construction — sinon la restriction ne veut plus dire la même chose.
    """
    univers, cfg = _univers(), _cfg()
    fiches = list({e["entity_id"]: e for seau in univers.values()
                   for e in seau}.values())
    partie = {"name": "IVAN IVANOV", "country": "RU", "bic": "",
              "birth_date": "", "birth_country": ""}

    a_la_construction = transactions._party_candidates(
        partie, transactions._filtering_index(fiches, cfg, ["WATCHLIST_OFAC"]), cfg)
    a_la_selection = transactions._party_candidates(
        partie, transactions.index_de_filtrage(univers, cfg, "hash-A"), cfg,
        ["WATCHLIST_OFAC"])

    assert set(a_la_construction) == set(a_la_selection)
    assert a_la_selection, "la sélection ne doit pas être vide sur ce jeu"
    assert all(e["_list_type"] == "WATCHLIST_OFAC" for e in a_la_selection.values())


def test_sans_restriction_toutes_les_listes_sont_candidates():
    univers, cfg = _univers(), _cfg()
    partie = {"name": "IVAN IVANOV", "country": "RU", "bic": "",
              "birth_date": "", "birth_country": ""}
    candidats = transactions._party_candidates(
        partie, transactions.index_de_filtrage(univers, cfg, "hash-A"), cfg, None)
    assert {e["_list_type"] for e in candidats.values()} == {
        "WATCHLIST_OFAC", "WATCHLIST_PEP"}

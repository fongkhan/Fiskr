"""
Préchargement du cache moteur au démarrage d'un processus web.

Sous Passenger, `a2wsgi.ASGIMiddleware` ne construit qu'un scope `http` par
requête : le `lifespan` de FastAPI ne s'exécute jamais, donc rien ne chargeait
le cache moteur. Mesuré en production : le PREMIER criblage payait le
chargement complet en pleine requête (64 s, puis 5,6 s à chaud) et la palette
Ctrl+K se repliait sur la base (30,9 s).

`fiskr/wsgi.py` précharge donc au démarrage, dans un thread de fond. Ces tests
verrouillent les trois propriétés qui rendent ce préchargement sûr :

- il ne bloque PAS le démarrage (Passenger tue un processus qui dépasse
  `passenger_start_timeout` — le site tomberait, bien pire que la lenteur) ;
- il n'échoue jamais bruyamment : une base indisponible ne doit pas empêcher
  l'application de démarrer ;
- il ne se dédouble pas : un criblage arrivé pendant la chauffe attend CE
  chargement au lieu d'en lancer un second en parallèle (deux fois le temps,
  deux fois la mémoire, pour un résultat identique).
"""
import ast
import threading
from pathlib import Path

import pytest

import fiskr.api as api
from fiskr.database import get_db


@pytest.fixture()
def cache_vide(monkeypatch):
    """Un processus web tel que Passenger le démarre : aucun cache."""
    monkeypatch.setattr(api, "watchlist_store", [])
    monkeypatch.setattr(api, "watchlist_index", {})
    monkeypatch.setattr(api, "watchlist_search_index", [])
    monkeypatch.setattr(api, "_last_epoch_seen", None)


def test_warm_loads_the_cache(cache_vide):
    """Le préchargement fait le travail : après lui, le cache est chaud."""
    appels = []
    vrai_load = api.load_watchlist_cache
    monkey = lambda db: (appels.append(1), vrai_load(db))[1]
    api.load_watchlist_cache = monkey
    try:
        assert api.warm_watchlist_cache() is not None
    finally:
        api.load_watchlist_cache = vrai_load
    assert appels, "le référentiel n'a jamais été lu"


def test_warm_never_raises_when_the_database_is_down(cache_vide, monkeypatch):
    """Une base indisponible ne doit PAS empêcher le processus de démarrer :
    le préchargement est un confort, pas une condition de service."""
    def _base_morte(_db=None):
        raise RuntimeError("connexion refusée")
    monkeypatch.setattr(api, "load_watchlist_cache", _base_morte)
    assert api.warm_watchlist_cache() is False


def test_warm_is_skipped_when_the_cache_is_already_hot(monkeypatch):
    """Si un criblage a devancé le thread, on ne relit pas le référentiel."""
    monkeypatch.setattr(api, "watchlist_index", {"CLE": [{"entity_id": "X"}]})
    appels = []
    monkeypatch.setattr(api, "load_watchlist_cache",
                        lambda db: appels.append(1))
    assert api.warm_watchlist_cache() is True
    assert not appels, "le cache déjà chaud a été rechargé pour rien"


def test_concurrent_screening_waits_instead_of_loading_twice(cache_vide, monkeypatch):
    """LE point de la manœuvre : le préchargement et un criblage simultané ne
    doivent donner qu'UN SEUL chargement. Deux lectures parallèles du
    référentiel, c'est deux fois la mémoire pour un résultat identique."""
    chargements = []
    demarre = threading.Event()

    def _load_lent(db):
        chargements.append(1)
        demarre.set()
        threading.Event().wait(0.3)       # chargement volontairement lent
        api.watchlist_index = {"CLE": [{"entity_id": "X"}]}

    monkeypatch.setattr(api, "load_watchlist_cache", _load_lent)
    monkeypatch.setattr(api, "watchlist_epoch", lambda db: 1)

    chauffe = threading.Thread(target=api.warm_watchlist_cache)
    chauffe.start()
    demarre.wait(timeout=2)               # le préchargement tient le verrou
    db = next(get_db())
    try:
        api._ensure_watchlist_cache(db)   # doit ATTENDRE, pas doubler
    finally:
        db.close()
    chauffe.join(timeout=5)

    assert len(chargements) == 1, (
        f"{len(chargements)} chargements concurrents du référentiel")


def test_fork_gets_a_fresh_lock():
    """Un fork ne copie pas les threads : un verrou tenu au moment du fork
    resterait tenu pour toujours dans l'enfant, qui se figerait au premier
    criblage. Le pool de criblage forke — la garde doit exister."""
    import os
    assert hasattr(os, "register_at_fork")
    source = Path("fiskr/api.py").read_text(encoding="utf-8")
    arbre = ast.parse(source)
    attributs = {n.func.attr for n in ast.walk(arbre)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "register_at_fork" in attributs, (
        "aucune remise à neuf du verrou après fork")


# ------------------ L'AMORCE WSGI ------------------

def _wsgi_arbre():
    return ast.parse(Path("fiskr/wsgi.py").read_text(encoding="utf-8"))


def test_preload_never_blocks_startup():
    """Bloquer le démarrage pour charger le cache ferait dépasser
    `passenger_start_timeout` (90 s par défaut) et mettrait le site à terre.
    Le préchargement DOIT partir dans un thread, en démon."""
    arbre = _wsgi_arbre()
    threads = [n for n in ast.walk(arbre)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "Thread"]
    assert threads, "le préchargement doit partir dans un thread"
    daemon = [kw for t in threads for kw in t.keywords
              if kw.arg == "daemon" and getattr(kw.value, "value", None) is True]
    assert daemon, "le thread doit être un démon (ne jamais retenir l'arrêt)"

    # …et surtout : le chargement ne doit pas être appelé AU NIVEAU DU MODULE.
    # Dans le corps d'une fonction (la cible du thread) c'est justement le but ;
    # à l'import, ce serait un démarrage bloquant. On descend donc l'arbre sans
    # entrer dans les définitions de fonctions.
    def _appels_a_l_import(noeud):
        for enfant in ast.iter_child_nodes(noeud):
            if isinstance(enfant, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.Lambda)):
                continue                       # differé : pas à l'import
            if isinstance(enfant, ast.Call) and isinstance(enfant.func, ast.Name):
                yield enfant.func.id
            yield from _appels_a_l_import(enfant)

    a_l_import = set(_appels_a_l_import(arbre))
    assert "warm_watchlist_cache" not in a_l_import, (
        "appelé à l'import : le démarrage serait bloqué")
    assert "load_watchlist_cache" not in a_l_import


def test_preload_is_opt_in_and_off_by_default():
    """Le préchargement est DÉSACTIVÉ par défaut, et c'est une conclusion de
    mesure, pas une préférence : sans `passenger_min_instances`, Passenger
    recycle les processus dès qu'ils sont inactifs, le thread de chauffe se
    dispute le CPU avec la requête et le premier criblage passait de 64 s à
    118 s. Avec `passenger_min_instances 1`, le processus survit et le cache
    chargé une fois suffit — le préchargement n'épargne plus que le tout
    premier criblage, pour 60 s de CPU et l'empreinte du référentiel dans
    chaque processus qui naît."""
    arbre = _wsgi_arbre()
    defauts = [n.args[1].value for n in ast.walk(arbre)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
               and n.func.attr == "get" and len(n.args) == 2
               and isinstance(n.args[0], ast.Constant)
               and n.args[0].value == "FISKR_PRELOAD_CACHE"
               and isinstance(n.args[1], ast.Constant)]
    assert defauts, "le défaut de FISKR_PRELOAD_CACHE doit être explicite"
    assert defauts[0].strip().lower() in ("0", "false", "no", "off"), (
        f"préchargement actif par défaut ({defauts[0]!r}) : sur un mutualisé "
        f"il coûte plus qu'il ne rapporte")


def test_preload_can_be_enabled_without_redeploying():
    """…mais reste activable sans redéploiement, pour un hébergement dédié où
    le démarrage n'est pas contraint et la mémoire pas partagée."""
    source = Path("fiskr/wsgi.py").read_text(encoding="utf-8")
    assert "FISKR_PRELOAD_CACHE" in source
    assert "FISKR_PRELOAD_CACHE=1" in source, "la façon de l'activer doit être écrite"

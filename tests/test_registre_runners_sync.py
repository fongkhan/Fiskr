"""
Registre des exécuteurs de synchronisation : une seule source de vérité.

`api.py` portait encore `_run_scheduled_syncs()`, l'ancien planificateur
quotidien remplacé par `_cron_sync_tick` (planification cron par source). Plus
personne ne l'appelait — aucune référence dans le code, les gabarits ou le
frontal — mais elle restait un piège actif :

* elle énumérait **15 sources à la main**, alors que le registre
  `_SYNC_RUNNERS` en compte 42 : les 27 autres n'y étaient pas, et toute source
  ajoutée au registre l'aurait ignorée en silence ;
* elle rechargeait le cache avec `load_watchlist_cache(db)`, forme purement
  locale au processus, corrigée partout ailleurs en `_refresh_production_cache`
  (seul canal d'invalidation entre processus). Réanimée telle quelle, elle
  aurait remis en production une liste que les autres processus n'auraient
  jamais vue.

Ce test verrouille sa disparition et l'invariant qu'elle enfreignait : le
registre couvre TOUTES les sources configurables, sans liste écrite à la main.
"""
import fiskr.api as api_module
from fiskr.sync import get_sync_config


def test_l_ancien_planificateur_quotidien_a_disparu():
    assert not hasattr(api_module, "_run_scheduled_syncs")


def test_le_registre_couvre_toutes_les_sources_configurables():
    """Une source déclarée dans la configuration mais absente du registre ne
    serait jamais synchronisée par le planificateur, sans la moindre erreur."""
    cfg = get_sync_config()
    sources = {k for k, v in cfg.items() if isinstance(v, dict) and "enabled" in v}
    assert sources, "configuration de synchronisation vide"
    manquantes = sources - set(api_module._SYNC_RUNNERS)
    assert not manquantes, f"sources sans exécuteur : {sorted(manquantes)}"
    orphelines = set(api_module._SYNC_RUNNERS) - sources
    assert not orphelines, f"exécuteurs sans configuration : {sorted(orphelines)}"


def test_chaque_executeur_a_un_alias_d_api():
    """`POST /api/sync/run` n'accepte que les alias : une source sans alias est
    injoignable manuellement, même si le planificateur la traite."""
    cles_alias = {run_key for (run_key, _engine) in api_module._SYNC_SOURCE_ALIASES.values()}
    manquantes = set(api_module._SYNC_RUNNERS) - cles_alias
    assert not manquantes, f"sources sans alias d'API : {sorted(manquantes)}"


def test_le_catalogue_du_frontal_couvre_toutes_les_sources():
    """
    L'écran de synchronisation affiche `SOURCE_CATALOG` (app.js) : une source
    ajoutée au registre serveur mais absente du catalogue est **invisible** —
    aucun bouton, aucune planification lisible, alors qu'elle est bel et bien
    configurable et synchronisable par l'API.

    Le sens inverse est pire encore : un bouton du catalogue sans source
    serveur rend un 400 au clic.
    """
    import re
    from pathlib import Path

    app_js = (Path(__file__).resolve().parent.parent / "fiskr" / "static"
              / "app.js").read_text(encoding="utf-8")
    bloc = re.search(r"const SOURCE_CATALOG = \[(.*?)\n\];", app_js, re.S)
    assert bloc, "SOURCE_CATALOG introuvable dans app.js"
    front = {a.upper() for a in re.findall(r'alias:\s*"([^"]+)"', bloc.group(1))}
    assert len(front) >= 40, "détection du catalogue cassée"

    serveur = set(api_module._SYNC_SOURCE_ALIASES)
    # Les synonymes (EU_FSF/FSF, ONU) pointent une cible déjà couverte : on
    # compare donc les CIBLES, pas les orthographes.
    cibles_serveur = {c for c in api_module._SYNC_SOURCE_ALIASES.values()}
    cibles_front = {api_module._SYNC_SOURCE_ALIASES[a] for a in front & serveur}

    fantomes = front - serveur
    assert not fantomes, (
        f"boutons du catalogue sans source serveur — 400 au clic : {sorted(fantomes)}")
    invisibles = cibles_serveur - cibles_front
    assert not invisibles, (
        f"sources synchronisables absentes de l'écran : {sorted(invisibles)}")

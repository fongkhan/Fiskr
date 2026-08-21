"""
Les planificateurs périodiques ne tournent pas en mode inline.

Chaque `TestClient(app)` entre dans le `lifespan` de l'application. En mode
`eager` — le mode des tests — six boucles y étaient démarrées : planificateur
de sources, purge de rétention, récapitulatif de notifications, digest KPI,
fouille d'homonymes, scrutation de l'inbox CFT. Toutes se réveillent **à la
minute pleine suivante**, et toutes travaillent sur la **même base** que les
tests.

Sur une suite de quatre minutes, cela faisait environ quatre tics qui
tombaient sur le test qui passait par là. D'où un échec par passe, sur un test
**différent à chaque fois**, avec des lignes disparues sous les pieds du test
en cours (`ObjectDeletedError`) — une rétention qui purge, un planificateur qui
soumet une synchronisation réelle.

`requeue_stale` est exclu pour la même raison : il bascule en ERROR les jobs
QUEUED orphelins, ce qui n'a aucun sens quand rien n'est jamais mis en file —
mais suffit à casser un test qui pose une ligne QUEUED à la main.

Ce qui est retiré est la **boucle**, pas la logique : les tics restent des
fonctions synchrones testées une par une.
"""
import asyncio
import inspect

import pytest
from fastapi.testclient import TestClient

from fiskr import api as api_module
from fiskr import jobs as job_queue
from fiskr.api import app


def test_le_mode_des_tests_est_bien_inline():
    """La prémisse : si le mode changeait, ce fichier ne testerait rien."""
    assert job_queue.jobs_mode() == "eager"


def test_aucune_tache_de_fond_n_est_creee(monkeypatch):
    """Le lifespan ne doit créer AUCUNE tâche asyncio en mode inline."""
    creees = []
    vrai = asyncio.create_task

    def espion(coro, *a, **k):
        creees.append(getattr(coro, "__qualname__", repr(coro)))
        coro.close()          # on ne la lance pas : on la compte
        return vrai(asyncio.sleep(0), *a, **k)

    monkeypatch.setattr(api_module.asyncio, "create_task", espion)
    with TestClient(app):
        pass
    assert not creees, f"tâches de fond démarrées en mode inline : {creees}"


def test_la_file_n_est_pas_reprise(monkeypatch):
    """`requeue_stale` bascule en ERROR les jobs QUEUED orphelins : appelé à
    chaque TestClient, il casse tout test qui pose une ligne QUEUED."""
    appels = []
    monkeypatch.setattr(job_queue, "requeue_stale",
                        lambda *a, **k: appels.append(a) or 0)
    with TestClient(app):
        pass
    assert not appels, "la file a été reprise en mode inline"


def test_les_tics_restent_appelables_un_par_un():
    """Ce qui est retiré est la boucle, pas la logique : chaque planificateur
    garde son tic synchrone, testable sans horloge."""
    for nom in ("_cron_sync_tick", "_retention_tick", "_digest_tick",
                "_notification_batch_tick", "_mining_tick"):
        tic = getattr(api_module, nom, None)
        assert callable(tic), f"{nom} a disparu : les tests de tic n'ont plus de prise"
        assert not inspect.iscoroutinefunction(tic), (
            f"{nom} est devenu asynchrone : il n'est plus appelable hors boucle")


def test_les_boucles_existent_toujours_pour_les_autres_modes():
    """Le mode `thread` (déploiement sans démon) doit garder ses boucles."""
    for nom in ("_cron_sync_scheduler", "_retention_scheduler", "_digest_scheduler",
                "_notification_batch_scheduler", "_resource_mining_scheduler",
                "_inbox_poller"):
        boucle = getattr(api_module, nom, None)
        assert inspect.iscoroutinefunction(boucle), f"{nom} manquant"

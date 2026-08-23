"""
Une synchronisation doit dire où elle en est, depuis un autre processus.

Constaté en production. Deux synchronisations affichaient « Analyse du
fichier… », barre indéterminée, compteur à zéro, pendant des heures. Elles
n'étaient pas bloquées : PEP a fini normalement, **707 951 fiches en treize
minutes et demie**. Elle était invisible.

Le mécanisme. `SyncProgress` publiait ses phases dans `fiskr.progress`, un
registre **mémoire, propre au processus qui l'écrit**. Une synchronisation
tourne dans le démon travailleur ; l'écran interroge un processus API qui ne
verra jamais ce registre (`/api/diagnostic/jobs` le confirmait :
`progress_active: []`). La ligne de la file, elle, traverse les processus — et
elle gardait la phase posée à la prise en charge, `PARSE`, du début à la fin.

Le pont existait déjà (`jobs.mirror_progress`), et sa docstring décrit
exactement ce cas : « le registre ne traverse pas les processus, la ligne jobs
si ». La synchronisation ne l'empruntait pas.

Ce que ça coûtait : l'exploitant n'avait aucun moyen de distinguer une source
qui avance d'une source qui ne répond plus. Sur la même capture, la source DFAT
— dont l'hôte accepte la connexion puis n'envoie jamais un octet — affichait
rigoureusement la même chose.
"""
import time

import pytest

from fiskr import jobs, sync
from fiskr.database import Job


@pytest.fixture
def journal(monkeypatch):
    """Capture ce que la synchronisation reporte sur la ligne de la file."""
    reflets = []

    def _faux_mirror(token, *, phase, processed=0, total=None, snapshot_id=None):
        reflets.append({"token": token, "phase": phase, "processed": processed,
                        "total": total, "snapshot_id": snapshot_id})

    monkeypatch.setattr(jobs, "mirror_progress", _faux_mirror)
    return reflets


def test_la_premiere_phase_part_des_la_creation(journal):
    """
    La phase posée à la prise en charge est `PARSE` — « Analyse du fichier… »
    à l'écran. Une synchronisation qui vient de démarrer télécharge ; elle doit
    le dire tout de suite, pas au premier tick utile.
    """
    sync.SyncProgress("PEP")
    assert journal, "aucun reflet à la création"
    assert journal[0]["token"] == "sync:pep"
    assert journal[0]["phase"] == "DOWNLOAD"


def test_chaque_changement_de_phase_traverse_les_processus(journal):
    """Le signal utile est le changement de phase, et il est rare : il ne doit
    jamais être avalé par une limitation de cadence."""
    tracker = sync.SyncProgress("PEP")
    for phase in ("HASH", "PARSE", "PERSIST", "DELTA", "RELOAD"):
        tracker.phase(phase, processed=1)
    vues = [r["phase"] for r in journal]
    for phase in ("DOWNLOAD", "HASH", "PARSE", "PERSIST", "DELTA", "RELOAD"):
        assert phase in vues, (phase, vues)


def test_une_phase_inchangee_ne_martele_pas_la_base(journal):
    """
    `mirror_progress` ouvre une session par appel. Un téléchargement publie un
    tick par mégaoctet et une persistance un tick par millier de fiches : sans
    borne, une seule synchronisation PEP ferait des centaines d'écritures pour
    répéter la même phase.
    """
    tracker = sync.SyncProgress("PEP")
    depart = len(journal)
    for i in range(200):
        tracker.phase("PERSIST", processed=i * 1000)
    assert len(journal) - depart <= 2, (
        f"{len(journal) - depart} écritures pour une phase inchangée")


def test_le_compteur_de_la_phase_en_cours_est_publie(journal):
    """« 593 000 fiches traitées » vaut mieux qu'une barre indéterminée : c'est
    ce qui distingue une source qui avance d'une source qui ne répond plus."""
    tracker = sync.SyncProgress("PEP")
    tracker.phase("PERSIST", processed=593000, total=707951, snapshot_id="snap-1")
    persist = [r for r in journal if r["phase"] == "PERSIST"]
    assert persist and persist[-1]["processed"] == 593000
    assert persist[-1]["total"] == 707951
    assert persist[-1]["snapshot_id"] == "snap-1"


def test_un_reflet_en_panne_n_interrompt_jamais_une_synchronisation(monkeypatch):
    """La progression est un confort ; une source officielle qui se met à jour
    ne l'est pas. Une base indisponible ne doit pas faire échouer la sync."""
    def _casse(*a, **k):
        raise RuntimeError("base injoignable")

    monkeypatch.setattr(jobs, "mirror_progress", _casse)
    tracker = sync.SyncProgress("PEP")      # ne doit pas lever
    tracker.phase("PERSIST", processed=1)   # ne doit pas lever non plus


def test_le_pont_inter_processus_est_bien_emprunte():
    """
    Garde de source. Publier au seul registre mémoire est le défaut corrigé
    ici, et il est indétectable à l'exécution : tout fonctionne, simplement
    personne ne voit rien.
    """
    import inspect

    code = inspect.getsource(sync.SyncProgress)
    assert "mirror_progress" in code, (
        "SyncProgress doit refléter ses phases sur la ligne de la file : le "
        "registre mémoire ne traverse pas les processus")


def test_la_ligne_de_file_porte_bien_une_colonne_de_phase():
    """Garde-fou : si la colonne disparaissait, le reflet n'aurait plus de
    destination et le test ci-dessus passerait sur du vide."""
    colonnes = {c.name for c in Job.__table__.columns}
    assert {"phase", "processed", "total"} <= colonnes

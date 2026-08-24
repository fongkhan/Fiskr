"""
Demon travailleur : execute la file de travaux persistee (fiskr.jobs) dans un
processus SEPARE de l'API. C'est lui qui porte tout le calcul lourd — cahier
de tests, synchronisations, re-criblages, simulations — pour que le front
reste rapide quoi qu'il arrive.

Lancement : `python -m fiskr.worker`. En production mutualisee (Passenger,
pas de systemd), l'API le demarre elle-meme (autostart, fiskr/api.py) quand
son battement de coeur manque ; le VERROU fichier (flock) garantit qu'il n'y
en a jamais deux, quelle que soit la course entre processus API.

Il heberge aussi les planificateurs periodiques (cron des sources, inbox CFT,
digest, retention, notifications, fouille) : sous Passenger, N processus API
signifiaient N planificateurs — N digests, N synchronisations. Un seul demon,
un seul tic.

Arret brutal (recyclage, kill) : les jobs RUNNING gardent leur ligne ; au
redemarrage suivant, `requeue_stale` les remet en file (relance de zero,
plafonnee par attempts) — c'est la reprise automatique demandee.
"""
import fcntl
import json
import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from fiskr import jobs
from fiskr.config import PROJECT_ROOT, config

logger = logging.getLogger("fiskr.worker")

LOCK_FILE = PROJECT_ROOT / "fiskr-worker.lock"
HEARTBEAT_SETTING = "jobs.worker"
HEARTBEAT_EVERY_S = 15.0
POLL_EVERY_S = 2.0

_stop = threading.Event()


def acquire_worker_lock():
    """
    Verrou d'unicite : flock est detenu par le processus et rendu par le
    noyau a sa mort, meme brutale — pas de fichier de PID a nettoyer, pas de
    verrou fantome. Retourne le descripteur (a garder vivant), ou None si un
    autre demon vit deja.
    """
    fh = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    return fh


def _write_heartbeat(session) -> None:
    from fiskr import buildinfo
    from fiskr.settings import set_setting
    # L'empreinte de code figee a l'import dit quelle version CE demon
    # execute : le diagnostic la compare au disque pour detecter un demon
    # reste sur l'ancien code apres un deploiement.
    set_setting(session, HEARTBEAT_SETTING, {
        "pid": os.getpid(), "host": socket.gethostname(),
        "at": datetime.utcnow().isoformat() + "Z",
        "version": buildinfo.LOADED_FINGERPRINT,
        "started_at": buildinfo.PROCESS_STARTED_AT,
        "python": buildinfo.PYTHON_VERSION,
    })


def reparer_instantanes_bloques(session) -> dict:
    """
    Remet d'aplomb les imports restes en PROCESSING.

    La reparation existait, testee, appelee depuis le `lifespan` de FastAPI —
    c'est-a-dire jamais en production : sous Passenger, `a2wsgi.ASGIMiddleware`
    ne construit qu'un scope `http` par requete et n'implemente pas le
    protocole `lifespan`. Le demarrage FastAPI n'y tourne pas. Constate sur
    l'installation reelle : un instantane PEP fige en PROCESSING depuis trois
    jours, alors que la reparation se declenche au bout d'une heure.

    Le demon travailleur, lui, demarre VRAIMENT en production — et il est
    unique (flock), donc la reparation n'arrive qu'une fois quel que soit le
    nombre de processus web. C'est ici, a cote de la reprise des jobs, que
    cette reprise-la doit vivre : le job mort est remis en file, l'instantane
    qu'il construisait doit l'etre aussi.

    Ne leve jamais : une reprise ne doit pas empecher le demon de demarrer.
    """
    try:
        from fiskr import api as api_module
        return api_module._repair_stuck_snapshots(session)
    except Exception as e:
        logger.warning(f"Reparation des instantanes bloques impossible : {e}")
        return {"repaired": 0, "failed": 0}


def reprise(session) -> None:
    """
    LA REPRISE, au demarrage du demon : ce qu'un arret brutal a laisse en
    plan. Les jobs tues repartent de zero (plafonnes par `attempts`), et les
    instantanes qu'ils construisaient sont recomptes depuis leurs fiches
    reelles — un job remis en file sans son instantane laisserait une liste
    hors production avec toutes ses fiches en base.
    """
    requeued = jobs.requeue_stale(session, worker_present=True)
    if requeued:
        logger.info(f"Reprise : {requeued} job(s) interrompu(s) traites.")
    bilan = reparer_instantanes_bloques(session)
    if bilan.get("repaired") or bilan.get("failed"):
        logger.warning(
            f"Reprise : {bilan['repaired']} instantane(s) remis en production, "
            f"{bilan['failed']} marque(s) en erreur (aucune fiche).")


def _heartbeat_loop():
    """Trois battements : les lignes jobs RUNNING de ce demon (la reprise s'y
    fie), le reglage global (l'autostart de l'API s'y fie), et — toutes les
    minutes — la REPARATION des zombies : un job laisse RUNNING par une
    incarnation morte du demon (OOM, kill) n'etait repare qu'au demarrage
    suivant ; tant que CE demon vivait, le zombie restait RUNNING pour
    toujours et, serialise, bloquait son genre entier (vu en production :
    deux cahiers de tests zombies, toute la file a l'arret)."""
    me = jobs.worker_id()
    beats = 0
    while not _stop.wait(HEARTBEAT_EVERY_S):
        beats += 1
        session = jobs._fresh_session()
        try:
            jobs.heartbeat(session, me)
            _write_heartbeat(session)
            if beats % 4 == 0:  # ~ toutes les 60 s
                repaired = jobs.requeue_stale(session, worker_present=True)
                if repaired:
                    logger.warning(f"Réparation périodique : {repaired} job(s) "
                                   f"zombie(s) traités (battement de coeur périmé).")
            if beats % 20 == 0:  # ~ toutes les 5 min
                # Un instantane peut se figer PENDANT que ce demon vit : le job
                # meurt, l'instantane reste. Attendre le redemarrage suivant,
                # c'est ce qui a laisse trois jours une liste hors production.
                # La cadence est lache a dessein : la reparation ne mord qu'au
                # bout d'une heure, et la requete est un filtre sur un index.
                reparer_instantanes_bloques(session)
        except Exception as e:
            logger.warning(f"Battement de coeur en echec : {e}")
            try:
                session.rollback()
            except Exception:
                pass
        finally:
            session.close()


def _start_schedulers():
    """
    Planificateurs periodiques, migres depuis le lifespan de l'API : le demon
    etant UNIQUE (flock), chaque tic n'arrive qu'une fois — sous Passenger
    multi-processus, ils partaient N fois. Les coroutines d'api.py n'ont
    d'async que le sommeil : ici, de simples threads.
    """
    from fiskr import api as api_module

    def _minute_loop(name, tick_fn):
        def _run():
            while not _stop.is_set():
                now = datetime.now()
                _stop.wait(60 - now.second - now.microsecond / 1_000_000 + 0.05)
                if _stop.is_set():
                    return
                try:
                    tick_fn()
                except Exception as e:
                    logger.error(f"Planificateur {name} en echec sur ce tick : {e}")
        threading.Thread(target=_run, name=f"sched-{name}", daemon=True).start()

    _minute_loop("sync", api_module._cron_sync_tick)
    _minute_loop("digest", api_module._digest_tick)
    _minute_loop("retention", api_module._retention_tick)
    _minute_loop("notifications", api_module._notification_batch_tick)
    _minute_loop("mining", api_module._mining_tick)

    # Inbox CFT : cadence propre (batch.inbox_poll_seconds), pas la minute
    def _inbox_run():
        from fiskr.settings import batch_inbox_settings
        while True:
            # Cadence relue a chaque passe : reglable a chaud sans redemarrage
            poll = float(batch_inbox_settings()["inbox_poll_seconds"])
            if _stop.wait(poll):
                return
            try:
                api_module._process_inbox_once()
            except Exception as e:
                logger.error(f"Inbox CFT en echec : {e}")
    threading.Thread(target=_inbox_run, name="sched-inbox", daemon=True).start()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    lock = acquire_worker_lock()
    if lock is None:
        logger.info("Un demon travailleur vit déjà : sortie.")
        return 0

    jobs.IN_WORKER = True
    os.environ["FISKR_JOBS_MODE"] = "worker"

    from fiskr import database
    database.init_db()

    # L'import d'api.py enregistre les taches (fiskr.tasks) et donne acces aux
    # corps des planificateurs. Il construit l'application FastAPI sans la
    # servir — cout d'import paye une fois au demarrage du demon.
    import fiskr.api  # noqa: F401

    session = jobs._fresh_session()
    try:
        reprise(session)
        jobs.purge_old(session)
        _write_heartbeat(session)
    finally:
        session.close()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: _stop.set())

    threading.Thread(target=_heartbeat_loop, daemon=True).start()
    _start_schedulers()

    slots = int((config.get("jobs") or {}).get("slots", 2) or 2)
    me = jobs.worker_id()
    logger.info(f"Demon travailleur pret ({me}), {slots} slot(s).")

    pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="job")
    inflight = set()

    def _run_and_release(job_id):
        try:
            jobs.run_job(job_id)
        finally:
            inflight.discard(job_id)

    try:
        while not _stop.is_set():
            if len(inflight) >= slots:
                _stop.wait(0.5)
                continue
            session = jobs._fresh_session()
            try:
                job_id = jobs.claim_next(session, me)
            except Exception as e:
                logger.error(f"Claim en echec : {e}")
                job_id = None
                try:
                    session.rollback()
                except Exception:
                    pass
            finally:
                session.close()
            if job_id is None:
                _stop.wait(POLL_EVERY_S)
                continue
            inflight.add(job_id)
            pool.submit(_run_and_release, job_id)
    finally:
        logger.info("Arret du demon : les jobs en cours seront repris au prochain demarrage.")
        pool.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
File de travaux persistee : soumission, execution et suivi des operations
longues, PARTAGEE entre le processus API et le demon travailleur
(fiskr/worker.py) via la table `jobs`.

Trois modes d'execution (FISKR_JOBS_MODE > config.yaml jobs.mode) :
- worker : la ligne est deposee QUEUED, le demon la prend (production) ;
- thread : execution dans un thread du processus appelant (repli sans demon,
  comportement historique de _start_job) ;
- eager  : execution inline, synchrone (tests : un endpoint 202 termine son
  job avant de repondre, les helpers wait_for_job passent tels quels).

La reprise apres redemarrage repose sur le battement de coeur : un job
RUNNING dont le heartbeat est perime a ete tue (recyclage Passenger, crash) ;
`requeue_stale` le remet en file (relance de zero) tant que attempts <
max_attempts, sinon le marque ERROR — relancable d'un clic. Un echec
APPLICATIF, lui, ne se relance jamais tout seul : un bug deterministe ne
doit pas boucler.
"""
import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional

from sqlalchemy import update as sa_update

from fiskr import progress as progress_registry
from fiskr.config import config
from fiskr.database import Job

logger = logging.getLogger("fiskr.jobs")

# Registre des taches nommees : kind -> fn(ctx, **params). Seules les taches
# enregistrees sont relancables (retry, requeue) : les jobs « legacy » poses
# par _start_job (closures non serialisables) vivent et meurent avec leur
# processus (max_attempts=1).
TASKS: Dict[str, Callable] = {}

# Battement de coeur : au-dela, un RUNNING est repute mort (6 battements de
# 15 s manques)
STALE_AFTER = timedelta(seconds=90)

# Taille maximale du rapport persiste (les listes de details sont deja
# tronquees en amont : 200 paires, 25 exemples — cette garde est un filet)
MAX_RESULT_BYTES = 300_000

# Accroche facultative posee par l'API : appelee a chaque submit() en mode
# worker pour reveiller/relancer le demon (autostart sous Passenger)
on_submit_hook: Optional[Callable[[], None]] = None

# Vrai UNIQUEMENT dans le demon travailleur (pose par fiskr/worker.py) : les
# taches s'en servent pour savoir si le cache de production local existe
# (processus API) ou s'il faut passer par l'epoque en base
IN_WORKER = False


class JobConflict(Exception):
    """Un job equivalent est deja en file ou en cours (dedupe_key)."""


def task(kind: str):
    """Enregistre une tache nommee, executable par n'importe quel processus."""
    def _register(fn):
        TASKS[kind] = fn
        return fn
    return _register


def jobs_mode() -> str:
    mode = os.environ.get("FISKR_JOBS_MODE") or (config.get("jobs") or {}).get("mode") or "thread"
    mode = str(mode).strip().lower()
    return mode if mode in ("worker", "thread", "eager") else "thread"


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _fresh_session():
    from fiskr.database import SessionLocal, init_db
    if SessionLocal is None:
        init_db()
        from fiskr.database import SessionLocal
    return SessionLocal()


class JobContext:
    """
    Passe a chaque tache : progression et resultat.

    La progression est ecrite DEUX fois : au registre memoire (utile quand la
    tache tourne dans le processus API — modes thread/eager) et sur la ligne
    `jobs` (seul canal qui traverse les processus et survit aux
    redemarrages), avec un commit throttle pour ne pas marteler la base.
    Chaque flush sert aussi de battement de coeur.
    """

    _FLUSH_EVERY_S = 0.5

    def __init__(self, job_id: int, token: str, kind: str, label: Optional[str],
                 started_by: Optional[str], mirror_registry: bool = True):
        self.job_id = job_id
        self.token = token
        self.kind = kind
        self.label = label
        self.started_by = started_by
        self.mirror_registry = mirror_registry
        self.result: Optional[Dict[str, Any]] = None
        self._last_flush = 0.0

    def update(self, *, phase: str, processed: int = 0, total: Optional[int] = None,
               snapshot_id: Optional[str] = None) -> None:
        if self.mirror_registry:
            progress_registry.update(self.token, phase=phase, processed=processed,
                                     total=total, snapshot_id=snapshot_id,
                                     kind=self.kind, label=self.label,
                                     started_by=self.started_by)
        now = time.monotonic()
        if now - self._last_flush < self._FLUSH_EVERY_S:
            return
        self._last_flush = now
        session = _fresh_session()
        try:
            session.execute(sa_update(Job).where(Job.id == self.job_id).values(
                phase=phase, processed=int(processed or 0),
                total=int(total) if total else Job.total,
                snapshot_id=snapshot_id if snapshot_id else Job.snapshot_id,
                heartbeat_at=datetime.utcnow(),
            ))
            session.commit()
        except Exception:
            session.rollback()  # la progression n'est jamais bloquante
        finally:
            session.close()

    def set_result(self, result: Optional[Dict[str, Any]]) -> None:
        self.result = result

    def session(self):
        """Session SQLAlchemy neuve — chaque tache ouvre et ferme la sienne."""
        return _fresh_session()


def _bounded_result(result: Optional[Dict[str, Any]]):
    """Normalise le rapport pour la colonne JSON (les datetimes deviennent des
    chaines) et le borne en taille — les listes de details sont deja tronquees
    en amont, cette garde est un filet."""
    if result is None:
        return None
    try:
        payload = json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return {"_truncated": True, "_reason": "resultat non serialisable"}
    if len(payload) > MAX_RESULT_BYTES:
        logger.warning(f"Resultat de job tronque ({len(payload)} octets > {MAX_RESULT_BYTES}).")
        kept = {k: v for k, v in result.items() if not isinstance(v, (list, dict))}
        kept["_truncated"] = True
        return json.loads(json.dumps(kept, ensure_ascii=False, default=str))
    return json.loads(payload)


def submit(kind: str, *, params: Optional[Dict[str, Any]] = None, token: Optional[str] = None,
           label: Optional[str] = None, created_by: Optional[str] = None,
           dedupe_key: Optional[str] = None, priority: int = 100,
           max_attempts: int = 2, snapshot_id: Optional[str] = None) -> Job:
    """
    Depose un job et, selon le mode, l'execute (eager/thread) ou le laisse au
    demon (worker). Retourne la ligne Job (rechargee : en eager elle est deja
    DONE/ERROR au retour).

    Leve JobConflict si un job portant le meme dedupe_key est deja QUEUED ou
    RUNNING — l'endpoint traduit en 409.
    """
    if kind not in TASKS:
        raise ValueError(f"Tache inconnue : {kind}")
    token = token or f"{kind}-{uuid.uuid4().hex[:8]}"
    mode = jobs_mode()

    session = _fresh_session()
    try:
        if dedupe_key:
            clash = session.query(Job.id).filter(
                Job.dedupe_key == dedupe_key,
                Job.status.in_(("QUEUED", "RUNNING")),
            ).first()
            if clash:
                raise JobConflict(f"Une operation equivalente est déjà en cours ({dedupe_key}).")
        job = Job(token=token, kind=kind, label=label, params=params or {},
                  status="QUEUED", created_by=created_by, dedupe_key=dedupe_key,
                  priority=priority, max_attempts=max_attempts,
                  snapshot_id=snapshot_id, phase="QUEUED")
        session.add(job)
        session.commit()
        job_id = job.id
    finally:
        session.close()

    # Modes eager/thread : l'operation existe des maintenant dans le registre
    # memoire de CE processus, qui la fera aussi vivre jusqu'au bout.
    # Mode worker : SURTOUT PAS — c'est le demon (autre processus) qui fait
    # vivre la ligne `jobs` ; une entree memoire locale resterait QUEUED pour
    # toujours et masquerait l'etat reel, car GET /api/progress prefere le
    # registre a la table. La pastille du front est servie par la fusion des
    # lignes `jobs` dans GET /api/progress/active.
    if mode != "worker":
        progress_registry.update(token, phase="QUEUED", kind=kind, label=label,
                                 started_by=created_by, snapshot_id=snapshot_id)

    if mode == "eager":
        run_job(job_id)
    elif mode == "thread":
        threading.Thread(target=run_job, args=(job_id,), daemon=True).start()
    else:  # worker
        if on_submit_hook is not None:
            try:
                on_submit_hook()
            except Exception as e:
                logger.warning(f"Accroche de reveil du demon en echec : {e}")

    session = _fresh_session()
    try:
        return session.get(Job, job_id)
    finally:
        session.close()


# Genres de jobs SERIALISES : jamais deux en meme temps, quel que soit le
# processus. Un cahier de tests (ou une simulation moteur) charge un univers
# de listes complet en memoire : deux passes simultanees ont deja epuise la
# RAM d'une machine de production. La file les enchaine au lieu de les cumuler.
SERIAL_KINDS = ("backtest", "engine_simulation")


def _serial_kind_busy(session, kind: str, exclude_job_id: Optional[int] = None) -> bool:
    """
    Un job du meme genre serialise tourne-t-il deja (ailleurs) ?

    Seuls les RUNNING au battement de coeur FRAIS comptent : un job laisse
    RUNNING par un demon mort (OOM, kill) n'occupe plus personne — sans ce
    filtre, un zombie bloquait son genre entier pour toujours (vu en
    production : deux cahiers de tests zombies, toute la file a l'arret).
    """
    if kind not in SERIAL_KINDS:
        return False
    cutoff = datetime.utcnow() - STALE_AFTER
    q = session.query(Job.id).filter(
        Job.kind == kind, Job.status == "RUNNING",
        Job.heartbeat_at.isnot(None), Job.heartbeat_at >= cutoff)
    if exclude_job_id is not None:
        q = q.filter(Job.id != exclude_job_id)
    return q.first() is not None


def claim_next(session, claimer: str) -> Optional[int]:
    """
    Prend le prochain job QUEUED (priorite puis anciennete) et le passe
    RUNNING. Retourne son id, ou None.

    PostgreSQL : SELECT ... FOR UPDATE SKIP LOCKED — deux demons ne peuvent
    pas prendre la meme ligne, par construction. Repli (SQLite de dev) :
    meme selection sans verrou, la garde `WHERE status='QUEUED'` de l'UPDATE
    et son rowcount arbitrent la course.

    Les genres SERIAL_KINDS ne sont pris que si aucun homologue ne tourne :
    les autres genres continuent de defiler pendant qu'un cahier de tests
    attend son tour.
    """
    now = datetime.utcnow()
    # Meme filtre de fraicheur que _serial_kind_busy : un job serialise
    # laisse RUNNING par un demon mort ne doit pas bloquer son genre
    stale_cutoff = now - STALE_AFTER
    running_serial = [k for (k,) in session.query(Job.kind).distinct().filter(
        Job.status == "RUNNING", Job.kind.in_(SERIAL_KINDS),
        Job.heartbeat_at.isnot(None), Job.heartbeat_at >= stale_cutoff).all()]
    q = session.query(Job).filter(
        Job.status == "QUEUED",
        (Job.not_before.is_(None)) | (Job.not_before <= now),
    )
    if running_serial:
        q = q.filter(~Job.kind.in_(running_serial))
    q = q.order_by(Job.priority.asc(), Job.id.asc()).limit(1)
    if session.bind.dialect.name == "postgresql":
        q = q.with_for_update(skip_locked=True)
    row = q.first()
    if row is None:
        session.rollback()
        return None
    res = session.execute(
        sa_update(Job)
        .where(Job.id == row.id, Job.status == "QUEUED")
        .values(status="RUNNING", claimed_by=claimer, attempts=Job.attempts + 1,
                started_at=now, heartbeat_at=now, phase="PARSE")
    )
    session.commit()
    if res.rowcount != 1:  # un concurrent l'a pris (chemin repli uniquement)
        return None
    return row.id


def run_job(job_id: int) -> None:
    """
    Execute un job pris (ou le prend s'il est encore QUEUED — chemin
    eager/thread) et persiste sa fin. Toute exception devient ERROR : un job
    qui casse ne casse jamais son hote, et son echec reste visible.
    """
    session = _fresh_session()
    try:
        job = session.get(Job, job_id)
        if job is None:
            return
        # Modes eager/thread : submit() execute immediatement, sans passer par
        # claim_next — la serialisation doit donc aussi vivre ici. Le thread
        # patiente tant qu'un homologue tourne (sommeil court, base rendue
        # entre deux sondages) ; en mode worker le job arrive deja RUNNING et
        # cette boucle est sans objet.
        if job.status == "QUEUED" and job.kind in SERIAL_KINDS:
            while _serial_kind_busy(session, job.kind, exclude_job_id=job_id):
                session.rollback()
                time.sleep(5)
                job = session.get(Job, job_id)
                if job is None or job.status != "QUEUED":
                    break
        if job is None:
            return
        if job.status == "QUEUED":
            res = session.execute(
                sa_update(Job).where(Job.id == job_id, Job.status == "QUEUED")
                .values(status="RUNNING", claimed_by=worker_id(),
                        attempts=Job.attempts + 1, started_at=datetime.utcnow(),
                        heartbeat_at=datetime.utcnow())
            )
            session.commit()
            if res.rowcount != 1:
                return
            session.refresh(job)
        elif job.status != "RUNNING":
            # Annule (ou deja termine) pendant l'attente de serialisation :
            # un job CANCELLED ne doit JAMAIS s'executer — sans cette garde,
            # le chemin thread sortait de la boucle d'attente et lancait
            # quand meme la tache.
            return
        token, kind, label, params = job.token, job.kind, job.label, dict(job.params or {})
        created_by = job.created_by
    finally:
        session.close()

    ctx = JobContext(job_id, token, kind, label, created_by)
    # Battement de coeur independant des ticks de progression : certaines
    # phases (construction d'index sur 750 000 fiches) restent silencieuses
    # plusieurs minutes, et un job silencieux n'est pas un job mort.
    stop_beat = threading.Event()

    def _beat():
        while not stop_beat.wait(15.0):
            session = _fresh_session()
            try:
                session.execute(sa_update(Job).where(Job.id == job_id)
                                .values(heartbeat_at=datetime.utcnow()))
                session.commit()
            except Exception:
                session.rollback()
            finally:
                session.close()

    beat = threading.Thread(target=_beat, daemon=True)
    beat.start()
    try:
        fn = TASKS.get(kind)
        if fn is None:
            raise RuntimeError(f"Tache inconnue dans ce processus : {kind}")
        fn(ctx, **params)
    except Exception as e:
        logger.error(f"Job {kind} « {label or token} » en erreur : {e}")
        stop_beat.set()
        finish_job(job_id, "ERROR", error=str(e)[:500])
    else:
        stop_beat.set()
        finish_job(job_id, "DONE", result=ctx.result)
    finally:
        stop_beat.set()


def finish_job(job_id: int, status: str, *, error: Optional[str] = None,
               result: Optional[Dict[str, Any]] = None) -> None:
    session = _fresh_session()
    try:
        job = session.get(Job, job_id)
        if job is None:
            return
        job.status = status
        job.error = error
        job.result = _bounded_result(result)
        job.finished_at = datetime.utcnow()
        job.heartbeat_at = job.finished_at
        job.phase = "DONE" if status == "DONE" else job.phase
        session.commit()
        token = job.token
    finally:
        session.close()
    # Miroir memoire : le front qui interroge le registre voit la fin aussitot
    progress_registry.finish(token, status=status if status in ("DONE", "ERROR") else "ERROR",
                             error=error, result=result)


def heartbeat(session, claimer: str) -> None:
    """Rafraichit le battement de coeur de tous les jobs RUNNING du demon."""
    session.execute(sa_update(Job).where(
        Job.status == "RUNNING", Job.claimed_by == claimer
    ).values(heartbeat_at=datetime.utcnow()))
    session.commit()


def requeue_stale(session, worker_present: Optional[bool] = None) -> int:
    """
    Reprise apres redemarrage : les RUNNING au battement de coeur perime ont
    ete tues. Relance de zero (QUEUED, backoff lineaire) tant que attempts <
    max_attempts ET que la tache est re-executable (enregistree, params
    serialises) ; sinon ERROR relancable a la main.

    `worker_present` : sans demon (modes thread/eager), une remise en file
    serait un mensonge — personne ne prendra jamais un QUEUED. Les orphelins
    passent alors directement en ERROR, y compris les QUEUED abandonnes.
    """
    if worker_present is None:
        worker_present = jobs_mode() == "worker"
    cutoff = datetime.utcnow() - STALE_AFTER
    touched = 0
    stale = session.query(Job).filter(
        Job.status == "RUNNING",
        (Job.heartbeat_at.is_(None)) | (Job.heartbeat_at < cutoff),
    ).all()
    if not worker_present:
        # Les QUEUED herites d'un processus mort ne seront jamais pris :
        # une file sans executant n'est pas une file, c'est une impasse
        stale += session.query(Job).filter(
            Job.status == "QUEUED", Job.created_at < cutoff,
        ).all()
    for job in stale:
        touched += 1
        rerunnable = (worker_present and job.kind in TASKS
                      and job.attempts < job.max_attempts and job.status == "RUNNING")
        if rerunnable:
            job.status = "QUEUED"
            job.claimed_by = None
            job.phase = "QUEUED"
            job.not_before = datetime.utcnow() + timedelta(seconds=60 * job.attempts)
            logger.warning(f"Job {job.kind} « {job.label or job.token} » interrompu : "
                           f"remis en file (tentative {job.attempts + 1}/{job.max_attempts}).")
        else:
            job.status = "ERROR"
            job.error = "Interrompu par un redémarrage du serveur."
            job.finished_at = datetime.utcnow()
            logger.error(f"Job {job.kind} « {job.label or job.token} » interrompu : "
                         f"marqué en erreur (non relançable automatiquement).")
    session.commit()
    return touched


def purge_old(session, keep_days: int = 30) -> int:
    cutoff = datetime.utcnow() - timedelta(days=keep_days)
    n = session.query(Job).filter(
        Job.status.in_(("DONE", "ERROR", "CANCELLED")),
        Job.created_at < cutoff,
    ).delete(synchronize_session=False)
    session.commit()
    return n


def latest_by_token(session, token: str) -> Optional[Job]:
    return session.query(Job).filter(Job.token == token).order_by(Job.id.desc()).first()


def mirror_progress(token: Optional[str], *, phase: str, processed: int = 0,
                    total: Optional[int] = None,
                    snapshot_id: Optional[str] = None) -> None:
    """
    Reflete un tick de progression sur la DERNIERE ligne de la file portant ce
    jeton. Pour les corps de taches historiques qui publient encore au registre
    memoire par jeton (import de fichiers) : le registre ne traverse pas les
    processus, la ligne jobs si. Jamais bloquant, appele a cadence lente
    (tous les 1 000 enregistrements).
    """
    if not token:
        return
    session = _fresh_session()
    try:
        job = latest_by_token(session, token)
        if job is None or job.status not in ("QUEUED", "RUNNING"):
            return
        session.execute(sa_update(Job).where(Job.id == job.id).values(
            phase=phase, processed=int(processed or 0),
            total=int(total) if total else Job.total,
            snapshot_id=snapshot_id if snapshot_id else Job.snapshot_id,
            heartbeat_at=datetime.utcnow(),
        ))
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()

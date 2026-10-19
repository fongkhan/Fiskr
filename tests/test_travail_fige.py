"""
Un travail figé passe pour vivant tant qu'on juge sur le battement de cœur.

Relevé sur l'installation de production le 19 octobre 2026, en lecture seule,
par le diagnostic prévu pour cela :

    « Synchronisation PEP »  RUNNING depuis le 23/09 04:01 — 26 jours
    phase HASH, 0 fiche traitée
    heartbeat_age_s: 5.4    heartbeat_stale: false

Le battement de cœur des jobs est rafraîchi pour TOUS les jobs du démon par
un fil indépendant. Il dit que le processus vit ; il ne dit rien d'un fil
d'exécution bloqué sur un appel qui ne rendra jamais la main. Conséquences,
toutes silencieuses : la clé `sync:pep` interdisait toute nouvelle
synchronisation PEP (la liste PEP en production date du 1er septembre), le job
occupait l'un des deux emplacements du démon, et diagnostic comme mise en
service disaient « le démon bat ».

Ce que ces tests tiennent : la progression se date à part du battement, et
seule une progression RÉELLE la date ; un RUNNING sans progression au-delà du
seuil est déclaré figé, sa file libérée ; un fil qui se réveille ensuite ne
réécrit pas l'histoire ; et le démon se recycle pour récupérer l'emplacement.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from fiskr import jobs
from fiskr.database import Job, get_db

UID = uuid.uuid4().hex[:8]


@pytest.fixture()
def db():
    session = next(get_db())
    yield session
    session.query(Job).filter(Job.token.like(f"fige-{UID}%")).delete(synchronize_session=False)
    session.commit()
    session.close()


def _job(db, suffixe, *, progres_il_y_a_h, battement_il_y_a_s=5, claimer="hote:1",
         phase="HASH", processed=0, kind="sync"):
    maintenant = datetime.utcnow()
    job = Job(token=f"fige-{UID}-{suffixe}", kind=kind, label=f"Synchro {suffixe}",
              status="RUNNING", claimed_by=claimer, phase=phase, processed=processed,
              started_at=maintenant - timedelta(hours=progres_il_y_a_h + 1),
              progress_at=maintenant - timedelta(hours=progres_il_y_a_h),
              heartbeat_at=maintenant - timedelta(seconds=battement_il_y_a_s),
              dedupe_key=f"fige-{UID}-{suffixe}")
    db.add(job)
    db.commit()
    return job.id


# --------------------------------------------------------------------------
# La déclaration
# --------------------------------------------------------------------------

def test_un_job_au_battement_parfait_mais_sans_progression_est_declare_fige(db):
    """La situation exacte de la production, en petit."""
    jid = _job(db, "pep", progres_il_y_a_h=26 * 24)
    figes = jobs.declarer_figes(db, "hote:1")
    assert [j.id for j in figes] == [jid]
    job = db.get(Job, jid)
    assert job.status == "ERROR"
    assert job.error.startswith(jobs.PREFIXE_FIGE)
    assert "624 h" in job.error and "phase HASH" in job.error


def test_un_job_qui_avance_n_est_jamais_touche(db):
    jid = _job(db, "vivant", progres_il_y_a_h=0.1)
    assert jobs.declarer_figes(db, "hote:1") == []
    assert db.get(Job, jid).status == "RUNNING"


def test_le_demon_ne_juge_que_ses_propres_jobs(db):
    """Un autre démon (autre hôte) répond de ses jobs : pas de verdict à sa place."""
    jid = _job(db, "ailleurs", progres_il_y_a_h=48, claimer="autre:2")
    assert jobs.declarer_figes(db, "hote:1") == []
    assert db.get(Job, jid).status == "RUNNING"


def test_sans_date_de_progression_le_demarrage_fait_foi(db):
    """Un job antérieur à la mesure n'échappe pas au garde-fou."""
    jid = _job(db, "ancien", progres_il_y_a_h=30)
    job = db.get(Job, jid)
    job.progress_at = None
    db.commit()
    assert [j.id for j in jobs.declarer_figes(db, "hote:1")] == [jid]


def test_declarer_fige_libere_la_cle_d_exclusivite(db):
    """
    Le cœur du dommage en production : tant que le job restait RUNNING, sa clé
    `sync:pep` interdisait toute nouvelle synchronisation PEP.
    """
    jid = _job(db, "cle", progres_il_y_a_h=48)
    jobs.declarer_figes(db, "hote:1")
    actifs = db.query(Job).filter(Job.dedupe_key == f"fige-{UID}-cle",
                                  Job.status.in_(("QUEUED", "RUNNING"))).count()
    assert actifs == 0


def test_le_seuil_est_reglable_et_borne(monkeypatch):
    monkeypatch.setitem(jobs.config, "jobs", {"stall_after_hours": 2})
    assert jobs.stall_after() == timedelta(hours=2)
    monkeypatch.setitem(jobs.config, "jobs", {"stall_after_hours": 0})
    assert jobs.stall_after() == timedelta(minutes=30), "un seuil nul tuerait tout"
    monkeypatch.setitem(jobs.config, "jobs", {"stall_after_hours": "n'importe quoi"})
    assert jobs.stall_after() == timedelta(hours=jobs.DEFAULT_STALL_AFTER_H)


# --------------------------------------------------------------------------
# Seule une progression réelle date le travail
# --------------------------------------------------------------------------

def test_un_tick_qui_repete_la_meme_progression_ne_rajeunit_pas_le_travail(db):
    """
    Sans cette distinction, une boucle qui republie « HASH, 0 » chaque seconde
    se donnerait elle-même un brevet de vie : le défaut serait seulement
    déplacé du battement vers la progression.
    """
    jid = _job(db, "tick", progres_il_y_a_h=10)
    avant = db.get(Job, jid).progress_at
    jobs.mirror_progress(f"fige-{UID}-tick", phase="HASH", processed=0)
    db.expire_all()
    assert db.get(Job, jid).progress_at == avant
    jobs.mirror_progress(f"fige-{UID}-tick", phase="PERSIST", processed=1000)
    db.expire_all()
    assert db.get(Job, jid).progress_at > avant


def test_le_contexte_de_job_date_la_progression_reelle(db):
    jid = _job(db, "ctx", progres_il_y_a_h=10, phase="PARSE")
    ctx = jobs.JobContext(jid, f"fige-{UID}-ctx", "sync", "ctx", "t", mirror_registry=False)
    avant = db.get(Job, jid).progress_at
    ctx.update(phase="PARSE", processed=0)
    ctx._last_flush = 0.0
    ctx.update(phase="PARSE", processed=0)          # répétition : pas de progrès
    db.expire_all()
    assert db.get(Job, jid).progress_at > avant      # le premier tick établit la base
    base = db.get(Job, jid).progress_at
    ctx._last_flush = 0.0
    ctx.update(phase="PARSE", processed=0)
    db.expire_all()
    assert db.get(Job, jid).progress_at == base
    ctx.update(phase="PARSE", processed=500)          # progrès, même sous la cadence
    db.expire_all()
    assert db.get(Job, jid).progress_at > base


# --------------------------------------------------------------------------
# Un fil qui se réveille ne réécrit pas l'histoire
# --------------------------------------------------------------------------

def test_un_job_declare_fige_qui_finit_tard_ne_redevient_pas_done(db):
    jid = _job(db, "tardif", progres_il_y_a_h=48)
    jobs.declarer_figes(db, "hote:1")
    jobs.finish_job(jid, "DONE", result={"ok": True})
    db.expire_all()
    job = db.get(Job, jid)
    assert job.status == "ERROR"
    assert job.error.startswith(jobs.PREFIXE_FIGE)


def test_une_erreur_ordinaire_reste_ecrasable_par_la_fin_normale(db):
    """La garde ne vise que les figés : le reste du cycle de vie est inchangé."""
    jid = _job(db, "ordinaire", progres_il_y_a_h=0.1)
    jobs.finish_job(jid, "DONE", result={"ok": True})
    db.expire_all()
    assert db.get(Job, jid).status == "DONE"


# --------------------------------------------------------------------------
# Le démon, le diagnostic, la mise en service
# --------------------------------------------------------------------------

def test_le_demon_cherche_les_figes_dans_son_battement():
    import inspect
    from fiskr import worker
    source = inspect.getsource(worker._heartbeat_loop)
    assert "jobs.declarer_figes(session, me)" in source
    assert "_recycler(" in source


def test_le_recyclage_force_la_sortie_quand_un_fil_bloque_retient_le_processus():
    """
    On ne tue pas un fil Python, et les fils du pool ne sont pas des démons :
    sans sortie forcée, le processus attendrait pour toujours le fil bloqué,
    et l'emplacement ne serait jamais rendu.
    """
    import inspect
    from fiskr import worker
    source = inspect.getsource(worker._recycler)
    assert "_stop.set()" in source
    assert "os._exit(" in source
    assert "daemon=True" in source


def test_le_recyclage_se_desactive_par_reglage(monkeypatch):
    from fiskr import worker
    monkeypatch.setitem(worker.config, "jobs", {"recycle_on_stall": False})
    worker._stop.clear()
    worker._recycler("essai")
    assert not worker._stop.is_set()


def test_l_evenement_est_immediat():
    from fiskr.events import EVENT_CATALOG, IMMEDIATE
    assert EVENT_CATALOG["job_fige"].urgency == IMMEDIATE


def test_la_mise_en_service_ne_dit_plus_le_demon_bat_sur_un_travail_fige(db, monkeypatch):
    from fiskr import mise_en_service
    monkeypatch.setattr("fiskr.jobs.jobs_mode", lambda: "worker")
    _job(db, "mes", progres_il_y_a_h=48, battement_il_y_a_s=2)
    verdict = mise_en_service._demon(db)
    assert verdict["etat"] == "BLOQUANT"
    assert "n'avancent plus" in verdict["constat"]
    assert f"Synchro mes" in verdict["constat"]


def test_le_diagnostic_montre_la_progression_a_cote_du_battement():
    import inspect
    from fiskr import api
    source = inspect.getsource(api.diagnostic_jobs)
    assert '"progress_stale"' in source and '"progress_age_s"' in source


def test_un_fil_bloque_n_empeche_pas_le_processus_de_se_relancer(tmp_path):
    """
    La vérification qui compte, dans un vrai processus : un fil non démon
    bloqué pour toujours, un recyclage — le processus doit sortir, avec le code
    que le superviseur reconnaîtra, au lieu d'attendre ce fil indéfiniment.
    """
    import subprocess
    import sys
    script = tmp_path / "recyclage.py"
    script.write_text(
        "import threading, time\n"
        "from fiskr import worker\n"
        "worker.RECYCLAGE_GRACE_S = 0.5\n"
        "threading.Thread(target=lambda: time.sleep(3600)).start()\n"
        "worker._recycler('essai')\n",
        encoding="utf-8")
    import os
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    fini = subprocess.run([sys.executable, str(script)], env=env, timeout=60,
                          capture_output=True)
    assert fini.returncode == 75, fini.stderr.decode()[-500:]

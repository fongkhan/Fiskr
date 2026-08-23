"""
Ce que le démarrage fait, et ce que la production n'exécute jamais.

En production Fiskr est servi par Passenger, via `a2wsgi.ASGIMiddleware` — qui
ne construit qu'un scope `http` par requête et **n'implémente pas** le
protocole ASGI `lifespan`. Le démarrage FastAPI n'y tourne donc pas. Ce n'est
pas une hypothèse : le code le dit à quatre endroits, chacun écrit après une
panne réelle (badge « Hash Actif » à N/A, premier criblage à 64 s, criblage
rendant NO_MATCH sur un cache vide).

D'où une classe de défaut qui ne se voit qu'en production : un travail branché
dans le `lifespan` marche partout — en développement sous uvicorn, dans toute
la suite de tests via `TestClient` — et nulle part là où ça compte.

Deux l'étaient encore, et les deux sont couverts ici :

* **L'accroche d'autostart du démon.** `jobs.on_submit_hook = ensure_worker`
  était posée dans le `lifespan`. Le mécanisme décrit dans le code comme « le
  seul moyen d'avoir un démon en hébergement mutualisé » était donc branché à
  l'endroit précis où l'hébergement mutualisé ne passe pas : déposer un job
  depuis l'application ne réveillait rien.
* **La réparation des instantanés restés en PROCESSING.** Elle se déclenche au
  bout d'une heure ; en production un instantané PEP est resté figé trois
  jours. Elle vit maintenant dans le démon travailleur, qui démarre vraiment,
  et qui est unique (flock) — donc un seul passage quel que soit le nombre de
  processus web.
"""
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, WatchlistEntity

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'demarrage.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ----------------------------------------------- l'accroche d'autostart du démon

def test_l_accroche_d_autostart_est_armee_sans_lifespan():
    """
    On simule EXACTEMENT ce que fait Passenger : charger `fiskr.wsgi`, et rien
    d'autre. Pas de `TestClient`, pas d'uvicorn — donc pas de `lifespan`. Si
    l'accroche n'est armée que par le démarrage FastAPI, elle est ici à None,
    et elle le restera dans tous les processus web de la production.

    Sous-processus obligatoire : un import déjà fait dans CE processus (par un
    autre test) ne rejouerait pas le module.
    """
    code = (
        "import fiskr.wsgi\n"
        "from fiskr import jobs\n"
        "import sys\n"
        "sys.__stdout__.write('HOOK=' + ('pose' if jobs.on_submit_hook else 'NONE'))\n"
    )
    env = dict(os.environ, FISKR_JOBS_MODE="worker", PYTHONPATH=RACINE)
    sortie = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=env, cwd=RACINE, timeout=180)
    assert "HOOK=pose" in sortie.stdout, (
        "l'accroche d'autostart n'est pas armée dans un processus qui n'entre "
        f"jamais dans le lifespan.\nstdout={sortie.stdout[-500:]}\n"
        f"stderr={sortie.stderr[-500:]}")


def test_l_accroche_reste_absente_hors_du_mode_worker():
    """En mode `thread` ou `eager`, personne ne doit réveiller un démon : le
    job s'exécute dans le processus appelant."""
    code = (
        "import fiskr.wsgi\n"
        "from fiskr import jobs\n"
        "import sys\n"
        "sys.__stdout__.write('HOOK=' + ('pose' if jobs.on_submit_hook else 'NONE'))\n"
    )
    env = dict(os.environ, FISKR_JOBS_MODE="thread", PYTHONPATH=RACINE)
    sortie = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, env=env, cwd=RACINE, timeout=180)
    assert "HOOK=NONE" in sortie.stdout, sortie.stdout[-500:]


def test_l_accroche_n_est_pas_armee_deux_fois(): 
    """
    Une règle, un endroit. L'affectation vivait dans le `lifespan` ; elle vit
    maintenant à l'import. La laisser aux deux endroits, c'est deux réglages
    qui finiront par diverger — l'un lu au chargement, l'autre au démarrage.
    """
    with open(os.path.join(RACINE, "fiskr", "api.py"), encoding="utf-8") as f:
        source = "\n".join(re.sub(r"#.*$", "", ligne) for ligne in f.read().splitlines())
    assert source.count("on_submit_hook = ensure_worker") == 1


# ------------------------------------- la reprise des instantanés, dans le démon

def _instantane_bloque(db, entites, age_heures=3):
    identifiant = f"bloque-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=identifiant, file_type="WATCHLIST_UN",
                    file_name="bloque.xml", file_hash=uuid.uuid4().hex,
                    record_count=0, status="PROCESSING",
                    uploaded_at=datetime.utcnow() - timedelta(hours=age_heures)))
    for i in range(entites):
        db.add(WatchlistEntity(snapshot_id=identifiant, entity_id=f"BLQ-{i}",
                               entity_type="I", primary_name=f"BLOQUÉ {i}",
                               entity_checksum=f"chk-blq-{uuid.uuid4().hex[:8]}"))
    db.commit()
    return identifiant


def test_la_reprise_du_demon_repare_les_instantanes_bloques(db):
    """
    Le job mort est remis en file ; l'instantané qu'il construisait doit l'être
    aussi. Sinon la liste reste hors production avec toutes ses fiches en base
    — l'état exact observé sur l'installation réelle.
    """
    from fiskr import worker

    identifiant = _instantane_bloque(db, entites=17)
    worker.reprise(db)

    instantane = db.query(Snapshot).filter(Snapshot.snapshot_id == identifiant).first()
    assert instantane.status in ("READY", "PENDING_REVIEW")
    assert instantane.record_count == 17


def test_la_reprise_ne_touche_pas_un_import_en_cours(db):
    """Un import qui travaille ne doit pas être « réparé » sous ses pieds."""
    from fiskr import worker

    identifiant = _instantane_bloque(db, entites=5, age_heures=0)
    worker.reprise(db)

    instantane = db.query(Snapshot).filter(Snapshot.snapshot_id == identifiant).first()
    assert instantane.status == "PROCESSING"


def test_la_reprise_n_empeche_jamais_le_demon_de_demarrer():
    """
    Une reprise qui lève empêcherait le démon de démarrer — donc plus aucun
    job, plus aucune synchronisation. Le remède serait pire que le mal.
    """
    from fiskr import worker

    class _SessionCassee:
        def query(self, *a, **k):
            raise RuntimeError("base injoignable")

        def rollback(self):
            pass

    assert worker.reparer_instantanes_bloques(_SessionCassee()) == {"repaired": 0, "failed": 0}


def test_la_reparation_est_aussi_periodique():
    """
    Garde de source. Un instantané peut se figer PENDANT que le démon vit : le
    job meurt, l'instantané reste. S'en remettre au seul démarrage, c'est
    attendre le prochain redémarrage — trois jours, en production.
    """
    with open(os.path.join(RACINE, "fiskr", "worker.py"), encoding="utf-8") as f:
        source = f.read()
    battement = source[source.index("def _heartbeat_loop("):source.index("def _start_schedulers(")]
    assert "reparer_instantanes_bloques(" in battement, (
        "la réparation n'est appelée qu'au démarrage du démon")

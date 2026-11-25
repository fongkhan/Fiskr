"""
Un import interrompu n'est pas une liste.

Relevé sur l'installation de production le 25 novembre 2026, en lecture
seule (journal d'administration, instantanés PEP, diagnostic des travaux) :

    SNAPSHOT_REPAIRED  pep-sync-20260820…  PROCESSING → PENDING_REVIEW  625 000 fiches
    SNAPSHOT_REPAIRED  pep-sync-20260901…  PROCESSING → PENDING_REVIEW  548 000 fiches
    SNAPSHOT_REPAIRED  pep-sync-20260905…  PROCESSING → PENDING_REVIEW   61 000 fiches
    SNAPSHOT_REPAIRED  pep-sync-20260923…  PROCESSING → ERROR                0 fiche
    SNAPSHOT_REPAIRED  pep-sync-20261023…  PROCESSING → PENDING_REVIEW  297 000 fiches

Une liste PEP complète compte environ 713 000 fiches ; ces comptes sont des
multiples de 1 000, la taille d'un lot de la boucle de persistance — là où
l'import s'était arrêté. La réparation des instantanés « bloqués » comptait
les fiches présentes et en concluait que l'import avait abouti. Celui de
548 000 a été homologué : c'est la liste PEP en production depuis le
1er septembre. Et la synchronisation du 23 octobre, figée à 296 000 fiches,
occupait encore un emplacement du démon 32 jours plus tard.

Ce que ces tests tiennent : un instantané dont le travail vit n'est pas
touché ; il ne se promeut qu'avec une preuve d'achèvement ; sans preuve,
c'est un import interrompu, en erreur ; un lot promu sans preuve ne
s'homologue pas, la file le dit, et la mise en service bloque sur une liste
en production qui en est issue ; enfin un travail figé dit où son fil est
bloqué.
"""
import threading
import time
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import completude, jobs
from fiskr import sync as sync_mod
from fiskr.database import (AdminAuditLog, Base, Job, Snapshot, SyncReport,
                            WatchlistEntity, get_db)
from tests.test_sync_integrity import _local_fetcher, _pivot_items

UID = uuid.uuid4().hex[:6]


@pytest.fixture
def isolee(tmp_path):
    """Base jetable : la réparation parcourt TOUS les instantanés en PROCESSING."""
    engine = create_engine(f"sqlite:///{tmp_path / 'interrompu.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def db():
    session = next(get_db())
    yield session
    session.rollback()
    for modele, colonne in ((AdminAuditLog, AdminAuditLog.target),
                            (SyncReport, SyncReport.snapshot_id),
                            (Job, Job.snapshot_id),
                            (WatchlistEntity, WatchlistEntity.snapshot_id),
                            (Snapshot, Snapshot.snapshot_id)):
        session.query(modele).filter(colonne.like(f"ii-{UID}-%")).delete(
            synchronize_session=False)
    session.query(Job).filter(Job.token.like(f"ii-{UID}-%")).delete(synchronize_session=False)
    session.commit()
    session.close()


def _instantane(session, fiches, *, statut="PROCESSING", age_heures=3,
                ftype="WATCHLIST_PEP", jours=0):
    sid = f"ii-{UID}-{uuid.uuid4().hex[:8]}"
    session.add(Snapshot(snapshot_id=sid, file_type=ftype, file_name=f"{sid}.csv",
                         file_hash=uuid.uuid4().hex, record_count=0 if statut == "PROCESSING" else fiches,
                         status=statut,
                         uploaded_at=datetime.utcnow() - timedelta(hours=age_heures, days=jours)))
    for i in range(fiches):
        session.add(WatchlistEntity(snapshot_id=sid, entity_id=f"II-{i}", entity_type="I",
                                    primary_name=f"PERSONNE {i}",
                                    entity_checksum=f"chk-{uuid.uuid4().hex[:10]}"))
    session.commit()
    return sid


def _travail(session, sid, statut, kind="sync"):
    session.add(Job(token=f"ii-{UID}-{uuid.uuid4().hex[:6]}", kind=kind, status=statut,
                    snapshot_id=sid, phase="PERSIST", processed=0,
                    started_at=datetime.utcnow() - timedelta(hours=3)))
    session.commit()


# --------------------------------------------------------------------------
# La réparation des instantanés
# --------------------------------------------------------------------------

def test_un_instantane_dont_le_travail_vit_n_est_pas_repare(isolee):
    """
    Trois heures en PROCESSING, et son travail tourne : il s'écrit, il n'est
    pas bloqué. C'est le travail figé qui se déclare (jobs.declarer_figes), pas
    l'instantané qu'il écrit.
    """
    from fiskr.api import _repair_stuck_snapshots
    sid = _instantane(isolee, 30)
    _travail(isolee, sid, "RUNNING")
    assert _repair_stuck_snapshots(isolee) == {"repaired": 0, "failed": 0}
    assert isolee.get(Snapshot, sid).status == "PROCESSING"


def test_un_import_interrompu_n_est_jamais_promu(isolee):
    """La situation exacte de la production : des fiches, aucune preuve."""
    from fiskr.api import _repair_stuck_snapshots
    sid = _instantane(isolee, 30)
    _travail(isolee, sid, "ERROR")
    bilan = _repair_stuck_snapshots(isolee)
    assert bilan == {"repaired": 0, "failed": 1}
    snap = isolee.get(Snapshot, sid)
    assert snap.status == "ERROR"
    trace = isolee.query(AdminAuditLog).filter(AdminAuditLog.target == sid).one()
    assert trace.action == completude.ACTION_REPARATION
    assert trace.after["status"] == "ERROR"
    assert trace.after["fiches_ecrites"] == 30
    assert "Import interrompu" in trace.detail
    assert completude.promotion_sans_preuve(isolee, sid) is None


def test_un_import_termine_dont_le_statut_s_etait_perdu_est_promu(isolee):
    """
    Le défaut d'origine — un import TERMINÉ dont le statut n'avait pas été
    persisté — reste réparé : son rapport de synchronisation l'atteste.
    """
    from fiskr.api import _repair_stuck_snapshots
    sid = _instantane(isolee, 42)
    isolee.add(SyncReport(source="PEP", status="PENDING_REVIEW", snapshot_id=sid,
                          added_count=42, modified_count=0, removed_count=0))
    isolee.commit()
    assert _repair_stuck_snapshots(isolee) == {"repaired": 1, "failed": 0}
    snap = isolee.get(Snapshot, sid)
    assert snap.status in ("READY", "PENDING_REVIEW")
    assert snap.record_count == 42


def test_un_import_de_fichier_termine_est_prouve_par_son_travail(isolee):
    from fiskr.api import _repair_stuck_snapshots
    sid = _instantane(isolee, 7)
    _travail(isolee, sid, "DONE", kind="ingest")
    assert _repair_stuck_snapshots(isolee)["repaired"] == 1


def test_l_homologation_d_un_lot_ne_prouve_pas_son_import(isolee):
    """Un travail `approve` porte l'instantané, mais il le met en production :
    il ne dit rien de son import."""
    sid = _instantane(isolee, 7)
    _travail(isolee, sid, "DONE", kind="approve")
    assert completude.achevement_prouve(isolee, sid) is False


def test_la_synchronisation_designe_son_instantane_des_sa_creation(isolee, monkeypatch):
    """
    Le 23 septembre, la synchronisation PEP s'est figée AVANT son premier lot
    de 1 000 fiches : sa ligne de travail ne disait pas quel instantané elle
    écrivait. Désormais elle le dit avant la première fiche — c'est ce qui
    protège l'instantané de la réparation tant que le travail vit.
    """
    fabrique = sessionmaker(bind=isolee.get_bind())
    monkeypatch.setattr(jobs, "_fresh_session", fabrique)
    isolee.add(Job(token="sync:pep", kind="sync", status="RUNNING", phase="DOWNLOAD"))
    isolee.commit()
    vu = {}

    def _lecteur(chemin):
        lecture = fabrique()
        try:
            job = lecture.query(Job).filter(Job.token == "sync:pep").one()
            vu["snapshot_id"], vu["phase"] = job.snapshot_id, job.phase
        finally:
            lecture.close()
        yield from _pivot_items(3, "PEP")

    monkeypatch.setattr(sync_mod, "parse_pep_targets", _lecteur)
    sync_mod.run_pep_sync(isolee, trigger="MANUAL", fetcher=_local_fetcher("x"))
    cree = isolee.query(Snapshot).filter(Snapshot.file_type == "WATCHLIST_PEP").one()
    assert vu == {"snapshot_id": cree.snapshot_id, "phase": "PERSIST"}


# --------------------------------------------------------------------------
# Ce qui a été promu sans preuve, avant cette correction
# --------------------------------------------------------------------------

def _promu_sans_preuve(db, fiches, statut, jours=0):
    """Un instantané tel que l'ancienne réparation l'a laissé."""
    sid = _instantane(db, 0, statut=statut, jours=jours)
    snap = db.get(Snapshot, sid)
    snap.record_count = fiches
    db.add(AdminAuditLog(username="systeme", action=completude.ACTION_REPARATION,
                         target=sid, before={"status": "PROCESSING", "record_count": 0},
                         after={"status": "PENDING_REVIEW", "record_count": fiches},
                         detail="Import resté en PROCESSING (défaut de session corrigé)"))
    db.commit()
    return sid


def test_un_lot_promu_sans_preuve_ne_s_homologue_pas(db):
    from fiskr.api import _approve_one_snapshot
    complet = _instantane(db, 0, statut="PENDING_REVIEW", jours=2)
    db.get(Snapshot, complet).record_count = 720_359
    db.commit()
    tronque = _promu_sans_preuve(db, 297_000, "PENDING_REVIEW", jours=1)
    with pytest.raises(HTTPException) as refus:
        _approve_one_snapshot(db, tronque, {"username": "relecteur"}, None)
    assert refus.value.status_code == 400
    assert "297 000 fiches" in refus.value.detail
    assert "720 359 fiches" in refus.value.detail
    assert db.get(Snapshot, tronque).status == "PENDING_REVIEW"


def test_la_file_d_homologation_le_dit(db):
    from fiskr.api import list_pending_reviews
    import asyncio
    tronque = _promu_sans_preuve(db, 61_000, "PENDING_REVIEW")
    complet = _instantane(db, 0, statut="PENDING_REVIEW")
    lignes = {l["snapshot_id"]: l for l in asyncio.run(
        list_pending_reviews(db=db, current_user={"username": "x"}))["pending"]}
    assert lignes[tronque]["import_interrompu"]["fiches"] == 61_000
    assert lignes[complet]["import_interrompu"] is None


def test_la_mise_en_service_bloque_sur_une_liste_en_production_tronquee(db):
    """
    L'état de la production aujourd'hui : la liste PEP en production est un
    import interrompu à 548 000 fiches, quand la version complète voisine en
    compte 720 359. Rien à l'écran ne le disait.
    """
    from fiskr import mise_en_service
    complet = _instantane(db, 0, statut="PENDING_REVIEW", jours=0)
    db.get(Snapshot, complet).record_count = 720_359
    db.commit()
    assert mise_en_service._listes_issues_d_imports_interrompus(db)["etat"] == mise_en_service.OK
    _promu_sans_preuve(db, 548_000, "READY", jours=80)
    controle = mise_en_service._listes_issues_d_imports_interrompus(db)
    assert controle["etat"] == mise_en_service.BLOQUANT
    assert "WATCHLIST_PEP : 548 000 fiches" in controle["constat"]
    assert "720 359" in controle["constat"]
    assert controle["cle"] == "listes_completes"


def test_la_sonde_de_conformite_rejoue_ce_controle():
    from fiskr import mise_en_service
    assert (mise_en_service._listes_issues_d_imports_interrompus
            in mise_en_service._CONTROLES_DE_LA_SONDE)


# --------------------------------------------------------------------------
# Où le fil était bloqué
# --------------------------------------------------------------------------

def test_un_travail_fige_dit_ou_son_fil_est_bloque(db):
    """
    Déclarer un travail figé sans dire où, c'est le relancer pour qu'il se
    fige au même endroit. La pile du fil bloqué part sur la ligne du travail,
    que le diagnostic sert en lecture seule.
    """
    tenue = threading.Event()
    nom = f"ii-{UID}-bloque"

    def _attend_un_appel_qui_ne_rend_pas_la_main(ctx):
        tenue.wait(30)

    jobs.TASKS[nom] = _attend_un_appel_qui_ne_rend_pas_la_main
    job = Job(token=nom, kind=nom, status="QUEUED", phase="QUEUED")
    db.add(job)
    db.commit()
    fil = threading.Thread(target=jobs.run_job, args=(job.id,), daemon=True)
    try:
        fil.start()
        limite = time.monotonic() + 10
        while job.id not in jobs._FILS_DES_JOBS and time.monotonic() < limite:
            time.sleep(0.02)
        db.rollback()  # relire la base telle que le fil l'a laissee
        figes = jobs.declarer_figes(db, jobs.worker_id(),
                                    maintenant=datetime.utcnow() + timedelta(hours=7))
        assert [j.id for j in figes] == [job.id]
        db.refresh(job)
        assert "Fil bloqué dans : " in job.error
        assert "_attend_un_appel_qui_ne_rend_pas_la_main" in job.error
    finally:
        tenue.set()
        fil.join(10)
        jobs.TASKS.pop(nom, None)
    db.refresh(job)
    assert job.status == "ERROR", "le fil réveillé ne réécrit pas l'histoire"
    assert job.id not in jobs._FILS_DES_JOBS


def test_sans_fil_dans_ce_processus_pas_de_pile_inventee():
    assert jobs.pile_du_job(-1) is None


# --------------------------------------------------------------------------
# Une source fermée le dit
# --------------------------------------------------------------------------

def test_la_liste_ofsi_fermee_le_dit_au_lieu_d_echouer_sur_un_409(isolee, monkeypatch):
    """
    Relevé en production chaque nuit : « Echec: Client error '409 Public access
    is not permitted on this storage account' ». La liste consolidée OFSI a
    fermé le 28 janvier 2026 ; la synchronisation le dit, sans réseau, et
    nomme la source qui la remplace.
    """
    appels = []
    monkeypatch.setattr(sync_mod, "download_to_file", lambda *a, **k: appels.append(a))
    rapport = sync_mod.run_ofsi_sync(isolee, trigger="SCHEDULED")
    assert rapport.status == "ERROR"
    assert "28 janvier 2026" in rapport.message and "GBFCDO" in rapport.message
    assert appels == [], "aucune requête vers un stockage fermé"

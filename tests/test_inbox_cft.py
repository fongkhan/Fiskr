"""
Inbox CFT surveillée : un fichier client déposé devient une campagne criblée.

C'est le point d'intégration avec le moniteur de transfert : CFT dépose un CSV
dans `batch.inbox_dir`, Fiskr le repère, l'archive et le crible avec les MÊMES
garanties que le criblage unitaire (quality gate, journal d'audit immuable,
alertes).

Ces tests parcourent la chaîne complète — dépôt du fichier, scrutation,
archivage, campagne, criblage, alerte — sur un listé réel du référentiel de
test. Ils verrouillent notamment :

- qu'un listé déposé par CFT ouvre bien une alerte (le contraire serait un
  blanchiment silencieux, exactement ce qui est arrivé aux processus web dont
  le cache moteur n'était jamais chargé) ;
- qu'un fichier encore en cours de transfert n'est PAS ramassé à moitié ;
- que le fichier traité est archivé, donc jamais criblé deux fois.
"""
import uuid
from pathlib import Path

import pytest

import fiskr.api as api
import fiskr.settings as settings_mod
from fiskr.api import load_watchlist_cache
from fiskr.database import (get_db, Alert, AlertEvent, AuditTrail, BatchCampaign,
                            BatchResult, Snapshot, WatchlistEntity)

UID = uuid.uuid4().hex[:8].upper()
SNAP = f"test-cft-{UID.lower()}"
LISTE = f"Dimitri Volkoning-{UID}"


@pytest.fixture()
def inbox(tmp_path, monkeypatch):
    """Un répertoire de dépôt CFT, et un référentiel qui contient un listé."""
    depot = tmp_path / "cft_in"
    archive = tmp_path / "cft_archive"
    depot.mkdir()
    monkeypatch.setattr(settings_mod, "batch_inbox_settings", lambda: {
        "inbox_dir": str(depot), "archive_dir": str(archive),
        "inbox_poll_seconds": 60,
    })

    db = next(get_db())
    db.add(Snapshot(snapshot_id=SNAP, file_type="WATCHLIST_DGT",
                    file_name=f"{SNAP}.json", file_hash=uuid.uuid4().hex,
                    record_count=1, status="READY"))
    db.add(WatchlistEntity(
        snapshot_id=SNAP, entity_id=f"CFT1-{UID}", entity_type="I",
        primary_name=LISTE,
        individual_name_parsed={"first_name": "Dimitri",
                                "last_name": f"Volkoning-{UID}", "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=["1975-03-14"], is_deceased=False,
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=f"chk-cft-{UID}",
    ))
    db.commit()
    load_watchlist_cache(db)

    yield {"db": db, "depot": depot, "archive": archive}

    try:
        ids = [a.id for a in db.query(Alert).filter(
            Alert.client_id.like(f"CFT-{UID}%")).all()]
        if ids:
            db.query(AlertEvent).filter(
                AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(
            AuditTrail.client_id.like(f"CFT-{UID}%")).delete(synchronize_session=False)
        camps = [c.id for c in db.query(BatchCampaign).filter(
            BatchCampaign.file_name.like(f"%{UID}%")).all()]
        if camps:
            db.query(BatchResult).filter(
                BatchResult.campaign_id.in_(camps)).delete(synchronize_session=False)
            db.query(BatchCampaign).filter(
                BatchCampaign.id.in_(camps)).delete(synchronize_session=False)
        db.query(WatchlistEntity).filter(
            WatchlistEntity.snapshot_id == SNAP).delete(synchronize_session=False)
        db.query(Snapshot).filter(
            Snapshot.snapshot_id == SNAP).delete(synchronize_session=False)
        db.commit()
        load_watchlist_cache(db)
    finally:
        db.close()


def _deposer(depot: Path, nom: str) -> Path:
    """Ce que CFT pose : un CSV de clients, un listé et un innocent."""
    fichier = depot / nom
    fichier.write_text(
        "client_id,client_type,first_name,last_name,dob,nationality\n"
        f"CFT-{UID}-1,PP,Dimitri,Volkoning-{UID},1975-03-14,RU\n"
        f"CFT-{UID}-2,PP,Camille,Durandel-{UID},1988-09-02,FR\n",
        encoding="utf-8")
    # Le poller ignore les fichiers de moins de 5 s (transfert en cours) :
    # on antidate pour simuler un dépôt terminé.
    import os
    import time
    vieux = time.time() - 60
    os.utime(fichier, (vieux, vieux))
    return fichier


def test_deposited_file_is_screened_and_raises_an_alert(inbox):
    """La chaîne complète : dépôt → campagne → criblage → alerte sur le listé."""
    db = inbox["db"]
    _deposer(inbox["depot"], f"clients_{UID}.csv")

    lancees = api._process_inbox_once()
    assert lancees == 1, "le fichier déposé n'a pas donné de campagne"

    campagne = db.query(BatchCampaign).filter(
        BatchCampaign.file_name == f"clients_{UID}.csv").first()
    assert campagne is not None
    assert campagne.total_clients == 2
    assert campagne.status == "DONE", campagne.error_message

    # Le listé doit être trouvé — c'est tout l'objet du dispositif
    assert campagne.alert_count >= 1, (
        "le listé déposé par CFT n'a levé aucune alerte : criblage à vide")
    resultats = db.query(BatchResult).filter(
        BatchResult.campaign_id == campagne.id).all()
    listee = next(r for r in resultats if r.client_id == f"CFT-{UID}-1")
    assert listee.status == "ALERT", f"statut rendu : {listee.status}"

    # …et l'innocent ne doit PAS l'être
    innocent = next(r for r in resultats if r.client_id == f"CFT-{UID}-2")
    assert innocent.status == "NO_MATCH"

    # Une alerte de travail existe, rattachée à une décision d'audit
    alerte = db.query(Alert).filter(Alert.client_id == f"CFT-{UID}-1").first()
    assert alerte is not None and alerte.audit_id


def test_screening_decision_is_recorded_with_the_real_list_hash(inbox):
    """La décision doit porter le hash du référentiel, jamais « N/A » : c'est
    la signature d'un criblage rendu sans listes en mémoire."""
    db = inbox["db"]
    _deposer(inbox["depot"], f"clients_{UID}.csv")
    api._process_inbox_once()

    decisions = db.query(AuditTrail).filter(
        AuditTrail.client_id.like(f"CFT-{UID}%")).all()
    assert decisions, "aucune décision journalisée"
    for d in decisions:
        assert d.watchlist_hash and d.watchlist_hash != "N/A", (
            f"décision rendue sur un cache vide : {d.client_name}")


def test_file_still_transferring_is_left_alone(inbox):
    """Un fichier écrit à l'instant est peut-être encore en cours de transfert :
    le cribler à moitié produirait une campagne tronquée, silencieusement."""
    fichier = inbox["depot"] / f"en_cours_{UID}.csv"
    fichier.write_text("client_id,client_type,first_name,last_name\n"
                       f"CFT-{UID}-9,PP,Jean,Neuf-{UID}\n", encoding="utf-8")
    # pas d'antidatage : mtime = maintenant
    assert api._process_inbox_once() == 0
    assert fichier.exists(), "le fichier en transfert a été consommé"


def test_processed_file_is_archived_and_never_screened_twice(inbox):
    """Le fichier traité quitte le dépôt : une seconde passe ne doit rien
    relancer (sinon doublons d'alertes à chaque scrutation)."""
    _deposer(inbox["depot"], f"clients_{UID}.csv")
    assert api._process_inbox_once() == 1
    assert not (inbox["depot"] / f"clients_{UID}.csv").exists()
    archives = list(inbox["archive"].glob(f"*clients_{UID}.csv"))
    assert len(archives) == 1, "le fichier traité doit être archivé"
    assert api._process_inbox_once() == 0, "seconde passe : rien à refaire"

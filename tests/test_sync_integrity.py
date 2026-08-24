"""
Intégrité des synchronisations de sources officielles.

Ces tests couvrent une panne de production : `persist_pivot_items` appelait
`db.expunge_all()` à chaque commit périodique, détachant **tous** les objets de
la session — dont le `Snapshot` en cours et le snapshot précédent que
l'appelant garde en main pendant toute la boucle. Conséquences observées :

- lecture de `previous.snapshot_id` après la boucle → `DetachedInstanceError`
  (« Instance <Snapshot> is not bound to a Session ») : la synchronisation UN
  échouait alors que les fiches étaient importées ;
- écriture de `snap.status` / `snap.record_count` sur un objet détaché : le
  commit ne persistait RIEN, le snapshot restait `PROCESSING` avec 0 fiche et
  la liste n'entrait jamais en production — **sans erreur visible**.

Déclencheur : plus de `commit_every` fiches ET un snapshot précédent, donc
toute installation en service depuis un moment.
"""
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import Base, Snapshot, WatchlistEntity
from fiskr import sync as sync_mod
from fiskr.sync import persist_pivot_items, run_dgt_sync, run_un_sync, run_ofac_sync

# Au-dela du commit_every de persist_pivot_items : la boucle commite (et
# expulsait) au moins deux fois
BULK = 2_500


@pytest.fixture
def db(tmp_path):
    """Session isolée (SQLite temporaire) : la base de dev n'est jamais touchée."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sync_integrity.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _seed_previous(db, file_type, count=3):
    """Snapshot READY antérieur : c'est lui que l'appelant garde en main."""
    snap_id = f"prev-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=snap_id, file_type=file_type,
                    file_name=f"previous_{file_type}.xml", file_hash=uuid.uuid4().hex,
                    record_count=count, status="READY"))
    for i in range(count):
        db.add(WatchlistEntity(snapshot_id=snap_id, entity_id=f"OLD-{i}",
                               entity_type="I", primary_name=f"ANCIEN {i}",
                               entity_checksum=f"chk-old-{i}"))
    db.commit()
    return snap_id


def _pivot_items(count, prefix="BULK"):
    """Fiches pivots valides pour le quality gate (nom exploitable)."""
    return [
        {"entity_id": f"{prefix}-{i}", "entity_type": "I",
         "primary_name": f"Boris Massifov{i}", "nationality": "RU"}
        for i in range(count)
    ]


# ------------------ CORRECTIF RACINE ------------------

def test_persist_keeps_caller_objects_attached(db):
    """Le helper borne sa mémoire SANS détacher les objets de l'appelant."""
    snap_id = f"snap-{uuid.uuid4().hex[:8]}"
    snap = Snapshot(snapshot_id=snap_id, file_type="WATCHLIST_UN",
                    file_name="bulk.xml", file_hash=uuid.uuid4().hex,
                    record_count=0, status="PROCESSING")
    db.add(snap)
    db.commit()
    previous_id = _seed_previous(db, "WATCHLIST_UN")
    previous = db.query(Snapshot).filter(Snapshot.snapshot_id == previous_id).first()

    count = persist_pivot_items(db, snap_id, iter(_pivot_items(BULK)))
    assert count == BULK

    # Les deux Snapshot de l'appelant restent lisibles : c'est exactement ce
    # qui manquait (DetachedInstanceError sur previous.snapshot_id)
    assert snap.snapshot_id == snap_id
    assert previous.snapshot_id == previous_id

    # …et les écritures sur le snapshot sont bien persistées
    snap.status = "READY"
    snap.record_count = count
    db.commit()
    db.expire_all()
    reloaded = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
    assert reloaded.status == "READY"
    assert reloaded.record_count == BULK


def test_persist_bounds_memory(db):
    """Le bénéfice mémoire est conservé : les entités ne s'accumulent pas."""
    snap_id = f"snap-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=snap_id, file_type="WATCHLIST_UN",
                    file_name="bulk.xml", file_hash=uuid.uuid4().hex,
                    record_count=0, status="PROCESSING"))
    db.commit()

    persist_pivot_items(db, snap_id, iter(_pivot_items(BULK)))
    entities_in_memory = [o for o in db.identity_map.values()
                          if isinstance(o, WatchlistEntity)]
    # Au plus le dernier lot partiel : jamais les 2 500 fiches
    assert len(entities_in_memory) < BULK


# ------------------ LES QUATRE SYNCHRONISATIONS ------------------

def _local_fetcher(payload: str):
    """Fetcher local : écrit le contenu attendu, sans réseau."""
    def _fetch(url, dest_path):
        dest_path.write_text(payload, encoding="utf-8")
    return _fetch


def test_generic_sync_survives_periodic_commits(db, monkeypatch):
    """Cycle générique (UN, FSF, PEP, OFSI) : c'est la panne UN signalée."""
    previous_id = _seed_previous(db, "WATCHLIST_UN")
    monkeypatch.setattr(sync_mod, "parse_un_consolidated_xml",
                        lambda path: iter(_pivot_items(BULK, "UN")))

    report = run_un_sync(db, trigger="MANUAL", fetcher=_local_fetcher("<xml/>"))

    assert report.status == "SUCCESS", report.message
    assert report.previous_snapshot_id == previous_id
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.status == "READY"
    assert snap.record_count == BULK


def test_dgt_sync_persists_status_and_count(db, monkeypatch):
    """DGT échouait en SILENCE : snapshot laissé PROCESSING avec 0 fiche."""
    previous_id = _seed_previous(db, "WATCHLIST_DGT")
    monkeypatch.setattr(sync_mod, "parse_dgt_gels_json",
                        lambda path: iter(_pivot_items(BULK, "DGT")))

    report = run_dgt_sync(db, trigger="MANUAL", fetcher=_local_fetcher("{}"))

    assert report.status == "SUCCESS", report.message
    assert report.previous_snapshot_id == previous_id
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.status == "READY"
    assert snap.record_count == BULK
    assert db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == report.snapshot_id).count() == BULK


def test_ofac_sync_persists_status_and_count(db, monkeypatch):
    """OFAC : même défaut silencieux que DGT."""
    previous_id = _seed_previous(db, "WATCHLIST_OFAC")
    monkeypatch.setattr(sync_mod, "parse_ofac_advanced_xml",
                        lambda path, relations_out=None: iter(_pivot_items(BULK, "OFAC")))

    report = run_ofac_sync(db, trigger="MANUAL", fetcher=_local_fetcher("<xml/>"))

    assert report.status == "SUCCESS", report.message
    assert report.previous_snapshot_id == previous_id
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.status == "READY"
    assert snap.record_count == BULK


def test_no_sync_leaves_snapshot_stuck_in_processing(db, monkeypatch):
    """Garde-fou général : aucune source ne laisse un snapshot à 0 fiche."""
    _seed_previous(db, "WATCHLIST_UN")
    monkeypatch.setattr(sync_mod, "parse_un_consolidated_xml",
                        lambda path: iter(_pivot_items(BULK, "UN")))
    run_un_sync(db, trigger="MANUAL", fetcher=_local_fetcher("<xml/>"))

    stuck = db.query(Snapshot).filter(Snapshot.status == "PROCESSING").all()
    assert not stuck, [s.snapshot_id for s in stuck]


# ------------------ REPARATION DE L'HERITAGE ------------------

def _stuck_snapshot(db, file_type, entities, age_hours=3):
    """Snapshot laissé en PROCESSING par le défaut de session."""
    snap_id = f"stuck-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=snap_id, file_type=file_type,
                    file_name=f"stuck_{file_type}.xml", file_hash=uuid.uuid4().hex,
                    record_count=0, status="PROCESSING",
                    uploaded_at=datetime.utcnow() - timedelta(hours=age_hours)))
    for i in range(entities):
        db.add(WatchlistEntity(snapshot_id=snap_id, entity_id=f"STK-{i}",
                               entity_type="I", primary_name=f"BLOQUE {i}",
                               entity_checksum=f"chk-stk-{uuid.uuid4().hex[:8]}"))
    db.commit()
    return snap_id


def test_repair_promotes_snapshot_that_really_has_entities(db):
    from fiskr.api import _repair_stuck_snapshots
    from fiskr.database import AdminAuditLog

    snap_id = _stuck_snapshot(db, "WATCHLIST_UN", entities=42)
    summary = _repair_stuck_snapshots(db)

    assert summary["repaired"] == 1
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
    assert snap.status in ("READY", "PENDING_REVIEW")
    assert snap.record_count == 42  # recompté depuis les fiches réelles
    # Correction tracée : elle doit être auditable
    log = db.query(AdminAuditLog).filter(
        AdminAuditLog.action == "SNAPSHOT_REPAIRED",
        AdminAuditLog.target == snap_id).first()
    assert log is not None
    assert log.before["record_count"] == 0
    assert log.after["record_count"] == 42


def test_repair_marks_empty_snapshot_as_error(db):
    from fiskr.api import _repair_stuck_snapshots

    snap_id = _stuck_snapshot(db, "WATCHLIST_UN", entities=0)
    summary = _repair_stuck_snapshots(db)

    assert summary["failed"] == 1
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
    assert snap.status == "ERROR"


def test_repair_leaves_recent_imports_alone(db):
    """Un import en cours ne doit surtout pas être 'réparé' sous ses pieds."""
    from fiskr.api import _repair_stuck_snapshots

    snap_id = _stuck_snapshot(db, "WATCHLIST_UN", entities=10, age_hours=0)
    summary = _repair_stuck_snapshots(db)

    assert summary == {"repaired": 0, "failed": 0}
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).first()
    assert snap.status == "PROCESSING"


def test_repair_never_raises(monkeypatch):
    """Un démarrage ne tombe jamais sur la réparation."""
    from fiskr.api import _repair_stuck_snapshots

    class _BrokenSession:
        def query(self, *a, **k):
            raise RuntimeError("base injoignable")

        def rollback(self):
            pass

    assert _repair_stuck_snapshots(_BrokenSession()) == {"repaired": 0, "failed": 0}


# ------------------ PROGRESSION DES QUATRE SOURCES ------------------

def _phases_seen(monkeypatch):
    """Espionne les phases publiées sur le jeton sync:<source>."""
    from fiskr import progress as registry
    seen = []
    real = registry.update

    def _spy(token, **kwargs):
        if token and str(token).startswith("sync:"):
            seen.append((token, kwargs.get("phase")))
        return real(token, **kwargs)

    monkeypatch.setattr(sync_mod.SyncProgress, "__init__", sync_mod.SyncProgress.__init__)
    monkeypatch.setattr(registry, "update", _spy)
    return seen


def test_ofac_publishes_progress_phases(db, monkeypatch):
    """OFAC ne publiait AUCUNE phase : barre indéterminée dans le tableau."""
    seen = _phases_seen(monkeypatch)
    monkeypatch.setattr(sync_mod, "parse_ofac_advanced_xml",
                        lambda path, relations_out=None: iter(_pivot_items(BULK, "OFAC")))
    run_ofac_sync(db, trigger="MANUAL", fetcher=_local_fetcher("<xml/>"))

    phases = [p for t, p in seen if t == "sync:ofac"]
    assert "HASH" in phases
    assert "PERSIST" in phases   # compteur de fiches pendant l'import
    assert "DELTA" in phases


def test_dgt_publishes_progress_phases(db, monkeypatch):
    seen = _phases_seen(monkeypatch)
    monkeypatch.setattr(sync_mod, "parse_dgt_gels_json",
                        lambda path: iter(_pivot_items(BULK, "DGT")))
    run_dgt_sync(db, trigger="MANUAL", fetcher=_local_fetcher("{}"))

    phases = [p for t, p in seen if t == "sync:dgt"]
    assert "HASH" in phases and "PERSIST" in phases and "DELTA" in phases


def test_persist_progress_is_recorded_on_snapshot(db, monkeypatch):
    """La progression survit à un redémarrage : elle est aussi persistée."""
    monkeypatch.setattr(sync_mod, "parse_un_consolidated_xml",
                        lambda path: iter(_pivot_items(BULK, "UN")))
    report = run_un_sync(db, trigger="MANUAL", fetcher=_local_fetcher("<xml/>"))
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == report.snapshot_id).first()
    assert snap.processed_count == BULK


# ------------------ BUDGET RESEAU PAR SOURCE (EUR-Lex) ------------------

def test_eurlex_gets_a_more_patient_retry_budget():
    """4 tentatives sur 18 s ne franchissaient pas l'interstitiel HTTP 202."""
    from fiskr.sync import network_for_url

    eurlex = network_for_url("https://eur-lex.europa.eu/oj/daily-view/L-series/default.html")
    other = network_for_url("https://sanctionslistservice.ofac.treas.gov/api/x.XML")
    assert eurlex["retries"] > other["retries"]
    assert eurlex["backoff_seconds"] > other["backoff_seconds"]


def test_source_network_override_from_config(monkeypatch):
    """L'exploitant garde la main : sync.<source>.network surcharge tout."""
    from fiskr.config import config
    from fiskr.sync import source_network_config

    sync_cfg = dict(config.get("sync") or {})
    sync_cfg["eurlex"] = {**(sync_cfg.get("eurlex") or {}), "network": {"retries": 42}}
    monkeypatch.setitem(config, "sync", sync_cfg)
    assert source_network_config("eurlex")["retries"] == 42


def test_persistent_202_avec_corps_est_une_page_d_attente():
    """
    Un 202 qui a un CORPS, c'est « la page se prépare » : attendre davantage
    peut aboutir, et c'est bien ce que le message doit conseiller.
    """
    from fiskr.sync import _with_retries, _RetryableHTTP

    def _toujours_202():
        raise _RetryableHTTP("HTTP 202")

    with pytest.raises(RuntimeError) as exc:
        _with_retries(_toujours_202, "https://eur-lex.europa.eu/x", retries=0, backoff=0)
    message = str(exc.value)
    assert "page d'attente" in message
    assert "retries" in message


def test_persistent_202_a_corps_vide_est_un_portique():
    """
    Un 202 à corps VIDE, c'est un refus. Mesuré sur le portail EUR-Lex depuis
    deux réseaux : corps vide sur la page du jour ET sur la racine, aucun
    cookie, aucun Retry-After. Conseiller d'augmenter le nombre de reprises
    envoyait l'exploitant sur une route sans issue — le message doit dire ce
    qui a été constaté, et ce qui change vraiment quelque chose.
    """
    from fiskr.sync import _with_retries, _RetryableHTTP

    def _portique():
        raise _RetryableHTTP("HTTP 202 (corps vide)", porte_close=True)

    with pytest.raises(RuntimeError) as exc:
        _with_retries(_portique, "https://eur-lex.europa.eu/x", retries=0, backoff=0)
    message = str(exc.value)
    assert "CORPS VIDE" in message
    assert "retries" not in message, "ce conseil-là ne peut rien donner ici"


def test_le_portique_ne_consomme_pas_tout_le_budget():
    """
    Sept tentatives sur un portail qui refuse, c'est presque deux minutes de
    créneau de travail par jour pour un résultat connu d'avance. Trois refus
    identiques suffisent à conclure.
    """
    from fiskr.sync import _with_retries, _RetryableHTTP

    appels = []

    def _portique():
        appels.append(1)
        raise _RetryableHTTP("HTTP 202 (corps vide)", porte_close=True)

    with pytest.raises(RuntimeError):
        _with_retries(_portique, "https://eur-lex.europa.eu/x", retries=6, backoff=0)
    assert len(appels) == 3, f"budget dépensé : {len(appels)} tentatives sur 7"


def test_un_202_avec_corps_garde_tout_son_budget():
    """La coupure ne doit mordre que sur le refus, jamais sur l'attente."""
    from fiskr.sync import _with_retries, _RetryableHTTP

    appels = []

    def _attente():
        appels.append(1)
        raise _RetryableHTTP("HTTP 202")

    with pytest.raises(RuntimeError):
        _with_retries(_attente, "https://eur-lex.europa.eu/x", retries=6, backoff=0)
    assert len(appels) == 7


def test_le_prechauffage_rapporte_ce_qu_il_a_obtenu(monkeypatch):
    """
    Un préchauffage muet est un rite : il coûte une requête par synchronisation
    et personne ne sait s'il sert. Il doit RAPPORTER — statut et cookies — pour
    qu'un échec ultérieur soit diagnosticable.
    """
    from fiskr import sync as m

    class _Reponse:
        status_code = 202

    class _Portail:
        cookies = {}

        def get(self, *a, **k):
            return _Reponse()

    monkeypatch.setattr(m, "_get_shared_client", lambda: _Portail())
    constat = m.warm_up_session("https://eur-lex.europa.eu/")
    assert constat["statut"] == 202
    assert constat["cookies"] == 0
    assert constat["erreur"] is None


def test_l_echec_eurlex_dit_que_le_portail_n_a_pas_ouvert_de_session(monkeypatch):
    """
    Savoir que la racine du portail n'a même pas donné de session, c'est la
    moitié du diagnostic : cela distingue « portail lent » de « requête
    refusée à la porte ».
    """
    from datetime import date

    from fiskr import sync as m

    monkeypatch.setattr(m, "warm_up_session",
                        lambda url: {"url": url, "statut": 202, "cookies": 0, "erreur": None})

    def _refus(url):
        raise RuntimeError("Echec apres 3 tentatives")

    with pytest.raises(RuntimeError) as exc:
        m.fetch_eurlex_acts(date(2026, 8, 24), _refus,
                            "https://eur-lex.europa.eu/oj/{date}", "mesures restrictives")
    assert "prechauffage" in str(exc.value)
    assert "n'a pas ouvert de session" in str(exc.value)


def test_le_prechauffage_reussi_ne_pollue_pas_l_erreur(monkeypatch):
    """Si le portail a bien ouvert une session, l'échec vient d'ailleurs : le
    message ne doit pas accuser le préchauffage."""
    from datetime import date

    from fiskr import sync as m

    monkeypatch.setattr(m, "warm_up_session",
                        lambda url: {"url": url, "statut": 200, "cookies": 2, "erreur": None})

    def _refus(url):
        raise RuntimeError("lecture impossible")

    with pytest.raises(RuntimeError) as exc:
        m.fetch_eurlex_acts(date(2026, 8, 24), _refus,
                            "https://eur-lex.europa.eu/oj/{date}", "mesures restrictives")
    assert "prechauffage" not in str(exc.value)


def test_warm_up_never_breaks_the_sync(monkeypatch):
    """Un préchauffage en échec ne doit jamais interrompre la synchronisation."""
    from fiskr import sync as m

    class _BrokenClient:
        def get(self, *a, **k):
            raise RuntimeError("portail injoignable")

    monkeypatch.setattr(m, "_get_shared_client", lambda: _BrokenClient())
    m.warm_up_session("https://eur-lex.europa.eu/")  # ne lève pas

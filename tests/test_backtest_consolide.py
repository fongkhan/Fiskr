"""
Un seul cahier de tests pour toute une vague de synchronisations.

« Synchroniser les sources activées » déposait un snapshot par source, donc un
cahier de tests par source. Or les cahiers sont sérialisés (`SERIAL_KINDS`) :
la file s'allongeait de plusieurs dizaines de minutes — constaté en production,
six cahiers en attente dont le plus ancien depuis 57 minutes — pour un travail
largement redondant, chacun recriblant le MÊME univers partagé.

Un cahier consolidé couvre tous les deltas en une fois. Ces tests verrouillent
les deux propriétés qui rendent le regroupement acceptable :

- **l'équivalence** : le périmètre candidat d'un cahier consolidé est celui
  qu'une approbation de toutes les listes produirait — ni plus, ni moins ;
- **le regroupement** : une vague de N synchronisations ne dépose qu'UN job, et
  ce job couvre aussi les synchronisations terminées après sa soumission.
"""
import uuid

import pytest

from fiskr import jobs as job_queue
from fiskr import tasks as task_mod
from fiskr.backtest import (_old_ids_by_type, _universe_snapshot_ids,
                            normalize_pending)
from fiskr.database import get_db, Job, Snapshot, WatchlistEntity

UID = uuid.uuid4().hex[:8].upper()


def _snapshot(db, suffixe, file_type, status, fiches=1, uploaded=None):
    sid = f"test-cons-{suffixe}-{UID.lower()}"
    snap = Snapshot(snapshot_id=sid, file_type=file_type,
                    file_name=f"{sid}.json", file_hash=uuid.uuid4().hex,
                    record_count=fiches, status=status)
    if uploaded:
        snap.uploaded_at = uploaded
    db.add(snap)
    for n in range(fiches):
        db.add(WatchlistEntity(
            snapshot_id=sid, entity_id=f"E-{suffixe}-{n}-{UID}", entity_type="I",
            primary_name=f"Personne {suffixe} {n} {UID}",
            aliases={"high_priority": [], "low_priority": []},
            dates_of_birth=[], is_deceased=False,
            countries={"citizenship": [], "residence": [], "birth_country": [],
                       "jurisdiction_country": []},
            entity_checksum=f"chk-{suffixe}-{n}-{UID}",
        ))
    return snap


@pytest.fixture()
def db():
    session = next(get_db())
    yield session
    session.query(WatchlistEntity).filter(
        WatchlistEntity.entity_id.like(f"%{UID}")).delete(synchronize_session=False)
    session.query(Snapshot).filter(
        Snapshot.snapshot_id.like(f"test-cons-%{UID.lower()}")).delete(
            synchronize_session=False)
    session.query(Job).filter(
        Job.token == task_mod.CONSOLIDATED_BACKTEST_TOKEN).delete(
            synchronize_session=False)
    session.commit()
    session.close()


# ------------------ ÉQUIVALENCE DU PÉRIMÈTRE ------------------

def test_candidate_universe_replaces_every_tested_list(db):
    """Le périmètre candidat doit être le miroir exact d'une approbation de
    TOUTES les listes testées : chaque type testé voit sa production remplacée,
    les autres types restent."""
    prod_a = _snapshot(db, "proda", "WATCHLIST_DGT", "READY")
    prod_b = _snapshot(db, "prodb", "WATCHLIST_UN", "READY")
    intact = _snapshot(db, "intact", "WATCHLIST_OFSI", "READY")
    cand_a = _snapshot(db, "canda", "WATCHLIST_DGT", "PENDING_REVIEW")
    cand_b = _snapshot(db, "candb", "WATCHLIST_UN", "PENDING_REVIEW")
    db.commit()

    courant, candidat = _universe_snapshot_ids(db, [cand_a, cand_b])

    assert prod_a.snapshot_id in courant and prod_b.snapshot_id in courant
    # Les deux productions testées sortent, les deux candidats entrent
    assert prod_a.snapshot_id not in candidat
    assert prod_b.snapshot_id not in candidat
    assert cand_a.snapshot_id in candidat and cand_b.snapshot_id in candidat
    # La liste non testée reste des deux côtés
    assert intact.snapshot_id in courant and intact.snapshot_id in candidat


def test_delta_is_computed_list_by_list(db):
    """Les anciens snapshots sont regroupés PAR TYPE : mélanger les
    identifiants d'entités de deux listes ferait passer une fiche pour
    supprimée au seul motif qu'elle n'existe pas dans l'autre liste."""
    prod_a = _snapshot(db, "pa", "WATCHLIST_DGT", "READY")
    prod_b = _snapshot(db, "pb", "WATCHLIST_UN", "READY")
    cand_a = _snapshot(db, "ca", "WATCHLIST_DGT", "PENDING_REVIEW")
    cand_b = _snapshot(db, "cb", "WATCHLIST_UN", "PENDING_REVIEW")
    db.commit()

    anciens = _old_ids_by_type(db, [cand_a, cand_b])
    assert anciens["WATCHLIST_DGT"] == [prod_a.snapshot_id]
    assert anciens["WATCHLIST_UN"] == [prod_b.snapshot_id]


def test_two_candidates_of_one_list_keep_only_the_latest(db):
    """Deux candidats du même type sont deux versions concurrentes d'une même
    liste : l'univers ne peut pas contenir les deux sans compter deux fois."""
    from datetime import datetime
    vieux = _snapshot(db, "v1", "WATCHLIST_DGT", "PENDING_REVIEW",
                      uploaded=datetime(2026, 1, 1))
    recent = _snapshot(db, "v2", "WATCHLIST_DGT", "PENDING_REVIEW",
                       uploaded=datetime(2026, 6, 1))
    db.commit()

    retenus = normalize_pending([vieux, recent])
    assert [s.snapshot_id for s in retenus] == [recent.snapshot_id]


def test_single_snapshot_still_accepted(db):
    """Le lancement manuel d'un cahier sur UNE liste ne change pas."""
    seul = _snapshot(db, "seul", "WATCHLIST_DGT", "PENDING_REVIEW")
    db.commit()
    assert [s.snapshot_id for s in normalize_pending(seul)] == [seul.snapshot_id]


# ------------------ REGROUPEMENT DE LA VAGUE ------------------

def test_scope_is_resolved_at_execution_not_at_submission(db):
    """LE point du regroupement : le périmètre est relu à l'exécution, donc un
    cahier encore en file couvre les synchronisations terminées APRÈS lui."""
    premier = _snapshot(db, "s1", "WATCHLIST_DGT", "PENDING_REVIEW")
    db.commit()
    assert premier.snapshot_id in {s.snapshot_id for s in
                                   task_mod.pending_backtest_scope(db)}

    # Une synchronisation se termine plus tard : elle entre dans le périmètre
    # sans qu'aucun nouveau cahier n'ait été déposé
    tardif = _snapshot(db, "s2", "WATCHLIST_UN", "PENDING_REVIEW")
    db.commit()
    perimetre = {s.snapshot_id for s in task_mod.pending_backtest_scope(db)}
    assert {premier.snapshot_id, tardif.snapshot_id} <= perimetre


def test_snapshot_already_tested_leaves_the_scope(db):
    """Un snapshot qui porte déjà son rapport ne doit pas être re-testé."""
    snap = _snapshot(db, "fait", "WATCHLIST_DGT", "PENDING_REVIEW")
    db.commit()
    assert snap.snapshot_id in {s.snapshot_id for s in
                                task_mod.pending_backtest_scope(db)}
    snap.backtest_report = {"verdict": "OK"}
    db.commit()
    assert snap.snapshot_id not in {s.snapshot_id for s in
                                    task_mod.pending_backtest_scope(db)}


def test_a_wave_of_syncs_submits_only_one_backtest(db, monkeypatch):
    """Le cœur de la demande : N synchronisations ne doivent déposer qu'UN
    cahier, là où chacune en déposait un."""
    _snapshot(db, "w1", "WATCHLIST_DGT", "PENDING_REVIEW")
    _snapshot(db, "w2", "WATCHLIST_UN", "PENDING_REVIEW")
    _snapshot(db, "w3", "WATCHLIST_OFSI", "PENDING_REVIEW")
    db.commit()

    monkeypatch.setattr(task_mod, "_resolve_auto_backtest_panel",
                        lambda session: "panel-test")
    monkeypatch.setattr("fiskr.settings.auto_backtest_enabled", lambda s: True)

    soumis = []

    def _faux_submit(kind, **kw):
        # Le dédoublonnage de la file : un jeton déjà présent est refusé
        if kw.get("dedupe_key") in soumis:
            raise job_queue.JobConflict("déjà en file")
        soumis.append(kw.get("dedupe_key"))
        return None

    monkeypatch.setattr(job_queue, "submit", _faux_submit)

    class _Rapport:
        status = "PENDING_REVIEW"
        added_count, modified_count, removed_count = 5, 0, 0
        source = "DGT"

    resultats = []
    for sid in ("a", "b", "c"):
        rapport = _Rapport()
        rapport.snapshot_id = f"test-cons-w-{sid}-{UID.lower()}"
        resultats.append(task_mod._maybe_auto_backtest(db, rapport))

    assert len(soumis) == 1, f"{len(soumis)} cahiers déposés au lieu d'un"
    assert soumis == [task_mod.CONSOLIDATED_BACKTEST_TOKEN]
    assert resultats[0]["submitted"] is True
    # Les suivantes ne relancent rien, mais disent à quoi elles se rattachent
    for suivant in resultats[1:]:
        assert suivant["submitted"] is False
        assert suivant["consolidated"] is True
        assert suivant["job_token"] == task_mod.CONSOLIDATED_BACKTEST_TOKEN


# ------------------ BOUT EN BOUT : DEUX LISTES, UN SEUL CAHIER ------------------

def test_one_backtest_covers_the_deltas_of_two_lists(tmp_path):
    """La preuve de bout en bout : deux listes ajoutent chacune un listé qui
    touche un client du panel. UN cahier doit voir LES DEUX nouvelles paires et
    poser son rapport sur les DEUX snapshots."""
    from fastapi.testclient import TestClient

    from fiskr.api import app
    from fiskr.auth import get_current_user
    from fiskr.backtest import reset_shared_pass_memo
    from fiskr.database import ClientEntity

    tag = uuid.uuid4().hex[:6].upper()
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "testeur", "full_name": "Testeur",
        "role": "admin", "roles": ["admin"],
    }
    reset_shared_pass_memo()
    session = next(get_db())
    try:
        with TestClient(app) as c:
            def _ingest(file_type, corps, nom):
                r = c.post("/api/ingest", data={"file_type": file_type},
                           files={"file": (nom, corps, "text/csv")})
                assert r.status_code == 200, r.text
                return r.json()

            # Deux formats différents, comme en production : la liste UE en CSV
            # générique, la seconde au format OpenSanctions targets.simple.csv.
            entete = "entity_id,entity_type,primary_name,nationality,dob\n"
            entete_os = "id,schema,name,aliases,birth_date,countries\n"

            # Production des deux listes (approbation coupée)
            assert c.put("/api/settings/ingestion",
                         json={"require_approval": False}).status_code == 200
            _ingest("WATCHLIST_EU",
                    entete + f"EU-{tag}-1,I,Boris Socleu{tag},RU,1960-05-05\n",
                    f"eu_prod_{tag}.csv")
            _ingest("WATCHLIST_EBRD",
                    entete_os + f"eb-{tag}-1,person,Pierre Soclebrd{tag},,1962-06-06,fr\n",
                    f"ebrd_prod_{tag}.csv")

            # Candidats : chaque liste ajoute un listé (approbation exigée)
            assert c.put("/api/settings/ingestion",
                         json={"require_approval": True}).status_code == 200
            eu = _ingest("WATCHLIST_EU",
                         entete
                         + f"EU-{tag}-1,I,Boris Socleu{tag},RU,1960-05-05\n"
                         + f"EU-{tag}-2,I,Igor Neufeu{tag},RU,1971-02-02\n",
                         f"eu_cand_{tag}.csv")
            ebrd = _ingest("WATCHLIST_EBRD",
                          entete_os
                          + f"eb-{tag}-1,person,Pierre Soclebrd{tag},,1962-06-06,fr\n"
                          + f"eb-{tag}-2,person,Marc Neufebrd{tag},,1975-03-03,fr\n",
                          f"ebrd_cand_{tag}.csv")
            assert eu["status"] == "PENDING_REVIEW" and ebrd["status"] == "PENDING_REVIEW"

            # Panel : un client par nouveauté
            panel = _ingest(
                "CLIENT_BASE",
                "client_id,client_type,client_first_name,client_last_name,"
                "client_dob,client_gender,nationality\n"
                f"CLI-{tag}-A,PP,Igor,Neufeu{tag},1971-02-02,M,RU\n"
                f"CLI-{tag}-B,PP,Marc,Neufdgt{tag},1975-03-03,M,FR\n",
                f"clients_{tag}.csv")

            from fiskr.backtest import run_backtest
            snaps = session.query(Snapshot).filter(Snapshot.snapshot_id.in_(
                [eu["snapshot_id"], ebrd["snapshot_id"]])).all()
            assert len(snaps) == 2
            rapport = run_backtest(session, snaps, panel["snapshot_id"],
                                   threshold_pct=100.0, executed_by="testeur")

        # Le rapport couvre les DEUX listes, chacune avec son propre delta
        couverts = {s["snapshot_id"] for s in rapport["snapshots"]}
        assert couverts == {eu["snapshot_id"], ebrd["snapshot_id"]}, rapport["snapshots"]
        assert {s["file_type"] for s in rapport["snapshots"]} == {
            "WATCHLIST_EU", "WATCHLIST_EBRD"}

        # Les deux nouveautés sont vues par UN seul cahier
        assert rapport["mode"] == "delta"
        assert rapport["delta_sizes"]["added"] == 2, rapport["delta_sizes"]
        assert rapport["new_pairs_count"] >= 2, rapport
        assert rapport["candidate"]["alerts"] > rapport["current"]["alerts"]
    finally:
        app.dependency_overrides.clear()
        reset_shared_pass_memo()
        # Le reglage d'homologation est GLOBAL : le laisser a `true` ferait
        # atterrir les listes des tests suivants en attente au lieu de la
        # production, et leurs criblages ne trouveraient plus rien. Ce nettoyage
        # n'est pas cosmetique — il a coute trois echecs ailleurs dans la suite.
        from fiskr.database import AppSetting
        from fiskr.settings import SETTING_REQUIRE_APPROVAL
        session.query(AppSetting).filter(
            AppSetting.key == SETTING_REQUIRE_APPROVAL).delete(
                synchronize_session=False)
        for modele, colonne in ((WatchlistEntity, WatchlistEntity.entity_id),
                                (ClientEntity, ClientEntity.client_id)):
            session.query(modele).filter(colonne.like(f"%{tag}%")).delete(
                synchronize_session=False)
        session.query(Snapshot).filter(
            Snapshot.file_name.like(f"%{tag}%")).delete(synchronize_session=False)
        session.commit()
        session.close()

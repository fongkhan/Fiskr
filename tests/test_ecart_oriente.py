"""
Cahier de tests : un écart a un SENS.

L'écart se calculait en valeur absolue (fiskr/backtest.py). Relevé en
production : huit faux positifs SUPPRIMÉS grâce à l'ajout des dates de
naissance dans la liste candidate étaient signalés « ÉCART ÉLEVÉ », avec le
conseil « Posez des Good Guys ou des exclusions » — qui aurait creusé l'écart —
et, le cahier de tests étant obligatoire, l'approbation bloquée. Une alerte de
moins n'est pas une interception de moins.

Ce qui reste bloquant, comme avant : une interception PERDUE (fiche retirée,
nom qui ne correspond plus, copie exacte d'un listé qui n'est plus
interceptée), et une hausse des alertes au-delà du seuil.
"""
import random
import string
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.backtest import (TEST_PANEL_FILE_TYPE, generate_test_panel, interception_attendue,
                            reset_shared_pass_memo, run_backtest)
from fiskr.database import (AppSetting, ClientEntity, Snapshot, WatchlistEntity,
                            compute_checksum, get_db)
from fiskr.settings import SETTING_BACKTEST_REQUIRED, set_setting
from tests.conftest import post_and_wait

# Lettres seules : un chiffre dans un nom en change la similarite
TAG = "".join(random.choice(string.ascii_uppercase) for _ in range(6))
LISTE = "WATCHLIST_JAPAN_MOF"
T0 = datetime(2026, 10, 1, 4, 0)
PRENOMS = ("Igor", "Boris", "Pavel", "Oleg", "Yuri", "Petr", "Ivan", "Fedor")


@pytest.fixture()
def db():
    reset_shared_pass_memo()
    session = next(get_db())
    session.crees = []
    yield session
    reset_shared_pass_memo()
    session.rollback()
    if session.crees:
        session.query(WatchlistEntity).filter(
            WatchlistEntity.snapshot_id.in_(session.crees)).delete(synchronize_session=False)
        session.query(ClientEntity).filter(
            ClientEntity.snapshot_id.in_(session.crees)).delete(synchronize_session=False)
        session.query(Snapshot).filter(
            Snapshot.snapshot_id.in_(session.crees)).delete(synchronize_session=False)
    session.query(AppSetting).filter(AppSetting.key == SETTING_BACKTEST_REQUIRED).delete(
        synchronize_session=False)
    session.commit()
    session.close()


def _liste(db, statut, fiches, heures=0):
    """fiches = [(n, prenom, dates_de_naissance)] — un homme, russe."""
    sid = f"eo-{statut.lower()}-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=sid, file_type=LISTE, file_name=f"{sid}.csv",
                    file_hash=uuid.uuid4().hex, record_count=len(fiches), status=statut,
                    uploaded_at=T0 + timedelta(hours=heures)))
    for n, prenom, ddn in fiches:
        valeurs = dict(
            entity_id=f"EO-{TAG}-{n}", entity_type="I",
            primary_name=f"{prenom} KUZNETSOVSKI{TAG}",
            individual_name_parsed={"first_name": prenom, "last_name": f"KUZNETSOVSKI{TAG}",
                                    "maiden_name": ""},
            aliases={"high_priority": [], "low_priority": []}, dates_of_birth=ddn,
            gender="M", countries={"citizenship": ["RU"], "residence": [],
                                   "birth_country": [], "jurisdiction_country": []})
        db.add(WatchlistEntity(snapshot_id=sid, entity_checksum=compute_checksum(valeurs),
                               **valeurs))
    db.commit()
    db.crees.append(sid)
    return db.query(Snapshot).filter(Snapshot.snapshot_id == sid).one()


def _panel(db, clients):
    """clients = [(client_id, prenom, nom, genre, date_de_naissance)]."""
    sid = f"eo-panel-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=sid, file_type=TEST_PANEL_FILE_TYPE, file_name=f"{sid}.gen",
                    file_hash=uuid.uuid4().hex, record_count=len(clients), status="READY"))
    for client_id, prenom, nom, genre, ddn in clients:
        valeurs = dict(client_id=client_id, client_type="PP", client_first_name=prenom,
                       client_last_name=nom, client_dob=ddn, client_gender=genre,
                       client_countries={"nationality": ["RU"], "residence": [],
                                         "birth_country": [], "registration_country": []})
        db.add(ClientEntity(snapshot_id=sid, entity_checksum=compute_checksum(valeurs), **valeurs))
    db.commit()
    db.crees.append(sid)
    return sid


def _homonymes(prefixe="CLI"):
    """Huit clientes homonymes des listés — même nom à une lettre près, née
    en 1990 — que seule une date de naissance distingue des listés nés en 1960."""
    return [(f"{prefixe}-{TAG}-{n}", prenom, f"Kouznetsovski{TAG}", "F", "1990-06-15")
            for n, prenom in enumerate(PRENOMS)]


# ------------------ MOINS DE FAUX POSITIFS ------------------

def test_huit_faux_positifs_en_moins_ne_sont_pas_un_ecart_eleve(db):
    _liste(db, "READY", [(n, p, []) for n, p in enumerate(PRENOMS)])
    candidate = _liste(db, "PENDING_REVIEW",
                       [(n, p, ["1960-01-01"]) for n, p in enumerate(PRENOMS)], heures=1)
    panel = _panel(db, _homonymes())

    rapport = run_backtest(db, candidate, panel, threshold_pct=20.0, executed_by="t")

    assert rapport["current"]["alerts"] == 8 and rapport["candidate"]["alerts"] == 0
    assert rapport["false_positives_removed_count"] == 8
    assert rapport["lost_interceptions_count"] == 0
    assert rapport["net_gap_pct"] == -100.0          # le mouvement, signé, reste visible
    assert rapport["gap_pct"] == 0.0                 # mais rien n'est à corriger
    assert rapport["gap_direction"] == "FEWER_FALSE_POSITIVES"
    assert rapport["verdict"] == "OK"
    assert {p["outcome"] for p in rapport["resolved_pairs"]} == {"FALSE_POSITIVE_REMOVED"}


# ------------------ UNE PERTE D'INTERCEPTION RESTE BLOQUANTE ------------------

def test_une_fiche_retiree_est_une_interception_perdue(db):
    _liste(db, "READY", [(0, "Igor", [])])
    candidate = _liste(db, "PENDING_REVIEW", [], heures=1)   # le listé est radié
    panel = _panel(db, [(f"CLI-{TAG}-0", "Igor", f"KUZNETSOVSKI{TAG}", "M", None)])

    rapport = run_backtest(db, candidate, panel, threshold_pct=20.0, executed_by="t")

    assert rapport["current"]["alerts"] == 1 and rapport["candidate"]["alerts"] == 0
    assert rapport["lost_interceptions_count"] == 1
    assert rapport["loss_pct"] == 100.0
    assert rapport["gap_direction"] == "INTERCEPTION_LOSS"
    assert rapport["verdict"] == "WARN"
    perdue = rapport["resolved_pairs"][0]
    assert perdue["outcome"] == "INTERCEPTION_LOST"
    assert perdue["outcome_reason"] == "fiche absente de la liste candidate"


def test_la_copie_exacte_d_un_liste_ecartee_par_une_date_reste_une_perte(db):
    """Une date de naissance corrompue dans la liste candidate ferait passer de
    vrais listés pour des homonymes : le pseudo-client construit pour ÊTRE
    intercepté qui ne l'est plus est une perte, quelle qu'en soit la raison.
    Celui construit pour ne PAS l'être, lui, est un faux positif écarté."""
    _liste(db, "READY", [(0, "Igor", []), (1, "Boris", [])])
    candidate = _liste(db, "PENDING_REVIEW", [(0, "Igor", ["1960-01-01"])], heures=1)
    panel = _panel(db, [
        (f"TEST-HIT-00001-{TAG}", "Igor", f"Kouznetsovski{TAG}", "F", "1990-06-15"),
        (f"TEST-NEAR-00002-{TAG}", "Boris", f"Kouznetsovski{TAG}", "F", "1990-06-15"),
    ])

    rapport = run_backtest(db, candidate, panel, threshold_pct=20.0, executed_by="t")

    issues = {p["client_id"]: p["outcome"] for p in rapport["resolved_pairs"]}
    assert issues == {f"TEST-HIT-00001-{TAG}": "INTERCEPTION_LOST",
                      f"TEST-NEAR-00002-{TAG}": "FALSE_POSITIVE_REMOVED"}
    assert rapport["gap_direction"] == "INTERCEPTION_LOSS"
    assert rapport["verdict"] == "WARN"
    # Les pertes d'abord : c'est ce que le réviseur doit voir
    assert rapport["resolved_pairs"][0]["outcome"] == "INTERCEPTION_LOST"


def test_les_identifiants_du_generateur_disent_ce_qui_est_attendu(db):
    """`interception_attendue` lit le motif d'identifiant que le générateur de
    panel écrit : un pseudo-client dont il ne saurait rien dire serait jugé
    comme un vrai client, sans vérité de terrain."""
    production = _liste(db, "READY", [(n, p, ["1960-01-01"]) for n, p in enumerate(PRENOMS)])
    panel = generate_test_panel(db, [production.snapshot_id], size=60, seed=7)
    db.crees.append(panel.snapshot_id)
    ids = [c.client_id for c in db.query(ClientEntity).filter(
        ClientEntity.snapshot_id == panel.snapshot_id).all()]
    attendus = {cid: interception_attendue(cid) for cid in ids}
    assert None not in attendus.values()
    assert {interception_attendue(c) for c in ids if c.startswith(("TEST-HIT-", "TEST-TYPO-", "TEST-SWAP-"))} == {True}
    assert {interception_attendue(c) for c in ids if c.startswith(("TEST-NEAR-", "TEST-NEUTRE-"))} == {False}
    assert interception_attendue("CLI-0001") is None


# ------------------ LE CONSEIL SUIT LE SENS DE L'ÉCART ------------------

def test_le_refus_d_approbation_donne_le_conseil_du_sens_de_l_ecart(db):
    """Devant une perte d'interception, « posez des Good Guys » creuserait la
    perte : ce conseil n'est plus donné. Devant une hausse, il reste le bon."""
    _liste(db, "READY", [(0, "Igor", [])])
    candidate = _liste(db, "PENDING_REVIEW", [(0, "Igor", []), (1, "Boris", [])], heures=1)
    set_setting(db, SETTING_BACKTEST_REQUIRED, True)
    db.commit()
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "testeur", "full_name": "Testeur", "role": "admin",
        "roles": ["admin"]}
    try:
        with TestClient(app) as client:
            for sens, attendu, absent in (("INTERCEPTION_LOSS", "perd des interceptions", "Good Guys"),
                                          ("INCREASE", "Good Guys", "perd des interceptions")):
                candidate.backtest_report = {"verdict": "WARN", "gap_pct": 50.0,
                                             "threshold_pct": 20.0, "gap_direction": sens}
                db.commit()
                reponse = post_and_wait(
                    client, f"/api/review/snapshots/{candidate.snapshot_id}/approve",
                    json={"comment": "x"})
                assert reponse.status_code == 400, sens
                detail = reponse.json()["detail"]
                assert "écart" in detail and attendu in detail and absent not in detail, sens
    finally:
        app.dependency_overrides.clear()

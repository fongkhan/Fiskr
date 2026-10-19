"""
Ce que la production montrait, et ce que l'écran disait.

Relevé sur l'installation de production le 1er octobre 2026, en lecture seule :

* les 39 listes en production avaient **entre 29 et 69 jours** — OFAC 33,
  DGT 33, UE 51, OFSI 69 — pendant que chaque nuit une version plus récente
  arrivait en homologation et y restait ; **639 lots** s'étaient empilés,
  jusqu'à 21 pour une même liste, portant 7,46 millions de lignes de fiches ;
* l'écran de mise en service était **entièrement vert** : il regardait la
  configuration (« une revue est exigée »), jamais le résultat (« les revues
  ont-elles lieu ? ») ;
* approuver la dernière version laissait les anciennes en file — la file ne
  redescendait jamais — et rien n'empêchait d'en approuver une ensuite, ce qui
  aurait fait **régresser** la liste d'autant ;
* la liste canadienne était **vide** en production depuis le 5 août, et sa
  synchronisation rapportait chaque nuit « 0 fiches lues : contenu identique à
  la liste active » — zéro comparé à zéro ;
* « Référentiel clients : 2 500 fiches — OK » comptait les panels de cahier de
  tests, juste au-dessus d'un contrôle qui disait, lui, qu'aucun référentiel
  n'était en production.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException

from fiskr import fraicheur
from fiskr.database import Snapshot, get_db

UID = uuid.uuid4().hex[:6]
T0 = datetime(2026, 9, 1, 4, 0)


@pytest.fixture()
def db():
    session = next(get_db())
    yield session
    session.query(Snapshot).filter(Snapshot.snapshot_id.like(f"fr-{UID}-%")).delete(
        synchronize_session=False)
    session.commit()
    session.close()


def _lot(db, ftype, statut, jours, fiches=10, nom=None):
    sid = f"fr-{UID}-{nom or uuid.uuid4().hex[:6]}"
    db.add(Snapshot(snapshot_id=sid, file_type=ftype, file_name=f"{sid}.csv",
                    file_hash=uuid.uuid4().hex, record_count=fiches,
                    uploaded_at=T0 + timedelta(days=jours), status=statut))
    db.commit()
    return sid


def _liste(etat, ftype):
    return next(l for l in etat["listes"] if l["type"] == ftype)


# --------------------------------------------------------------------------
# La mesure
# --------------------------------------------------------------------------

def test_le_retard_se_mesure_depuis_la_plus_ancienne_version_en_attente(db):
    """
    C'est depuis la PREMIÈRE version non homologuée que la production n'est
    plus à jour — pas depuis la dernière, qui ne dirait que l'âge du dernier
    passage.
    """
    _lot(db, "WATCHLIST_CZ_TERROR", "READY", 0)
    for jour in (10, 20, 29):
        _lot(db, "WATCHLIST_CZ_TERROR", "PENDING_REVIEW", jour)
    etat = fraicheur.mesurer(db, maintenant=T0 + timedelta(days=30))
    cz = _liste(etat, "WATCHLIST_CZ_TERROR")
    assert cz["versions_en_attente"] == 3
    assert cz["retard_heures"] == 20 * 24
    assert cz["production_age_jours"] == 30


def test_une_version_plus_ancienne_que_la_production_est_perimee_pas_en_retard(db):
    """
    Un lot plus vieux que la production ne porte aucune réalité nouvelle :
    il ne doit ni compter comme un retard, ni rester approuvable.
    """
    _lot(db, "WATCHLIST_MAS", "PENDING_REVIEW", 1)
    _lot(db, "WATCHLIST_MAS", "READY", 5)
    etat = fraicheur.mesurer(db, maintenant=T0 + timedelta(days=6))
    mas = _liste(etat, "WATCHLIST_MAS")
    assert mas["versions_en_attente"] == 0
    assert mas["lots_perimes"] == 1
    assert mas["retard_heures"] == 0


def test_une_liste_vide_en_production_est_nommee(db):
    """La liste canadienne : comptée parmi les listes en production, vide."""
    _lot(db, "WATCHLIST_CANADA", "READY", 0, fiches=0)
    etat = fraicheur.mesurer(db, maintenant=T0 + timedelta(days=1))
    assert "WATCHLIST_CANADA" in etat["listes_vides"]


def test_les_ajouts_manuels_ne_sont_pas_la_version_d_une_liste(db):
    """Un snapshot d'ajout manuel coexiste avec la production : il ne la date pas."""
    _lot(db, "WATCHLIST_US_FTO", "READY", 0)
    db.add(Snapshot(snapshot_id=f"manual-watchlist-fr-{UID}", file_type="WATCHLIST_US_FTO",
                    file_name="m.csv", file_hash=uuid.uuid4().hex, record_count=1,
                    uploaded_at=T0 + timedelta(days=25), status="READY"))
    db.commit()
    try:
        _lot(db, "WATCHLIST_US_FTO", "PENDING_REVIEW", 10)
        etat = fraicheur.mesurer(db, maintenant=T0 + timedelta(days=30))
        assert _liste(etat, "WATCHLIST_US_FTO")["versions_en_attente"] == 1
    finally:
        db.query(Snapshot).filter(Snapshot.snapshot_id == f"manual-watchlist-fr-{UID}").delete()
        db.commit()


def test_le_badge_compte_les_listes_et_non_les_lots(db):
    """
    639 lots pour 29 listes : approuver la dernière version d'une liste retire
    les autres de la file, le travail est donc de 29 gestes. Un compteur qui
    l'annonce vingt fois plus grand décourage de le commencer.
    """
    avant = fraicheur.a_homologuer(db)
    _lot(db, "WATCHLIST_NL_TERROR", "READY", 0)
    for jour in range(1, 6):
        _lot(db, "WATCHLIST_NL_TERROR", "PENDING_REVIEW", jour)
    _lot(db, "WATCHLIST_NL_TERROR", "PENDING_REVIEW", -3)   # périmé : ne compte pas
    assert fraicheur.a_homologuer(db) == avant + 1


# --------------------------------------------------------------------------
# L'homologation ne fait plus régresser une liste, et la file se vide
# --------------------------------------------------------------------------

def test_approuver_une_version_plus_ancienne_que_la_production_est_refuse(db):
    from fiskr.api import _approve_one_snapshot
    _lot(db, "WATCHLIST_ZA_FIC", "READY", 10)
    vieux = _lot(db, "WATCHLIST_ZA_FIC", "PENDING_REVIEW", 3)
    with pytest.raises(HTTPException) as refus:
        _approve_one_snapshot(db, vieux, {"username": "relecteur"}, None)
    assert refus.value.status_code == 400
    assert "plus ancien que la version en production" in refus.value.detail
    assert db.query(Snapshot).filter(Snapshot.snapshot_id == vieux).one().status == "PENDING_REVIEW"


def test_approuver_la_derniere_version_retire_les_precedentes_de_la_file(db, monkeypatch):
    """
    Les lots antérieurs ne portent plus rien : le delta de la version approuvée,
    calculé contre l'ancienne production, contient déjà ce qu'ils contenaient.
    Ils passent en SUPERSEDED — conservés, mais plus approuvables.
    """
    from fiskr import api as api_mod
    monkeypatch.setattr(api_mod, "_submit_job", lambda *a, **k: None)
    monkeypatch.setattr(api_mod, "backtest_required", lambda db_: False)
    _lot(db, "WATCHLIST_QA_NCTC", "READY", 0)
    anciens = [_lot(db, "WATCHLIST_QA_NCTC", "PENDING_REVIEW", j) for j in (1, 2, 3)]
    dernier = _lot(db, "WATCHLIST_QA_NCTC", "PENDING_REVIEW", 4)
    plus_recent = _lot(db, "WATCHLIST_QA_NCTC", "PENDING_REVIEW", 5)

    rendu = api_mod._approve_one_snapshot(db, dernier, {"username": "relecteur"}, None)

    statuts = {s.snapshot_id: s.status for s in db.query(Snapshot).filter(
        Snapshot.snapshot_id.like(f"fr-{UID}-%"), Snapshot.file_type == "WATCHLIST_QA_NCTC")}
    assert statuts[dernier] == "READY"
    assert all(statuts[a] == "SUPERSEDED" for a in anciens)
    # Le plus récent reste en file : il porte une réalité que la production n'a pas.
    assert statuts[plus_recent] == "PENDING_REVIEW"
    assert rendu["superseded_pending"] == 3
    assert "3 version(s) plus ancienne(s)" in rendu["message"]


def test_une_homologation_groupee_converge_vers_la_version_la_plus_recente(db, monkeypatch):
    """
    Avec 21 lots par liste et un lot groupé de cinquante, l'ordre de sélection
    décidait de la version finale. Dans un ordre comme dans l'autre, c'est
    désormais la plus récente qui reste en production.
    """
    from fiskr import api as api_mod
    monkeypatch.setattr(api_mod, "_submit_job", lambda *a, **k: None)
    monkeypatch.setattr(api_mod, "backtest_required", lambda db_: False)
    _lot(db, "WATCHLIST_SA_TERROR", "READY", 0)
    lots = [_lot(db, "WATCHLIST_SA_TERROR", "PENDING_REVIEW", j) for j in (1, 2, 3)]
    for sid in reversed(lots):          # la plus récente d'abord
        try:
            api_mod._approve_one_snapshot(db, sid, {"username": "relecteur"}, None)
        except HTTPException:
            db.rollback()
    production = db.query(Snapshot).filter(
        Snapshot.file_type == "WATCHLIST_SA_TERROR", Snapshot.status == "READY",
        Snapshot.snapshot_id.like(f"fr-{UID}-%")).all()
    assert [s.snapshot_id for s in production] == [lots[-1]]


# --------------------------------------------------------------------------
# La mise en service regarde le résultat
# --------------------------------------------------------------------------

def _controle_fraicheur(monkeypatch, etat):
    monkeypatch.setattr(fraicheur, "mesurer", lambda db_, *a, **k: etat)
    from fiskr.mise_en_service import _fraicheur_des_listes
    return _fraicheur_des_listes(None)


def _etat(retards):
    listes = [{"type": t, "versions_en_attente": 1 if h else 0, "retard_heures": h,
               "lots_perimes": 0} for t, h in retards.items()]
    en_retard = [l for l in listes if l["versions_en_attente"]]
    return {"listes": listes, "listes_en_production": len(listes),
            "listes_en_retard": len(en_retard),
            "retard_max_heures": max((l["retard_heures"] for l in en_retard), default=0),
            "lots_en_attente": len(en_retard), "lots_perimes": 0, "listes_vides": []}


def test_un_mois_de_retard_est_bloquant(monkeypatch):
    """L'état exact de la production : il était vert, il est bloquant."""
    verdict = _controle_fraicheur(monkeypatch, _etat({
        "WATCHLIST_OFAC": 20 * 24, "WATCHLIST_DGT": 19 * 24, "WATCHLIST_UN": 0}))
    assert verdict["etat"] == "BLOQUANT"
    assert "OFAC (20 jours)" in verdict["constat"]
    assert "2 liste(s) sur 3" in verdict["constat"]


def test_trois_jours_de_retard_sont_une_attention(monkeypatch):
    verdict = _controle_fraicheur(monkeypatch, _etat({"WATCHLIST_OFAC": 72}))
    assert verdict["etat"] == "ATTENTION"


def test_une_relecture_en_cours_n_est_pas_une_alarme(monkeypatch):
    """Une nuit d'attente, c'est le temps d'une relecture : pas de quoi crier."""
    verdict = _controle_fraicheur(monkeypatch, _etat({"WATCHLIST_OFAC": 12}))
    assert verdict["etat"] == "OK"


def test_des_listes_a_jour_donnent_un_controle_vert(monkeypatch):
    verdict = _controle_fraicheur(monkeypatch, _etat({"WATCHLIST_OFAC": 0}))
    assert verdict["etat"] == "OK"
    assert "dernière version" in verdict["constat"]


def test_le_controle_de_fraicheur_est_enregistre():
    from fiskr.mise_en_service import _CONTROLES_BASE, _fraicheur_des_listes
    assert _fraicheur_des_listes in _CONTROLES_BASE


def test_une_liste_vide_en_production_n_est_plus_un_controle_vert(db):
    from fiskr.mise_en_service import _listes_en_production
    _lot(db, "WATCHLIST_CANADA", "READY", 0, fiches=0)
    verdict = _listes_en_production(db)
    assert verdict["etat"] == "ATTENTION"
    assert "WATCHLIST_CANADA" in verdict["constat"]


# --------------------------------------------------------------------------
# Le référentiel clients : une seule définition
# --------------------------------------------------------------------------

def test_les_panels_de_test_ne_sont_pas_un_referentiel_clients(db):
    """
    « 2 500 fiches — OK » : c'étaient les trois panels de cahier de tests.
    Ajouter un client de panel ne doit rien changer au référentiel ; ajouter un
    client au référentiel en production, si. Le contrôle et la couverture lisent
    désormais cette même définition.
    """
    from fiskr.couverture import clients_en_production
    from fiskr.database import ClientEntity
    avant = clients_en_production(db)
    panel = _lot(db, "CLIENT_TEST_PANEL", "READY", 0, fiches=1)
    base = _lot(db, "CLIENT_BASE", "READY", 0, fiches=1)
    try:
        db.add(ClientEntity(snapshot_id=panel, client_id=f"P-{UID}", client_type="PP",
                            client_last_name="Panel", entity_checksum="x" * 8))
        db.commit()
        assert clients_en_production(db) == avant, "un panel de test n'est pas un portefeuille"
        db.add(ClientEntity(snapshot_id=base, client_id=f"B-{UID}", client_type="PP",
                            client_last_name="Client", entity_checksum="y" * 8))
        db.commit()
        assert clients_en_production(db) == avant + 1
    finally:
        db.query(ClientEntity).filter(ClientEntity.client_id.in_([f"P-{UID}", f"B-{UID}"])).delete(
            synchronize_session=False)
        db.commit()


def test_le_controle_lit_la_meme_definition_que_la_couverture():
    import inspect
    from fiskr import mise_en_service
    source = inspect.getsource(mise_en_service._referentiel_clients)
    assert "clients_en_production" in source
    assert "query(ClientEntity).count()" not in source

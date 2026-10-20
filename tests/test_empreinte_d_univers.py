"""
« Contre quoi a-t-on criblé ? » doit avoir une réponse vraie.

Deux enquêtes indépendantes — une chasse aux défauts sur base jetable, une
lecture de la production — ont trouvé le même défaut. Chaque ligne du journal
d'audit porte une « version des listes » et une empreinte ; en production, 28
décisions sur 28 portaient la version « Database Active Snapshot », une
constante, et l'empreinte n'était que le hash du SEUL lot en production déposé
le plus récemment. Le badge publiait une empreinte pour 39 listes et 670 864
fiches.

Trois gestes changeaient l'univers sans changer cette clé : homologuer un lot
qui n'est pas le dernier déposé, exclure une fiche, corriger une fiche. Le
même client sortait NO_MATCH puis ALERT sous la même version et la même
empreinte, et l'index de filtrage ISO 20022, clé sur elle, n'était pas
reconstruit : un paiement à une personne désignée le jour même passait en
PASS, zéro candidat, pendant que /api/screen rendait ALERT sur le même nom.

Le même lot referme deux défauts voisins :
- couper une capacité du moteur n'atteignait que le processus qui recevait la
  requête — le rechargement par époque ne relisait pas les capacités, et le
  canal FILTERING n'incrémentait même pas l'époque ;
- sur une installation neuve, cinq fiches de démonstration entraient en
  production sous le type OFAC, et le refus de cribler sans univers ne mordait
  jamais.
"""
import json
import os
import random
import string
import subprocess
import sys
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr import api as api_mod
from fiskr import capabilities as caps
from fiskr import rarete, transactions
from fiskr.auth import get_current_user
from fiskr.database import (AuditTrail, Snapshot, WatchlistEntity,
                            WatchlistEntityChange, compute_checksum, get_db)
from fiskr.settings import (SETTING_ENGINE_CAPABILITIES, bump_watchlist_epoch,
                            get_setting, set_setting, watchlist_epoch)
from tests.conftest import post_and_wait

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UID = uuid.uuid4().hex[:6]
# Un suffixe de LETTRES : un chiffre dans un nom changerait sa phonétique, et
# le nom doit n'exister nulle part ailleurs dans la base partagée des tests.
SUFFIXE = "".join(random.choice(string.ascii_lowercase) for _ in range(4))
IVAN = ("Ivan", f"Nouveaulistov{SUFFIXE}")
BORIS = ("Boris", f"Ancienov{SUFFIXE}")
OLEG = ("Oleg", f"Sdnov{SUFFIXE}")

# Deux types que nulle autre suite n'emploie : une homologation fait passer en
# SUPERSEDED les lots READY du même type, et refuse un lot plus ancien que la
# production de ce type — un lot laissé par un autre test fausserait les deux.
NATIONALE = "WATCHLIST_MC_FREEZE"   # le rôle de la DGT dans la reproduction
AUTRE = "WATCHLIST_UA_NSDC"         # le rôle de l'OFAC, déposée après elle

# Le libellé que portaient les 28 décisions relevées en production. Écrit en
# toutes lettres plutôt que relu dans fiskr/univers.py : ce test doit pouvoir
# échouer sur le code d'avant, qui n'avait pas ce module.
VERSION_CONSTANTE = "Database Active Snapshot"

ADMIN = {"id": 1, "username": "relecteur_univers", "full_name": "Relecteur",
         "role": "admin", "roles": ["admin"]}


@pytest.fixture()
def client():
    api_mod.app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield TestClient(api_mod.app)
    api_mod.app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture()
def db():
    session = next(get_db())
    transactions.invalider_index_de_filtrage()
    yield session
    session.rollback()
    ids = [s.snapshot_id for s in session.query(Snapshot.snapshot_id).filter(
        Snapshot.snapshot_id.like(f"univ-{UID}-%")
        | Snapshot.snapshot_id.like("manual-watchlist-mc_freeze")).all()]
    if ids:
        pks = [pk for (pk,) in session.query(WatchlistEntity.id).filter(
            WatchlistEntity.snapshot_id.in_(ids)).all()]
        if pks:
            session.query(WatchlistEntityChange).filter(
                WatchlistEntityChange.entity_pk.in_(pks)).delete(synchronize_session=False)
        session.query(WatchlistEntity).filter(
            WatchlistEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
        session.query(Snapshot).filter(
            Snapshot.snapshot_id.in_(ids)).delete(synchronize_session=False)
        session.commit()
    api_mod.load_watchlist_cache(session)
    transactions.invalider_index_de_filtrage()
    session.close()


def _fiche(eid, prenom, nom, dob):
    return {"entity_id": eid, "entity_type": "I", "primary_name": f"{prenom} {nom}",
            "individual_name_parsed": {"first_name": prenom, "last_name": nom},
            "dates_of_birth": [dob], "gender": "M",
            "countries": {"citizenship": ["RU"]}, "aliases": {}}


def _lot(db, ftype, nom, depose_le, statut="READY", fiches=()):
    sid = f"univ-{UID}-{nom}"
    db.add(Snapshot(snapshot_id=sid, file_type=ftype, file_name=f"{sid}.xml",
                    file_hash=uuid.uuid4().hex, record_count=len(fiches),
                    status=statut, uploaded_at=depose_le))
    db.flush()
    for f in fiches:
        db.add(WatchlistEntity(snapshot_id=sid, entity_checksum=compute_checksum(f), **f))
    db.commit()
    return sid


@pytest.fixture()
def homologation_en_retard(db, monkeypatch):
    """
    La reproduction r5b, sur la base partagée des tests : la liste nationale
    v2 (déposée à T+1h, elle désigne Ivan) attend son homologation, pendant
    qu'une autre liste, déposée APRÈS elle, est déjà en production. Celle-ci
    est datée du lendemain pour être, quoi que la base contienne d'autre, le
    lot READY le plus récent : c'est son hash que l'ancienne empreinte citait.
    """
    monkeypatch.setattr(api_mod, "backtest_required", lambda _db: False)
    t0 = datetime.utcnow() - timedelta(hours=10)
    v1 = _lot(db, NATIONALE, "v1", t0,
              fiches=[_fiche(f"NAT-1-{UID}", *BORIS, "1950-01-01")])
    v2 = _lot(db, NATIONALE, "v2", t0 + timedelta(hours=1), statut="PENDING_REVIEW",
              fiches=[_fiche(f"NAT-1-{UID}", *BORIS, "1950-01-01"),
                      _fiche(f"NAT-2-{UID}", *IVAN, "1961-03-03")])
    _lot(db, AUTRE, "autre", datetime.utcnow() + timedelta(days=1),
         fiches=[_fiche(f"AUT-1-{UID}", *OLEG, "1955-05-05")])
    api_mod.load_watchlist_cache(db)
    return {"v1": v1, "v2": v2}


def _homologuer(client, snapshot_id):
    reponse = post_and_wait(client, f"/api/review/snapshots/{snapshot_id}/approve",
                            json={"comment": "homologation de test"})
    assert reponse.status_code == 202, reponse.text


def _pacs(beneficiaire):
    return f"""<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08"><FIToFICstmrCdtTrf>
<GrpHdr><MsgId>M-{uuid.uuid4().hex[:8]}</MsgId><NbOfTxs>1</NbOfTxs></GrpHdr>
<CdtTrfTxInf><PmtId><EndToEndId>E</EndToEndId></PmtId><IntrBkSttlmAmt Ccy="EUR">10</IntrBkSttlmAmt>
<Dbtr><Nm>Jean Dupont</Nm></Dbtr><Cdtr><Nm>{beneficiaire}</Nm></Cdtr></CdtTrfTxInf></FIToFICstmrCdtTrf></Document>""".encode()


def _filtrer(client, beneficiaire):
    reponse = client.post("/api/transactions/screen",
                          files={"file": ("m.xml", _pacs(beneficiaire), "application/xml")})
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def _cribler(client, prenom, nom, dob):
    # Un client NEUF a chaque criblage : une alerte encore ouverte pour le meme
    # couple serait re-detectee, et rendrait la decision d'un test precedent.
    reponse = client.post("/api/screen", json={
        "client_id": f"univ-{UID}-{uuid.uuid4().hex[:6]}", "client_type": "PP",
        "client_first_name": prenom, "client_last_name": nom, "client_dob": dob,
        "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [],
                             "birth_country": [], "registration_country": []}})
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


# --------------------------------------------------------------------------
# Le cœur : homologuer un lot qui n'est pas le dernier déposé
# --------------------------------------------------------------------------

def test_homologuer_un_lot_qui_n_est_pas_le_dernier_depose_rend_le_paiement_en_hit(
        client, db, homologation_en_retard):
    """
    La reproduction r5b. Avant : le paiement à Ivan rendait PASS, zéro
    candidat — l'index de filtrage, clé sur le hash du lot le plus récent (qui
    n'avait pas bougé), n'était pas reconstruit — pendant que /api/screen
    rendait ALERT sur le même nom.
    """
    # Un premier paiement bâtit l'index de filtrage du processus.
    assert _filtrer(client, " ".join(BORIS))["verdict"] == "HIT"

    _homologuer(client, homologation_en_retard["v2"])

    rendu = _filtrer(client, " ".join(IVAN))
    ivan = next(p for p in rendu["parties"] if p["name"] == " ".join(IVAN))
    assert ivan["candidates_count"] >= 1, "l'index de filtrage n'a pas été reconstruit"
    assert ivan["status"] == "ALERT"
    assert rendu["verdict"] == "HIT"


def test_deux_decisions_de_part_et_d_autre_d_une_homologation_ne_citent_pas_le_meme_univers(
        client, db, homologation_en_retard):
    """
    La reproduction r5 : le même client obtenait NO_MATCH puis ALERT sous la
    même version et la même empreinte. Les deux décisions citent désormais
    deux univers, et chacune dit LEQUEL : la composition figée nomme le lot
    homologué, et non celui qu'il remplace.
    """
    avant = _cribler(client, *IVAN, "1961-03-03")
    _homologuer(client, homologation_en_retard["v2"])
    apres = _cribler(client, *IVAN, "1961-03-03")
    assert apres["best_match"]["status"] == "ALERT"

    db.expire_all()
    a = db.get(AuditTrail, avant["audit_trail_id"])
    b = db.get(AuditTrail, apres["audit_trail_id"])
    assert a.watchlist_hash != b.watchlist_hash
    assert a.watchlist_version != b.watchlist_version
    assert a.watchlist_version != VERSION_CONSTANTE
    assert [NATIONALE, homologation_en_retard["v1"]] in a.watchlist_composition
    assert [NATIONALE, homologation_en_retard["v2"]] not in a.watchlist_composition
    assert [NATIONALE, homologation_en_retard["v2"]] in b.watchlist_composition
    assert [NATIONALE, homologation_en_retard["v1"]] not in b.watchlist_composition


def test_le_badge_et_le_journal_citent_la_meme_empreinte(client, db, homologation_en_retard):
    """
    /api/watchlist/summary publiait le hash du lot le plus récent : il ne
    bougeait pas à l'homologation d'un autre lot. Il lit maintenant la même
    définition que le cache et le journal, et publie la composition.
    """
    avant = client.get("/api/watchlist/summary").json()
    assert avant["hash"] == api_mod.watchlist_hash
    _homologuer(client, homologation_en_retard["v2"])
    apres = client.get("/api/watchlist/summary").json()
    assert apres["hash"] == api_mod.watchlist_hash
    assert apres["hash"] != avant["hash"]
    lots = {(l["file_type"], l["snapshot_id"]) for l in apres["composition"]}
    assert (NATIONALE, homologation_en_retard["v2"]) in lots
    assert (NATIONALE, homologation_en_retard["v1"]) not in lots


def test_la_table_de_rarete_d_un_autre_processus_suit_l_univers(client, db, homologation_en_retard):
    """
    Le démon construit sa table de rareté depuis la base, mémorisée par
    empreinte. Mémorisée par le hash du lot le plus récent, elle survivait à
    l'homologation d'un autre lot et comptait les mots d'un corpus qui n'était
    plus celui du criblage.
    """
    ancienne = rarete.table_pour(db)
    # Ce qu'un AUTRE processus a fait : bascule en base et époque, rien d'autre.
    db.query(Snapshot).filter(Snapshot.snapshot_id == homologation_en_retard["v2"]).update(
        {"status": "READY"})
    db.query(Snapshot).filter(Snapshot.snapshot_id == homologation_en_retard["v1"]).update(
        {"status": "SUPERSEDED"})
    db.commit()
    bump_watchlist_epoch(db)
    nouvelle = rarete.table_pour(db)
    assert nouvelle is not ancienne
    assert nouvelle.empreinte != ancienne.empreinte
    assert nouvelle.total == ancienne.total + 1   # Ivan est entré, Boris est resté


# --------------------------------------------------------------------------
# Les deux autres gestes : exclure, corriger — et ajouter à la main
# --------------------------------------------------------------------------

def test_exclure_une_fiche_en_production_change_l_empreinte(db, homologation_en_retard):
    """
    Une fiche exclue sort de l'univers sans que son lot change de hash. Une
    exclusion posée sur un lot EN ATTENTE, elle, ne change rien à ce qui est
    criblé — l'empreinte ne doit pas bouger : un signal qui bouge pour rien est
    un signal que l'on cesse de lire.
    """
    initiale = api_mod.watchlist_hash
    en_attente = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == homologation_en_retard["v2"]).first()
    en_attente.excluded = True
    db.commit()
    api_mod.load_watchlist_cache(db)
    assert api_mod.watchlist_hash == initiale

    en_production = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == homologation_en_retard["v1"]).one()
    en_production.excluded = True
    db.commit()
    api_mod.load_watchlist_cache(db)
    assert api_mod.watchlist_hash != initiale
    assert api_mod.watchlist_reference.fiches_exclues >= 1


def test_corriger_une_fiche_change_l_empreinte(client, db, homologation_en_retard):
    """Une fiche corrigée ne change ni de lot ni de hash : seul son journal en garde trace."""
    initiale = api_mod.watchlist_hash
    fiche = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == homologation_en_retard["v1"]).one()
    reponse = client.patch(f"/api/watchlist/entity/{fiche.id}",
                           json={"place_of_birth": "Moscou"})
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["changed_fields"] == ["place_of_birth"]
    assert api_mod.watchlist_hash != initiale


def test_ajouter_une_fiche_a_la_main_change_l_empreinte(client, db, homologation_en_retard):
    """
    Les lots d'ajouts manuels gardent le même hash de fichier quand on leur
    ajoute une fiche (« manual-watchlist-…-hash ») : seul leur nombre de
    fiches bouge.
    """
    def ajouter(nom):
        reponse = client.post("/api/watchlist/entity", json={
            "list_type": NATIONALE, "entity_type": "I", "primary_name": nom,
            "first_name": nom.split()[0], "last_name": nom.split()[1]})
        assert reponse.status_code == 200, reponse.text

    ajouter(f"Piotr Manuelov{SUFFIXE}")
    apres_une = api_mod.watchlist_hash
    ajouter(f"Pavel Manuelov{SUFFIXE}")
    assert api_mod.watchlist_hash != apres_une


# --------------------------------------------------------------------------
# Ce que les pièces citent
# --------------------------------------------------------------------------

def test_le_rapport_d_alerte_cite_l_univers_et_non_une_constante(client, db, homologation_en_retard):
    """Le rapport imprimable affichait « Version de watchlist : Database Active Snapshot »."""
    _homologuer(client, homologation_en_retard["v2"])
    alerte = _cribler(client, *IVAN, "1961-03-03")["alert_id"]
    page = client.get(f"/api/alerts/{alerte}/report").text
    assert VERSION_CONSTANTE not in page
    assert api_mod.watchlist_version in page
    assert api_mod.watchlist_hash in page
    assert homologation_en_retard["v2"] in page

    reponse = client.post(f"/api/alerts/{alerte}/narrative")
    assert reponse.status_code == 200, reponse.text
    assert homologation_en_retard["v2"] in json.dumps(reponse.json(), ensure_ascii=False)


# --------------------------------------------------------------------------
# Les capacités du moteur dans les autres processus
# --------------------------------------------------------------------------

@pytest.fixture()
def capacites_restaurees(db):
    avant = get_setting(db, SETTING_ENGINE_CAPABILITIES, None)
    yield
    set_setting(db, SETTING_ENGINE_CAPABILITIES, avant if avant is not None else {})
    caps.invalidate_context()


def test_une_capacite_coupee_atteint_les_autres_processus_par_l_epoque(db, capacites_restaurees):
    """
    La reproduction r9. Le processus B (un autre processus web, le démon)
    n'a pour canal vers A que la base : le réglage et l'époque. Il rechargeait
    son cache et gardait ses capacités d'avant — `adjust.dob` coupée en base,
    active dans B, et l'index rebâti avec.
    """
    api_mod._ensure_watchlist_cache(db)
    assert caps.is_active(caps.CAP_ADJUST_DOB)            # B a lu ses capacités

    # A : écrit le réglage et incrémente l'époque — l'invalidation qu'il fait
    # dans SA mémoire n'atteint pas B, elle n'est donc pas rejouée ici.
    set_setting(db, SETTING_ENGINE_CAPABILITIES,
                {caps.CHANNEL_SCREENING: {caps.CAP_ADJUST_DOB: False}}, updated_by="test")
    bump_watchlist_epoch(db)

    api_mod._ensure_watchlist_cache(db)                    # B voit l'époque changer
    assert not caps.is_active(caps.CAP_ADJUST_DOB)
    assert caps.CAP_ADJUST_DOB in (caps.describe_context(caps.CHANNEL_SCREENING) or {}).get("disabled", [])


def test_couper_une_capacite_du_filtrage_incremente_l_epoque(client, db, capacites_restaurees):
    """
    Sur le canal FILTERING l'époque n'était même pas incrémentée : les autres
    processus web filtraient indéfiniment avec les capacités d'avant.
    """
    capacite = next(c for c in caps.capabilities_for_channel(caps.CHANNEL_FILTERING)
                    if caps.defaults_for_channel(caps.CHANNEL_FILTERING)[c])
    avant = watchlist_epoch(db)
    reponse = client.put("/api/settings/engine", json={
        "channel": caps.CHANNEL_FILTERING, "capabilities": {capacite: False}})
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["changed"] == {capacite: False}
    db.expire_all()
    assert watchlist_epoch(db) > avant


# --------------------------------------------------------------------------
# L'installation neuve
# --------------------------------------------------------------------------

_BASE_VIDE = r"""
import json, os, sys
os.environ["FISKR_JOBS_MODE"] = "eager"
from fiskr import database
database.pg_url = "sqlite-volontaire://"   # jamais une base de l'hote
database.sqlite_path = sys.argv[1]
database.init_db()
from fastapi.testclient import TestClient
from fiskr import api, mise_en_service
from fiskr.auth import get_current_user
from fiskr.database import Snapshot, AuditTrail
api.app.dependency_overrides[get_current_user] = lambda: {
    "id": 1, "username": "admin", "full_name": "A", "role": "admin", "roles": ["admin"]}
client = TestClient(api.app)   # sans `with` : un processus web sous Passenger
r = client.post("/api/screen", json={
    "client_id": "CLI-0001", "client_type": "PP", "client_first_name": "Vladimir",
    "client_last_name": "Putin", "client_dob": "1952-10-07",
    "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [],
                         "registration_country": []}})
db = database.SessionLocal()
controles = {c["cle"]: c["etat"] for c in mise_en_service.etat_de_mise_en_service(db)["controles"]}
sys.__stdout__.write("RESULTAT=" + json.dumps({
    "statut": r.status_code, "lots": db.query(Snapshot).count(),
    "decisions": db.query(AuditTrail).count(),
    "listes": controles.get("listes"), "fraicheur": controles.get("fraicheur")}))
"""


def test_sur_une_base_vide_aucun_lot_n_est_seme_et_le_criblage_est_refuse(tmp_path):
    """
    La reproduction r1, dans un processus à part sur une base neuve : le
    chargement du cache y semait watchlist.json — un lot WATCHLIST_OFAC READY
    de cinq fiches —, le criblage rendait 200 « aucune correspondance » avec sa
    ligne d'audit, le refus de cribler sans univers ne mordait jamais, et la
    mise en service affichait « Listes : OK » et « Fraîcheur : OK ». La
    fraîcheur de zéro liste n'est pas OK : il n'y a rien à dater.
    """
    env = dict(os.environ, PYTHONPATH=RACINE)
    sortie = subprocess.run(
        [sys.executable, "-c", _BASE_VIDE, str(tmp_path / "neuve.sqlite3")],
        capture_output=True, text=True, env=env, cwd=str(tmp_path), timeout=180)
    ligne = next((l for l in sortie.stdout.splitlines() if "RESULTAT=" in l), None)
    assert ligne, f"stdout={sortie.stdout[-800:]}\nstderr={sortie.stderr[-1500:]}"
    resultat = json.loads(ligne.split("RESULTAT=", 1)[1])
    assert resultat["lots"] == 0, "un lot a été semé en production"
    assert resultat["statut"] == 409
    assert resultat["decisions"] == 0, "une décision a été journalisée sans univers"
    assert resultat["listes"] == "BLOQUANT"
    assert resultat["fraicheur"] != "OK", "la fraîcheur de zéro liste était déclarée OK"

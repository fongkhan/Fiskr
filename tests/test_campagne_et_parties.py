"""
Une campagne de criblage batch crible aussi les parties liées de chaque client.

Constat (rapport du lot « parties liées ») : c'était le seul chemin de criblage
laissé de côté. Temps réel, webhooks, « recribler ce client », re-criblage après
mise à jour de liste, lookback, clients nouveaux ou modifiés : tous criblent le
client ET ses bénéficiaires effectifs, mandataires, dirigeants et associés.
Une campagne lancée sur ACME ne criblait que la ligne du fichier : le
bénéficiaire effectif listé d'ACME n'y ressortait pas.

Ce que ces tests tiennent : les parties passent par le même chemin que le
criblage unitaire (`_cribler_les_parties`), même si la ligne du client est
refusée par le contrôle qualité ; le compte rendu de campagne distingue les
clients (lignes du fichier) des parties (comptées à part, chaque ligne de
résultat dit laquelle et comment elle se rattache).
"""
import inspect
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr import parties
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (Alert, AlertEvent, AuditTrail, PartieLiee, Snapshot,
                            WatchlistEntity, get_db)

ADMIN = {"id": 1, "username": "admin_campagne_parties", "full_name": "Admin",
         "role": "admin,reviewer", "roles": ["admin", "reviewer"]}


@pytest.fixture
def ctx():
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    session = next(get_db())
    etat = {"listes": [], "clients": []}
    try:
        with TestClient(app) as http:
            yield {"http": http, "db": session, **etat}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        session.rollback()
        clients = etat["clients"] or ["-"]
        alertes = [a.id for a in session.query(Alert).filter(Alert.client_id.in_(clients))]
        session.query(AlertEvent).filter(AlertEvent.alert_id.in_(alertes or [-1])) \
            .delete(synchronize_session=False)
        session.query(Alert).filter(Alert.id.in_(alertes or [-1])).delete(synchronize_session=False)
        session.query(AuditTrail).filter(AuditTrail.client_id.in_(clients)) \
            .delete(synchronize_session=False)
        session.query(PartieLiee).filter(PartieLiee.client_id.in_(clients)) \
            .delete(synchronize_session=False)
        if etat["listes"]:
            from fiskr.settings import bump_watchlist_epoch
            session.query(WatchlistEntity).filter(
                WatchlistEntity.snapshot_id.in_(etat["listes"])).delete(synchronize_session=False)
            session.query(Snapshot).filter(
                Snapshot.snapshot_id.in_(etat["listes"])).delete(synchronize_session=False)
            bump_watchlist_epoch(session)
        session.commit()
        session.close()


def _liste(ctx, personnes):
    corps = "entity_id,entity_type,primary_name,nationality,dob\n" + "".join(
        f"{eid},I,{nom},RU,{dob}\n" for eid, nom, dob in personnes)
    r = ctx["http"].post("/api/ingest", data={"file_type": "WATCHLIST_EU"}, files={
        "file": (f"test_campagne_partie_{uuid.uuid4().hex[:6]}.csv", corps, "text/csv")})
    assert r.status_code == 200, r.text
    ctx["listes"].append(r.json()["snapshot_id"])


def _declarer(ctx, client_id, *lues):
    parties.remplacer_les_parties(ctx["db"], client_id, list(lues),
                                  source=parties.SOURCE_IMPORT, source_ref="test", username="test")
    ctx["db"].commit()


def _beneficiaire(prenom, nom, dob, pct):
    return parties.lire_une_partie(
        {"client_type": "PP", "client_first_name": prenom, "client_last_name": nom,
         "client_dob": dob, "client_gender": "M",
         "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [],
                              "registration_country": []}},
        roles=["BE"], pourcentage=pct)


def _campagne(ctx, lignes):
    corps = "client_id,client_type,client_company_name,client_last_name\n" + "".join(lignes)
    r = ctx["http"].post("/api/batch/campaigns", data={"name": "test_campagne_parties"},
                         files={"file": ("clients.csv", corps, "text/csv")})
    assert r.status_code == 200, r.text
    identifiant = r.json()["id"]
    limite = time.time() + 30
    while time.time() < limite:
        c = ctx["http"].get(f"/api/batch/campaigns/{identifiant}").json()
        if c["status"] != "RUNNING":
            return c
        time.sleep(0.2)
    raise AssertionError("campagne non terminée")


def test_une_campagne_sur_acme_trouve_son_beneficiaire_effectif_liste(ctx):
    """Avant : 1 client criblé, 0 en alerte, et rien sur la partie — le
    bénéficiaire effectif listé d'ACME passait la campagne sans être vu."""
    tag = uuid.uuid4().hex[:6].upper()
    acme = f"test_cp_ACME_{tag}"
    ctx["clients"].append(acme)
    _liste(ctx, [(f"CP-ZOL-{tag}", f"Vsevolod Zolotarev{tag}", "1954-01-27")])
    _declarer(ctx, acme, _beneficiaire("Vsevolod", f"Zolotarev{tag}", "1954-01-27", 35))
    partie = ctx["db"].query(PartieLiee).filter(PartieLiee.client_id == acme).one()

    c = _campagne(ctx, [f"{acme},PM,ACME HOLDING {tag},\n"])
    assert c["status"] == "DONE", c
    # Les clients restent les lignes du fichier ; les parties sont comptées à part
    assert c["processed_clients"] == 1 and c["alert_count"] == 0 and c["no_match_count"] == 1
    assert c["parties_screened"] == 1 and c["parties_alert_count"] == 1, c
    assert c["parties_rejected_count"] == 0
    ligne = next(r for r in c["results"] if r["partie_liee_id"] == partie.id)
    assert ligne["status"] == "ALERT" and ligne["client_id"] == acme
    assert ligne["watchlist_entity_id"] == f"CP-ZOL-{tag}" and ligne["alert_id"]
    assert ligne["via_partie"] == (f"via le bénéficiaire effectif Vsevolod Zolotarev{tag} (35 %) "
                                   f"du client ACME HOLDING {tag}")
    client_seul = next(r for r in c["results"] if r["partie_liee_id"] is None)
    assert client_seul["status"] == "NO_MATCH"

    # L'alerte est celle du client, la partie nommée — comme partout ailleurs
    alerte = ctx["db"].get(Alert, ligne["alert_id"])
    assert alerte.client_id == acme and alerte.partie_liee_id == partie.id
    export = ctx["http"].get(f"/api/export/batch/{c['id']}.csv").content.decode("utf-8")
    assert "rattachement_partie" in export and f"Vsevolod Zolotarev{tag} (35 %)" in export


def test_la_partie_est_criblee_meme_si_la_ligne_du_client_est_refusee(ctx):
    """La ligne du fichier sans nom est refusée par le contrôle qualité ; la
    partie, elle, a son identité au référentiel : elle n'en est pas moins due."""
    tag = uuid.uuid4().hex[:6].upper()
    acme = f"test_cp_VIDE_{tag}"
    ctx["clients"].append(acme)
    _liste(ctx, [(f"CP-SOK-{tag}", f"Arkhip Sokoline{tag}", "1958-08-08")])
    _declarer(ctx, acme, _beneficiaire("Arkhip", f"Sokoline{tag}", "1958-08-08", 60))
    c = _campagne(ctx, [f"{acme},PM,,\n"])
    assert c["rejected_count"] == 1 and c["parties_screened"] == 1
    assert c["parties_alert_count"] == 1, c


def test_campagne_et_criblage_unitaire_partagent_le_chemin_des_parties():
    from fiskr import api
    assert "_cribler_les_parties(" in inspect.getsource(api._run_batch_campaign)
    assert "_cribler_les_parties(" in inspect.getsource(api.cribler_un_profil)

"""
Une paire en liste blanche se réexamine quand la fiche listée ou le client change.

Lignes directrices conjointes DGT/ACPR sur le gel des avoirs (18 mars 2026),
§169 : les « good guys » sont admis à condition que l'outil re-détecte la
personne « immédiatement après l'entrée en vigueur […] d'une modification d'une
mesure existante, ou si les informations relatives au client ont changé ».

La paire ne gardait que le couple (client, fiche) et la liste blanche
s'appliquait avant tout autre examen. Reproduit : paire CUST-777 × DGT-2 posée
sur la v1 ; la v2 ajoute à DGT-2 le numéro de passeport du client ; le
re-criblage rendait `whitelisted_suppressed == 1` et aucune alerte.

Ces tests passent par les vraies portes — l'API de pose et de reconduction, le
re-criblage, le criblage temps réel, la campagne — sur une base isolée.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fiskr.api as api_module
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    AdminAuditLog, Alert, AlertEvent, AuditTrail, Base, BatchCampaign, ClientEntity,
    Snapshot, WatchlistEntity, WhitelistPair, compute_checksum, get_db,
)
from fiskr.rescreen import rescreen_after_snapshot_change, rescreen_lookback

REVISEUR = {"id": 1, "username": "reviseur-lb", "full_name": "Réviseur",
            "role": "reviewer", "roles": ["reviewer"]}
PASSEPORT = [{"number": "77PA12345", "issuing_country": "RU"}]


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'liste_blanche.sqlite3'}")
    Base.metadata.create_all(engine)
    fabrique = sessionmaker(bind=engine)
    session = fabrique()
    session.fabrique = fabrique
    yield session
    session.close()


@pytest.fixture
def api(db):
    """L'API réelle, branchée sur la base isolée."""
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: REVISEUR
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _referentiel(db, **champs):
    snap = Snapshot(snapshot_id=f"clients-{uuid.uuid4().hex[:6]}", file_type="CLIENT_BASE",
                    file_name="clients.csv", file_hash=uuid.uuid4().hex, record_count=1,
                    status="READY")
    db.add(snap)
    client = dict(
        client_id="CUST-777", client_type="PP",
        client_first_name="IGOR", client_last_name="PETROV", client_dob="1965-03-12",
        client_gender="M", client_passport_documents=PASSEPORT,
        client_countries={"nationality": ["RU"], "residence": [], "birth_country": [],
                          "registration_country": []},
    )
    client.update(champs)
    db.add(ClientEntity(snapshot_id=snap.snapshot_id, entity_checksum=uuid.uuid4().hex, **client))
    db.commit()
    return snap.snapshot_id


def _fiche(snap_id, **champs):
    donnees = dict(
        entity_id="DGT-2", entity_type="I", primary_name="Igor PETROV",
        individual_name_parsed={"first_name": "Igor", "last_name": "PETROV", "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=["1965-03-12"], is_deceased=False, gender="M",
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
    )
    donnees.update(champs)
    # Empreinte calculee comme a l'ingestion : elle change avec le contenu
    return WatchlistEntity(snapshot_id=snap_id, entity_checksum=compute_checksum(donnees), **donnees)


def _liste(db, snap_id, fiches):
    """Met une version de la DGT en production ; la précédente est remplacée."""
    for ancienne in db.query(Snapshot).filter(Snapshot.file_type == "WATCHLIST_DGT",
                                              Snapshot.status == "READY").all():
        ancienne.status = "SUPERSEDED"
    db.add(Snapshot(snapshot_id=snap_id, file_type="WATCHLIST_DGT", file_name=f"{snap_id}.json",
                    file_hash=uuid.uuid4().hex, record_count=len(fiches), status="READY"))
    db.add_all(fiches)
    db.commit()


def _clore_en_faux_positif(db, client_id, entity_id):
    for alerte in db.query(Alert).filter(Alert.client_id == client_id,
                                         Alert.watchlist_entity_id == entity_id).all():
        alerte.status = "CLOSED_FALSE_POSITIVE"
    db.commit()


def _poser(api, client_id="CUST-777", entity_id="DGT-2"):
    reponse = api.post("/api/whitelist", data={
        "client_id": client_id, "watchlist_entity_id": entity_id,
        "justification": "Homonyme avéré : passeport différent."})
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def _paire_posee_sur_la_v1(db, api, **client):
    """Le parcours réel : la v1 entre en production, le re-criblage ouvre
    l'alerte, l'analyste la clôt en faux positif et pose la paire."""
    _referentiel(db, **client)
    _liste(db, "wl-v1", [_fiche("wl-v1")])
    premier = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "wl-v1", None)
    assert premier["new_alerts"] == 1, premier
    _clore_en_faux_positif(db, "CUST-777", "DGT-2")
    return _poser(api)


def _evenements(db, action):
    return db.query(AlertEvent).filter(AlertEvent.action == action).all()


# ------------------ LE SCÉNARIO DU CONSTAT ------------------

def test_une_fiche_modifiee_depuis_la_decision_rouvre_l_alerte(db, api):
    paire = _paire_posee_sur_la_v1(db, api, client_passport_documents=PASSEPORT)
    ligne_v1 = db.query(AuditTrail).filter(AuditTrail.watchlist_id == "DGT-2").one()

    # v2 : la DGT ajoute à DGT-2 le numéro de passeport du client
    _liste(db, "wl-v2", [_fiche("wl-v2", passport_documents=PASSEPORT)])
    resultat = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "wl-v2", "wl-v1")

    assert resultat["whitelisted_suppressed"] == 0, resultat
    assert resultat["new_alerts"] == 1, "la v2 n'a pas été examinée : l'alerte s'ouvre"
    assert resultat["whitelist_stale"] == 1, resultat
    # La pose avait lu ce que l'analyste avait sous les yeux : le criblage
    # journalisé de la v1, pas l'état du jour.
    assert paire["fingerprint_audit_id"] == ligne_v1.id

    alerte = db.query(Alert).filter(Alert.client_id == "CUST-777",
                                    Alert.status == "OPEN").one()
    actions = [e.action for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == alerte.id).order_by(AlertEvent.id).all()]
    assert actions == ["CREATED", "WHITELIST_STALE"]
    detail = _evenements(db, "WHITELIST_STALE")[0].detail
    assert "la fiche listée a changé" in detail
    assert "passport_documents" in detail, "le champ-à-champ dit ce qui a changé"
    assert f"paire #{paire['id']}" in detail

    # La paire n'est ni supprimée ni révoquée : son historique reste
    relue = db.query(WhitelistPair).filter(WhitelistPair.id == paire["id"]).one()
    assert relue.revoked_at is None
    assert relue.stale_since is not None and relue.stale_reason == "ENTITY"


def test_un_client_modifie_depuis_la_decision_rouvre_l_alerte(db, api):
    _paire_posee_sur_la_v1(db, api, client_dob=None)
    # Le référentiel apprend la date de naissance du client
    rangee = db.query(ClientEntity).filter(ClientEntity.client_id == "CUST-777").one()
    rangee.client_dob = "1965-03-12"
    db.commit()

    resultat = rescreen_lookback(db)
    assert resultat["whitelisted_suppressed"] == 0, resultat
    assert resultat["whitelist_stale"] == 1, resultat
    assert resultat["new_alerts"] == 1, resultat
    detail = _evenements(db, "WHITELIST_STALE")[0].detail
    assert "l'identification du client a changé" in detail
    assert db.query(WhitelistPair).one().stale_reason == "CLIENT"


def test_rien_de_change_la_paire_absorbe_toujours(db, api):
    """L'intention d'origine de la liste blanche, gardée. Y compris quand un
    champ que le moteur ne lit pas bouge : une notation de risque interne
    n'est pas une information d'identification, et une paire qui se
    rouvrirait pour elle crierait au loup."""
    _paire_posee_sur_la_v1(db, api)
    resultat = rescreen_lookback(db)
    assert resultat["whitelisted_suppressed"] == 1, resultat
    assert resultat["whitelist_stale"] == 0
    assert resultat["new_alerts"] == 0

    rangee = db.query(ClientEntity).filter(ClientEntity.client_id == "CUST-777").one()
    rangee.client_risk_rating = "ELEVE"
    rangee.client_email = "igor@exemple.test"
    db.commit()
    resultat = rescreen_lookback(db)
    assert resultat["whitelisted_suppressed"] == 1, resultat
    assert resultat["whitelist_stale"] == 0
    assert db.query(WhitelistPair).one().stale_since is None
    assert not _evenements(db, "WHITELIST_STALE")


def test_l_empreinte_du_client_porte_sur_ce_que_le_moteur_lit_quelle_que_soit_la_porte():
    """Les champs viennent du code du moteur, pas d'une liste recopiée ; et le
    même client reçu par l'API ou lu dans le référentiel a la même empreinte —
    sinon chaque changement de canal rouvrirait la paire."""
    from fiskr.api import ScreenClientRequest
    from fiskr.empreintes import champs_client_cribles, empreinte_client

    champs = set(champs_client_cribles())
    assert {"client_first_name", "client_last_name", "client_dob",
            "client_passport_documents", "client_countries", "client_lei_number"} <= champs
    assert not champs & {"client_id", "client_risk_rating", "client_segment",
                         "client_email", "client_status"}

    donnees = dict(client_id="C-X", client_type="PP", client_first_name="Igor",
                   client_last_name="PETROV", client_gender="M",
                   client_countries={"nationality": ["RU"], "residence": [],
                                     "birth_country": [], "registration_country": []})
    par_l_api = ScreenClientRequest(**donnees).model_dump()
    du_referentiel = {c.name: None for c in ClientEntity.__table__.columns}
    du_referentiel.update(donnees, client_first_name="IGOR", entity_checksum="x")
    assert empreinte_client(par_l_api) == empreinte_client(du_referentiel)
    assert empreinte_client(par_l_api) != empreinte_client({**par_l_api, "client_dob": "1965-03-12"})


# ------------------ RECONDUCTION ------------------

def test_la_reconduction_absorbe_a_nouveau_et_reste_tracee(db, api):
    paire = _paire_posee_sur_la_v1(db, api)
    _liste(db, "wl-v2", [_fiche("wl-v2", passport_documents=PASSEPORT)])
    rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "wl-v2", "wl-v1")

    # L'écran montre la paire à réexaminer, filtre et compteur compris
    a_revoir = api.get("/api/whitelist", params={"state": "STALE"}).json()
    assert [p["id"] for p in a_revoir["items"]] == [paire["id"]]
    assert a_revoir["stale_total"] == 1
    assert a_revoir["items"][0]["state"] == "STALE"
    assert a_revoir["items"][0]["stale_reason"] == "ENTITY"
    assert a_revoir["items"][0]["stale_reason_label"]

    # Poser une deuxième paire sur le couple est refusé : on reconduit
    doublon = api.post("/api/whitelist", data={"client_id": "CUST-777",
                                               "watchlist_entity_id": "DGT-2",
                                               "justification": "x"})
    assert doublon.status_code == 409 and "réexaminer" in doublon.json()["detail"]

    # Motif obligatoire, rôle réviseur exigé
    assert api.post(f"/api/whitelist/{paire['id']}/reconduct",
                    json={"comment": " "}).status_code == 400
    app.dependency_overrides[get_current_user] = lambda: {**REVISEUR, "role": "user",
                                                          "roles": ["user"]}
    assert api.post(f"/api/whitelist/{paire['id']}/reconduct",
                    json={"comment": "vu"}).status_code == 403
    app.dependency_overrides[get_current_user] = lambda: REVISEUR

    avant = db.query(WhitelistPair).one().entity_checksum
    reponse = api.post(f"/api/whitelist/{paire['id']}/reconduct",
                       json={"comment": "Passeport de la v2 différent de celui du client."})
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["state"] == "ACTIVE"
    db.expire_all()
    reconduite = db.query(WhitelistPair).one()
    assert reconduite.entity_checksum != avant, "la nouvelle empreinte est celle de la v2"
    assert reconduite.stale_since is None and reconduite.id == paire["id"]

    trace = db.query(AdminAuditLog).filter(AdminAuditLog.action == "WHITELIST_RECONDUCTED").one()
    assert trace.username == REVISEUR["username"]
    assert trace.before["entity_checksum"] == avant
    assert trace.after["entity_checksum"] == reconduite.entity_checksum
    assert "Passeport de la v2" in trace.detail

    # Absorbée à nouveau
    resultat = rescreen_lookback(db)
    assert resultat["whitelist_stale"] == 0
    assert resultat["whitelisted_suppressed"] >= 1
    assert resultat["new_alerts"] == 0

    # Rien à réexaminer : une seconde reconduction n'aurait pas d'objet
    assert api.post(f"/api/whitelist/{paire['id']}/reconduct",
                    json={"comment": "encore"}).status_code == 409


# ------------------ CE QUI N'A PAS D'EMPREINTE ------------------

def test_une_paire_anterieure_au_suivi_est_a_reexaminer(db, api):
    """Choix assumé : une paire sans empreinte n'absorbe plus. Rien n'établit
    que rien n'a changé depuis sa décision, et §169 conditionne l'admission à
    la re-détection. Aucune empreinte ne lui est posée après coup : celle du
    jour de la migration se ferait passer pour celle de la décision."""
    _referentiel(db)
    _liste(db, "wl-v1", [_fiche("wl-v1")])
    db.add(WhitelistPair(client_id="CUST-777", watchlist_entity_id="DGT-2",
                         justification="FP avéré", created_by="reviseur1"))
    db.commit()
    assert api.get("/api/whitelist").json()["items"][0]["state"] == "STALE"

    resultat = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "wl-v1", None)
    assert resultat["whitelisted_suppressed"] == 0
    assert resultat["new_alerts"] == 1
    detail = _evenements(db, "WHITELIST_STALE")[0].detail
    assert "antérieure au suivi" in detail
    paire = db.query(WhitelistPair).one()
    assert paire.entity_checksum is None, "aucune empreinte posée en silence"
    assert paire.stale_reason == "NO_FINGERPRINT"


def test_la_pose_est_refusee_quand_rien_ne_dit_ce_qu_elle_couvre(db, api):
    """Un couple jamais criblé, un client absent de tout référentiel : une
    paire posée là couvrirait toute version future du client."""
    _liste(db, "wl-v1", [_fiche("wl-v1")])
    reponse = api.post("/api/whitelist", data={"client_id": "INCONNU-1",
                                               "watchlist_entity_id": "DGT-2",
                                               "justification": "x"})
    assert reponse.status_code == 409
    assert "criblez-le d'abord" in reponse.json()["detail"]
    assert db.query(WhitelistPair).count() == 0


# ------------------ CRIBLAGE TEMPS RÉEL ------------------

def _index_simule(monkeypatch, fiches):
    class _Index(dict):
        def get(self, cle, defaut=None):
            return fiches

    monkeypatch.setattr(api_module, "watchlist_index", _Index())
    monkeypatch.setattr(api_module, "watchlist_store", fiches)
    monkeypatch.setattr(api_module, "watchlist_types", {"WATCHLIST_DGT"})
    monkeypatch.setattr(api_module, "_ensure_watchlist_cache", lambda db: None)


def _fiche_en_cache(entity_id, nom, prenom, **champs):
    donnees = dict(entity_id=entity_id, entity_type="I", primary_name=f"{prenom} {nom}",
                   individual_name_parsed={"first_name": prenom, "last_name": nom},
                   aliases={"high_priority": [], "low_priority": []},
                   dates_of_birth=["1961-03-03"], gender="M",
                   countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                              "jurisdiction_country": []})
    donnees.update(champs)
    donnees["entity_checksum"] = compute_checksum(donnees)
    donnees["_list_type"] = "WATCHLIST_DGT"
    return donnees


def test_le_criblage_temps_reel_rouvre_sur_un_client_modifie(db, api, monkeypatch):
    fiche = _fiche_en_cache("DGT-RT", "PETROVSKY", "Ivan")
    _index_simule(monkeypatch, [fiche])
    profil = {"client_id": "C-RT", "client_type": "PP", "client_first_name": "Ivan",
              "client_last_name": "Petrovsky", "client_gender": "M",
              "client_countries": {"nationality": ["RU"]}}
    premier = api.post("/api/screen", json=profil).json()
    assert premier["best_match"]["status"] == "ALERT"
    _clore_en_faux_positif(db, "C-RT", "DGT-RT")
    _poser(api, "C-RT", "DGT-RT")

    assert api.post("/api/screen", json=profil).json()["whitelisted"] is True

    modifie = api.post("/api/screen", json={**profil, "client_dob": "1961-03-03"}).json()
    assert modifie["whitelisted"] is False
    assert modifie["hits"]["whitelist_stale"] == 1
    assert modifie["alert_id"] is not None
    assert [e.action for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == modifie["alert_id"]).order_by(AlertEvent.id)] == \
        ["CREATED", "WHITELIST_STALE"]


# ------------------ CAMPAGNE (défaut voisin, r8) ------------------

def test_en_campagne_le_client_compte_en_alerte_si_une_autre_correspondance_alerte(
        db, api, monkeypatch):
    """La meilleure correspondance du client est en liste blanche, une autre
    ouvre une alerte : le client est en ALERT, et le filtre ALERT de la
    campagne le montre, sur la correspondance qui a ouvert l'alerte. Il était
    compté « sans correspondance » et le filtre restait vide."""
    meilleure = _fiche_en_cache("DGT-1", "PETROVSKY", "Ivan")
    autre = _fiche_en_cache("DGT-2", "PETROVSKIY", "Ivan")
    _index_simule(monkeypatch, [meilleure, autre])
    monkeypatch.setattr("fiskr.database.SessionLocal", db.fabrique)
    ligne = {"client_id": "C-1", "client_type": "PP", "first_name": "Ivan",
             "last_name": "Petrovsky", "dob": "1961-03-03", "nationality": "RU"}
    profil = api_module._batch_row_to_profile(ligne)

    def campagne(nom):
        c = BatchCampaign(name=nom, file_name="c.csv", trigger="MANUAL", status="RUNNING",
                          total_clients=1, created_by="admin")
        db.add(c)
        db.commit()
        identifiant = c.id
        # La campagne tourne dans sa propre session, comme dans le démon
        api_module._run_batch_campaign(identifiant, [profil], "admin", None)
        db.expire_all()
        return api.get(f"/api/batch/campaigns/{identifiant}", params={"status": "ALERT"}).json()

    premiere = campagne("R8-a")
    assert premiere["status"] == "DONE", premiere
    assert premiere["alert_count"] == 1, premiere
    _clore_en_faux_positif(db, "C-1", "DGT-1")
    _poser(api, "C-1", "DGT-1")      # l'analyste : C-1 n'est pas DGT-1

    seconde = campagne("R8-b")
    assert seconde["alert_count"] == 1, seconde
    assert seconde["no_match_count"] == 0, seconde
    assert seconde["results_total"] == 1, "le filtre ALERT montre le client"
    rangee = seconde["results"][0]
    assert rangee["watchlist_entity_id"] == "DGT-2", "la correspondance qui alerte"
    alerte = db.query(Alert).filter(Alert.id == rangee["alert_id"]).one()
    assert alerte.watchlist_entity_id == "DGT-2"


# ------------------ CAHIER DE TESTS ------------------

def test_le_cahier_de_tests_predit_la_reouverture():
    """Le cahier doit prédire la production : une liste candidate qui modifie
    une fiche blanchie y rouvre l'alerte, comme elle le fera après
    homologation. Il annonçait la paire absorbée."""
    from fiskr import screenpool
    from fiskr.config import config
    from fiskr.empreintes import EmpreintesDePaire, empreinte_client

    client = {"client_id": "C-CT", "client_type": "PP", "client_first_name": "Ivan",
              "client_last_name": "Petrovsky", "client_gender": "M",
              "client_countries": {"nationality": ["RU"]}}
    fiche_v1 = _fiche_en_cache("DGT-CT", "PETROVSKY", "Ivan")
    fiche_v2 = _fiche_en_cache("DGT-CT", "PETROVSKY", "Ivan", passport_documents=PASSEPORT)

    def sort(fiche):
        class _Index(dict):
            def get(self, cle, defaut=None):
                return [fiche]
        paires = {("C-CT", "DGT-CT"): EmpreintesDePaire(
            1, fiche_v1["entity_checksum"], empreinte_client(client))}
        return screenpool.screen_one(client, _Index(), dict(config), paires, [])[0]

    assert sort(fiche_v1) == "whitelisted"
    assert sort(fiche_v2) == "alert"


# ------------------ RANG DES ALERTES (défaut voisin) ------------------

def test_les_alertes_rendues_sont_alignees_sur_les_correspondances(db):
    """`alert_ids` listait les alertes re-détectées d'abord, les nouvelles
    ensuite. Rapprochées par rang — ce que font le criblage temps réel pour
    `alert_id` et la campagne pour sa ligne —, la meilleure correspondance
    recevait l'alerte d'une autre dès que les deux sortes se mêlaient."""
    from fiskr.alerts import open_or_redetect_alerts

    def ligne(entity_id):
        audit = AuditTrail(client_id="C-R", client_name="X", client_type="PP",
                           watchlist_id=entity_id, watchlist_name=entity_id,
                           base_score=90.0, final_score=90.0, status="ALERT",
                           decision_tree={}, config_state={}, watchlist_version="v",
                           watchlist_hash="h")
        db.add(audit)
        db.flush()
        return audit

    ancienne = Alert(audit_id=ligne("E-2").id, client_id="C-R", client_name="X",
                     watchlist_entity_id="E-2", watchlist_name="E-2", final_score=80.0,
                     status="OPEN")
    db.add(ancienne)
    db.commit()
    correspondances = [
        {"audit": ligne(e), "rule": None,
         "match": {"final_score": s, "watchlist_entity": {"entity_id": e, "primary_name": e}}}
        for e, s in (("E-1", 95.0), ("E-2", 85.0))]
    rendu = open_or_redetect_alerts(db, correspondances, client_id="C-R", username="t")
    nouvelle = db.query(Alert).filter(Alert.watchlist_entity_id == "E-1").one()
    assert rendu["alert_ids"] == [nouvelle.id, ancienne.id]

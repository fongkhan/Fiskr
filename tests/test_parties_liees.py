"""
Les parties liées d'un client — bénéficiaires effectifs, mandataires,
dirigeants, associés — sont criblées, et une personne morale détenue ou
contrôlée par une personne désignée se voit.

Constat (enquête de conformité, octobre 2026) : le README annonçait le criblage
des « mandataires, bénéficiaires effectifs ». Rien ne pouvait porter ce lien :
`ClientEntity` n'en avait aucun, l'import non plus, aucune occurrence de « ubo »
ou de « mandataire » dans le code. La règle des 50 % ne parcourait que des liens
entre fiches LISTÉES : ACME, cliente non listée, détenue à 60 % par une personne
inscrite au registre national des gels, ne ressortait d'aucun criblage.

Lignes directrices conjointes DGT/ACPR sur le gel des avoirs (18 mars 2026) :
§117 et §141 (cribler bénéficiaires effectifs, mandataires, associés,
dirigeants, à l'entrée en relation et à chaque mise à jour), §142-144 (si le
bénéficiaire effectif désigné peut engager les fonds : s'abstenir et
interroger la DG Trésor ; actionnaire minoritaire : pas de gel), §131 (50 %,
détention agrégée : 30 % + 25 %). Commission des sanctions de l'ACPR,
9 février 2022, n° 2020-11, §27 : « aucun filtrage des mandataires et des
bénéficiaires effectifs n'étant réalisé » — grief fondé.
"""
import io
import json
import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import gels, parties
from fiskr.database import (Base, Alert, AlertEvent, AuditTrail, ClientEntity, MesureDeGel,
                            PartieLiee, Snapshot, WatchlistEntity, get_db)
from fiskr.identification import CLE_DES_PARTIES, empreinte_d_identification
from fiskr.rescreen import (_client_dicts, cribler_les_clients_nouveaux_ou_modifies,
                            rescreen_after_snapshot_change, rescreen_lookback)


# ================================================================ base isolée

@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'parties.sqlite3'}")
    Base.metadata.create_all(engine)
    # Comme la session de l'application (fiskr/database.py) : pas de vidage
    # implicite — une lecture ne voit que ce que le code a lui-meme vide.
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()


def _acme(db, client_id="ACME-1", nom="ACME HOLDING SAS"):
    snap = Snapshot(snapshot_id=f"clients-{uuid.uuid4().hex[:6]}", file_type="CLIENT_BASE",
                    file_name="clients.csv", file_hash=uuid.uuid4().hex,
                    record_count=1, status="READY")
    db.add(snap)
    db.add(ClientEntity(
        snapshot_id=snap.snapshot_id, client_id=client_id, client_type="PM",
        client_company_name=nom,
        client_countries={"nationality": [], "residence": [], "birth_country": [],
                          "registration_country": ["FR"]},
        entity_checksum=uuid.uuid4().hex))
    db.commit()
    return snap.snapshot_id


def _partie(prenom, nom, dob, pct=None, roles=("BENEFICIAIRE_EFFECTIF",), engage=None):
    return parties.lire_une_partie(
        {"client_type": "PP", "client_first_name": prenom, "client_last_name": nom,
         "client_dob": dob, "client_gender": "M",
         "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [],
                              "registration_country": []}},
        roles=list(roles), pourcentage=pct, peut_engager=engage)


def _declarer(db, client_id, lues):
    bilan = parties.remplacer_les_parties(db, client_id, lues, source=parties.SOURCE_IMPORT,
                                          source_ref="test", username="test")
    db.commit()
    return bilan


def _fiche(snap_id, entity_id, prenom, nom, dob, checksum=None):
    return WatchlistEntity(
        snapshot_id=snap_id, entity_id=entity_id, entity_type="I",
        primary_name=f"{prenom} {nom}",
        individual_name_parsed={"first_name": prenom, "last_name": nom, "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=[dob], is_deceased=False, gender="M",
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=checksum or f"chk-{entity_id}")


def _liste(db, snap_id, fiches, status="READY"):
    db.add(Snapshot(snapshot_id=snap_id, file_type="WATCHLIST_DGT",
                    file_name=f"{snap_id}.json", file_hash=uuid.uuid4().hex,
                    record_count=len(fiches), status=status))
    for f in fiches:
        db.add(f)
    db.commit()


def _confirmer(db, alerte):
    """Ce que fait `_close_alert` (fiskr/api.py) à toute confirmation."""
    from datetime import datetime
    alerte.status = "CLOSED_CONFIRMED"
    alerte.decided_by = "reviseur"
    alerte.decided_at = datetime.utcnow()
    mesure = gels.ouvrir_si_due(db, alerte, "reviseur")
    db.commit()
    return mesure


def _evenements(db, alerte, action):
    return [e.detail for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == alerte.id, AlertEvent.action == action).all()]


# ------------------------------------------------- la partie est criblée

def test_un_beneficiaire_effectif_liste_dgt_a_60_pct_ouvre_l_alerte_d_acme_qui_nomme_la_partie(db):
    """ACME n'est pas listée ; son bénéficiaire effectif l'est, à 60 %. Avant :
    aucune table ne portait ce lien, le lookback ne voyait qu'ACME."""
    _acme(db)
    _declarer(db, "ACME-1", [_partie("Viktor", "ZOLOTAREV", "1954-01-27", pct=60)])
    partie = db.query(PartieLiee).one()
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL", "Viktor", "ZOLOTAREV", "1954-01-27")])

    r = rescreen_lookback(db, "WATCHLIST_DGT")
    assert r["new_alerts"] == 1, r
    assert r["clients_screened"] == 1 and r["parties_screened"] == 1

    alerte = db.query(Alert).one()
    # Rattachée au CLIENT : c'est sa relation qui est en cause
    assert alerte.client_id == "ACME-1"
    assert alerte.client_name == "ACME HOLDING SAS"
    assert alerte.watchlist_entity_id == "DGT-ZOL"
    # ... avec la partie identifiée, et la chaîne qui le dit
    assert alerte.partie_liee_id == partie.id
    assert alerte.via_partie == ("via le bénéficiaire effectif Viktor ZOLOTAREV (60 %) "
                                 "du client ACME HOLDING SAS")
    assert any("via le bénéficiaire effectif Viktor ZOLOTAREV (60 %) du client ACME" in d
               for d in _evenements(db, alerte, "CREATED"))
    # Le journal porte la décision sur la partie : identité comparée, partie figée
    audit = db.get(AuditTrail, alerte.audit_id)
    assert audit.client_id == "ACME-1" and audit.partie_liee_id == partie.id
    assert audit.decision_tree["partie_liee"]["roles"] == ["BENEFICIAIRE_EFFECTIF"]
    assert audit.decision_tree["client_identification"]["client_last_name"] == "ZOLOTAREV"

    # Un second lookback re-détecte, il n'ouvre rien de plus
    r2 = rescreen_lookback(db, "WATCHLIST_DGT")
    assert r2["new_alerts"] == 0 and r2["redetected_alerts"] == 1
    assert db.query(Alert).count() == 1


def test_une_liste_mise_a_jour_qui_ajoute_la_partie_ouvre_l_alerte_sur_acme(db):
    """Re-criblage après mise en production d'une version de liste : seules
    les fiches nouvelles ou modifiées sont confrontées — aux clients ET à
    leurs parties. Puis une fiche modifiée qui touche encore la partie ne rend
    pas l'alerte caduque : la caducité se juge sur la partie, pas sur ACME."""
    _acme(db)
    _declarer(db, "ACME-1", [_partie("Viktor", "ZOLOTAREV", "1954-01-27", pct=35)])
    autre = ("Boris", "TCHERNOV", "1960-02-02")
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-TCH", *autre)])
    assert rescreen_lookback(db, "WATCHLIST_DGT")["new_alerts"] == 0

    db.query(Snapshot).filter(Snapshot.snapshot_id == "dgt-v1").update({"status": "SUPERSEDED"})
    _liste(db, "dgt-v2", [_fiche("dgt-v2", "DGT-TCH", *autre),
                          _fiche("dgt-v2", "DGT-ZOL", "Viktor", "ZOLOTAREV", "1954-01-27")])
    r = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v2", "dgt-v1")
    assert r["changed_entities"] == 1 and r["new_alerts"] == 1, r
    alerte = db.query(Alert).one()
    assert alerte.client_id == "ACME-1" and alerte.partie_liee_id is not None
    assert "(35 %) du client ACME HOLDING SAS" in alerte.via_partie

    # v3 : la fiche du listé change (nouvelle empreinte), la partie la touche toujours
    db.query(Snapshot).filter(Snapshot.snapshot_id == "dgt-v2").update({"status": "SUPERSEDED"})
    _liste(db, "dgt-v3", [_fiche("dgt-v3", "DGT-TCH", *autre),
                          _fiche("dgt-v3", "DGT-ZOL", "Viktor", "ZOLOTAREV", "1954-01-27",
                                 checksum="chk-DGT-ZOL-v3")])
    r3 = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v3", "dgt-v2")
    assert r3["redetected_alerts"] == 1 and r3["lapsed_alerts"] == 0, r3
    db.refresh(alerte)
    assert alerte.lapsed_since is None


# ------------------------------------------------- détention réputée

def _deux_beneficiaires(db, pct1, pct2, engage2=None):
    _acme(db)
    _declarer(db, "ACME-1", [_partie("Viktor", "ZOLOTAREV", "1954-01-27", pct=pct1),
                             _partie("Igor", "SOKOLINE", "1958-08-08", pct=pct2,
                                     engage=engage2)])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL", "Viktor", "ZOLOTAREV", "1954-01-27"),
                          _fiche("dgt-v1", "DGT-SOK", "Igor", "SOKOLINE", "1958-08-08")])
    assert rescreen_lookback(db, "WATCHLIST_DGT")["new_alerts"] == 2
    return (db.query(Alert).filter(Alert.watchlist_entity_id == "DGT-ZOL").one(),
            db.query(Alert).filter(Alert.watchlist_entity_id == "DGT-SOK").one())


def test_deux_parties_confirmees_a_30_et_25_pct_rendent_acme_reputee_detenue(db):
    """§131 : « si une première personne désignée possède 30 % [...] et qu'une
    deuxième en possède 25 %, l'entité détenue conjointement devrait être
    gelée en conséquence »."""
    premiere, seconde = _deux_beneficiaires(db, 30, 25)

    # 30 % seuls : pas de détention réputée, et l'historique dit pourquoi
    assert _confirmer(db, premiere) is None
    assert any("30 % cumulés" in d for d in _evenements(db, premiere, "DETENTION_NON_ETABLIE"))
    assert db.query(MesureDeGel).count() == 0

    # + 25 % : 55 % cumulés — ACME réputée détenue, mesure de GEL d'ACME
    mesure = _confirmer(db, seconde)
    assert mesure is not None
    assert mesure.client_id == "ACME-1" and mesure.client_name == "ACME HOLDING SAS"
    assert mesure.type_action == "GEL" and mesure.statut == gels.STATUT_A_DECLARER
    assert mesure.fondement == parties.FONDEMENT_DETENTION
    assert mesure.detention["cumul_pct"] == 55.0
    assert {p["nom"] for p in mesure.detention["parties"]} == {"Viktor ZOLOTAREV", "Igor SOKOLINE"}
    assert any("réputé détenu" in d for d in _evenements(db, seconde, "DETENTION_REPUTEE"))
    vue = gels.serialiser(db, mesure)
    assert vue["fondement_label"] == parties.FONDEMENTS[parties.FONDEMENT_DETENTION]
    # Les deux vrais positifs ne passent pas pour des « confirmés sans mesure »
    assert gels.alertes_confirmees_sans_mesure(db) == []


def test_une_partie_a_20_pct_sans_pouvoir_d_engager_ne_rend_pas_acme_reputee_detenue(db):
    """§144 : « dans les autres cas, l'organisme financier n'a pas à geler les
    fonds de la personne morale (par exemple, lorsque le bénéficiaire effectif
    n'est qu'actionnaire minoritaire) »."""
    _acme(db)
    _declarer(db, "ACME-1", [_partie("Viktor", "ZOLOTAREV", "1954-01-27", pct=20)])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL", "Viktor", "ZOLOTAREV", "1954-01-27")])
    rescreen_lookback(db, "WATCHLIST_DGT")
    alerte = db.query(Alert).one()

    assert _confirmer(db, alerte) is None
    evaluation = parties.evaluer_la_detention(db, "ACME-1")
    assert evaluation["reputee"] is False and evaluation["cumul_pct"] == 20.0
    assert db.query(MesureDeGel).count() == 0
    assert _evenements(db, alerte, "DETENTION_NON_ETABLIE"), "la décision doit se lire"
    # Le registre ne réclame pas de mesure pour ce vrai positif (§144)
    assert gels.alertes_confirmees_sans_mesure(db) == []
    # Le geste du doute reste ouvert à l'analyste : suspendre, interroger
    mesure = gels.ouvrir(db, alerte, "SUSPENSION_MISE_A_DISPOSITION", "analyste")
    assert mesure.client_id == "ACME-1"


def test_une_partie_designee_qui_peut_engager_les_fonds_fait_suspendre_et_interroger_sans_geler(db):
    """§144 : le bénéficiaire effectif désigné peut engager les fonds → « il
    s'abstient d'exécuter l'opération et interroge la DG Trésor ». Pas « gelez » :
    la mesure est une suspension à déclarer, le client n'est pas marqué gelé."""
    premiere, seconde = _deux_beneficiaires(db, 10, None, engage2=True)
    mesure = _confirmer(db, seconde)
    assert mesure is not None
    assert mesure.fondement == parties.FONDEMENT_CONTROLE
    assert mesure.type_action == "SUSPENSION_MISE_A_DISPOSITION"
    assert mesure.type_action not in gels.ACTIONS_QUI_GELENT
    assert any("interroger la DG Trésor" in d for d in _evenements(db, seconde, "DETENTION_REPUTEE"))
    # Une seconde partie confirmée n'ouvre pas une seconde mesure
    assert _confirmer(db, premiere) is None
    assert db.query(MesureDeGel).count() == 1
    assert any(f"#{mesure.id} déjà ouverte" in d
               for d in _evenements(db, premiere, "DETENTION_REPUTEE"))


def test_un_vrai_positif_hors_sanction_sur_une_partie_ne_compte_pas(db):
    """Un PEP n'est pas une « personne désignée » : sa détention ne s'agrège pas."""
    premiere, seconde = _deux_beneficiaires(db, 60, 10)
    premiere.list_type = "WATCHLIST_PEP"
    db.commit()
    assert _confirmer(db, premiere) is None
    assert parties.evaluer_la_detention(db, "ACME-1")["reputee"] is False


# ------------------------------------------------- la partie date le client

def test_une_partie_ajoutee_rend_le_client_modifie_et_le_fait_recribler(db):
    """Une partie criblée date son client : l'empreinte d'identification de la
    relation couvre ses parties. Un bénéficiaire effectif ajouté est une mise à
    jour des éléments d'identification (§141) : le criblage des clients
    nouveaux ou modifiés reprend ACME — et lui seul. Sans partie, l'empreinte
    est celle d'avant : la mise à jour ne recrible personne pour rien."""
    snap = _acme(db)
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL", "Viktor", "ZOLOTAREV", "1954-01-27")])
    fiche = _client_dicts(db)[0]
    assert CLE_DES_PARTIES not in fiche
    # La formule d'avant, recalculée ici : un client sans partie n'en change pas
    import hashlib
    from fiskr.identification import champs_lus_par_le_moteur
    lus = champs_lus_par_le_moteur()
    avant = hashlib.sha256(json.dumps({c: fiche[c] for c in sorted(lus) if c in fiche},
                                      sort_keys=True, default=str, ensure_ascii=False)
                           .encode("utf-8")).hexdigest()
    assert empreinte_d_identification(fiche) == avant

    premier = cribler_les_clients_nouveaux_ou_modifies(db, snap)
    assert premier["clients_screened"] == 1 and premier["new_alerts"] == 0
    assert cribler_les_clients_nouveaux_ou_modifies(db, snap)["unchanged_clients"] == 1

    _declarer(db, "ACME-1", [_partie("Viktor", "ZOLOTAREV", "1954-01-27", pct=60)])
    second = cribler_les_clients_nouveaux_ou_modifies(db, snap)
    assert second["clients_screened"] == 1, "la partie ajoutée n'a pas fait recribler ACME"
    assert second["parties_screened"] == 1 and second["new_alerts"] == 1
    trace = db.query(ClientEntity).one()
    assert trace.screened_identity_sha256 == empreinte_d_identification(_client_dicts(db)[0])
    assert cribler_les_clients_nouveaux_ou_modifies(db, snap)["unchanged_clients"] == 1


# ------------------------------------------------- vocabulaire dérivé, refus francs

def test_l_identite_d_une_partie_est_derivee_des_elements_d_identification_du_client():
    from fiskr.empreintes import champs_client_cribles
    assert parties.champs_d_identite() == champs_client_cribles()
    assert "client_last_name" in parties.champs_d_identite()
    assert "client_segment" not in parties.champs_d_identite()
    with pytest.raises(parties.RefusDePartie, match="client_segment"):
        parties.lire_une_partie({"client_last_name": "X", "client_segment": "PME"},
                                roles=["BE"])
    with pytest.raises(parties.RefusDePartie, match="rôle inconnu"):
        parties.lire_une_partie({"client_last_name": "Dupont"}, roles=["COMPTABLE"])
    with pytest.raises(parties.RefusDePartie, match="hors de"):
        parties.lire_une_partie({"client_last_name": "Dupont"}, roles=["BE"], pourcentage=150)
    lue = parties.lire_une_partie({"client_first_name": "Jean", "client_last_name": "Dupont"},
                                  roles="BE, gérant", pourcentage="35,5 %")
    assert lue["roles"] == ["BENEFICIAIRE_EFFECTIF", "DIRIGEANT"]
    assert lue["pourcentage_detention"] == 35.5
    # Un mandataire est autorisé à agir au nom du client (§117, §132) : sauf
    # « non » explicite, il peut engager les fonds
    assert parties.lire_une_partie({"client_last_name": "Mandat"}, roles=["MANDATAIRE"]
                                   )["peut_engager_les_fonds"] is True
    assert parties.lire_une_partie({"client_last_name": "Mandat"}, roles=["MANDATAIRE"],
                                   peut_engager="non")["peut_engager_les_fonds"] is False


def test_un_jeu_de_parties_remplace_le_precedent_sans_rien_supprimer(db):
    """Mise à jour en place (même identifiant, mêmes alertes), retrait daté,
    réactivation : jamais de suppression qui laisserait une alerte orpheline."""
    viktor = _partie("Viktor", "ZOLOTAREV", "1954-01-27", pct=60)
    igor = _partie("Igor", "SOKOLINE", "1958-08-08", pct=10)
    assert _declarer(db, "ACME-1", [viktor, igor])["creees"] == 2
    ids = {p.partie_id: p.id for p in db.query(PartieLiee).all()}
    bilan = _declarer(db, "ACME-1", [{**viktor, "pourcentage_detention": 70.0}])
    assert bilan["modifiees"] == 1 and bilan["retirees"] == 1
    assert bilan["identite_changee"] is True, "un retrait change la relation"
    assert db.query(PartieLiee).count() == 2
    assert db.get(PartieLiee, ids[viktor["partie_id"]]).pourcentage_detention == 70.0
    assert db.get(PartieLiee, ids[igor["partie_id"]]).retiree_le is not None
    bilan = _declarer(db, "ACME-1", [{**viktor, "pourcentage_detention": 70.0}, igor])
    assert bilan["reactivees"] == 1 and db.get(PartieLiee, ids[igor["partie_id"]]).retiree_le is None
    assert _declarer(db, "ACME-1", [{**viktor, "pourcentage_detention": 70.0}, igor]
                     )["identite_changee"] is False


# ================================================================ par l'API

ADMIN = {"id": 1, "username": "admin_partie", "full_name": "Admin",
         "role": "admin,reviewer", "roles": ["admin", "reviewer"]}


@pytest.fixture
def client():
    from fiskr.api import app
    from fiskr.auth import get_current_user
    from fiskr.settings import SETTING_ALERT_FOUR_EYES, set_setting
    from fiskr.database import AppSetting
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    session = next(get_db())
    avant = session.query(AppSetting).filter(AppSetting.key == SETTING_ALERT_FOUR_EYES).first()
    valeur = avant.value if avant else None
    set_setting(session, SETTING_ALERT_FOUR_EYES, False)
    _LISTES.clear()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        ligne = session.query(AppSetting).filter(AppSetting.key == SETTING_ALERT_FOUR_EYES).first()
        if avant is None and ligne is not None:
            session.delete(ligne)
        elif ligne is not None:
            ligne.value = valeur
        # Les listes de ce test quittent la production : un homonyme d'un autre
        # module (« Igor Corpsov… », même pays) les rencontrerait sinon.
        if _LISTES:
            from fiskr.settings import bump_watchlist_epoch
            session.query(WatchlistEntity).filter(
                WatchlistEntity.snapshot_id.in_(_LISTES)).delete(synchronize_session=False)
            session.query(Snapshot).filter(
                Snapshot.snapshot_id.in_(_LISTES)).delete(synchronize_session=False)
            bump_watchlist_epoch(session)
        session.commit()
        session.close()
        app.dependency_overrides.pop(get_current_user, None)


_LISTES: list = []


def _ingest(client, file_type, corps, nom):
    r = client.post("/api/ingest", data={"file_type": file_type},
                    files={"file": (f"{nom}_{uuid.uuid4().hex[:6]}.csv", corps, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _liste_ue(client, personnes):
    corps = "entity_id,entity_type,primary_name,nationality,dob\n" + "".join(
        f"{eid},I,{nom},RU,{dob}\n" for eid, nom, dob in personnes)
    resultat = _ingest(client, "WATCHLIST_EU", corps, "test_partie_wl")
    _LISTES.append(resultat["snapshot_id"])
    return resultat


def _alertes(client_id):
    session = next(get_db())
    try:
        return session.query(Alert).filter(Alert.client_id == client_id).order_by(Alert.id).all()
    finally:
        session.close()


def test_le_fichier_des_parties_liees_s_importe_crible_et_rend_acme_reputee_detenue(client):
    """De bout en bout, par les portes de l'exploitant : liste en production,
    référentiel clients, fichier des parties liées (aperçu compris), alertes
    rattachées à ACME, confirmations, mesure de gel au registre, vue 360."""
    tag = uuid.uuid4().hex[:6].upper()
    acme = f"test_partie_ACME_{tag}"
    _liste_ue(client, [(f"PL-ZOL-{tag}", f"Vsevolod Zolotarev{tag}", "1954-01-27"),
                       (f"PL-SOK-{tag}", f"Arkhip Sokoline{tag}", "1958-08-08")])
    _ingest(client, "CLIENT_BASE", "client_id,client_type,client_company_name\n"
            f"{acme},PM,ACME HOLDING {tag}\n", "test_partie_cb")

    fichier = ("client_id,partie_id,role,client_type,client_first_name,client_last_name,"
               "client_dob,nationality,pourcentage_detention,peut_engager_les_fonds\n"
               f"{acme},UBO-1,BE,PP,Vsevolod,Zolotarev{tag},1954-01-27,RU,30,non\n"
               f"{acme},UBO-2,beneficiaire effectif,PP,Arkhip,Sokoline{tag},1958-08-08,RU,25,\n")
    apercu = client.post("/api/ingest/preview", data={"file_type": "CLIENT_PARTIES"},
                         files={"file": ("parties.csv", fichier, "text/csv")})
    assert apercu.status_code == 200, apercu.text
    assert apercu.json()["acceptees"] == 2
    assert "pourcentage_detention" in apercu.json()["champs_attendus"]

    resultat = _ingest(client, "CLIENT_PARTIES", fichier, "test_partie_pl")
    assert resultat["record_count"] == 2, resultat
    assert "2 partie(s) liée(s) enregistrée(s)" in resultat["message"]

    alertes = _alertes(acme)
    assert len(alertes) == 2, resultat
    assert all(a.partie_liee_id for a in alertes)
    assert {a.watchlist_entity_id for a in alertes} == {f"PL-ZOL-{tag}", f"PL-SOK-{tag}"}
    zol = next(a for a in alertes if a.watchlist_entity_id == f"PL-ZOL-{tag}")
    assert zol.via_partie == (f"via le bénéficiaire effectif Vsevolod Zolotarev{tag} (30 %) "
                              f"du client ACME HOLDING {tag}")

    detail = client.get(f"/api/alerts/{zol.id}").json()
    assert detail["via_partie"] == zol.via_partie
    assert detail["partie_liee"]["actuelle"]["nom"] == f"Vsevolod Zolotarev{tag}"
    assert detail["comparison"]["client_source"] == "PARTIE_LIEE"

    for alerte in alertes:
        r = client.post(f"/api/alerts/{alerte.id}/propose",
                        json={"decision": "CONFIRMED", "comment": "Identité alignée, pièces au dossier."})
        assert r.status_code == 200, r.text
    mesures = client.get(f"/api/gels?q={acme}").json()["items"]
    assert len(mesures) == 1, mesures
    assert mesures[0]["fondement"] == parties.FONDEMENT_DETENTION
    assert mesures[0]["type_action"] == "GEL" and mesures[0]["client_id"] == acme

    vue = client.get(f"/api/clients/{acme}/overview").json()
    assert len(vue["parties"]) == 2
    assert {p["nom"] for p in vue["parties"]} == {f"Vsevolod Zolotarev{tag}", f"Arkhip Sokoline{tag}"}
    assert vue["detention"]["reputee"] is True and vue["detention"]["cumul_pct"] == 55.0


def test_une_ligne_refusee_ne_retire_pas_les_parties_existantes_du_client(client):
    tag = uuid.uuid4().hex[:6].upper()
    acme = f"test_partie_REF_{tag}"
    entete = "client_id,partie_id,role,client_type,client_last_name,pourcentage_detention\n"
    _ingest(client, "CLIENT_PARTIES", entete + f"{acme},P1,BE,PP,Premier{tag},40\n", "test_partie_pl")
    apercu = client.post("/api/ingest/preview", data={"file_type": "CLIENT_PARTIES"},
                         files={"file": ("p.csv", entete + f"{acme},P2,COMPTABLE,PP,Second{tag},10\n",
                                         "text/csv")}).json()
    assert apercu["acceptees"] == 0 and "rôle inconnu" in apercu["lignes"][0]["motifs"][0]

    r = _ingest(client, "CLIENT_PARTIES",
                entete + f"{acme},P2,BE,PP,Second{tag},10\n{acme},P3,COMPTABLE,PP,Tiers{tag},5\n",
                "test_partie_pl")
    assert r["rejected_count"] == 1 and "NON mis à jour" in r["message"]
    session = next(get_db())
    try:
        actives = session.query(PartieLiee).filter(PartieLiee.client_id == acme,
                                                   PartieLiee.retiree_le.is_(None)).all()
        assert [p.partie_id for p in actives] == ["P1"], "une ligne refusée a retiré une partie déclarée"
    finally:
        session.close()


@contextmanager
def _sans_session_humaine():
    from fiskr.api import app
    from fiskr.auth import get_current_user
    sauve = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield
    finally:
        if sauve is not None:
            app.dependency_overrides[get_current_user] = sauve


def _upsert(client, cle_api, corps):
    with _sans_session_humaine():
        return client.post("/api/hooks/client-upsert", content=json.dumps(corps),
                           headers={"X-API-Key": cle_api, "Content-Type": "application/json"})


def test_un_upsert_sans_parties_n_efface_pas_les_parties_existantes(client):
    """CONTRAT : la charge utile remplace la fiche, pas les parties dont elle
    ne dit rien. Une liste, même vide, est le jeu complet."""
    tag = uuid.uuid4().hex[:6].upper()
    acme = f"test_partie_UPS_{tag}"
    _liste_ue(client, [(f"PL-UPS-{tag}", f"Yaropolk Voronine{tag}", "1962-06-06")])
    _ingest(client, "CLIENT_BASE", "client_id,client_type,client_last_name\n"
            f"test_partie_socle_{tag},PP,Socle\n", "test_partie_cb")
    cle = client.post("/api/apikeys", json={"name": f"test_partie_{tag}", "role": "user"}).json()["api_key"]
    fiche = {"client_id": acme, "client_type": "PM", "client_company_name": f"ACME UPSERT {tag}"}
    partie = {"partie_id": "UBO-9", "roles": ["DIRIGEANT", "BENEFICIAIRE_EFFECTIF"],
              "pourcentage_detention": 60, "peut_engager_les_fonds": True,
              "identite": {"client_type": "PP", "client_first_name": "Yaropolk",
                           "client_last_name": f"Voronine{tag}", "client_dob": "1962-06-06",
                           "client_countries": {"nationality": ["RU"]}}}

    r = _upsert(client, cle, {**fiche, "parties": [partie]})
    assert r.status_code == 200, r.text
    assert r.json()["parties"]["creees"] == 1
    criblage = r.json()["screening"]
    assert criblage["performed"] is True
    assert "en alerte" in criblage["summary"] and "bénéficiaire effectif" in criblage["summary"]
    assert [a.partie_liee_id is not None for a in _alertes(acme)] == [True]

    # Upsert SANS parties (le SI ne les connaît pas) : elles restent
    r = _upsert(client, cle, {**fiche, "client_email": "contact@acme.test"})
    assert r.status_code == 200, r.text
    assert r.json()["parties"]["applied"] is False
    session = next(get_db())
    try:
        assert session.query(PartieLiee).filter(PartieLiee.client_id == acme,
                                                PartieLiee.retiree_le.is_(None)).count() == 1
    finally:
        session.close()

    # Un champ d'identité inconnu est refusé, pas ignoré — et rien n'est écrit
    mauvaise = {**partie, "identite": {**partie["identite"], "client_segment": "VIP"}}
    r = _upsert(client, cle, {**fiche, "parties": [mauvaise]})
    assert r.status_code == 422 and "client_segment" in r.text

    # Liste vide : le SI dit qu'il n'y a plus de parties — retrait daté
    r = _upsert(client, cle, {**fiche, "parties": []})
    assert r.status_code == 200, r.text
    assert r.json()["parties"]["retirees"] == 1
    session = next(get_db())
    try:
        assert session.query(PartieLiee).filter(PartieLiee.client_id == acme,
                                                PartieLiee.retiree_le.is_(None)).count() == 0
    finally:
        session.close()


def test_le_criblage_temps_reel_du_client_crible_ses_parties(client):
    tag = uuid.uuid4().hex[:6].upper()
    acme = f"test_partie_TR_{tag}"
    _liste_ue(client, [(f"PL-TR-{tag}", f"Svyatoslav Grekov{tag}", "1949-09-09")])
    _ingest(client, "CLIENT_BASE", "client_id,client_type,client_company_name\n"
            f"{acme},PM,ACME TEMPS REEL {tag}\n", "test_partie_cb")
    _ingest(client, "CLIENT_PARTIES",
            "client_id,role,client_type,client_first_name,client_last_name,client_dob,nationality\n"
            f"{acme},MANDATAIRE,PP,Svyatoslav,Grekov{tag},1949-09-09,RU\n", "test_partie_pl")
    r = client.post(f"/api/clients/{acme}/screen")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data["parties"]) == 1
    assert data["parties"][0]["status"] == "ALERT" and data["parties"][0]["alert_id"]
    assert data["parties"][0]["partie"]["chaine"].startswith(
        f"via le mandataire Svyatoslav Grekov{tag} (peut engager les fonds) du client ACME")

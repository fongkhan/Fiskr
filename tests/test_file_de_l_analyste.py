"""
Traiter une file d'alertes sans perdre sa place ni refaire dix fois le même geste.

Mesuré dans un vrai navigateur, sur une file de 26 alertes ouvertes :

* une seule fiche listée (PIERRE DUBOIS, liste UE) en produisait 10, et deux
  clients en portaient deux chacun (une OFAC, une UE, la même personne). La
  même décision se motivait dix fois à la main : `/api/alerts/bulk` ne
  savait qu'assigner et changer la priorité ;
* l'accueil d'une analyste disait « Rien ne vous attend » devant ces 26
  alertes non assignées — le compte ne retenait que `assigned_to == moi` ;
* après avoir proposé l'alerte n° 20, en 13ᵉ ligne, puis Échap et `j`, la
  surbrillance repartait du haut de la file et le focus tombait sur BODY.

Ce que ces tests tiennent :
- une proposition en masse écrit UNE proposition par alerte, avec le motif
  commun ; le 4-yeux reste unitaire — le proposeur ne valide aucune de ses
  propositions, et la validation ne se fait pas en masse ;
- une alerte assignée à quelqu'un d'autre, close, déjà proposée, ou d'un
  périmètre où la règle l'interdit est écartée, avec sa raison ;
- le motif appartient à la décision proposée, à l'unité comme en masse (une
  seule fonction : settings.motifs_d_une_autre_decision) ;
- un faux positif en masse sur une liste de gel exige une confirmation
  explicite, et rien n'est écrit sans elle ;
- « Prendre la suivante » s'assigne ATOMIQUEMENT l'alerte non assignée la plus
  urgente — échéance, puis priorité — et dit franchement quand il n'y a rien ;
- l'accueil compte les alertes non assignées avec les mêmes critères, sans
  les dire « à vous ».
"""
import os
import re
import threading
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from fiskr import database
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import Alert, AlertEvent, AppSetting, AuditTrail, MesureDeGel, get_db
from fiskr.settings import (DEFAULT_ALERT_CLOSE_REASONS, SETTING_ALERT_CLOSE_REASONS,
                            SETTING_ALERT_FOUR_EYES, set_setting)

UID = uuid.uuid4().hex[:8].upper()
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")
MOTIF_FP = "Homonymie : date de naissance incompatible avec la fiche listée."
MOTIF_VP = "Correspondance confirmée : identité alignée sur la fiche listée (pièces au dossier)."


def _connecter(username, roles=("user", "reviewer")):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": ",".join(roles), "roles": list(roles),
    }


def _nouvelle_alerte(db, list_type="WATCHLIST_PEP", statut="OPEN", assignee=None,
                     priorite="MEDIUM", echeance=None, reportee_jusqu_a=None,
                     canal="SCREENING", score=88.0, nom=None):
    nom = nom or uuid.uuid4().hex[:6]
    audit = AuditTrail(client_id=f"FILE-{UID}-{nom}", client_name=f"Client {nom}",
                       client_type="PP", watchlist_id=f"WL-{UID}-{nom}",
                       watchlist_name=f"Listé {nom}", base_score=score, final_score=score,
                       status="ALERT", decision_tree={"hard_match_triggered": False},
                       config_state={}, watchlist_version="test", watchlist_hash="test",
                       list_type=list_type)
    db.add(audit)
    db.commit()
    alerte = Alert(audit_id=audit.id, client_id=audit.client_id, client_name=audit.client_name,
                   watchlist_entity_id=audit.watchlist_id, watchlist_name=audit.watchlist_name,
                   final_score=score, list_type=list_type, status=statut, channel=canal,
                   priority=priorite, assigned_to=assignee, due_at=echeance,
                   snoozed_until=reportee_jusqu_a)
    db.add(alerte)
    db.commit()
    return alerte.id


def _traduit_partout(message):
    """Les messages du serveur se traduisent dans les cinq autres langues de
    l'interface (fiskr/apimessages.py) : un gabarit qui ne correspond plus
    laisserait du français devant un lecteur qui a choisi l'anglais."""
    from fiskr.apimessages import translate_message
    for langue in ("en", "de", "es", "zh", "ar"):
        assert translate_message(message, langue), f"[{langue}] {message}"


def _evenements(db, alert_id, action):
    return db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id,
                                       AlertEvent.action == action).all()


# =================================================== proposition en masse

@pytest.fixture()
def file():
    """Base partagée de la suite, 4-yeux actif, bibliothèque de motifs livrée."""
    db = next(get_db())
    cles = (SETTING_ALERT_FOUR_EYES, SETTING_ALERT_CLOSE_REASONS)
    avant = {r.key: r.value for r in db.query(AppSetting).filter(AppSetting.key.in_(cles))}
    set_setting(db, SETTING_ALERT_FOUR_EYES, True)
    db.query(AppSetting).filter(AppSetting.key == SETTING_ALERT_CLOSE_REASONS) \
      .delete(synchronize_session=False)
    db.commit()
    creees = []

    def alerte(**kw):
        creees.append(_nouvelle_alerte(db, **kw))
        return creees[-1]

    client = TestClient(app)
    _connecter(f"analyste_{UID}")
    yield {"db": db, "client": client, "alerte": alerte}
    app.dependency_overrides.pop(get_current_user, None)
    db.rollback()
    for cle in cles:
        db.query(AppSetting).filter(AppSetting.key == cle).delete(synchronize_session=False)
        if cle in avant:
            db.add(AppSetting(key=cle, value=avant[cle]))
    audits = [a.audit_id for a in db.query(Alert).filter(Alert.id.in_(creees)).all()]
    db.query(MesureDeGel).filter(MesureDeGel.alert_id.in_(creees)).delete(synchronize_session=False)
    db.query(AlertEvent).filter(AlertEvent.alert_id.in_(creees)).delete(synchronize_session=False)
    db.query(Alert).filter(Alert.id.in_(creees)).delete(synchronize_session=False)
    db.query(AuditTrail).filter(AuditTrail.id.in_(audits)).delete(synchronize_session=False)
    db.commit()
    db.close()


def _proposer(client, ids, decision="FALSE_POSITIVE", comment=MOTIF_FP, **extra):
    return client.post("/api/alerts/bulk", json={"ids": ids, "action": "propose",
                                                  "decision": decision, "comment": comment, **extra})


def test_proposer_en_masse_trois_alertes_ecrit_trois_propositions_et_ecarte_celle_d_un_autre(file):
    """Le cas mesuré : trois alertes instruites d'un geste, une quatrième au
    collègue qui l'a prise. Chacune des trois reçoit SON événement PROPOSED,
    avec le motif commun ; la quatrième est rendue avec sa raison, intacte."""
    db, client = file["db"], file["client"]
    ids = [file["alerte"]() for _ in range(3)]
    autre = file["alerte"](assignee="collegue_occupe", statut="IN_PROGRESS")
    r = _proposer(client, ids + [autre])
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["updated"] == ids
    assert [(e["id"], e["code"], e["assigned_to"]) for e in corps["skipped"]] == \
        [(autre, "ASSIGNEE_A_UN_AUTRE", "collegue_occupe")]
    assert corps["skipped"][0]["reason"]
    _traduit_partout(corps["message"])
    db.expire_all()
    for alert_id in ids:
        propositions = _evenements(db, alert_id, "PROPOSED")
        assert len(propositions) == 1
        assert MOTIF_FP in propositions[0].detail
        alerte = db.get(Alert, alert_id)
        assert alerte.status == "PENDING_VALIDATION"
        assert alerte.proposed_by == f"analyste_{UID}"
        assert alerte.proposed_decision == "FALSE_POSITIVE"
    assert not _evenements(db, autre, "PROPOSED")
    assert db.get(Alert, autre).status == "IN_PROGRESS"


def test_le_proposeur_ne_valide_aucune_de_ses_propositions_en_masse(file):
    """Le proposeur porte ici le rôle réviseur : la règle des quatre yeux lui
    interdit quand même chacune de SES propositions, une par une. Un autre
    réviseur, lui, valide — à l'unité."""
    client = file["client"]
    ids = [file["alerte"]() for _ in range(3)]
    assert _proposer(client, ids).status_code == 200
    for alert_id in ids:
        r = client.post(f"/api/alerts/{alert_id}/validate", json={"approve": True})
        assert r.status_code == 403, r.text
    _connecter(f"reviseur_{UID}")
    r = client.post(f"/api/alerts/{ids[0]}/validate", json={"approve": True, "comment": "Vu."})
    assert r.status_code == 200, r.text
    assert file["db"].get(Alert, ids[0]).status == "CLOSED_FALSE_POSITIVE"


def test_la_validation_4_yeux_ne_se_fait_pas_en_masse(file):
    alert_id = file["alerte"](statut="PENDING_VALIDATION")
    r = file["client"].post("/api/alerts/bulk", json={"ids": [alert_id], "action": "validate"})
    assert r.status_code == 400
    assert "reste unitaire" in r.json()["detail"]


def test_un_motif_de_l_autre_decision_est_refuse_a_l_unite_comme_en_masse(file):
    """Un faux positif motivé par « Correspondance confirmée… » est une pièce
    qui se contredit. La même fonction le refuse sur les deux chemins, et rien
    n'est écrit. Un motif écrit par l'administrateur n'a pas de décision
    connue : il vaut pour les deux."""
    db, client = file["db"], file["client"]
    unitaire, en_masse = file["alerte"](), file["alerte"]()
    r = client.post(f"/api/alerts/{unitaire}/propose",
                    json={"decision": "FALSE_POSITIVE", "comment": MOTIF_VP})
    assert r.status_code == 400, r.text
    assert "Motif réservé" in r.json()["detail"]
    _traduit_partout(r.json()["detail"])
    r = _proposer(client, [en_masse], comment=f"Après instruction. {MOTIF_VP}")
    assert r.status_code == 400, r.text
    r = _proposer(client, [en_masse], decision="CONFIRMED", comment=MOTIF_FP)
    assert r.status_code == 400, r.text
    db.expire_all()
    for alert_id in (unitaire, en_masse):
        assert not _evenements(db, alert_id, "PROPOSED")
        assert db.get(Alert, alert_id).status == "OPEN"
    maison = "Motif maison sans décision connue."
    set_setting(db, SETTING_ALERT_CLOSE_REASONS, list(DEFAULT_ALERT_CLOSE_REASONS) + [maison])
    assert _proposer(client, [en_masse], decision="CONFIRMED", comment=maison).status_code == 200


def test_un_faux_positif_en_masse_sur_une_liste_de_gel_exige_une_confirmation_explicite(file):
    """Périmètre SANCTION : une correspondance de gel manquée est constatable à
    l'audit et sanctionnable. Sans confirmation, RIEN n'est écrit — pas même
    l'alerte PEP du même lot — et la réponse nomme les alertes concernées."""
    db, client = file["db"], file["client"]
    ofac, pep = file["alerte"](list_type="WATCHLIST_OFAC"), file["alerte"]()
    r = _proposer(client, [ofac, pep])
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "CONFIRMATION_SANCTION"
    assert detail["alert_ids"] == [ofac]
    assert "périmètre SANCTION" in detail["message"] and "Rien n'est clos" in detail["message"]
    _traduit_partout(detail["message"])
    db.expire_all()
    assert not _evenements(db, ofac, "PROPOSED") and not _evenements(db, pep, "PROPOSED")
    r = _proposer(client, [ofac, pep], confirm_sanction=True)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == [ofac, pep]
    # Un vrai positif ne perd rien : pas de confirmation à réclamer
    autre_ofac = file["alerte"](list_type="WATCHLIST_OFAC")
    assert _proposer(client, [autre_ofac], decision="CONFIRMED", comment=MOTIF_VP).status_code == 200


def test_sans_4_yeux_une_liste_de_gel_ne_se_clot_pas_en_masse(file):
    """4-yeux désactivé : proposer, c'est clore. Le périmètre SANCTION ne se clôt
    pas en volume — l'alerte OFAC est écartée, même confirmée ; l'alerte PEP est
    close comme à l'unité (PROPOSED puis VALIDATED)."""
    db, client = file["db"], file["client"]
    set_setting(db, SETTING_ALERT_FOUR_EYES, False)
    ofac, pep = file["alerte"](list_type="WATCHLIST_OFAC"), file["alerte"]()
    r = _proposer(client, [ofac, pep], confirm_sanction=True)
    assert r.status_code == 200, r.text
    assert r.json()["updated"] == [pep]
    _traduit_partout(r.json()["message"])
    assert [(e["id"], e["code"]) for e in r.json()["skipped"]] == [(ofac, "SANCTION_SANS_QUATRE_YEUX")]
    db.expire_all()
    assert db.get(Alert, ofac).status == "OPEN"
    assert db.get(Alert, pep).status == "CLOSED_FALSE_POSITIVE"
    assert _evenements(db, pep, "PROPOSED") and _evenements(db, pep, "VALIDATED")


def test_une_alerte_close_ou_deja_proposee_est_ecartee_avec_sa_raison(file):
    close = file["alerte"](statut="CLOSED_CONFIRMED")
    proposee = file["alerte"](statut="PENDING_VALIDATION")
    r = _proposer(file["client"], [close, proposee, 987654321])
    assert r.status_code == 200, r.text
    ecartees = {e["id"]: e for e in r.json()["skipped"]}
    assert ecartees[close]["code"] == "CLOSE" and ecartees[close]["status"] == "CLOSED_CONFIRMED"
    assert ecartees[proposee]["code"] == "EN_ATTENTE_DE_VALIDATION"
    assert ecartees[987654321]["code"] == "INTROUVABLE"
    assert all(e["reason"] for e in ecartees.values())
    assert r.json()["updated"] == []


# ====================================================== prendre la suivante

@pytest.fixture()
def base_isolee(tmp_path):
    """Une base à soi : « la plus urgente des non assignées » se lit sur TOUTE la
    base, et celle de la suite porte les alertes ouvertes des autres modules."""
    moteur = create_engine(f"sqlite:///{tmp_path / 'file.sqlite3'}",
                           connect_args={"check_same_thread": False})
    database._tune_sqlite(moteur)
    database.Base.metadata.create_all(bind=moteur)
    Session = sessionmaker(bind=moteur, autoflush=False)

    def _get_db():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    def _utilisateur(request: Request):
        nom = request.headers.get("X-Utilisateur", "marie")
        role = request.headers.get("X-Role", "user")
        return {"id": 1, "username": nom, "full_name": nom, "role": role, "roles": [role]}

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = _utilisateur
    db = Session()
    yield {"moteur": moteur, "db": db, "client": TestClient(app),
           "alerte": lambda **kw: _nouvelle_alerte(db, **kw)}
    db.close()
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    moteur.dispose()


def _prendre(client, qui="marie", canal=None):
    url = "/api/alerts/claim-next" + (f"?channel={canal}" if canal else "")
    return client.post(url, headers={"X-Utilisateur": qui})


def test_deux_prises_concurrentes_obtiennent_deux_alertes_differentes(base_isolee):
    """Deux analystes, deux sessions, deux fils. Un rendez-vous force le pire
    entrelacement : les deux ont LU le même candidat avant que l'un ou l'autre
    n'écrive. Un « lire puis écrire » donnait alors la même alerte aux deux,
    le second écrasant le premier sans que personne le sache."""
    b = base_isolee
    dans_une_heure = datetime.utcnow() + timedelta(hours=1)
    premiere = b["alerte"](echeance=dans_une_heure)
    seconde = b["alerte"](echeance=dans_une_heure + timedelta(hours=1))
    rendez_vous = threading.Barrier(2, timeout=5)
    fil = threading.local()

    def _attendre_l_autre(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE ALERTS") and not getattr(fil, "passe", False):
            fil.passe = True
            try:
                rendez_vous.wait()
            except threading.BrokenBarrierError:
                pass

    event.listen(b["moteur"], "before_cursor_execute", _attendre_l_autre)
    reponses = {}

    def _prise(qui):
        reponses[qui] = _prendre(TestClient(app), qui)

    try:
        fils = [threading.Thread(target=_prise, args=(qui,)) for qui in ("ana", "bea")]
        for t in fils:
            t.start()
        for t in fils:
            t.join(30)
    finally:
        event.remove(b["moteur"], "before_cursor_execute", _attendre_l_autre)
    assert all(r.status_code == 200 for r in reponses.values()), {k: r.text for k, r in reponses.items()}
    obtenues = {qui: r.json()["alert"]["id"] for qui, r in reponses.items()}
    assert obtenues["ana"] != obtenues["bea"]
    assert set(obtenues.values()) == {premiere, seconde}
    db = b["db"]
    db.expire_all()
    for qui, alert_id in obtenues.items():
        alerte = db.get(Alert, alert_id)
        assert alerte.assigned_to == qui and alerte.status == "IN_PROGRESS"
        assert len(_evenements(db, alert_id, "ASSIGNED")) == 1


def test_sans_alerte_disponible_la_reponse_le_dit_sans_erreur(base_isolee):
    """Rien à prendre n'est pas une panne : 200, `claimed: false`, une phrase,
    et le nombre d'alertes non assignées qu'un report tient à l'écart — rien
    n'est assigné au passage."""
    b = base_isolee
    demain = datetime.utcnow() + timedelta(days=1)
    b["alerte"](assignee="paul", statut="IN_PROGRESS")
    b["alerte"](statut="PENDING_VALIDATION")
    b["alerte"](statut="ESCALATED")
    b["alerte"](statut="CLOSED_FALSE_POSITIVE")
    reportee = b["alerte"](reportee_jusqu_a=demain)
    r = _prendre(b["client"])
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["claimed"] is False and corps["alert"] is None
    _traduit_partout(corps["message"])
    assert corps["snoozed_unassigned"] == 1
    b["db"].expire_all()
    assert b["db"].get(Alert, reportee).assigned_to is None


def test_l_ordre_de_prise_suit_l_echeance_puis_la_priorite(base_isolee):
    """L'échéance d'abord (l'obligation qui court), la priorité à échéance
    égale, les alertes sans échéance en dernier. Le tri de la file affichée
    (priorité puis échéance) aurait donné C, D, B, A."""
    b = base_isolee
    maintenant = datetime.utcnow()
    dans_deux_heures = maintenant + timedelta(hours=2)
    a = b["alerte"](priorite="LOW", echeance=maintenant + timedelta(hours=48))
    bb = b["alerte"](priorite="MEDIUM", echeance=dans_deux_heures)
    c = b["alerte"](priorite="CRITICAL", echeance=dans_deux_heures)
    d = b["alerte"](priorite="HIGH", echeance=None)
    b["alerte"](priorite="HIGH", echeance=maintenant + timedelta(hours=1),
                reportee_jusqu_a=maintenant + timedelta(days=2))
    b["alerte"](priorite="CRITICAL", echeance=maintenant - timedelta(hours=5),
                assignee="paul", statut="IN_PROGRESS")
    b["alerte"](priorite="CRITICAL", echeance=maintenant - timedelta(hours=5),
                statut="PENDING_VALIDATION")
    prises = [_prendre(b["client"]).json() for _ in range(5)]
    assert [p["alert"]["id"] for p in prises[:4]] == [c, bb, a, d]
    _traduit_partout(prises[0]["message"])
    assert prises[4]["claimed"] is False


def test_prendre_la_suivante_reste_dans_son_canal(base_isolee):
    b = base_isolee
    filtrage = b["alerte"](canal="FILTERING", echeance=datetime.utcnow() + timedelta(hours=5))
    b["alerte"](canal="SCREENING", echeance=datetime.utcnow() + timedelta(hours=1))
    r = _prendre(b["client"], canal="FILTERING")
    assert r.json()["alert"]["id"] == filtrage
    assert _prendre(b["client"], canal="FILTERING").json()["claimed"] is False
    assert _prendre(b["client"], canal="INCONNU").status_code == 400


def test_l_accueil_compte_les_non_assignees_sans_les_dire_a_moi(base_isolee):
    """« Rien ne vous attend » devant 26 alertes non assignées : le compte ne
    retenait que les miennes. Les non assignées se comptent à part, avec les
    critères mêmes de « Prendre la suivante » — l'accueil n'annonce rien que le
    bouton ne puisse donner."""
    b = base_isolee
    maintenant = datetime.utcnow()
    b["alerte"](echeance=maintenant - timedelta(hours=1))
    b["alerte"](echeance=maintenant + timedelta(hours=8))
    b["alerte"](assignee="marie", statut="IN_PROGRESS")
    b["alerte"](reportee_jusqu_a=maintenant + timedelta(days=1))
    b["alerte"](statut="PENDING_VALIDATION")
    journee = b["client"].get("/api/me/journee", headers={"X-Utilisateur": "marie"}).json()
    assert journee["mes_alertes"]["total"] == 1
    assert journee["non_assignees"]["total"] == 2
    assert journee["non_assignees"]["en_retard"] == 1
    assert journee["non_assignees"]["peut_prendre"] is True
    auditeur = b["client"].get("/api/me/journee",
                               headers={"X-Utilisateur": "aude", "X-Role": "auditor"}).json()
    assert auditeur["non_assignees"]["peut_prendre"] is False


# ============================================= la place dans la file (écran)

def _app_js():
    with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as f:
        return f.read()


def _corps(source, signature):
    debut = source.index(signature)
    return source[debut:source.index("\n}\n", debut)]


def test_echap_qui_ferme_une_modale_ne_relache_pas_la_visee_de_la_file():
    """Les deux écouteurs sont sur `document` ; celui des modales passe le
    premier, ferme l'alerte, et le triage voyait ensuite une page sans modale :
    Échap relâchait AUSSI la visée, et `j` repartait du haut."""
    js = _app_js()
    a11y = _corps(js, "function initA11y()")
    assert re.search(r"if \(open\) \{ fermerModale\(open\); e\.preventDefault\(\); \}", a11y)
    triage = _corps(js, "function initTriageClavier()")
    assert triage.index("if (e.defaultPrevented) return;") < triage.index('e.key === "Escape"')
    assert js.index("initA11y();") < js.index("initTriageClavier();")


def test_la_visee_passe_a_la_ligne_suivante_et_le_focus_revient_dans_la_file():
    js = _app_js()
    poste = _corps(js, "async function _postAlertAction(path, body)")
    assert '["propose", "validate", "escalate"].includes(path)' in poste
    assert "_viseeApresTraitement(currentAlertId)" in poste
    assert "_revenirALaFile();" in _corps(js, "function closeAlertModal()")
    assert "_focaliserLaVisee(ligne);" in _corps(js, "function _viserLigne(ligne)")
    suivante = _corps(js, "function _viseeApresTraitement(alertId)")
    assert "lignes[i + 1] || lignes[i - 1]" in suivante


def test_la_file_et_l_accueil_offrent_prendre_la_suivante_et_le_regroupement():
    js = _app_js()
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    for canal, prefixe in (("SCREENING", "screening"), ("FILTERING", "filtering")):
        assert f"prendreLaSuivante('{canal}')" in html
        assert f'id="{prefixe}-group-by"' in html
        assert f"bulkProposeSelected('{canal}', 'FALSE_POSITIVE')" in html
        assert f'id="{prefixe}-bulk-report"' in html
    assert 'onclick="prendreLaSuivante()"' in _corps(js, "async function renderMaJourneeWidget(body)")
    assert "boutonProposerEtSuivante()" in _corps(js, "async function openAlertModal(alertId)")

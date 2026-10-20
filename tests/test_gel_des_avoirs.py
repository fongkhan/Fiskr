"""
Un vrai positif sur une liste de gel a une suite : le gel, sa déclaration à la
DG Trésor, sa levée.

Relevé en production (lecture seule, 19 octobre 2026) : deux alertes
confirmées sur une fiche OFAC étaient en priorité MEDIUM (échéance 120 h),
closes directement par la personne qui proposait et validait ; ensuite, aucune
pièce, aucun projet de déclaration, et un narratif rédigé dix-sept jours après
la clôture. `CLOSED_CONFIRMED` était final : une alerte DGT se closait
exactement comme une alerte PEP.

Ce que ces tests tiennent (CMF L.562-4 et R.562-3 ; lignes directrices
conjointes DGT/ACPR du 18 mars 2026) :
- une confirmation sur le périmètre SANCTION ouvre une mesure « à déclarer »
  (§180-182), et seulement sur ce périmètre — dérivé de fiskr/perimeters.py ;
- une correspondance sur une liste de gel ne descend pas sous HIGH ;
- une mesure non déclarée au-delà du seuil sonne, une fois (§99, §182) ;
- « déclarée » ne sort que d'une pièce de transmission (§168) ;
- la levée rend au client son statut (§209-213).
"""
import io
import re
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr import gels
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, Alert, AlertAttachment, AlertEvent, AppSetting, AuditTrail, ClientEntity,
    DossierPPE, MesureDeGel, Snapshot,
)
from fiskr.settings import (
    SETTING_ALERT_FOUR_EYES, SETTING_GEL_DELAI, SETTING_NOTIFICATION_BATCH,
    SETTING_PERIMETERS, set_setting,
)

UID = uuid.uuid4().hex[:8].upper()
PREFIXE = f"test_gel_{UID}"


def _connecter(username, roles=("admin", "reviewer")):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": ",".join(roles), "roles": list(roles),
    }


def _espionner_emit(monkeypatch):
    appels = []
    espion = lambda db, key, payload=None, **kw: appels.append((key, payload or {}))
    import fiskr.api as api_mod
    import fiskr.notifier as notifier_mod
    monkeypatch.setattr(api_mod, "emit", espion)
    monkeypatch.setattr(notifier_mod, "emit", espion)
    return appels


@pytest.fixture()
def ctx():
    db = next(get_db())
    _connecter(f"{PREFIXE}_analyste")
    reglages = (SETTING_ALERT_FOUR_EYES, SETTING_GEL_DELAI, SETTING_PERIMETERS,
                SETTING_NOTIFICATION_BATCH)
    avant = {r.key: r.value for r in db.query(AppSetting).filter(AppSetting.key.in_(reglages))}
    lots = []

    def alerte(list_type="WATCHLIST_DGT", nom="Alerte", statut="OPEN", score=88.0,
               priorite="HIGH", client_id=None):
        client_id = client_id or f"{PREFIXE}_{nom}"
        audit = AuditTrail(client_id=client_id, client_name=f"Client {nom} {UID}",
                           client_type="PP", watchlist_id=f"GEL-{UID}-{nom}",
                           watchlist_name=f"Listé {nom} {UID}", base_score=score,
                           final_score=score, status="ALERT",
                           decision_tree={"hard_match_triggered": False},
                           config_state={}, watchlist_version="test", watchlist_hash="test",
                           list_type=list_type)
        db.add(audit)
        db.commit()
        a = Alert(audit_id=audit.id, client_id=client_id, client_name=audit.client_name,
                  watchlist_entity_id=audit.watchlist_id, watchlist_name=audit.watchlist_name,
                  final_score=score, list_type=list_type, status=statut, channel="SCREENING",
                  priority=priorite)
        db.add(a)
        db.commit()
        return a

    def referentiel(client_id, statut_client="ACTIF"):
        snap_id = f"GELCLI-{uuid.uuid4().hex[:10]}"
        db.add(Snapshot(snapshot_id=snap_id, file_type="CLIENT_BASE", file_name="clients.csv",
                        file_hash="x" * 64, record_count=1, status="READY"))
        db.add(ClientEntity(snapshot_id=snap_id, client_id=client_id, client_type="PP",
                            client_first_name="Jean", client_last_name=f"Gel{UID}",
                            client_status=statut_client, entity_checksum="y" * 64))
        db.commit()
        lots.append(snap_id)
        return snap_id

    def confirmer(a):
        """Proposition par l'analyste, validation 4-yeux par un réviseur."""
        client = ctx_client
        _connecter(f"{PREFIXE}_analyste")
        r = client.post(f"/api/alerts/{a.id}/propose",
                        json={"decision": "CONFIRMED", "comment": "Identité alignée, pièces au dossier."})
        assert r.status_code == 200, r.text
        _connecter(f"{PREFIXE}_reviseur")
        r = client.post(f"/api/alerts/{a.id}/validate", json={"approve": True, "comment": "Validé."})
        assert r.status_code == 200, r.text
        _connecter(f"{PREFIXE}_analyste")
        return r.json()

    set_setting(db, SETTING_ALERT_FOUR_EYES, True)
    ctx_client = TestClient(app)
    yield {"db": db, "client": ctx_client, "alerte": alerte, "confirmer": confirmer,
           "referentiel": referentiel}
    app.dependency_overrides.pop(get_current_user, None)
    try:
        db.rollback()
        ids = [a.id for a in db.query(Alert).filter(Alert.client_id.like(f"{PREFIXE}%")).all()]
        if ids:
            db.query(MesureDeGel).filter(MesureDeGel.alert_id.in_(ids)).delete(synchronize_session=False)
            # Une PPE confirmée ouvre un dossier : orphelin, il retiendrait l'alerte
            # qui réutiliserait son identifiant (SQLite recycle les rowid).
            db.query(DossierPPE).filter(DossierPPE.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertAttachment).filter(AlertAttachment.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like(f"{PREFIXE}%")).delete(synchronize_session=False)
        if lots:
            db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(lots)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(lots)).delete(synchronize_session=False)
        for cle in reglages:
            ligne = db.query(AppSetting).filter(AppSetting.key == cle).first()
            if cle in avant:
                if ligne is not None:
                    ligne.value = avant[cle]
            elif ligne is not None:
                db.delete(ligne)
        db.commit()
    finally:
        db.close()


def _mesures(ctx, alert_id):
    r = ctx["client"].get(f"/api/gels?alert_id={alert_id}")
    assert r.status_code == 200, r.text
    return r.json()["items"]


# ------------------------------------------------- la confirmation ouvre un dû

def test_confirmer_une_alerte_dgt_ouvre_une_mesure_a_declarer_et_son_projet_repond(ctx):
    a = ctx["alerte"]("WATCHLIST_DGT", "Dgt")
    reponse = ctx["confirmer"](a)
    assert "Mesure de gel" in reponse["message"], "la clôture doit dire qu'un dû s'ouvre"

    mesures = _mesures(ctx, a.id)
    assert len(mesures) == 1
    m = mesures[0]
    assert m["statut"] == gels.STATUT_A_DECLARER
    assert m["type_action"] == "GEL"
    # Le délai court depuis la confirmation (validation 4-yeux), pas la proposition
    detail = ctx["client"].get(f"/api/alerts/{a.id}").json()
    assert m["ouverte_le"] == detail["decided_at"]
    assert [x["id"] for x in detail["gel_mesures"]] == [m["id"]]
    assert "GEL_OUVERT" in [e["action"] for e in detail["events"]]

    projet = ctx["client"].get(f"/api/gels/{m['id']}/draft")
    assert projet.status_code == 200, projet.text
    corps = projet.json()
    assert corps["type"] == "PROJET_DECLARATION_GEL"
    assert corps["personne_listee"]["identifiant"] == a.watchlist_entity_id
    # Rien n'est saisi : le projet DIT ce qui manque au minimum du formulaire
    assert any("date du gel" in x for x in corps["informations_minimales_manquantes"])
    imprimable = ctx["client"].get(f"/api/gels/{m['id']}/draft/print")
    assert imprimable.status_code == 200
    assert "Projet de déclaration de gel" in imprimable.text
    assert "sanctions-gel-avoirs@dgtresor.gouv.fr" in imprimable.text
    assert "Informations minimales manquantes" in imprimable.text


def test_la_cloture_directe_sans_quatre_yeux_ouvre_aussi_la_mesure(ctx):
    """Le défaut de production : clôture directe par le proposeur. Ce chemin-là
    aussi doit ouvrir la mesure — elle vit dans `_close_alert`, pas dans un
    des deux appelants."""
    set_setting(ctx["db"], SETTING_ALERT_FOUR_EYES, False)
    a = ctx["alerte"]("WATCHLIST_OFAC", "Direct")
    r = ctx["client"].post(f"/api/alerts/{a.id}/propose",
                           json={"decision": "CONFIRMED", "comment": "Concordance établie."})
    assert r.status_code == 200, r.text
    assert len(_mesures(ctx, a.id)) == 1


def test_une_liste_hors_perimetre_sanction_n_ouvre_aucune_mesure(ctx):
    a = ctx["alerte"]("WATCHLIST_PEP", "Pep", priorite="MEDIUM")
    reponse = ctx["confirmer"](a)
    assert "Mesure de gel" not in reponse["message"]
    assert _mesures(ctx, a.id) == []
    assert ctx["client"].get(f"/api/alerts/{a.id}").json()["gel_mesures"] == []
    # Et on ne peut pas en forcer une : pas de gel, pas de déclaration
    r = ctx["client"].post(f"/api/alerts/{a.id}/gels", json={"type_action": "GEL"})
    assert r.status_code == 409


def test_le_perimetre_decide_pas_une_liste_recopiee(ctx):
    """Le classement SANCTION / HORS_SANCTION est un arbitrage surchargeable :
    le registre le lit, il ne garde pas sa propre liste de types."""
    set_setting(ctx["db"], SETTING_PERIMETERS,
                {"WATCHLIST_OFAC": "HORS_SANCTION", "WATCHLIST_PEP": "SANCTION"})
    ofac = ctx["alerte"]("WATCHLIST_OFAC", "OfacDeclasse")
    pep = ctx["alerte"]("WATCHLIST_PEP", "PepClasse")
    ctx["confirmer"](ofac)
    ctx["confirmer"](pep)
    assert _mesures(ctx, ofac.id) == []
    assert len(_mesures(ctx, pep.id)) == 1


def test_un_faux_positif_n_ouvre_aucune_mesure(ctx):
    a = ctx["alerte"]("WATCHLIST_DGT", "Fp")
    _connecter(f"{PREFIXE}_analyste")
    ctx["client"].post(f"/api/alerts/{a.id}/propose",
                       json={"decision": "FALSE_POSITIVE", "comment": "Date de naissance incompatible."})
    _connecter(f"{PREFIXE}_reviseur")
    assert ctx["client"].post(f"/api/alerts/{a.id}/validate", json={"approve": True}).status_code == 200
    assert _mesures(ctx, a.id) == []


def test_un_vrai_positif_confirme_avant_le_registre_est_nomme_et_rattrapable(ctx):
    """Les deux alertes OFAC de production ont été confirmées avant que le
    registre n'existe : rien ne les y fera entrer seules. Le contrôle de mise
    en service les NOMME, et la mesure ouverte après coup court depuis la
    confirmation — le registre ne rajeunit pas un retard de dix-sept jours."""
    from fiskr.mise_en_service import BLOQUANT, _declarations_de_gel
    db = ctx["db"]
    a = ctx["alerte"]("WATCHLIST_OFAC", "Historique", statut="CLOSED_CONFIRMED")
    a.decided_at = datetime.utcnow() - timedelta(days=17)
    a.decided_by = "ancien_analyste"
    db.commit()
    controle = _declarations_de_gel(db)
    assert controle["etat"] == BLOQUANT and f"alerte #{a.id}" in controle["constat"]
    assert ctx["client"].get(f"/api/alerts/{a.id}").json()["gel_du"] is True

    r = ctx["client"].post(f"/api/alerts/{a.id}/gels", json={"type_action": "GEL"})
    assert r.status_code == 200, r.text
    assert r.json()["ouverte_le"] == a.decided_at.isoformat()
    assert r.json()["en_retard"] is True
    assert f"alerte #{a.id}" not in _declarations_de_gel(db)["constat"]
    # Une mesure SUIVANTE (crédit reçu plus tard) court, elle, depuis son ouverture
    suivante = ctx["client"].post(f"/api/alerts/{a.id}/gels",
                                  json={"type_action": "CREDIT_COMPTE_GELE"}).json()
    assert suivante["en_retard"] is False


# ------------------------------------------------- le plancher de priorité

def _correspondance(list_type, score=81.0, hard=False):
    return {"final_score": score, "cut_off_applied": 75.0, "hard_match_triggered": hard,
            "watchlist_entity": {"_list_type": list_type}}


def test_une_correspondance_dgt_a_81_est_au_plancher_high():
    from fiskr.alerts import compute_alert_priority
    # Avant : MEDIUM (81 >= seuil 75 + 5), échéance 120 h — le cas de production
    assert compute_alert_priority(_correspondance("WATCHLIST_DGT")) == "HIGH"
    assert compute_alert_priority(_correspondance("WATCHLIST_OFAC", 76.0)) == "HIGH"
    # Sans crier au loup : hors gel, la priorité reste celle du score
    assert compute_alert_priority(_correspondance("WATCHLIST_PEP")) == "MEDIUM"
    assert compute_alert_priority(_correspondance("WATCHLIST_PEP", 76.0)) == "LOW"
    # La concordance d'identifiant reste au-dessus de tout
    assert compute_alert_priority(_correspondance("WATCHLIST_DGT", hard=True)) == "CRITICAL"


def test_le_criblage_ouvre_l_alerte_dgt_au_plancher(ctx):
    """Le chemin réel d'ouverture (criblage, re-criblage, filtrage) passe par
    `open_or_redetect_alerts` : le plancher doit y être appliqué."""
    from fiskr.alerts import open_or_redetect_alerts
    db = ctx["db"]
    audit = AuditTrail(client_id=f"{PREFIXE}_crible", client_name=f"Crible {UID}",
                       client_type="PP", watchlist_id=f"GEL-{UID}-crible",
                       watchlist_name=f"Listé crible {UID}", base_score=81.0, final_score=81.0,
                       status="ALERT", decision_tree={}, config_state={},
                       watchlist_version="test", watchlist_hash="test", list_type="WATCHLIST_DGT")
    db.add(audit)
    db.commit()
    match = {**_correspondance("WATCHLIST_DGT"),
             "watchlist_entity": {"entity_id": audit.watchlist_id, "primary_name": audit.watchlist_name,
                                  "_list_type": "WATCHLIST_DGT"}}
    resultat = open_or_redetect_alerts(db, [{"audit": audit, "match": match, "rule": None}],
                                       client_id=audit.client_id, username="test")
    alerte = db.query(Alert).filter(Alert.id == resultat["alert_ids"][0]).first()
    assert alerte.priority == "HIGH"


def test_une_alerte_ouverte_avant_le_plancher_y_remonte_au_re_criblage(ctx):
    from fiskr.alerts import open_or_redetect_alerts
    db = ctx["db"]
    a = ctx["alerte"]("WATCHLIST_OFAC", "Ancienne", priorite="MEDIUM", score=81.0)
    audit = db.query(AuditTrail).filter(AuditTrail.id == a.audit_id).first()
    match = {**_correspondance("WATCHLIST_OFAC"),
             "watchlist_entity": {"entity_id": a.watchlist_entity_id, "_list_type": "WATCHLIST_OFAC"}}
    open_or_redetect_alerts(db, [{"audit": audit, "match": match, "rule": None}],
                            client_id=a.client_id, username="re-criblage")
    db.refresh(a)
    assert a.priority == "HIGH"
    assert a.due_at is not None
    assert "PRIORITY_CHANGED" in [e.action for e in db.query(AlertEvent)
                                  .filter(AlertEvent.alert_id == a.id).all()]


def test_l_analyste_ne_descend_pas_une_alerte_sanction_sous_le_plancher(ctx):
    dgt = ctx["alerte"]("WATCHLIST_DGT", "Plancher")
    pep = ctx["alerte"]("WATCHLIST_PEP", "PasDePlancher")
    r = ctx["client"].post(f"/api/alerts/{dgt.id}/priority", json={"priority": "MEDIUM"})
    assert r.status_code == 409 and "HIGH" in r.json()["detail"]
    assert ctx["client"].post(f"/api/alerts/{dgt.id}/priority",
                              json={"priority": "CRITICAL"}).status_code == 200
    assert ctx["client"].post(f"/api/alerts/{pep.id}/priority",
                              json={"priority": "LOW"}).status_code == 200
    # Et l'action en masse ne fait pas le détour
    r = ctx["client"].post("/api/alerts/bulk", json={"ids": [dgt.id, pep.id], "action": "priority",
                                                     "priority": "LOW"})
    assert r.status_code == 200
    assert r.json()["below_floor"] == [dgt.id]
    ctx["db"].refresh(dgt)
    assert dgt.priority == "CRITICAL"


# ------------------------------------------------- le délai de déclaration

def _vieillir(ctx, mesure_id, heures):
    db = ctx["db"]
    m = db.query(MesureDeGel).filter(MesureDeGel.id == mesure_id).first()
    m.ouverte_le = datetime.utcnow() - timedelta(hours=heures)
    db.commit()
    return m


def test_une_mesure_non_declaree_au_dela_du_seuil_leve_evenement_et_controle(ctx, monkeypatch):
    from fiskr.mise_en_service import BLOQUANT, _declarations_de_gel
    import fiskr.api as api_mod
    appels = _espionner_emit(monkeypatch)
    a = ctx["alerte"]("WATCHLIST_DGT", "Retard")
    ctx["confirmer"](a)
    m = _mesures(ctx, a.id)[0]
    _vieillir(ctx, m["id"], 5)          # seuil par défaut : 4 h

    # Le récapitulatif peut être coupé : la détection ne l'attend pas
    set_setting(ctx["db"], SETTING_NOTIFICATION_BATCH, {"enabled": False, "cron": "0 * * * *"})
    api_mod._notification_batch_tick()
    retards = [p for k, p in appels if k == "gel_declaration_en_retard"]
    assert len(retards) == 1 and retards[0]["Mesure"] == f"#{m['id']}"

    # Une seule fois par mesure
    gels.detecter_retards(ctx["db"])
    assert len([1 for k, _ in appels if k == "gel_declaration_en_retard"]) == 1

    controle = _declarations_de_gel(ctx["db"])
    assert controle["etat"] == BLOQUANT
    assert f"#{m['id']}" in controle["constat"]
    assert _mesures(ctx, a.id)[0]["en_retard"] is True
    filtre = ctx["client"].get("/api/gels?statut=EN_RETARD").json()
    assert m["id"] in [x["id"] for x in filtre["items"]]


def test_dans_le_delai_rien_ne_sonne_et_rien_ne_bloque(ctx, monkeypatch):
    from fiskr.mise_en_service import ATTENTION, _declarations_de_gel
    appels = _espionner_emit(monkeypatch)
    a = ctx["alerte"]("WATCHLIST_DGT", "DansLeDelai")
    ctx["confirmer"](a)
    _vieillir(ctx, _mesures(ctx, a.id)[0]["id"], 3)
    assert gels.detecter_retards(ctx["db"]) == 0
    assert not [k for k, _ in appels if k == "gel_declaration_en_retard"]
    # À déclarer, mais pas en retard : un point à traiter, pas un blocage
    assert _declarations_de_gel(ctx["db"])["etat"] == ATTENTION


def test_le_seuil_est_un_reglage_borne(ctx, monkeypatch):
    _connecter(f"{PREFIXE}_admin", roles=("admin",))
    assert ctx["client"].put("/api/settings/ingestion",
                             json={"gel_declaration_delay_hours": 0}).status_code == 400
    assert ctx["client"].put("/api/settings/ingestion",
                             json={"gel_declaration_delay_hours": 500}).status_code == 400
    r = ctx["client"].put("/api/settings/ingestion", json={"gel_declaration_delay_hours": 2})
    assert r.status_code == 200, r.text
    assert r.json()["gel_declaration_delay_hours"] == 2
    appels = _espionner_emit(monkeypatch)
    a = ctx["alerte"]("WATCHLIST_DGT", "SeuilCourt")
    ctx["confirmer"](a)
    _vieillir(ctx, _mesures(ctx, a.id)[0]["id"], 3)   # 3 h > 2 h réglées
    assert gels.detecter_retards(ctx["db"]) >= 1
    assert [k for k, _ in appels if k == "gel_declaration_en_retard"]


# ------------------------------------------------- déclaration, KPI, levée

def _declarer(ctx, mesure_id, avec_piece=True, **champs):
    fichiers = {"file": ("accuse_dgt.pdf", io.BytesIO(b"%PDF-1.4 accuse"), "application/pdf")} \
        if avec_piece else None
    _connecter(f"{PREFIXE}_reviseur")
    r = ctx["client"].post(f"/api/gels/{mesure_id}/declaration", files=fichiers,
                           data={"reference": "DGT-2026-001",
                                 "canal": "sanctions-gel-avoirs@dgtresor.gouv.fr", **champs})
    _connecter(f"{PREFIXE}_analyste")
    return r


def test_la_declaration_exige_la_piece_et_alimente_le_kpi(ctx):
    a = ctx["alerte"]("WATCHLIST_DGT", "Declaree")
    ctx["confirmer"](a)
    m = _mesures(ctx, a.id)[0]
    _vieillir(ctx, m["id"], 2)

    # Sans gel effectif enregistré : la DG Trésor demande la date du gel
    assert _declarer(ctx, m["id"]).status_code == 409
    r = ctx["client"].put(f"/api/gels/{m['id']}", json={
        "date_effet": (datetime.utcnow() - timedelta(hours=1)).isoformat(),
        "comptes": "FR7612345678901234567890123 ; compte courant ; EUR ; 12 500,00 ; 12 500,00"})
    assert r.status_code == 200, r.text
    assert r.json()["comptes"][0]["montant_gel"] == "12 500,00"
    # Sans pièce de transmission : refus franc, rien n'est « déclaré »
    sans = _declarer(ctx, m["id"], avec_piece=False)
    assert sans.status_code == 400
    assert _mesures(ctx, a.id)[0]["statut"] == gels.STATUT_A_DECLARER

    avec = _declarer(ctx, m["id"])
    assert avec.status_code == 200, avec.text
    corps = avec.json()
    assert corps["statut"] == gels.STATUT_DECLAREE
    assert corps["reference_dgt"] == "DGT-2026-001"
    assert corps["piece_declaration"]["file_present"] is True
    # La pièce est une pièce jointe de l'alerte : l'inventaire probant la voit
    piece = ctx["db"].query(AlertAttachment).filter(
        AlertAttachment.id == corps["piece_declaration"]["id"]).first()
    assert piece.alert_id == a.id
    # ~2 h entre confirmation et déclaration, mesurées par LA définition
    assert 1.9 * 3600 <= corps["delai_declaration_secondes"] <= 2.1 * 3600
    kpi = ctx["client"].get("/api/kpi").json()["asset_freezes"]
    assert kpi["declared"] >= 1 and kpi["avg_declaration_delay_seconds"] is not None
    # Une mesure déclarée ne se réécrit pas
    assert ctx["client"].put(f"/api/gels/{m['id']}", json={"comptes": "X ; ; ; 1"}).status_code == 409


def test_la_levee_rend_au_client_son_statut(ctx):
    client_id = f"{PREFIXE}_client_gele"
    ctx["referentiel"](client_id, "ACTIF")
    a = ctx["alerte"]("WATCHLIST_DGT", "Levee", client_id=client_id)
    ctx["confirmer"](a)
    m = _mesures(ctx, a.id)[0]
    db = ctx["db"]

    def statut_client():
        db.expire_all()
        return db.query(ClientEntity).filter(ClientEntity.client_id == client_id).first().client_status

    # Confirmer n'est pas geler : le statut ne change qu'au gel effectif
    assert statut_client() == "ACTIF"
    ctx["client"].put(f"/api/gels/{m['id']}", json={
        "date_effet": datetime.utcnow().isoformat(), "comptes": "FR76 0001 ; courant ; EUR ; 100 ; 100"})
    assert statut_client() == gels.STATUT_CLIENT_GELE

    # Une mesure non déclarée ne se lève pas : la déclaration reste due
    _connecter(f"{PREFIXE}_reviseur")
    trop_tot = ctx["client"].post(f"/api/gels/{m['id']}/levee", json={
        "motif": "RADIATION", "justification": "Règlement d'exécution (UE) 2026/123."})
    assert trop_tot.status_code == 409
    assert _declarer(ctx, m["id"]).status_code == 200
    _connecter(f"{PREFIXE}_reviseur")
    assert ctx["client"].post(f"/api/gels/{m['id']}/levee", json={
        "motif": "RADIATION", "justification": ""}).status_code == 400
    r = ctx["client"].post(f"/api/gels/{m['id']}/levee", json={
        "motif": "RADIATION", "justification": "Règlement d'exécution (UE) 2026/123."})
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == gels.STATUT_LEVEE
    assert r.json()["client_gele"] is False
    assert statut_client() == "ACTIF"


def test_un_import_du_referentiel_ne_degele_personne(ctx):
    client_id = f"{PREFIXE}_reimport"
    ctx["referentiel"](client_id, "ACTIF")
    a = ctx["alerte"]("WATCHLIST_DGT", "Reimport", client_id=client_id)
    ctx["confirmer"](a)
    m = _mesures(ctx, a.id)[0]
    ctx["client"].put(f"/api/gels/{m['id']}", json={"date_effet": datetime.utcnow().isoformat()})
    # Le SI amont renvoie la fiche, « ACTIF » : le registre l'emporte
    nouveau = ctx["referentiel"](client_id, "ACTIF")
    db = ctx["db"]
    assert gels.reappliquer_aux_clients(db, nouveau) == 1
    db.commit()
    ligne = db.query(ClientEntity).filter(ClientEntity.snapshot_id == nouveau).first()
    assert ligne.client_status == gels.STATUT_CLIENT_GELE


def test_la_purge_ne_touche_pas_une_alerte_portant_une_mesure(ctx):
    """Une alerte qui porte une mesure de gel, ou un dossier PPE, est le
    dossier de cette mesure : ses pièces y sont jointes et le registre la
    référence. Le témoin était une PPE confirmée ; depuis le dossier PPE, une
    PPE confirmée est tenue elle aussi — le témoin est une liste de régulateur,
    qui n'ouvre ni mesure ni dossier."""
    from fiskr.retention import _purgeable_alert_ids
    a = ctx["alerte"]("WATCHLIST_DGT", "Purge")
    ctx["confirmer"](a)
    ppe = ctx["alerte"]("WATCHLIST_PEP", "PurgePpe")
    ctx["confirmer"](ppe)
    temoin = ctx["alerte"]("WATCHLIST_AMF", "PurgeTemoin")
    ctx["confirmer"](temoin)
    purgeables = _purgeable_alert_ids(ctx["db"], datetime.utcnow() + timedelta(days=1))
    assert temoin.id in purgeables, "le témoin prouve que la date limite purgerait"
    assert a.id not in purgeables
    assert ppe.id not in purgeables, "le dossier PPE perdrait l'origine des fonds"


def test_le_delai_de_declaration_n_a_qu_une_definition():
    from fiskr.kpi import declaration_en_retard, delai_de_declaration_secondes
    t0 = datetime(2026, 10, 19, 8, 0)
    assert delai_de_declaration_secondes(t0, t0 + timedelta(hours=3)) == 3 * 3600
    # Déclarée pendant la validation 4-yeux, avant la clôture dans Fiskr : 0
    assert delai_de_declaration_secondes(t0, t0 - timedelta(minutes=30)) == 0
    # Non déclarée : le délai court encore
    assert delai_de_declaration_secondes(t0, None, t0 + timedelta(hours=5)) == 5 * 3600
    assert declaration_en_retard(gels.STATUT_A_DECLARER, t0, None, 4, t0 + timedelta(hours=5))
    assert not declaration_en_retard(gels.STATUT_A_DECLARER, t0, None, 4, t0 + timedelta(hours=3))
    # Déclarée hors délai : un fait passé, plus une action à mener
    assert not declaration_en_retard(gels.STATUT_DECLAREE, t0, t0 + timedelta(hours=9), 4,
                                     t0 + timedelta(hours=10))


# ------------------------------------------------- lecture par l'auditeur

def test_le_registre_se_lit_et_s_exporte_par_l_auditeur(ctx):
    a = ctx["alerte"]("WATCHLIST_DGT", "Auditeur")
    ctx["confirmer"](a)
    m = _mesures(ctx, a.id)[0]
    _connecter(f"{PREFIXE}_auditeur", roles=("auditor",))
    registre = ctx["client"].get("/api/gels?statut=A_DECLARER")
    assert registre.status_code == 200
    assert m["id"] in [x["id"] for x in registre.json()["items"]]
    assert registre.json()["synthese"]["to_declare"] >= 1
    export = ctx["client"].get("/api/export/gels.csv?statut=A_DECLARER")
    assert export.status_code == 200
    assert re.search(rf"^{m['id']};{a.id};GEL;A_DECLARER;", export.text.lstrip("﻿"), re.M)
    # Le projet de déclaration trace sa génération : il reste aux réviseurs
    assert ctx["client"].get(f"/api/gels/{m['id']}/draft").status_code == 403


def test_l_ecran_du_registre_existe_et_appelle_le_registre():
    import os
    racine = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fiskr", "static")
    with open(os.path.join(racine, "index.html"), encoding="utf-8") as f:
        index = f.read()
    with open(os.path.join(racine, "app.js"), encoding="utf-8") as f:
        app_js = f.read()
    assert 'id="sub-sec-alerts-gels"' in index and 'id="sub-btn-alerts-gels"' in index
    for appel in ("/api/gels?", "/api/export/gels.csv", "/declaration", "/levee", "/draft/print"):
        assert appel in app_js, appel


def test_la_sonde_de_conformite_bloque_sans_nommer_personne(ctx):
    """La sonde /api/health/conformite est faite pour un outil de supervision
    externe (UptimeRobot, Zabbix…). Un vrai positif non déclaré la fait passer
    en 503 — c'est un défaut de conformité — mais le corps ne nomme ni le
    client ni la personne listée : une supervision tierce n'a pas à recevoir
    le nom d'une personne sous mesure de gel. L'écran de mise en service, lui,
    les nomme."""
    db = ctx["db"]
    a = ctx["alerte"]("WATCHLIST_OFAC", "Sondov", statut="CLOSED_CONFIRMED")
    a.decided_at = datetime.utcnow() - timedelta(days=2)
    db.commit()
    from fiskr.mise_en_service import etat_de_conformite
    etat = etat_de_conformite(db)
    gel = [b for b in etat["bloquants"] if b["cle"] == "gels"]
    assert gel, etat["bloquants"]
    corps = str(etat)
    assert a.client_name not in corps and a.watchlist_name not in corps

"""
Tests du lot investigation :
- messages d'API multilingues : resolution Accept-Language, traduction des
  champs detail/message (catalogue exact + gabarits a variables), repli
  francais pour les messages inconnus et les langues non supportees ;
- dossier d'investigation : agregat complet, checklist parametrable a chaud
  (coche tracee CHECKLIST, garde admin, bornes), dossier imprimable ;
- simulation d'impact des seuils : rejeu de l'historique avec delta par
  liste, aucune ecriture, bornes et garde admin.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.apimessages import resolve_lang, translate_message
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AuditTrail, AdminAuditLog, AppSetting
from fiskr.settings import SETTING_CHECKLIST, SETTING_SCORE_THRESHOLDS, DEFAULT_CHECKLIST


def _override_user(username: str, role: str = "user"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_inv_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like("test_inv_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_inv").delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key.in_(
            [SETTING_CHECKLIST, SETTING_SCORE_THRESHOLDS])).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin_inv", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_inv_{uuid.uuid4().hex[:8]}"


def _screen_putin(client, client_id):
    response = client.post("/api/screen", json={
        "client_id": client_id, "client_type": "PP",
        "client_first_name": "Vladimir", "client_last_name": "Putin",
        "client_dob": "1952-10-07", "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    })
    assert response.status_code == 200, response.text
    return response.json()


# ------------------ MESSAGES D'API MULTILINGUES ------------------

def test_accept_language_resolution():
    assert resolve_lang(None) == "fr"
    assert resolve_lang("en") == "en"
    assert resolve_lang("en-US,en;q=0.9,fr;q=0.5") == "en"
    assert resolve_lang("pt-BR,pt;q=0.9") == "fr"        # non supportee -> francais
    assert resolve_lang("de;q=0.3,zh;q=0.8") == "zh"      # meilleure qualite
    assert resolve_lang("ar-SA") == "ar"


def test_translate_message_catalogue_and_templates():
    assert translate_message("Alerte introuvable.", "en") == "Alert not found."
    assert translate_message("Alerte introuvable.", "zh") == "未找到警报。"
    # Gabarit a variable : le nombre est reinjecte
    out = translate_message(
        "Compte temporairement verrouillé après trop d'échecs. Réessayez dans 7 minute(s).", "de")
    assert out == "Konto nach zu vielen Fehlversuchen vorübergehend gesperrt. Erneut versuchen in 7 Minute(n)."
    # Message hors catalogue -> None (le middleware laisse le francais)
    assert translate_message("Un message inédit jamais traduit.", "en") is None
    assert translate_message("Alerte introuvable.", "fr") is None


def test_api_messages_translated_via_middleware(client):
    # 404 connu : detail traduit selon Accept-Language
    response = client.get("/api/alerts/99999999", headers={"Accept-Language": "en"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Alert not found."
    response_es = client.get("/api/alerts/99999999", headers={"Accept-Language": "es-ES,es;q=0.9"})
    assert response_es.json()["detail"] == "Alerta no encontrada."
    # Sans en-tete (ou francais) : message d'origine
    assert client.get("/api/alerts/99999999").json()["detail"] == "Alerte introuvable."
    # Langue non supportee : repli francais
    assert client.get("/api/alerts/99999999",
                      headers={"Accept-Language": "pt-BR"}).json()["detail"] == "Alerte introuvable."


# ------------------ DOSSIER D'INVESTIGATION ------------------

def test_casefile_aggregate_and_checklist_flow(client):
    cid = _new_client_id()
    alert_id = _screen_putin(client, cid)["alert_id"]

    casefile = client.get(f"/api/alerts/{alert_id}/casefile")
    assert casefile.status_code == 200, casefile.text
    data = casefile.json()
    assert data["id"] == alert_id and data["client_id"] == cid
    assert data["decision_tree"] is not None
    assert isinstance(data["events"], list) and isinstance(data["attachments"], list)
    assert data["client_context"]["screenings"] >= 1
    assert "count" in data["entity_relations"] and "inherited_risk" in data["entity_relations"]
    # Checklist par defaut, rien de coche
    assert [i["label"] for i in data["checklist"]] == list(DEFAULT_CHECKLIST)
    assert all(not i["done"] for i in data["checklist"])

    # Coche un point : etat persiste + evenement CHECKLIST trace
    toggled = client.post(f"/api/alerts/{alert_id}/checklist", json={"index": 0, "done": True})
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["done"] == 1
    item = toggled.json()["checklist"][0]
    assert item["done"] is True and item["by"] == "admin_inv"
    db = next(get_db())
    try:
        assert db.query(AlertEvent).filter(
            AlertEvent.alert_id == alert_id, AlertEvent.action == "CHECKLIST").count() == 1
    finally:
        db.close()
    # Decoche + bornes
    assert client.post(f"/api/alerts/{alert_id}/checklist",
                       json={"index": 0, "done": False}).json()["done"] == 0
    assert client.post(f"/api/alerts/{alert_id}/checklist",
                       json={"index": 99, "done": True}).status_code == 400

    # Dossier imprimable autonome
    printable = client.get(f"/api/alerts/{alert_id}/casefile/print")
    assert printable.status_code == 200
    assert "Dossier d'investigation" in printable.text
    assert "Checklist d'instruction" in printable.text and "PUTIN" in printable.text.upper()


def test_checklist_setting_hot_editable(client):
    custom = ["Vérifier la pièce d'identité", "Contrôler le pays de résidence"]
    ok = client.put("/api/settings/checklist", json={"items": custom})
    assert ok.status_code == 200 and ok.json()["items"] == custom
    assert client.get("/api/settings/checklist").json()["items"] == custom
    # Bornes : trop de points / point trop long
    assert client.put("/api/settings/checklist",
                      json={"items": ["x"] * 21}).status_code == 400
    assert client.put("/api/settings/checklist",
                      json={"items": ["y" * 201]}).status_code == 400
    # Liste vide = retour au defaut
    assert client.put("/api/settings/checklist", json={"items": []}).json()["items"] == list(DEFAULT_CHECKLIST)
    # Garde admin
    _override_user("test_inv_user", "user")
    assert client.put("/api/settings/checklist", json={"items": custom}).status_code == 403


# ------------------ SIMULATION D'IMPACT DES SEUILS ------------------

def test_scoring_simulation(client):
    cid = _new_client_id()
    _screen_putin(client, cid)  # une decision ALERT recente a rejouer

    # Seuil candidat a 100 : l'alerte Putin (score < 100 ou = 100) est recomptee
    simulation = client.post("/api/settings/scoring/simulate",
                             json={"cut_off_threshold": 100, "days": 7})
    assert simulation.status_code == 200, simulation.text
    data = simulation.json()
    assert data["candidate"]["cut_off_threshold"] == 100.0
    assert data["totals"]["replayed"] >= 1
    # Le durcissement ne peut pas creer PLUS d'alertes que l'existant
    assert data["totals"]["alerts_candidate"] <= data["totals"]["alerts_now"]
    for bucket in data["by_list"].values():
        assert bucket["delta"] == bucket["alerts_candidate"] - bucket["alerts_now"]

    # Seuil laxiste a 0 : toutes les decisions rejouees deviennent des alertes
    permissive = client.post("/api/settings/scoring/simulate",
                             json={"cut_off_threshold": 0, "days": 7}).json()
    assert permissive["totals"]["alerts_candidate"] == permissive["totals"]["replayed"]
    assert permissive["totals"]["delta"] >= 0

    # Aucune ecriture : le reglage reel est inchange
    assert client.get("/api/settings/scoring").json()["cut_off_threshold"] == 75.0
    # Bornes + garde admin
    assert client.post("/api/settings/scoring/simulate",
                       json={"cut_off_threshold": 150}).status_code == 400
    assert client.post("/api/settings/scoring/simulate",
                       json={"days": 0}).status_code == 400
    _override_user("test_inv_user", "user")
    assert client.post("/api/settings/scoring/simulate",
                       json={"cut_off_threshold": 90}).status_code == 403

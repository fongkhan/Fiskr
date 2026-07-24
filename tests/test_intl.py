"""
Tests du lot international & roles :
- delegation d'absence : reassignation des alertes ouvertes, redirection des
  nouvelles assignations vers le delegue, fin d'absence, validations ;
- seuils de score a chaud : reglage > config.yaml, surcharge par liste,
  application au criblage (cut_off_applied), bornes 0-100 ;
- role auditeur : exclusif, lecture seule reelle (GET 200 / POST 403 via le
  vrai chemin d'authentification), gestion de sa propre session autorisee ;
- i18n : moteur + dictionnaires 5 langues presents, selecteur de langue sur
  le dashboard et la page de connexion, regles RTL pour l'arabe.
"""
import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user, normalize_roles
from fiskr.database import get_db, User, Alert, AlertEvent, AuditTrail, AdminAuditLog, AppSetting
from fiskr.settings import (
    score_thresholds, scoring_config_with_thresholds, SETTING_SCORE_THRESHOLDS,
)
from fiskr.scoring import resolve_cut_off

STRONG_PW = "InternationalFort1"
STATIC = Path(__file__).resolve().parent.parent / "fiskr" / "static"


def _override_user(username: str, role: str = "user"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_intl_%")).all()
        ids = [a.id for a in test_alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like("test_intl_%")).delete(synchronize_session=False)
        db.query(User).filter(User.username.like("test_intl_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username.like("test_intl_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_intl").delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key == SETTING_SCORE_THRESHOLDS).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin_intl", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _new_client_id():
    return f"test_intl_{uuid.uuid4().hex[:8]}"


def _create_user(client, username, role="user"):
    response = client.post("/api/users", json={
        "username": username, "password": STRONG_PW, "full_name": username, "role": role})
    assert response.status_code == 200, response.text
    db = next(get_db())
    try:
        return db.query(User).filter(User.username == username).first().id
    finally:
        db.close()


def _screen_putin(client, client_id):
    response = client.post("/api/screen", json={
        "client_id": client_id, "client_type": "PP",
        "client_first_name": "Vladimir", "client_last_name": "Putin",
        "client_dob": "1952-10-07", "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}
    })
    assert response.status_code == 200, response.text
    return response.json()


# ------------------ DELEGATION D'ABSENCE ------------------

def test_absence_delegation_flow(client):
    absent = f"test_intl_a_{uuid.uuid4().hex[:6]}"
    delegate = f"test_intl_d_{uuid.uuid4().hex[:6]}"
    absent_id = _create_user(client, absent)
    _create_user(client, delegate)

    # Une alerte deja assignee a l'absent
    alert_id = _screen_putin(client, _new_client_id())["alert_id"]
    assigned = client.post(f"/api/alerts/{alert_id}/assign", json={"assignee": absent})
    assert assigned.status_code == 200

    # Declaration d'absence par l'admin : l'alerte ouverte part au delegue
    until = (datetime.utcnow() + timedelta(days=7)).isoformat()
    set_abs = client.put(f"/api/users/{absent_id}/absence", json={
        "absent_until": until, "delegate_to": delegate, "reassign_open": True})
    assert set_abs.status_code == 200, set_abs.text
    assert set_abs.json()["reassigned"] >= 1
    db = next(get_db())
    try:
        alert = db.query(Alert).filter(Alert.id == alert_id).first()
        assert alert.assigned_to == delegate
        assert db.query(AlertEvent).filter(
            AlertEvent.alert_id == alert_id,
            AlertEvent.detail.like("%délégation d'absence%")).count() == 1
    finally:
        db.close()

    # Nouvelle assignation vers l'absent -> redirigee vers le delegue
    alert2_id = _screen_putin(client, _new_client_id())["alert_id"]
    redirected = client.post(f"/api/alerts/{alert2_id}/assign", json={"assignee": absent})
    assert redirected.status_code == 200
    assert delegate in redirected.json()["message"]
    db = next(get_db())
    try:
        assert db.query(Alert).filter(Alert.id == alert2_id).first().assigned_to == delegate
    finally:
        db.close()
    # L'annuaire expose l'absence
    row = next(u for u in client.get("/api/users/directory").json()["items"] if u["username"] == absent)
    assert row["absent"] is True and row["delegate_to"] == delegate

    # Fin d'absence : les assignations reviennent a l'interesse
    assert client.put(f"/api/users/{absent_id}/absence", json={"absent_until": None}).status_code == 200
    alert3_id = _screen_putin(client, _new_client_id())["alert_id"]
    back = client.post(f"/api/alerts/{alert3_id}/assign", json={"assignee": absent})
    assert back.status_code == 200
    db = next(get_db())
    try:
        assert db.query(Alert).filter(Alert.id == alert3_id).first().assigned_to == absent
    finally:
        db.close()


def test_absence_validations(client):
    someone = f"test_intl_{uuid.uuid4().hex[:6]}"
    user_id = _create_user(client, someone)
    auditor = f"test_intl_aud_{uuid.uuid4().hex[:6]}"
    _create_user(client, auditor, role="auditor")
    future = (datetime.utcnow() + timedelta(days=3)).isoformat()
    # Date passee, delegue manquant, auto-delegation, delegue auditeur, inconnu
    past = (datetime.utcnow() - timedelta(days=1)).isoformat()
    assert client.put(f"/api/users/{user_id}/absence", json={
        "absent_until": past, "delegate_to": "admin_intl"}).status_code == 400
    assert client.put(f"/api/users/{user_id}/absence", json={
        "absent_until": future}).status_code == 400
    assert client.put(f"/api/users/{user_id}/absence", json={
        "absent_until": future, "delegate_to": someone}).status_code == 400
    assert client.put(f"/api/users/{user_id}/absence", json={
        "absent_until": future, "delegate_to": auditor}).status_code == 400
    assert client.put(f"/api/users/{user_id}/absence", json={
        "absent_until": future, "delegate_to": "test_intl_inconnu"}).status_code == 404


# ------------------ SEUILS DE SCORE A CHAUD ------------------

def test_score_thresholds_hot_setting(client):
    # Defaut : valeurs de config.yaml
    initial = client.get("/api/settings/scoring").json()
    assert initial["cut_off_threshold"] == 75.0 and initial["source"] == "config"
    # Bornes
    assert client.put("/api/settings/scoring", json={"cut_off_threshold": 120}).status_code == 400
    assert client.put("/api/settings/scoring", json={
        "cut_off_overrides": {"WATCHLIST_PEP": 250}}).status_code == 400
    assert client.put("/api/settings/scoring", json={}).status_code == 400

    # Reglage a chaud : global 88 + surcharge PEP 95
    ok = client.put("/api/settings/scoring", json={
        "cut_off_threshold": 88, "cut_off_overrides": {"watchlist_pep": 95}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["cut_off_threshold"] == 88.0
    assert ok.json()["cut_off_overrides"]["WATCHLIST_PEP"] == 95.0

    db = next(get_db())
    try:
        # Injecte dans la config de scoring et resolu par le moteur
        cfg = scoring_config_with_thresholds(db)
        assert resolve_cut_off(cfg) == 88.0
        assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_PEP"}) == 95.0
        assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_OFAC"}) == 88.0
    finally:
        db.close()

    # Applique au criblage reel : le cut_off trace suit le reglage
    data = client.post("/api/screen", json={
        "client_id": _new_client_id(), "client_type": "PP",
        "client_first_name": "Xyzabc", "client_last_name": "Qwertyuiop",
        "client_countries": {"nationality": ["FR"], "residence": [], "birth_country": [], "registration_country": []}
    }).json()
    db = next(get_db())
    try:
        audit = db.query(AuditTrail).filter(AuditTrail.id == data["audit_trail_id"]).first()
        assert audit.decision_tree["cut_off_applied"] == 88.0
    finally:
        db.close()

    # Surcharge retiree (None) -> retour au global
    cleared = client.put("/api/settings/scoring", json={"cut_off_overrides": {"WATCHLIST_PEP": None}})
    assert cleared.status_code == 200
    assert "WATCHLIST_PEP" not in cleared.json()["cut_off_overrides"]


# ------------------ ROLE AUDITEUR (LECTURE SEULE) ------------------

def test_auditor_role_exclusive():
    assert normalize_roles("auditor") == "auditor"
    with pytest.raises(ValueError):
        normalize_roles("auditor,user")


def test_auditor_readonly_enforced(client):
    auditor = f"test_intl_aud_{uuid.uuid4().hex[:6]}"
    _create_user(client, auditor, role="auditor")
    # Combinaison interdite a la creation
    bad = client.post("/api/users", json={
        "username": f"test_intl_{uuid.uuid4().hex[:6]}", "password": STRONG_PW,
        "full_name": "x", "role": "auditor,user"})
    assert bad.status_code == 400

    token = client.post("/api/auth/login", json={
        "username": auditor, "password": STRONG_PW}).json()["access_token"]

    # Chemin d'authentification reel (sans override) : lecture OK, ecriture 403
    saved_override = app.dependency_overrides.pop(get_current_user)
    try:
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/counters", headers=headers).status_code == 200
        assert client.get("/api/alerts?page=1&page_size=1", headers=headers).status_code == 200
        forbidden = client.post("/api/alerts/bulk", headers=headers,
                                json={"ids": [1], "action": "assign"})
        assert forbidden.status_code == 403
        assert "lecture seule" in forbidden.json()["detail"]
        assert client.put("/api/settings/scoring", headers=headers,
                          json={"cut_off_threshold": 50}).status_code == 403
        # Gestion de sa propre session : autorisee (mot de passe, logout)
        pw_change = client.put("/api/users/me/password", headers=headers, json={
            "old_password": STRONG_PW, "new_password": "NouveauFort2026x"})
        assert pw_change.status_code == 200, pw_change.text
        assert client.post("/api/auth/logout", headers=headers).status_code == 200
    finally:
        app.dependency_overrides[get_current_user] = saved_override


# ------------------ I18N (6 LANGUES + RTL) ------------------

def test_i18n_assets_complete():
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    login = (STATIC / "login.html").read_text(encoding="utf-8")
    styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    # Les 5 langues cibles declarees + libelles natifs
    for lang, native in (("en", "English"), ("de", "Deutsch"), ("es", "Español"),
                         ("zh", "中文"), ("ar", "العربية")):
        assert f'{lang}: "{native}"' in i18n, f"langue manquante : {lang}"
    # Chaque entree du dictionnaire porte les 5 langues (echantillon cle)
    for probe in ('en: "Overview"', 'de: "Übersicht"', 'es: "Resumen"',
                  'zh: "总览"', 'ar: "نظرة عامة"'):
        assert probe in i18n
    # Volume de couverture : au moins 200 entrees traduites
    assert len(re.findall(r'\ben: "', i18n)) >= 200

    # Moteur : observer du contenu dynamique + RTL + persistance
    for marker in ("MutationObserver", 'dir = (lang === "ar")', "fiskr_lang", "setLang"):
        assert marker in i18n

    # Selecteur de langue present sur le dashboard ET la page de connexion
    assert index.count('id="lang-select"') == 1 and "i18n.js" in index
    assert login.count('id="lang-select"') == 1 and "i18n.js" in login
    # Regles RTL pour l'arabe
    assert '[dir="rtl"] .sidebar' in styles and '[dir="rtl"] .main-content' in styles


def test_i18n_paragraphs_and_locales():
    """Couverture etendue : paragraphes descriptifs, chaines composees
    (regex), locales de dates — chaque paragraphe de l'ecran est traduit
    dans les 5 langues et les dates suivent la langue active."""
    i18n = (STATIC / "i18n.js").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")

    # Tous les paragraphes section-desc de l'ecran sont dans le dictionnaire P
    import html as html_mod
    descs = re.findall(r'<p class="section-desc"[^>]*>(.*?)</p>', index, re.S)
    keys = set()
    for d in descs:
        text = html_mod.unescape(re.sub(r"<[^>]+>", "", d))
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            keys.add(text)
    assert len(keys) >= 45
    missing = [k for k in keys if json.dumps(k, ensure_ascii=False) not in i18n]
    assert not missing, f"paragraphes non traduits : {missing[:3]}"
    # Chaque entree paragraphe porte les 5 langues (sondage sur une entree)
    assert '"Historique immuable de toutes les décisions de criblage' in i18n
    for probe in ('"Immutable history', '"Unveränderliche Historie',
                  '"Historial inmutable', '引擎所有筛查决策', 'تاريخ ثابت لكل قرارات'):
        assert probe in i18n

    # Chaines composees (pagination, selection) traduites par regles regex
    assert "élément\\(s\\) — page" in i18n and "sélectionnée\\(s\\)" in i18n
    # Locales par langue + dates localisees dans app.js
    assert 'zh: "zh-CN"' in i18n and 'ar: "ar-SA-u-nu-latn"' in i18n
    assert "function uiLocale()" in app_js
    assert '"fr-FR"' not in app_js.replace('? window.fiskrI18n.locale() : "fr-FR"', "")

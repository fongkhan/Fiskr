"""
Tests des regles anti-faux positifs Python (canaux SCREENING / FILTERING),
du cycle de vie mode DEV (brouillon -> tests verts -> soumission -> 4-yeux ->
production, versionnage branche/merge) et du blocking key par canal.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, Alert, AlertEvent, AuditTrail, FpRule, FpRuleChange, FpRuleTest, AppSetting,
)
from fiskr.fprules import run_rule, compile_rule, evaluate_fp_rules
from fiskr.settings import SETTING_BLOCKING_SCREENING, SETTING_BLOCKING_FILTERING


def _override_user(username: str, role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup():
    db = next(get_db())
    try:
        rules = db.query(FpRule).filter(FpRule.name.like("test_fpr_%")).all()
        ids = [r.id for r in rules]
        if ids:
            db.query(FpRuleTest).filter(FpRuleTest.rule_id.in_(ids)).delete(synchronize_session=False)
            db.query(FpRuleChange).filter(FpRuleChange.rule_id.in_(ids)).delete(synchronize_session=False)
            db.query(FpRule).filter(FpRule.id.in_(ids)).delete(synchronize_session=False)
        test_alerts = db.query(Alert).filter(Alert.client_id.like("test_fpr_%")).all()
        aids = [a.id for a in test_alerts]
        if aids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(aids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(aids)).delete(synchronize_session=False)
        db.query(AppSetting).filter(
            AppSetting.key.in_([SETTING_BLOCKING_SCREENING, SETTING_BLOCKING_FILTERING])
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("param1", "rules,blocking,user")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup()


# ------------------ MOTEUR (unitaire, sans HTTP) ------------------

def test_compile_and_run_rule():
    fn = compile_rule("def rule(ctx):\n    return ctx['final_score'] < 80")
    assert fn({"final_score": 70}) is True
    assert fn({"final_score": 95}) is False


def test_compile_rejects_missing_function():
    with pytest.raises(ValueError):
        compile_rule("x = 1")
    with pytest.raises(ValueError):
        compile_rule("def rule(ctx) return True")  # syntaxe


def test_run_rule_reports_error():
    result, error = run_rule("def rule(ctx):\n    return ctx['nope'] + 1", {"final_score": 1})
    assert result is None
    assert "KeyError" in error


def test_evaluate_fail_open_on_exception():
    """Une regle qui leve est ignoree (fail-open) : l'alerte est conservee."""
    db = next(get_db())
    try:
        rule = FpRule(channel="SCREENING", name="test_fpr_boom", code="def rule(ctx):\n    raise ValueError('boom')",
                      status="ACTIVE", enabled=True, run_order=10)
        db.add(rule)
        db.commit()
        result = evaluate_fp_rules(db, "SCREENING", {"final_score": 50})
        assert result is None  # aucune suppression malgre le match
        db.query(FpRule).filter(FpRule.id == rule.id).delete()
        db.commit()
    finally:
        db.close()


# ------------------ CRUD + DROITS ------------------

def test_fprules_requires_role(client):
    _override_user("simple", "user")
    assert client.get("/api/fprules").status_code == 403
    assert client.post("/api/fprules", json={"channel": "SCREENING", "name": "x"}).status_code == 403
    _override_user("param1", "rules,user")
    assert client.get("/api/fprules").status_code == 200
    _override_user("adm", "admin")
    assert client.get("/api/fprules").status_code == 200


def test_create_rule_is_draft_and_validated_syntax(client):
    resp = client.post("/api/fprules", json={
        "channel": "SCREENING", "name": "test_fpr_draft",
        "code": "def rule(ctx):\n    return False",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "DRAFT"
    assert data["version"] == 1

    # Code invalide -> 400
    bad = client.post("/api/fprules", json={
        "channel": "SCREENING", "name": "test_fpr_bad", "code": "not python !!",
    })
    assert bad.status_code == 400

    # Canal inconnu -> 400
    assert client.post("/api/fprules", json={"channel": "NOPE", "name": "test_fpr_x"}).status_code == 400


# ------------------ CYCLE DE VIE MODE DEV (4-yeux, tests, merge) ------------------

def _make_draft(client, channel="SCREENING", code="def rule(ctx):\n    return ctx['final_score'] < 80"):
    return client.post("/api/fprules", json={
        "channel": channel, "name": f"test_fpr_{uuid.uuid4().hex[:6]}", "code": code,
    }).json()


def test_submit_requires_green_tests(client):
    rule = _make_draft(client)
    rid = rule["id"]
    # Aucun test -> soumission refusee
    assert client.post(f"/api/fprules/{rid}/submit").status_code == 400

    # Ajoute un test qui ECHOUE (attendu conserver, mais la regle supprime)
    client.post(f"/api/fprules/{rid}/tests", json={
        "name": "cas", "ctx": {"final_score": 50, "hard_match": False}, "expected": False,
    })
    assert client.post(f"/api/fprules/{rid}/submit").status_code == 400

    # Corrige l'attendu -> tous verts -> soumission acceptee
    client.put(f"/api/fprules/{rid}", json={"code": "def rule(ctx):\n    return ctx['final_score'] < 80"})
    # Le test ci-dessus (score 50) attend maintenant supprimer
    tests = client.get(f"/api/fprules/{rid}/tests").json()["items"]
    client.delete(f"/api/fprules/{rid}/tests/{tests[0]['id']}")
    client.post(f"/api/fprules/{rid}/tests", json={
        "name": "supprime", "ctx": {"final_score": 50}, "expected": True,
    })
    resp = client.post(f"/api/fprules/{rid}/submit")
    assert resp.status_code == 200
    assert resp.json()["status"] == "PENDING_VALIDATION"


def test_four_eyes_validation_and_merge(client):
    rule = _make_draft(client)
    rid = rule["id"]
    client.post(f"/api/fprules/{rid}/tests", json={"name": "t", "ctx": {"final_score": 50}, "expected": True})
    client.post(f"/api/fprules/{rid}/submit")

    # Le soumetteur ne peut pas valider (4-yeux)
    assert client.post(f"/api/fprules/{rid}/validate", json={}).status_code == 403

    # Un autre utilisateur habilite valide -> ACTIVE
    _override_user("param2", "rules,user")
    resp = client.post(f"/api/fprules/{rid}/validate", json={"comment": "ok"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "ACTIVE"

    # Modifier une ACTIVE cree une nouvelle version DRAFT (branche)
    v2 = client.put(f"/api/fprules/{rid}", json={"code": "def rule(ctx):\n    return ctx['final_score'] < 70"}).json()
    assert v2["status"] == "DRAFT"
    assert v2["version"] == 2
    assert v2["replaces_rule_id"] == rid

    # Valider la v2 supersede la v1 (merge)
    client.post(f"/api/fprules/{v2['id']}/tests", json={"name": "t", "ctx": {"final_score": 60}, "expected": True})
    client.post(f"/api/fprules/{v2['id']}/submit")
    _override_user("param1", "rules,user")
    client.post(f"/api/fprules/{v2['id']}/validate", json={})
    db = next(get_db())
    try:
        assert db.query(FpRule).filter(FpRule.id == rid).first().status == "SUPERSEDED"
        assert db.query(FpRule).filter(FpRule.id == v2["id"]).first().status == "ACTIVE"
    finally:
        db.close()


def test_reject_returns_to_draft(client):
    rule = _make_draft(client)
    rid = rule["id"]
    client.post(f"/api/fprules/{rid}/tests", json={"name": "t", "ctx": {"final_score": 50}, "expected": True})
    client.post(f"/api/fprules/{rid}/submit")
    _override_user("param2", "rules,user")
    assert client.post(f"/api/fprules/{rid}/reject", json={}).status_code == 400  # commentaire requis
    resp = client.post(f"/api/fprules/{rid}/reject", json={"comment": "à revoir"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "DRAFT"


def test_draft_never_applied_in_production(client):
    """Une regle DRAFT ne supprime rien (seules les ACTIVE + enabled s'appliquent)."""
    db = next(get_db())
    try:
        draft = FpRule(channel="SCREENING", name="test_fpr_draftprod",
                       code="def rule(ctx):\n    return True", status="DRAFT", enabled=True)
        db.add(draft)
        db.commit()
        assert evaluate_fp_rules(db, "SCREENING", {"final_score": 99}) is None
        draft.status = "ACTIVE"
        db.commit()
        assert evaluate_fp_rules(db, "SCREENING", {"final_score": 99}) is not None
        db.query(FpRule).filter(FpRule.id == draft.id).delete()
        db.commit()
    finally:
        db.close()


def test_channels_are_independent(client):
    """Une regle SCREENING ne s'applique pas au canal FILTERING."""
    db = next(get_db())
    try:
        r = FpRule(channel="SCREENING", name="test_fpr_chan",
                   code="def rule(ctx):\n    return True", status="ACTIVE", enabled=True)
        db.add(r)
        db.commit()
        assert evaluate_fp_rules(db, "SCREENING", {"final_score": 10}) is not None
        assert evaluate_fp_rules(db, "FILTERING", {"final_score": 10}) is None
        db.query(FpRule).filter(FpRule.id == r.id).delete()
        db.commit()
    finally:
        db.close()


# ------------------ BLOCKING PAR CANAL ------------------

def test_blocking_settings_role_and_validation(client):
    _override_user("simple", "user")
    assert client.get("/api/settings/blocking").status_code == 403
    _override_user("param1", "blocking,user")
    data = client.get("/api/settings/blocking").json()
    assert set(data["screening"]["layout"]) <= set(data["components"])

    # Composante inconnue -> 400
    assert client.put("/api/settings/blocking", json={"screening_layout": ["NOPE"]}).status_code == 400
    # Layout valide -> 200 + rechargement du cache signale
    resp = client.put("/api/settings/blocking", json={"screening_layout": ["PHONETIC_FIRST"]})
    assert resp.status_code == 200
    assert resp.json()["cache_reloaded"] is True
    assert resp.json()["screening"]["layout"] == ["PHONETIC_FIRST"]
    # Restaure le layout par defaut (cache)
    client.put("/api/settings/blocking", json={"screening_layout": ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]})


def test_filtering_layout_default_is_phonetic(client):
    data = client.get("/api/settings/blocking").json()
    assert data["filtering"]["layout"] == ["PHONETIC_FIRST"]
    assert data["filtering"]["source"] == "config"


# ------------------ INTEGRATION : SUPPRESSION EN PRODUCTION (CLOSED_BY_RULE) ------------------

def _putin_payload(client_id):
    return {
        "client_id": client_id, "client_type": "PP",
        "client_first_name": "Vladimir", "client_last_name": "Putin",
        "client_dob": "1952-10-07", "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []},
    }


def _activate_rule(client, channel, code):
    """Cree, teste, soumet et valide une regle en production (4-yeux)."""
    rule = client.post("/api/fprules", json={
        "channel": channel, "name": f"test_fpr_prod_{uuid.uuid4().hex[:6]}", "code": code,
    }).json()
    rid = rule["id"]
    client.post(f"/api/fprules/{rid}/tests", json={"name": "t", "ctx": {"final_score": 100, "hard_match": True}, "expected": True})
    client.post(f"/api/fprules/{rid}/submit")
    _override_user("param2", "rules,user")
    client.post(f"/api/fprules/{rid}/validate", json={})
    _override_user("param1", "rules,blocking,user")
    return rid


def test_active_rule_closes_alert_and_audits(client):
    # Regle qui supprime tout (SCREENING). Le criblage cree l'alerte puis
    # l'auto-cloture CLOSED_BY_RULE, avec trace au journal d'audit.
    rid = _activate_rule(client, "SCREENING", "def rule(ctx):\n    return True")
    cid = f"test_fpr_prod_{uuid.uuid4().hex[:8]}"
    data = client.post("/api/screen", json=_putin_payload(cid)).json()
    assert data["best_match"]["status"] == "ALERT"
    alert_id = data["alert_id"]
    assert alert_id is not None

    detail = client.get(f"/api/alerts/{alert_id}").json()
    assert detail["status"] == "CLOSED_BY_RULE"
    assert detail["channel"] == "SCREENING"
    assert detail["decided_by"] == "fp-rule"
    # Trace immuable : la regle appliquee est dans le decision_tree
    assert detail["decision_tree"]["fp_rule_applied"]["id"] == rid
    # Evenement RULE_SUPPRESSED dans l'historique
    assert any(e["action"] == "RULE_SUPPRESSED" for e in detail["events"])

    # hit_count incremente
    rules = client.get("/api/fprules?channel=SCREENING").json()["items"]
    assert next(r for r in rules if r["id"] == rid)["hit_count"] >= 1

    # Nettoyage de l'alerte creee
    db = next(get_db())
    try:
        db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id).delete(synchronize_session=False)
        db.query(Alert).filter(Alert.id == alert_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_alert_channel_filter(client):
    # Une alerte SCREENING n'apparait pas dans la file FILTERING
    cid = f"test_fpr_chan_{uuid.uuid4().hex[:8]}"
    data = client.post("/api/screen", json=_putin_payload(cid)).json()
    alert_id = data["alert_id"]
    assert alert_id is not None
    screening = client.get("/api/alerts", params={"channel": "SCREENING", "page_size": 200}).json()
    assert alert_id in [a["id"] for a in screening["items"]]
    filtering = client.get("/api/alerts", params={"channel": "FILTERING", "page_size": 200}).json()
    assert alert_id not in [a["id"] for a in filtering["items"]]
    assert all(a["channel"] == "SCREENING" for a in screening["items"])

    # Compteurs par canal
    counters = client.get("/api/counters").json()
    assert "open_alerts_screening" in counters and "open_alerts_filtering" in counters

    db = next(get_db())
    try:
        db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id).delete(synchronize_session=False)
        db.query(Alert).filter(Alert.id == alert_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ------------------ AIDES A L'EDITION (validate / contexte / generation) ------------------

def test_validate_endpoint_reports_error_line(client):
    # Erreur de syntaxe ligne 3 : la ligne remonte pour positionner le curseur
    resp = client.post("/api/fprules/validate", json={
        "code": "def rule(ctx):\n    x = 1\n    return x +\n"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is False
    assert data["line"] == 3

    ok = client.post("/api/fprules/validate", json={
        "code": "def rule(ctx):\n    return ctx['final_score'] < 80\n"
    })
    assert ok.json()["valid"] is True

    # Compile mais ne definit pas rule(ctx) : invalide sans numero de ligne
    norule = client.post("/api/fprules/validate", json={"code": "x = 1"})
    assert norule.json()["valid"] is False
    assert norule.json()["line"] is None


def test_context_from_alert_prefills_test_ctx(client):
    db = next(get_db())
    try:
        audit = AuditTrail(
            client_id="test_fpr_ctx1", client_name="Jean Contexte", client_type="PP",
            watchlist_id="EU-CTX-1", watchlist_name="Ivan CONTEXTOV",
            base_score=82.0, final_score=87.5, status="ALERT",
            decision_tree={"base_score": 82.0, "hard_match_triggered": False,
                           "adjustments": {"dob_bonus": 5.5},
                           "watchlist_entity": {"entity_type": "I"}},
            config_state={}, watchlist_version="test", watchlist_hash="h" * 64,
            list_type="WATCHLIST_EU",
        )
        db.add(audit)
        db.flush()
        alert = Alert(
            audit_id=audit.id,
            client_id="test_fpr_ctx1", client_name="Jean Contexte",
            watchlist_entity_id="EU-CTX-1", watchlist_name="Ivan CONTEXTOV",
            list_type="WATCHLIST_EU", final_score=87.5,
            status="OPEN", channel="SCREENING",
        )
        db.add(alert)
        db.commit()
        alert_id = alert.id
    finally:
        db.close()

    resp = client.get(f"/api/fprules/context-from-alert/{alert_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["alert_id"] == alert_id
    ctx = data["ctx"]
    assert ctx["client_name"] == "Jean Contexte"
    assert ctx["entity_name"] == "Ivan CONTEXTOV"
    assert ctx["final_score"] == 87.5
    assert ctx["channel"] == "SCREENING"
    # Reconstruit depuis le decision_tree du journal d'audit
    assert ctx["base_score"] == 82.0
    assert ctx["hard_match"] is False
    assert ctx["adjustments"] == {"dob_bonus": 5.5}
    assert ctx["entity"] == {"entity_type": "I"}

    missing = client.get("/api/fprules/context-from-alert/99999999")
    assert missing.status_code == 404


def test_generate_rule_503_when_llm_not_configured(client):
    # Config par defaut : fprules.llm_enabled = false -> erreur EXPLICITE
    resp = client.post("/api/fprules/generate", json={
        "instruction": "supprimer les alertes sous 80 sans hard match",
        "channel": "SCREENING",
    })
    assert resp.status_code == 503
    assert "formulaire" in resp.json()["detail"].lower()


def test_generate_rule_success_mocked(client, monkeypatch):
    import fiskr.api as api_mod

    def fake_generate(instruction, channel, model=None):
        assert channel == "SCREENING"
        return {"code": "def rule(ctx):\n    return ctx['final_score'] < 80 and not ctx['hard_match']",
                "explanation": "Supprime les scores faibles hors correspondance exacte.",
                "model": "claude-sonnet-5"}

    monkeypatch.setattr(api_mod, "generate_rule_code", fake_generate)
    resp = client.post("/api/fprules/generate", json={
        "instruction": "supprimer sous 80 sauf hard match", "channel": "SCREENING",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "def rule(ctx)" in data["code"]
    assert data["explanation"]
    assert data["model"] == "claude-sonnet-5"


def test_generate_rule_422_returns_raw_code(client, monkeypatch):
    import fiskr.api as api_mod
    from fiskr.fprules import RuleGenerationFailed

    def fake_generate(instruction, channel, model=None):
        raise RuleGenerationFailed("Le code généré reste invalide après relance : syntaxe",
                                   raw_code="def rule(ctx)\n    return Tru")

    monkeypatch.setattr(api_mod, "generate_rule_code", fake_generate)
    resp = client.post("/api/fprules/generate", json={
        "instruction": "quelque chose d'impossible", "channel": "SCREENING",
    })
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "raw_code" in detail
    assert "def rule" in detail["raw_code"]


def test_generate_rule_invalid_channel_or_empty(client):
    assert client.post("/api/fprules/generate", json={
        "instruction": "x", "channel": "AUTRE"}).status_code == 400
    assert client.post("/api/fprules/generate", json={
        "instruction": "   ", "channel": "SCREENING"}).status_code == 400

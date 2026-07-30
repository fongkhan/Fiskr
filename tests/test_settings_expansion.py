"""
Familles de config.yaml devenues pilotables depuis l'application (admin) :
institution/TRACFIN, adverse media, IA (narratifs + regles), politique
d'acces, reseau des synchronisations, inbox CFT, ponderations et bonus/malus
du scoring, webhooks de notification.

Regle verrouillee partout : base > config.yaml (le fichier ne fournit que les
defauts du premier demarrage), validation stricte cote serveur, ecriture
reservee a l'admin, et chaque consommateur lit l'etat EFFECTIF.

Et la gestion du journal des notifications : filtre serveur par statut,
suppression unitaire, purge en masse (QUEUED protege par defaut), renvoi
d'un echec — toutes admin, toutes tracees.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr import api as api_module
from fiskr.api import app, _PORTABLE_SETTINGS
from fiskr.auth import get_current_user, security_config
from fiskr.database import get_db, AppSetting, NotificationDelivery, AdminAuditLog
from fiskr.settings import (
    set_setting, scoring_config_with_thresholds, notification_webhooks,
    sync_network_settings, institution_config,
    SETTING_INSTITUTION, SETTING_ADVERSE_MEDIA, SETTING_NARRATIVE_LLM,
    SETTING_FPRULES_LLM, SETTING_SECURITY_ACCESS, SETTING_SYNC_NETWORK,
    SETTING_BATCH_INBOX, SETTING_SCORING_WEIGHTS, SETTING_SCORING_CONTEXT,
    SETTING_NOTIFY_WEBHOOKS,
)

_ALL_KEYS = (SETTING_INSTITUTION, SETTING_ADVERSE_MEDIA, SETTING_NARRATIVE_LLM,
             SETTING_FPRULES_LLM, SETTING_SECURITY_ACCESS, SETTING_SYNC_NETWORK,
             SETTING_BATCH_INBOX, SETTING_SCORING_WEIGHTS, SETTING_SCORING_CONTEXT,
             SETTING_NOTIFY_WEBHOOKS)


def _override_user(username: str, role: str = "admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_(_ALL_KEYS)).delete(synchronize_session=False)
        db.query(NotificationDelivery).filter(
            NotificationDelivery.event_key.like("test_exp_%")).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_exp").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _cleanup()
    _override_user("admin_exp", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup()


@pytest.fixture
def db_session():
    db = next(get_db())
    yield db
    db.close()


# ------------------ SURCHARGE A CHAUD + CONSOMMATEURS ------------------

def test_institution_hot_setting_reaches_tracfin_reader(client, db_session):
    r = client.put("/api/settings/ingestion", json={"institution": {
        "name": "Banque Test", "siren": "123456789",
        "correspondent_name": "Alice Martin",
        "correspondent_email": "alice@test.example", "correspondent_phone": "0102030405"}})
    assert r.status_code == 200
    assert r.json()["institution"]["name"] == "Banque Test"
    # Le lecteur TRACFIN lit l'etat effectif
    assert api_module._institution_config(db_session)["siren"] == "123456789"
    assert institution_config(db_session)["correspondent_name"] == "Alice Martin"


def test_institution_rejects_bad_siren(client):
    r = client.put("/api/settings/ingestion", json={"institution": {"siren": "12AB"}})
    assert r.status_code == 400
    assert "SIREN" in r.json()["detail"]


def test_scoring_weights_and_rules_flow_into_engine_config(client, db_session):
    r = client.put("/api/settings/scoring", json={
        "weights": {"jaro_winkler": 0.6, "damerau_levenshtein": 0.3, "token_sort": 0.1},
        "contextual_rules": {"gender_conflict_malus": -30},
    })
    assert r.status_code == 200
    cfg = scoring_config_with_thresholds(db_session)
    assert cfg["scoring"]["weights"]["jaro_winkler"] == 0.6
    assert cfg["scoring"]["contextual_rules"]["gender_conflict_malus"] == -30
    # Les autres regles gardent leur defaut (fusion, pas remplacement)
    assert cfg["scoring"]["contextual_rules"]["dob_exact_bonus"] == 15


def test_scoring_weights_validation(client):
    # Poids tous nuls -> moteur aveugle, refuse
    r = client.put("/api/settings/scoring", json={
        "weights": {"jaro_winkler": 0, "damerau_levenshtein": 0, "token_sort": 0}})
    assert r.status_code == 400
    # Poids negatif refuse
    assert client.put("/api/settings/scoring",
                      json={"weights": {"jaro_winkler": -1}}).status_code == 400
    # Cle inconnue refusee
    assert client.put("/api/settings/scoring",
                      json={"weights": {"pas_une_metrique": 0.5}}).status_code == 400


def test_security_access_effective_immediately(client, db_session):
    r = client.put("/api/settings/ingestion", json={"security_access": {
        "max_login_failures": 3, "min_password_length": 16}})
    assert r.status_code == 200
    effective = security_config(db_session)
    assert effective["max_login_failures"] == 3
    assert effective["min_password_length"] == 16
    assert effective["lockout_minutes"] == 15   # non touche : defaut conserve
    # Bornes
    assert client.put("/api/settings/ingestion",
                      json={"security_access": {"min_password_length": 4}}).status_code == 400


def test_sync_network_hot_and_standalone(client, db_session):
    r = client.put("/api/settings/sync", json={"network": {"retries": 7, "backoff_seconds": 10}})
    assert r.status_code == 200
    # Lecture avec session ET standalone (celle des helpers HTTP du moteur)
    assert sync_network_settings(db_session)["retries"] == 7
    assert sync_network_settings()["retries"] == 7
    from fiskr.sync import get_sync_config
    assert get_sync_config()["network"]["backoff_seconds"] == 10.0
    # Validation
    assert client.put("/api/settings/sync",
                      json={"network": {"retries": 999}}).status_code == 400
    assert client.put("/api/settings/sync",
                      json={"network": {"user_agent": ""}}).status_code == 400


def test_batch_inbox_requires_absolute_paths(client):
    assert client.put("/api/settings/ingestion", json={"batch_inbox": {
        "inbox_dir": "relatif/inbox"}}).status_code == 400
    ok = client.put("/api/settings/ingestion", json={"batch_inbox": {
        "inbox_dir": "/tmp/fiskr_inbox_test", "inbox_poll_seconds": 30}})
    assert ok.status_code == 200
    assert ok.json()["batch_inbox"]["inbox_poll_seconds"] == 30


def test_webhooks_validated_and_read_by_transport(client):
    bad = client.put("/api/settings/ingestion",
                     json={"notification_webhooks": ["ftp://nope"]})
    assert bad.status_code == 400
    ok = client.put("/api/settings/ingestion",
                    json={"notification_webhooks": ["https://hooks.example.com/fiskr"]})
    assert ok.status_code == 200
    from fiskr.notify import _webhook_urls
    assert _webhook_urls() == ["https://hooks.example.com/fiskr"]


def test_llm_settings_hot(client, db_session):
    r = client.put("/api/settings/ingestion", json={
        "fprules_llm": {"llm_enabled": True, "llm_model": "claude-opus-5"}})
    assert r.status_code == 200
    from fiskr.fprules import get_fprules_llm_config
    assert get_fprules_llm_config(db_session) == {"llm_enabled": True, "llm_model": "claude-opus-5"}
    # Active sans modele -> refuse
    assert client.put("/api/settings/ingestion", json={
        "narrative_llm": {"llm_enabled": True, "llm_model": ""}}).status_code == 400


def test_non_admin_cannot_write_any_family():
    _cleanup()
    _override_user("user_exp", "reviewer,user")
    try:
        with TestClient(app) as c:
            assert c.put("/api/settings/ingestion",
                         json={"institution": {"name": "X"}}).status_code == 403
            assert c.put("/api/settings/scoring",
                         json={"weights": {"jaro_winkler": 1}}).status_code == 403
        db = next(get_db())
        try:
            assert db.query(AppSetting).filter(AppSetting.key.in_(_ALL_KEYS)).count() == 0
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()
        _cleanup()


def test_portability_includes_new_families_but_not_machine_paths():
    for key in (SETTING_INSTITUTION, SETTING_SECURITY_ACCESS, SETTING_SYNC_NETWORK,
                SETTING_SCORING_WEIGHTS, SETTING_SCORING_CONTEXT, SETTING_NOTIFY_WEBHOOKS):
        assert key in _PORTABLE_SETTINGS, key
    assert SETTING_BATCH_INBOX not in _PORTABLE_SETTINGS  # chemins propres a la machine


# ------------------ GESTION DU JOURNAL DES NOTIFICATIONS ------------------

def _delivery(db, *, status="FAILED", event_key="test_exp_event", days_old=0):
    row = NotificationDelivery(
        event_key=event_key, category="lists", urgency="immediate",
        status=status, recipients="ops@test.example",
        payload={"Liste": "WATCHLIST_TEST"},
        created_at=datetime.utcnow() - timedelta(days=days_old),
    )
    db.add(row)
    db.commit()
    return row.id


def test_log_filters_by_status_server_side(client, db_session):
    _delivery(db_session, status="FAILED")
    _delivery(db_session, status="SENT")
    data = client.get("/api/notifications/log?status=FAILED&limit=200").json()
    assert all(i["status"] == "FAILED" for i in data["items"])
    assert client.get("/api/notifications/log?status=NIMPORTE").status_code == 400


def test_delete_single_entry(client, db_session):
    delivery_id = _delivery(db_session)
    r = client.delete(f"/api/notifications/log/{delivery_id}")
    assert r.status_code == 200
    assert db_session.query(NotificationDelivery).filter(
        NotificationDelivery.id == delivery_id).first() is None
    assert client.delete("/api/notifications/log/99999999").status_code == 404


def test_purge_protects_queued_by_default(client, db_session):
    sent_id = _delivery(db_session, status="SENT")
    queued_id = _delivery(db_session, status="QUEUED")
    r = client.post("/api/notifications/log/purge", json={})
    assert r.status_code == 200
    assert r.json()["deleted"] >= 1
    remaining = {row.id for row in db_session.query(NotificationDelivery).filter(
        NotificationDelivery.event_key == "test_exp_event").all()}
    assert queued_id in remaining          # le recapitulatif en attente survit
    assert sent_id not in remaining
    # QUEUED ne part que sur demande explicite
    r2 = client.post("/api/notifications/log/purge", json={"statuses": ["QUEUED"]})
    assert r2.status_code == 200
    db_session.expire_all()
    assert db_session.query(NotificationDelivery).filter(
        NotificationDelivery.id == queued_id).first() is None


def test_purge_by_age(client, db_session):
    old_id = _delivery(db_session, status="SENT", days_old=100)
    recent_id = _delivery(db_session, status="SENT", days_old=1)
    r = client.post("/api/notifications/log/purge", json={"older_than_days": 30})
    assert r.status_code == 200
    remaining = {row.id for row in db_session.query(NotificationDelivery).filter(
        NotificationDelivery.event_key == "test_exp_event").all()}
    assert old_id not in remaining and recent_id in remaining


def test_resend_failed_updates_row(client, db_session, monkeypatch):
    delivery_id = _delivery(db_session, status="FAILED")
    sent = {}
    monkeypatch.setattr(api_module, "notify_smtp_configured", lambda: True)
    monkeypatch.setattr(api_module, "notify_send_email",
                        lambda recipients, subject, body, html_body=None: sent.update(
                            {"recipients": recipients, "subject": subject}))
    r = client.post(f"/api/notifications/log/{delivery_id}/resend")
    assert r.status_code == 200, r.text
    assert sent["recipients"] == ["ops@test.example"]
    db_session.expire_all()
    row = db_session.query(NotificationDelivery).filter(
        NotificationDelivery.id == delivery_id).first()
    assert row.status == "SENT" and row.sent_at is not None and row.error is None


def test_resend_refused_on_sent_entry(client, db_session):
    delivery_id = _delivery(db_session, status="SENT")
    assert client.post(f"/api/notifications/log/{delivery_id}/resend").status_code == 400


def test_notifications_management_is_admin_only(db_session):
    delivery_id = _delivery(db_session, status="FAILED")
    _override_user("user_notif", "reviewer,user")
    try:
        with TestClient(app) as c:
            assert c.get("/api/notifications/log").status_code == 403
            assert c.delete(f"/api/notifications/log/{delivery_id}").status_code == 403
            assert c.post("/api/notifications/log/purge", json={}).status_code == 403
            assert c.post(f"/api/notifications/log/{delivery_id}/resend").status_code == 403
    finally:
        app.dependency_overrides.clear()
        _cleanup()

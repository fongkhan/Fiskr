"""
Tests du systeme de notifications par etape (production des listes, criblage,
filtrage) : catalogue, routage par role, immediat vs recapitulatif, accroches
metier, rendu des mails et garantie de non-blocage.

Aucun SMTP reel n'est sollicite : le transport est systematiquement monkeypatche.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr import notifier, notify
from tests.conftest import post_and_wait
from fiskr.api import app
from fiskr.auth import VALID_ROLES, get_current_user
from fiskr.database import (
    get_db, Alert, AlertEvent, AuditTrail, NotificationDelivery, Snapshot,
    User, WatchlistEntity, WhitelistPair, AppSetting, hash_password,
)
from fiskr.events import (
    AUDIENCE_ACTOR, AUDIENCE_ASSIGNEE, CATEGORY_LABELS, DIGEST, EVENT_CATALOG,
    IMMEDIATE, URGENCIES,
)
from fiskr.settings import (
    DEFAULT_NOTIFICATION_EVENTS, SETTING_NOTIFICATIONS, SETTING_NOTIFICATION_BATCH,
    SETTING_WHITELIST_EXPIRY_NOTIFIED, SETTING_REQUIRE_APPROVAL, set_setting,
)


def _override_user(role: str, username: str = "notif_admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup():
    db = next(get_db())
    try:
        db.query(NotificationDelivery).delete(synchronize_session=False)
        db.query(AppSetting).filter(AppSetting.key.in_(
            [SETTING_NOTIFICATIONS, SETTING_NOTIFICATION_BATCH,
             SETTING_WHITELIST_EXPIRY_NOTIFIED, SETTING_REQUIRE_APPROVAL]
        )).delete(synchronize_session=False)
        users = db.query(User).filter(User.username.like("notif_%")).all()
        for u in users:
            db.delete(u)
        alerts = db.query(Alert).filter(Alert.client_id.like("test_notif_%")).all()
        ids = [a.id for a in alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(WhitelistPair).filter(WhitelistPair.client_id.like("test_notif_%")).delete(synchronize_session=False)
        snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_notif_%")).all()
        snap_ids = [s.snapshot_id for s in snaps]
        if snap_ids:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup()


@pytest.fixture
def db():
    session = next(get_db())
    yield session
    session.close()
    _cleanup()


@pytest.fixture
def captured(monkeypatch):
    """Capture les envois SMTP sans jamais ouvrir de connexion."""
    sent = []

    def fake_send(recipients, subject, body, html_body=None):
        sent.append({"recipients": list(recipients), "subject": subject,
                     "text": body, "html": html_body})

    monkeypatch.setattr(notify, "send_email", fake_send)
    monkeypatch.setattr(notifier.notify, "send_email", fake_send)
    monkeypatch.setattr(notify, "smtp_configured", lambda: True)
    monkeypatch.setattr(notifier.notify, "smtp_configured", lambda: True)
    monkeypatch.setattr(notify, "_webhook_urls", lambda: [])
    monkeypatch.setattr(notifier.notify, "_webhook_urls", lambda: [])
    return sent


def _make_user(db, username, email, role):
    h, salt = hash_password("Motdepasse-1234")
    user = User(username=username, hashed_password=h, salt=salt,
                full_name=username, email=email, role=role)
    db.add(user)
    db.commit()
    return user


# ------------------ CATALOGUE ------------------

def test_catalog_is_coherent():
    """Le catalogue est la source unique : defauts, libelles et audiences en dependent."""
    from fiskr.notify import EVENT_LABELS
    assert set(EVENT_CATALOG) == set(DEFAULT_NOTIFICATION_EVENTS) == set(EVENT_LABELS)
    allowed = set(VALID_ROLES) | {AUDIENCE_ASSIGNEE, AUDIENCE_ACTOR}
    for key, event in EVENT_CATALOG.items():
        assert event.label.strip(), key
        assert event.category in CATEGORY_LABELS, key
        assert event.urgency in URGENCIES, key
        assert event.audience, key
        assert set(event.audience) <= allowed, (key, event.audience)


def test_catalog_covers_production_steps():
    """Les etapes structurantes demandees sont bien toutes notifiables."""
    expected = {
        "snapshot_approved", "snapshot_rejected", "list_import_done", "list_import_failed",
        "sync_completed", "sync_error", "review_exclusions_changed", "backtest_completed",
        "test_panel_generated", "whitelist_bulk_created", "rescreen_completed",
        "alert_created", "alert_assigned", "alert_escalated", "alert_pending_validation",
        "alert_decision_validated", "alert_decision_returned", "alert_overdue_sla",
        "fprule_submitted", "fprule_activated", "fprule_rejected",
        "filtering_hit", "batch_campaign_done", "batch_campaign_failed",
    }
    assert expected <= set(EVENT_CATALOG)


def test_catalog_endpoint_and_unknown_event_refused(client):
    response = client.get("/api/settings/notifications/catalog")
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["events"]) == len(EVENT_CATALOG)
    assert set(data["categories"]) == set(CATEGORY_LABELS)
    assert "smtp_configured" in data and "batch" in data
    # Une cle inconnue reste refusee par le reglage (non-regression lot B)
    bad = client.put("/api/settings/ingestion", json={"notification_events": {"invente": True}})
    assert bad.status_code == 400


# ------------------ ROUTAGE PAR ROLE ------------------

def test_recipients_resolved_by_role(db):
    _make_user(db, "notif_rev", "rev@banque.test", "reviewer")
    _make_user(db, "notif_user", "user@banque.test", "user")
    # snapshot_approved vise reviewer + admin
    recipients = notifier.resolve_recipients(db, "snapshot_approved", {})
    assert "rev@banque.test" in recipients
    assert "user@banque.test" not in recipients


def test_recipients_assignee_follows_absence_delegation(db):
    _make_user(db, "notif_absent", "absent@banque.test", "user")
    _make_user(db, "notif_delegue", "delegue@banque.test", "user")
    absent = db.query(User).filter(User.username == "notif_absent").first()
    absent.absent_until = datetime.utcnow() + timedelta(days=3)
    absent.delegate_to = "notif_delegue"
    db.commit()

    recipients = notifier.resolve_recipients(db, "alert_assigned", {"_assignee": "notif_absent"})
    assert "absent@banque.test" in recipients
    assert "delegue@banque.test" in recipients  # le délégué reçoit aussi


def test_recipients_fallback_to_global_list(db, monkeypatch):
    """Sans email de compte, on retombe sur NOTIFY_EMAIL_TO (comportement historique)."""
    monkeypatch.setattr(notify, "default_recipients", lambda: ["conformite@banque.test"])
    monkeypatch.setattr(notifier.notify, "default_recipients", lambda: ["conformite@banque.test"])
    recipients = notifier.resolve_recipients(db, "sync_error", {})
    assert recipients == ["conformite@banque.test"]


def test_disabled_event_is_silent(db, captured):
    set_setting(db, SETTING_NOTIFICATIONS, {"snapshot_approved": False}, updated_by="test")
    db.commit()
    notifier.emit(db, "snapshot_approved", {"Liste": "WATCHLIST_EU"})
    assert captured == []
    assert db.query(NotificationDelivery).filter(
        NotificationDelivery.event_key == "snapshot_approved").count() == 0


# ------------------ IMMÉDIAT VS RÉCAPITULATIF ------------------

def test_digest_event_is_queued_not_sent(db, captured):
    notifier.emit(db, "list_import_done", {"Liste": "WATCHLIST_EU", "Fiches": 42})
    assert captured == []  # rien n'est parti tout de suite
    row = db.query(NotificationDelivery).filter(
        NotificationDelivery.event_key == "list_import_done").first()
    assert row is not None and row.status == "QUEUED" and row.urgency == DIGEST


def test_immediate_event_is_sent_and_logged(db, captured, monkeypatch):
    monkeypatch.setattr(notifier.notify, "default_recipients", lambda: ["ops@banque.test"])
    # Envoi synchrone pour un test deterministe (pas de thread)
    monkeypatch.setattr(notifier.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})())
    notifier.emit(db, "snapshot_rejected", {"Liste": "WATCHLIST_EU", "Motif": "delta aberrant"})
    assert len(captured) == 1
    assert captured[0]["recipients"] == ["ops@banque.test"]
    assert "Liste rejetée" in captured[0]["subject"]
    assert "delta aberrant" in captured[0]["html"]


def test_flush_digest_groups_one_mail_per_recipient(db, captured, monkeypatch):
    monkeypatch.setattr(notifier.notify, "default_recipients", lambda: ["ops@banque.test"])
    for i in range(3):
        notifier.emit(db, "list_import_done", {"Liste": "WATCHLIST_EU", "Fiches": i})
    notifier.emit(db, "review_exclusions_changed", {"Snapshot": "snap-1", "Entités": 2})

    report = notifier.flush_digest(db)
    assert report["events"] == 4
    assert report["recipients"] == 1
    assert len(captured) == 1                      # UN seul mail malgré 4 évènements
    assert "Récapitulatif" in captured[0]["subject"]
    assert "Production des listes" in captured[0]["html"]
    assert db.query(NotificationDelivery).filter(
        NotificationDelivery.status == "QUEUED").count() == 0


def test_purge_deliveries_keeps_queued(db):
    notifier.emit(db, "list_import_done", {"Liste": "X"})
    old = NotificationDelivery(event_key="sync_completed", category="production_listes",
                               urgency=DIGEST, status="SENT",
                               created_at=datetime.utcnow() - timedelta(days=120))
    db.add(old)
    db.commit()
    deleted = notifier.purge_deliveries(db)
    assert deleted == 1
    assert db.query(NotificationDelivery).filter(
        NotificationDelivery.status == "QUEUED").count() == 1


# ------------------ RENDU DES MAILS ------------------

def test_render_event_email_with_and_without_link():
    text, html = notify.render_event_email(
        "Liste approuvée et mise en production",
        {"Liste": "WATCHLIST_EU", "Fiches": 1200},
        link_url="https://fiskr.example/#watchlists/watchlists-review")
    assert "Liste approuvée" in text and "WATCHLIST_EU" in text
    assert "Ouvrir dans Fiskr" in html and "fiskr.example" in html
    assert "1200" in html

    _text2, html2 = notify.render_event_email("Test", {"Clé": "valeur"}, link_url="")
    assert "Ouvrir dans Fiskr" not in html2   # jamais de lien cassé


def test_render_digest_email_lists_all_events():
    text, html, total = notify.render_digest_email({
        "production_listes": [{"label": "Import de liste mis en production",
                               "summary": "Liste : WATCHLIST_EU", "at": "2026-07-25T10:00:00"}],
        "criblage": [{"label": "Nouvelle alerte créée", "summary": "Score : 91",
                      "at": "2026-07-25T10:05:00"}],
    })
    assert total == 2
    assert "Production des listes" in html and "Criblage clients" in html
    assert "Nouvelle alerte créée" in text


# ------------------ ROBUSTESSE ------------------

def test_emit_never_raises(db, monkeypatch):
    """Un transport qui explose ne doit jamais casser l'opération métier."""
    def boom(*args, **kwargs):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(notifier.notify, "send_email", boom)
    monkeypatch.setattr(notifier.notify, "smtp_configured", lambda: True)
    monkeypatch.setattr(notifier.notify, "default_recipients", lambda: ["ops@banque.test"])
    monkeypatch.setattr(notifier.notify, "_webhook_urls", lambda: [])
    monkeypatch.setattr(notifier.threading, "Thread",
                        lambda target, args, daemon: type("T", (), {"start": lambda s: target(*args)})())
    notifier.emit(db, "snapshot_approved", {"Liste": "WATCHLIST_EU"})   # ne lève pas
    row = db.query(NotificationDelivery).filter(
        NotificationDelivery.event_key == "snapshot_approved").first()
    assert row is not None and row.status == "FAILED" and "SMTP down" in (row.error or "")


def test_emit_unknown_event_is_ignored(db):
    notifier.emit(db, "evenement_inexistant", {"x": 1})   # ne lève pas
    assert db.query(NotificationDelivery).filter(
        NotificationDelivery.event_key == "evenement_inexistant").count() == 0


# ------------------ ACCROCHES MÉTIER ------------------

def _spy_emit(monkeypatch):
    calls = []
    import fiskr.api as api_mod
    monkeypatch.setattr(api_mod, "emit", lambda db, key, payload=None, **kw: calls.append((key, payload or {}, kw)))
    return calls


def test_list_import_emits_step_event(client, monkeypatch):
    # Mise en production directe (sans homologation) : la branche READY
    assert client.put("/api/settings/ingestion", json={"require_approval": False}).status_code == 200
    calls = _spy_emit(monkeypatch)
    body = "entity_id,entity_type,primary_name,nationality\nEU-NOTIF-1,I,Ivan Notifov,RU\n"
    response = client.post(
        "/api/ingest", data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_notif_{uuid.uuid4().hex[:6]}.csv", body, "text/csv")})
    assert response.status_code == 200, response.text
    keys = [c[0] for c in calls]
    assert "list_import_done" in keys
    payload = next(p for k, p, _ in calls if k == "list_import_done")
    assert payload["Liste"] == "WATCHLIST_EU" and payload["Fiches"] == 1


def test_snapshot_approval_and_rejection_emit_events(client, monkeypatch):
    assert client.put("/api/settings/ingestion", json={"require_approval": True}).status_code == 200
    body = "entity_id,entity_type,primary_name,nationality\nEU-NOTIF-2,I,Boris Notifov,RU\n"
    pending = client.post(
        "/api/ingest", data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_notif_{uuid.uuid4().hex[:6]}.csv", body, "text/csv")}).json()
    assert pending["status"] == "PENDING_REVIEW"

    calls = _spy_emit(monkeypatch)
    approved = post_and_wait(client, f"/api/review/snapshots/{pending['snapshot_id']}/approve",
                             json={"comment": "test notif"})
    assert approved.status_code == 202, approved.text
    key, payload, _ = next(c for c in calls if c[0] == "snapshot_approved")
    assert payload["Liste"] == "WATCHLIST_EU"
    assert payload["Approuvé par"] == "notif_admin"

    # Rejet d'un second snapshot
    pending2 = client.post(
        "/api/ingest", data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_notif_{uuid.uuid4().hex[:6]}.csv",
                        body.replace("NOTIF-2", "NOTIF-3"), "text/csv")}).json()
    calls.clear()
    rejected = client.post(f"/api/review/snapshots/{pending2['snapshot_id']}/reject",
                           json={"comment": "delta aberrant"})
    assert rejected.status_code == 200, rejected.text
    _key, payload, _kw = next(c for c in calls if c[0] == "snapshot_rejected")
    assert payload["Motif"] == "delta aberrant"


def test_alert_lifecycle_emits_events(client, monkeypatch, db):
    audit = AuditTrail(
        client_id="test_notif_cli", client_name="Jean Notif", client_type="PP",
        watchlist_id="EU-NOTIF-9", watchlist_name="Ivan NOTIFOV",
        base_score=80.0, final_score=91.0, status="ALERT",
        decision_tree={}, config_state={}, watchlist_version="t", watchlist_hash="h" * 64,
    )
    db.add(audit)
    db.flush()
    alert = Alert(audit_id=audit.id, client_id="test_notif_cli", client_name="Jean Notif",
                  watchlist_entity_id="EU-NOTIF-9", watchlist_name="Ivan NOTIFOV",
                  final_score=91.0, status="OPEN", channel="SCREENING", priority="HIGH")
    db.add(alert)
    db.commit()
    alert_id = alert.id

    calls = _spy_emit(monkeypatch)
    assert client.post(f"/api/alerts/{alert_id}/assign", json={}).status_code == 200
    assert client.post(f"/api/alerts/{alert_id}/escalate",
                       json={"comment": "doute sérieux"}).status_code == 200
    keys = [c[0] for c in calls]
    assert "alert_assigned" in keys and "alert_escalated" in keys
    assigned = next(p for k, p, _ in calls if k == "alert_assigned")
    assert assigned["_assignee"] == "notif_admin"   # routage vers l'analyste


def test_overdue_sla_detection_emits_once(client, db, monkeypatch):
    from fiskr.api import _detect_overdue_alerts
    audit = AuditTrail(
        client_id="test_notif_sla", client_name="Retard SLA", client_type="PP",
        watchlist_id="EU-NOTIF-SLA", watchlist_name="Listé SLA",
        base_score=80.0, final_score=88.0, status="ALERT",
        decision_tree={}, config_state={}, watchlist_version="t", watchlist_hash="h" * 64,
    )
    db.add(audit)
    db.flush()
    alert = Alert(audit_id=audit.id, client_id="test_notif_sla", client_name="Retard SLA",
                  watchlist_entity_id="EU-NOTIF-SLA", watchlist_name="Listé SLA",
                  final_score=88.0, status="OPEN", channel="SCREENING", priority="HIGH",
                  assigned_to="notif_admin", due_at=datetime.utcnow() - timedelta(hours=2))
    db.add(alert)
    db.commit()

    assert _detect_overdue_alerts(db) == 1
    assert _detect_overdue_alerts(db) == 0   # jamais deux fois la même alerte
    queued = db.query(NotificationDelivery).filter(
        NotificationDelivery.event_key == "alert_overdue_sla").all()
    assert len(queued) == 1 and queued[0].status == "QUEUED"


def test_expiring_whitelist_reminder(client, db):
    from fiskr.api import _detect_expiring_whitelist
    pair = WhitelistPair(
        client_id="test_notif_wl", watchlist_entity_id="EU-NOTIF-WL",
        client_name="Client WL", watchlist_name="Listé WL",
        justification="homonyme avéré", created_by="notif_admin",
        expires_at=datetime.utcnow() + timedelta(days=3),
    )
    db.add(pair)
    db.commit()
    assert _detect_expiring_whitelist(db) == 1
    assert _detect_expiring_whitelist(db) == 0   # rappel unique par paire


# ------------------ RÉGLAGES, JOURNAL, TEST ------------------

def test_batch_settings_roundtrip_and_cron_validation(client):
    ok = client.put("/api/settings/notifications", json={"enabled": True, "cron": "*/30 * * * *"})
    assert ok.status_code == 200, ok.text
    assert ok.json()["cron"] == "*/30 * * * *"
    bad = client.put("/api/settings/notifications", json={"cron": "pas un cron"})
    assert bad.status_code == 400
    bad_cat = client.put("/api/settings/notifications",
                         json={"extra_recipients": {"categorie_inconnue": ["a@b.fr"]}})
    assert bad_cat.status_code == 400
    bad_mail = client.put("/api/settings/notifications",
                          json={"extra_recipients": {"criblage": ["pas-un-email"]}})
    assert bad_mail.status_code == 400


def test_notification_log_and_flush_endpoints(client, db, monkeypatch):
    notifier.emit(db, "list_import_done", {"Liste": "WATCHLIST_EU"})
    log = client.get("/api/notifications/log")
    assert log.status_code == 200
    data = log.json()
    assert data["queued"] >= 1
    assert any(i["event_key"] == "list_import_done" for i in data["items"])
    assert data["items"][0]["label"]   # libellé résolu depuis le catalogue

    monkeypatch.setattr(notify, "smtp_configured", lambda: False)
    monkeypatch.setattr(notifier.notify, "smtp_configured", lambda: False)
    flushed = client.post("/api/notifications/flush")
    assert flushed.status_code == 200
    assert flushed.json()["events"] >= 1


def test_test_email_requires_smtp(client, monkeypatch):
    monkeypatch.setattr("fiskr.api.notify_smtp_configured", lambda: False)
    response = client.post("/api/settings/notifications/test")
    assert response.status_code == 503
    assert "SMTP" in response.json()["detail"]


def test_test_email_sends_when_configured(client, monkeypatch):
    sent = {}
    monkeypatch.setattr("fiskr.api.notify_smtp_configured", lambda: True)
    monkeypatch.setattr("fiskr.api.notify_default_recipients", lambda: ["ops@banque.test"])
    monkeypatch.setattr("fiskr.api.notify_send_email",
                        lambda recipients, subject, body, html_body=None: sent.update(
                            {"to": recipients, "subject": subject, "html": html_body}))
    response = client.post("/api/settings/notifications/test")
    assert response.status_code == 200, response.text
    assert sent["to"] == ["ops@banque.test"]
    assert "Test de configuration" in sent["subject"]


def test_user_email_roundtrip(client):
    created = client.post("/api/users", json={
        "username": f"notif_u{uuid.uuid4().hex[:6]}", "password": "Motdepasse-1234",
        "full_name": "Analyste Notif", "email": "analyste@banque.test", "role": "user"})
    assert created.status_code == 200, created.text
    user_id = created.json()["user"]["id"]
    assert created.json()["user"]["email"] == "analyste@banque.test"

    bad = client.put(f"/api/users/{user_id}", json={"email": "pas-un-email"})
    assert bad.status_code == 400

    cleared = client.put(f"/api/users/{user_id}", json={"email": ""})
    assert cleared.status_code == 200
    assert cleared.json()["user"]["email"] is None

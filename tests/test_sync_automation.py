"""
Synchronisation automatique pilotable depuis l'application (ADMIN uniquement).

Avant : `sync.auto_enabled` et `sync.<source>.enabled` ne vivaient que dans
config.yaml — couper ou relancer les recuperations planifiees exigeait un
acces au serveur, une edition de fichier et un redemarrage.

Ce que ces tests verrouillent :
- la surcharge a chaud (base) prime sur config.yaml, et get_sync_config(db)
  la rend — c'est la forme que lisent les planificateurs ;
- l'ecriture est reservee a l'ADMIN (403 pour les autres roles) ;
- le planificateur respecte l'interrupteur ET l'activation par source,
  sans redemarrage ;
- un lancement MANUEL reste possible sur une source exclue de l'automatique
  (couper l'automatisme n'ampute pas l'exploitant).
"""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr import api as api_module
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, AppSetting
from fiskr.settings import (
    SETTING_SYNC_AUTO_ENABLED, SETTING_SYNC_SOURCES_ENABLED, SETTING_SYNC_SCHEDULES,
    sync_auto_enabled, sync_sources_enabled, set_setting,
)
from fiskr.sync import get_sync_config


def _override_user(username: str, role: str = "admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_((
            SETTING_SYNC_AUTO_ENABLED, SETTING_SYNC_SOURCES_ENABLED, SETTING_SYNC_SCHEDULES,
        ))).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _cleanup()
    _override_user("admin_sync", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup()


@pytest.fixture
def db_session():
    db = next(get_db())
    yield db
    db.close()


# ------------------ SURCHARGE A CHAUD ------------------

def test_hot_settings_override_config_file(client, db_session):
    """La base prime sur config.yaml, et sans surcharge on retombe dessus."""
    from fiskr.config import config
    file_auto = bool((config.get("sync", {}) or {}).get("auto_enabled", False))
    assert sync_auto_enabled(db_session) is file_auto      # rien en base -> fichier

    set_setting(db_session, SETTING_SYNC_AUTO_ENABLED, not file_auto)
    db_session.commit()
    assert sync_auto_enabled(db_session) is (not file_auto)


def test_get_sync_config_applies_overlay_only_with_a_session(client, db_session):
    """get_sync_config(db) rend l'etat EFFECTIF ; sans session, le fichier seul
    (les appels internes — parametres reseau, URL — ne touchent pas la base)."""
    set_setting(db_session, SETTING_SYNC_AUTO_ENABLED, True)
    set_setting(db_session, SETTING_SYNC_SOURCES_ENABLED, {"ofac": False, "un": True})
    db_session.commit()

    effective = get_sync_config(db_session)
    assert effective["auto_enabled"] is True
    assert effective["ofac"]["enabled"] is False   # surcharge a chaud
    assert effective["un"]["enabled"] is True

    from fiskr.config import config
    file_only = get_sync_config()
    assert file_only["ofac"]["enabled"] is bool((config.get("sync", {}) or {}).get("ofac", {}).get("enabled", True))


def test_sources_without_override_keep_config_value(client, db_session):
    set_setting(db_session, SETTING_SYNC_SOURCES_ENABLED, {"ofac": False})
    db_session.commit()
    effective = sync_sources_enabled(db_session)
    from fiskr.config import config
    sync_cfg = config.get("sync", {}) or {}
    assert effective["ofac"] is False
    assert effective["un"] is bool((sync_cfg.get("un") or {}).get("enabled", False))


# ------------------ ENDPOINT : GARDE ADMIN ------------------

def test_only_admin_can_change_automation():
    """Le coeur de la demande : la synchronisation automatique ne se regle que
    par un ADMIN. Les autres roles — y compris reviewer — sont refuses."""
    _cleanup()
    try:
        for role in ("user", "reviewer,user", "blocking", "auditor"):
            _override_user(f"user_{role.split(',')[0]}", role)
            with TestClient(app) as c:
                r = c.put("/api/settings/sync", json={"auto_enabled": True})
            assert r.status_code == 403, f"role {role} ne doit pas pouvoir régler l'automatique"
        # Aucune de ces tentatives n'a rien ecrit
        db = next(get_db())
        try:
            assert db.query(AppSetting).filter(
                AppSetting.key == SETTING_SYNC_AUTO_ENABLED).first() is None
        finally:
            db.close()
    finally:
        app.dependency_overrides.clear()
        _cleanup()


def test_admin_updates_switch_sources_and_cron_in_one_call(client, db_session):
    r = client.put("/api/settings/sync", json={
        "auto_enabled": True,
        "sources_enabled": {"ofac": False, "un": True},
        "schedules": {"un": "0 7 * * 1-5"},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["auto_enabled"] is True
    assert body["sources_enabled"]["ofac"] is False
    assert body["sources_enabled"]["un"] is True
    assert body["schedules"]["un"] == "0 7 * * 1-5"
    # Effet immediat cote moteur, sans redemarrage
    assert get_sync_config(db_session)["ofac"]["enabled"] is False


def test_partial_update_leaves_the_rest_untouched(client):
    client.put("/api/settings/sync", json={"auto_enabled": True,
                                           "sources_enabled": {"ofac": False}})
    # Une mise a jour du seul cron ne doit pas reactiver la source coupee
    r = client.put("/api/settings/sync", json={"schedules": {"ofac": "0 5 * * *"}})
    assert r.status_code == 200
    assert r.json()["sources_enabled"]["ofac"] is False
    assert r.json()["auto_enabled"] is True


def test_validation_rejects_unknown_source_and_empty_payload(client):
    assert client.put("/api/settings/sync", json={}).status_code == 400
    bad = client.put("/api/settings/sync", json={"sources_enabled": {"pas_une_source": True}})
    assert bad.status_code == 400
    assert "pas_une_source" in bad.json()["detail"]
    bad_cron = client.put("/api/settings/sync", json={"schedules": {"ofac": "pas un cron"}})
    assert bad_cron.status_code == 400


def test_config_endpoint_exposes_effective_state_and_origin(client):
    client.put("/api/settings/sync", json={"auto_enabled": True,
                                           "sources_enabled": {"ofac": False}})
    cfg = client.get("/api/sync/config").json()
    assert cfg["auto_enabled"] is True
    assert cfg["ofac"]["enabled"] is False
    # Provenance : ce qui est surcharge dans l'application vs herite du fichier
    assert cfg["automation_sources"]["auto_enabled"] == "database"
    assert cfg["automation_sources"]["ofac"] == "database"
    assert cfg["automation_sources"]["un"] == "config"


# ------------------ EFFET SUR LE PLANIFICATEUR ------------------

def test_scheduler_tick_respects_the_hot_switch(client, db_session, monkeypatch):
    """L'interrupteur coupe le planificateur SANS redemarrage : aucun job
    n'est soumis tant qu'il est a l'arret."""
    submitted = []
    monkeypatch.setattr(api_module.job_queue, "submit",
                        lambda kind, **kw: submitted.append(kw.get("token")))
    # Cron qui matche a coup sur, sur une source activee a chaud
    client.put("/api/settings/sync", json={
        "auto_enabled": False,
        "sources_enabled": {"ofac": True},
        "schedules": {"ofac": "* * * * *"},
    })
    api_module._cron_sync_tick(datetime.now())
    assert submitted == [], "interrupteur coupé : aucune synchronisation ne doit partir"

    client.put("/api/settings/sync", json={"auto_enabled": True})
    api_module._cron_sync_tick(datetime.now())
    assert "sync:ofac" in submitted, "interrupteur activé : la source planifiée doit partir"


def test_scheduler_skips_sources_disabled_in_the_app(client, monkeypatch):
    submitted = []
    monkeypatch.setattr(api_module.job_queue, "submit",
                        lambda kind, **kw: submitted.append(kw.get("token")))
    client.put("/api/settings/sync", json={
        "auto_enabled": True,
        "sources_enabled": {"ofac": False},
        "schedules": {"ofac": "* * * * *"},
    })
    api_module._cron_sync_tick(datetime.now())
    assert "sync:ofac" not in submitted


def test_manual_run_still_allowed_on_a_source_excluded_from_automation(client, monkeypatch):
    """Couper l'automatique n'ampute pas l'exploitant : le lancement manuel
    d'une source exclue reste accepte (acte explicite, trace)."""
    monkeypatch.setattr(api_module, "_submit_job", lambda *a, **k: None)
    client.put("/api/settings/sync", json={"auto_enabled": False,
                                           "sources_enabled": {"ofac": False}})
    r = client.post("/api/sync/run", json={"source": "OFAC"})
    assert r.status_code in (200, 202), r.text

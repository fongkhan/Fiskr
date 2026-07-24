"""
Reglages applicatifs modifiables a chaud (stockes en base, repli sur config.yaml).

La ligne AppSetting en base gagne toujours sur la valeur de config.yaml, qui ne
sert que de valeur par defaut tant qu'aucun admin n'a modifie le reglage.
"""
import logging
from typing import Any, Dict, Optional

from fiskr.config import config
from fiskr.database import AppSetting

logger = logging.getLogger("fiskr.settings")

# Mode homologation : tout snapshot watchlist entrant attend une validation humaine
SETTING_REQUIRE_APPROVAL = "ingestion.require_approval"
# Exigences modulaires lors de l'exclusion d'entites pendant la revue
SETTING_EXCLUSION_JUSTIFICATION_REQUIRED = "review.exclusion_justification_required"
SETTING_EXCLUSION_FILE_REQUIRED = "review.exclusion_file_required"
# Validation 4-yeux des decisions d'alertes (validateur different du proposeur)
SETTING_ALERT_FOUR_EYES = "review.alert_four_eyes_required"
# Exigences modulaires lors d'une mise en liste blanche client x liste
SETTING_WHITELIST_JUSTIFICATION_REQUIRED = "review.whitelist_justification_required"
SETTING_WHITELIST_FILE_REQUIRED = "review.whitelist_file_required"
# Re-criblage automatique du referentiel clients apres chaque mise a jour de liste
SETTING_AUTO_RESCREEN = "ingestion.auto_rescreen"
# Cahier de tests (backtest) avant promotion : seuil d'ecart tolere du taux
# d'interception (%) et exigence d'un backtest au verdict OK pour approuver
SETTING_BACKTEST_MAX_GAP_PCT = "review.backtest_max_gap_pct"
SETTING_BACKTEST_REQUIRED = "review.backtest_required"
# Blocking keys par canal : layouts ordonnes de composantes de cle
SETTING_BLOCKING_SCREENING = "blocking.screening_layout"
SETTING_BLOCKING_FILTERING = "blocking.filtering_layout"
# Planification cron par source de synchronisation (source -> expression 5 champs)
SETTING_SYNC_SCHEDULES = "sync.schedules"
SYNC_SOURCES = ("ofac", "eurlex", "dgt", "eu_fsf", "un", "pep", "ofsi")
# SLA de traitement des alertes : delai (heures) par priorite, 0 = pas d'echeance
SETTING_ALERT_SLA_HOURS = "alerts.sla_hours"
# Notifications metier : activation par evenement
SETTING_NOTIFICATIONS = "notifications.events"
# Digest KPI periodique (synthese conformite envoyee par email/webhooks)
SETTING_DIGEST = "notifications.digest"
# Retention des donnees : duree de conservation (jours) par famille, 0 = illimite.
# Le journal des actions d'administration n'est JAMAIS purge (append-only).
SETTING_RETENTION = "retention.policy"
# Seuils de score du criblage : seuil global + surcharges par type de liste,
# modifiables a chaud (prioritaires sur config.yaml scoring.*)
SETTING_SCORE_THRESHOLDS = "scoring.thresholds"

DEFAULT_ALERT_SLA_HOURS = {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 120, "LOW": 240}
DEFAULT_DIGEST = {"enabled": False, "cron": "0 8 * * 1-5"}
RETENTION_FAMILIES = ("audit_trail", "closed_alerts", "sync_reports", "batch_campaigns")
RETENTION_MIN_DAYS = 30  # garde-fou : jamais moins de 30 jours quand une purge est activee
DEFAULT_RETENTION = {"audit_trail": 0, "closed_alerts": 0, "sync_reports": 0,
                     "batch_campaigns": 0, "cron": "30 2 * * *", "archive": True}
DEFAULT_NOTIFICATION_EVENTS = {
    "alert_created": False,
    "alert_pending_validation": False,
    "snapshot_pending_review": False,
    "sync_error": True,
}

BLOCKING_COMPONENTS = ("COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST")
DEFAULT_FILTERING_LAYOUT = ["PHONETIC_FIRST"]


def _config_default(key: str, default: Any = None) -> Any:
    """Resout la valeur par defaut d'un reglage depuis config.yaml (cle pointee 'section.champ')."""
    section, _, field = key.partition(".")
    return config.get(section, {}).get(field, default)


def get_setting(db, key: str, default: Any = None) -> Any:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row is not None:
        return row.value
    return default


def get_setting_with_source(db, key: str, default: Any = None) -> Dict[str, Any]:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row is not None:
        return {"value": row.value, "source": "database"}
    return {"value": _config_default(key, default), "source": "config"}


def set_setting(db, key: str, value: Any, updated_by: Optional[str] = None) -> AppSetting:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row is None:
        row = AppSetting(key=key, value=value, updated_by=updated_by)
        db.add(row)
    else:
        row.value = value
        row.updated_by = updated_by
    db.commit()
    return row


def require_approval_enabled(db) -> bool:
    """True si le mode homologation est actif (base d'abord, sinon config.yaml)."""
    return bool(get_setting_with_source(db, SETTING_REQUIRE_APPROVAL, False)["value"])


def alert_four_eyes_required(db) -> bool:
    """True si la decision d'alerte exige un second regard (defaut : oui)."""
    return bool(get_setting_with_source(db, SETTING_ALERT_FOUR_EYES, True)["value"])


def whitelist_requirements(db) -> Dict[str, bool]:
    """Exigences modulaires de justification lors d'une mise en liste blanche."""
    return {
        "justification_required": bool(
            get_setting_with_source(db, SETTING_WHITELIST_JUSTIFICATION_REQUIRED, True)["value"]
        ),
        "file_required": bool(
            get_setting_with_source(db, SETTING_WHITELIST_FILE_REQUIRED, False)["value"]
        ),
    }


def auto_rescreen_enabled(db) -> bool:
    """True si le re-criblage automatique post-delta est actif (defaut : oui)."""
    return bool(get_setting_with_source(db, SETTING_AUTO_RESCREEN, True)["value"])


def backtest_max_gap_pct(db) -> float:
    """Seuil d'ecart tolere (%) entre taux d'interception actuel et candidat (defaut : 20)."""
    try:
        return float(get_setting_with_source(db, SETTING_BACKTEST_MAX_GAP_PCT, 20.0)["value"])
    except (TypeError, ValueError):
        return 20.0


def backtest_required(db) -> bool:
    """True si un cahier de tests au verdict OK est exige avant toute promotion (defaut : non)."""
    return bool(get_setting_with_source(db, SETTING_BACKTEST_REQUIRED, False)["value"])


def _valid_layout(value) -> bool:
    return (
        isinstance(value, list) and len(value) > 0
        and all(isinstance(c, str) and c in BLOCKING_COMPONENTS for c in value)
        and len(set(value)) == len(value)
    )


def blocking_layout_with_source(db, channel: str) -> Dict[str, Any]:
    """
    Layout de blocking effectif d'un canal (SCREENING = criblage clients,
    FILTERING = filtrage transactionnel) : base d'abord, sinon defaut du canal.
    Defauts = comportement historique : criblage -> layout de config.yaml ;
    filtrage -> phonetique seule (les donnees de paiement sont trop pauvres
    pour filtrer sur le pays ou le type).
    """
    if channel == "FILTERING":
        key, default = SETTING_BLOCKING_FILTERING, list(DEFAULT_FILTERING_LAYOUT)
    else:
        key = SETTING_BLOCKING_SCREENING
        default = list((config.get("blocking", {}) or {}).get(
            "custom_key_layout", ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
        ))
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row is not None and _valid_layout(row.value):
        return {"layout": list(row.value), "source": "database"}
    return {"layout": default, "source": "config"}


def blocking_layout(db, channel: str) -> list:
    return blocking_layout_with_source(db, channel)["layout"]


def blocking_config_for(layout: list) -> Dict[str, Any]:
    """Copie de la config globale avec le layout de blocking injecte."""
    cfg = dict(config)
    blocking_cfg = dict(config.get("blocking", {}) or {})
    blocking_cfg["custom_key_layout"] = list(layout)
    cfg["blocking"] = blocking_cfg
    return cfg


def sync_schedules(db) -> Dict[str, str]:
    """
    Expression cron effective par source de synchronisation :
    reglage a chaud (base) > config.yaml (sync.<source>.schedule) > repli sur
    l'horaire quotidien global (sync.schedule_time -> « M H * * * »).
    """
    sync_cfg = config.get("sync", {}) or {}
    try:
        hour, minute = (int(p) for p in str(sync_cfg.get("schedule_time", "06:00")).split(":"))
    except (TypeError, ValueError):
        hour, minute = 6, 0
    default_cron = f"{minute} {hour} * * *"
    overrides = get_setting(db, SETTING_SYNC_SCHEDULES, {}) or {}
    out: Dict[str, str] = {}
    for source in SYNC_SOURCES:
        expr = ""
        if isinstance(overrides, dict):
            expr = str(overrides.get(source) or "").strip()
        if not expr:
            expr = str((sync_cfg.get(source) or {}).get("schedule") or "").strip()
        out[source] = expr or default_cron
    return out


def alert_sla_hours(db) -> Dict[str, int]:
    """Delais SLA (heures) par priorite d'alerte ; 0 ou absent = pas d'echeance."""
    value = get_setting_with_source(db, SETTING_ALERT_SLA_HOURS, dict(DEFAULT_ALERT_SLA_HOURS))["value"]
    out = dict(DEFAULT_ALERT_SLA_HOURS)
    if isinstance(value, dict):
        for prio, hours in value.items():
            try:
                out[str(prio).upper()] = max(0, int(hours))
            except (TypeError, ValueError):
                continue
    return out


def notification_events(db) -> Dict[str, bool]:
    """Evenements metier declenchant une notification (email/webhook)."""
    value = get_setting_with_source(db, SETTING_NOTIFICATIONS, dict(DEFAULT_NOTIFICATION_EVENTS))["value"]
    out = dict(DEFAULT_NOTIFICATION_EVENTS)
    if isinstance(value, dict):
        for event, enabled in value.items():
            if event in out:
                out[event] = bool(enabled)
    return out


def digest_settings(db) -> Dict[str, Any]:
    """Reglage du digest KPI periodique : activation + expression cron 5 champs
    (defaut : 8h00 en semaine)."""
    value = get_setting_with_source(db, SETTING_DIGEST, dict(DEFAULT_DIGEST))["value"]
    out = dict(DEFAULT_DIGEST)
    if isinstance(value, dict):
        out["enabled"] = bool(value.get("enabled", out["enabled"]))
        cron_expr = str(value.get("cron") or "").strip()
        if cron_expr:
            out["cron"] = cron_expr
    return out


def score_thresholds(db) -> Dict[str, Any]:
    """
    Seuils de cut-off effectifs : reglage a chaud (base) prioritaire sur
    config.yaml (scoring.cut_off_threshold / cut_off_overrides).
    """
    scoring_cfg = config.get("scoring", {}) or {}
    out = {
        "cut_off_threshold": float(scoring_cfg.get("cut_off_threshold", 75.0)),
        "cut_off_overrides": {
            str(k): float(v) for k, v in (scoring_cfg.get("cut_off_overrides") or {}).items()
            if isinstance(v, (int, float))
        },
        "source": "config",
    }
    value = get_setting(db, SETTING_SCORE_THRESHOLDS, None)
    if isinstance(value, dict):
        out["source"] = "database"
        try:
            out["cut_off_threshold"] = float(value.get("cut_off_threshold", out["cut_off_threshold"]))
        except (TypeError, ValueError):
            pass
        overrides = value.get("cut_off_overrides")
        if isinstance(overrides, dict):
            cleaned = {}
            for list_type, threshold in overrides.items():
                try:
                    cleaned[str(list_type).upper()] = float(threshold)
                except (TypeError, ValueError):
                    continue
            out["cut_off_overrides"] = cleaned
    return out


def scoring_config_with_thresholds(db) -> Dict[str, Any]:
    """Copie de la config globale avec les seuils a chaud injectes — a passer
    au moteur de scoring pour que le reglage prenne effet sans redemarrage."""
    thresholds = score_thresholds(db)
    cfg = dict(config)
    scoring_cfg = dict(config.get("scoring", {}) or {})
    scoring_cfg["cut_off_threshold"] = thresholds["cut_off_threshold"]
    scoring_cfg["cut_off_overrides"] = dict(thresholds["cut_off_overrides"])
    cfg["scoring"] = scoring_cfg
    return cfg


def retention_policy(db) -> Dict[str, Any]:
    """Politique de retention effective : jours par famille (0 = conservation
    illimitee) + expression cron de la purge quotidienne."""
    value = get_setting_with_source(db, SETTING_RETENTION, dict(DEFAULT_RETENTION))["value"]
    out = dict(DEFAULT_RETENTION)
    if isinstance(value, dict):
        for family in RETENTION_FAMILIES:
            try:
                out[family] = max(0, int(value.get(family, out[family])))
            except (TypeError, ValueError):
                continue
        cron_expr = str(value.get("cron") or "").strip()
        if cron_expr:
            out["cron"] = cron_expr
        if "archive" in value:
            out["archive"] = bool(value.get("archive"))
    return out


def exclusion_requirements(db) -> Dict[str, bool]:
    """Exigences modulaires de justification lors d'une exclusion d'entite."""
    return {
        "justification_required": bool(
            get_setting_with_source(db, SETTING_EXCLUSION_JUSTIFICATION_REQUIRED, True)["value"]
        ),
        "file_required": bool(
            get_setting_with_source(db, SETTING_EXCLUSION_FILE_REQUIRED, False)["value"]
        ),
    }

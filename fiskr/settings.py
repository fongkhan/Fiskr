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

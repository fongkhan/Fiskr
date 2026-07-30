"""
Reglages applicatifs modifiables a chaud (stockes en base, repli sur config.yaml).

La ligne AppSetting en base gagne toujours sur la valeur de config.yaml, qui ne
sert que de valeur par defaut tant qu'aucun admin n'a modifie le reglage.
"""
import logging
from typing import Any, Dict, Optional

from fiskr.config import config
from fiskr.database import AppSetting
from fiskr.events import EVENT_CATALOG

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
from fiskr.sources import OPENSANCTIONS_BY_KEY as _OS_BY_KEY

SYNC_SOURCES = ("ofac", "ofac_nonsdn", "eurlex", "dgt", "eu_fsf", "un", "pep", "ofsi", "seco", "csl", "canada", "dfat",
                "hk_sfc", "amf", "worldbank") + tuple(_OS_BY_KEY)
# Capacites du moteur de rapprochement, activables PAR CANAL. Le catalogue des
# capacites vit dans fiskr/capabilities.py (source de verite unique, lue aussi
# par le moteur, l'API et l'ecran) ; ici on ne stocke que l'activation :
#   {"SCREENING": {"translit": true, ...}, "FILTERING": {...}}
SETTING_ENGINE_CAPABILITIES = "engine.capabilities"
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
# Ressources linguistiques : activation par type de champ. Une table
# d'equivalences augmente le rappel AU PRIX de la precision — chaque classe
# cree des matches qui n'existaient pas. Le defaut est donc DESACTIVE partout :
# une installation existante ne change pas de comportement, et l'activation
# passe par le cahier de tests qui chiffre l'ecart avant mise en production.
SETTING_RESOURCE_FIELDS = "resources.enabled_fields"
# Prenoms et noms de famille sont ACTIFS par defaut, les trois autres types
# non. Ce n'est pas un choix de confort : il est mesure. Sur un panel de 716
# clients crible contre 124 fiches designees (cf. Documentation/
# MESURE_RESSOURCES.md), les activer rattrape 6 vrais positifs, n'ajoute
# AUCUNE alerte sur les 600 clients ordinaires du panel et n'en fait perdre
# aucune ; 18 paires deja detectees voient leur score monter, aucune baisser.
#
# Les types city / country / state restent inactifs : ils n'ont pas ete
# mesures sur un panel reel, et le principe reste qu'un changement de
# parametrage de criblage se chiffre avant de s'appliquer (Ressources >
# Mesurer l'impact).
DEFAULT_RESOURCE_FIELDS: Dict[str, bool] = {
    "given_name": True,
    "surname": True,
    "city": False,
    "country": False,
    "state": False,
}

# Fouille quotidienne d'homonymes dans le graphe d'alias des listes et les
# alertes confirmees. 3h15 du matin : apres les synchronisations nocturnes,
# donc sur des listes fraiches, et hors des heures de criblage.
SETTING_MINING = "resources.mining"
DEFAULT_MINING: Dict[str, Any] = {
    "enabled": True,
    "cron": "15 3 * * *",
    "min_occurrences": 2,
    "min_similarity": 0.75,
    "auto_approve_confidence": 0.85,
    "sources": ["ALIAS", "ANALYST"],
}
# Checklist d'instruction des alertes (dossier d'investigation)
SETTING_CHECKLIST = "investigation.checklist"
DEFAULT_CHECKLIST = [
    "Identité vérifiée (nom, date de naissance, pays)",
    "Identifiants croisés (passeport, LEI, BIC, crypto...)",
    "Relations et détentions examinées (règle des 50 %)",
    "Adverse media consulté",
    "Historique du client et alertes antérieures revus",
    "Décision documentée et justifiée",
]

DEFAULT_ALERT_SLA_HOURS = {"CRITICAL": 24, "HIGH": 72, "MEDIUM": 120, "LOW": 240}
DEFAULT_DIGEST = {"enabled": False, "cron": "0 8 * * 1-5"}
RETENTION_FAMILIES = ("audit_trail", "closed_alerts", "sync_reports", "batch_campaigns")
RETENTION_MIN_DAYS = 30  # garde-fou : jamais moins de 30 jours quand une purge est activee
DEFAULT_RETENTION = {"audit_trail": 0, "closed_alerts": 0, "sync_reports": 0,
                     "batch_campaigns": 0, "cron": "30 2 * * *", "archive": True}
# Activation par defaut de CHAQUE etape notifiable, derivee du catalogue
# (fiskr/events.py) : declarer une nouvelle etape la-bas suffit a la rendre
# activable ici, traduisible dans les mails et affichable dans les reglages.
DEFAULT_NOTIFICATION_EVENTS = {
    key: event.default_enabled for key, event in EVENT_CATALOG.items()
}

# Recapitulatif periodique des etapes a fort volume (urgence DIGEST) :
# un seul mail par destinataire et par periode, jamais d'inondation.
SETTING_NOTIFICATION_BATCH = "notifications.batch"
DEFAULT_NOTIFICATION_BATCH = {"enabled": True, "cron": "0 * * * *", "extra_recipients": {}}
# Marqueur interne : paires de liste blanche dont l'echeance de revue a deja
# ete signalee (evite de rappeler la meme paire a chaque passage de la boucle)
SETTING_WHITELIST_EXPIRY_NOTIFIED = "notifications.whitelist_expiry_notified"
# Qualite des donnees clients : score global minimal attendu (%, 0 = controle
# desactive) et cache du dernier calcul post-import (evite tout scan complet
# du referentiel dans le digest ou le tableau de bord)
SETTING_QUALITY_MIN_SCORE = "quality.min_score_pct"
SETTING_QUALITY_LAST = "quality.last_report"

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


# ------------------ EPOQUE DU CACHE DE PRODUCTION ------------------

# Compteur incremente a CHAQUE changement des listes en production (approbation,
# synchronisation appliquee, import direct, exclusion, equivalence decidee...).
# C'est le signal inter-processus : le demon travailleur ne peut pas recharger
# le cache memoire d'un processus API ; il bump l'epoque, et chaque processus
# API la surveille (verification throttlee) pour recharger SON cache.
SETTING_WATCHLIST_EPOCH = "watchlist.epoch"


def watchlist_epoch(db) -> int:
    try:
        return int(get_setting(db, SETTING_WATCHLIST_EPOCH, 0) or 0)
    except (TypeError, ValueError):
        return 0


def bump_watchlist_epoch(db) -> int:
    epoch = watchlist_epoch(db) + 1
    set_setting(db, SETTING_WATCHLIST_EPOCH, epoch)
    return epoch


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


def blocking_config_for(layout: list, channel: str = "SCREENING") -> Dict[str, Any]:
    """Copie de la config globale avec le layout de blocking injecte.

    Le CANAL voyage avec la config : les capacites du moteur se reglent par
    canal, et `generate_blocking_keys` doit savoir sous quel canal il travaille
    sans qu'on ait a changer sa signature ni celle de ses six appelants.
    """
    cfg = dict(config)
    blocking_cfg = dict(config.get("blocking", {}) or {})
    blocking_cfg["custom_key_layout"] = list(layout)
    blocking_cfg["channel"] = channel
    cfg["blocking"] = blocking_cfg
    cfg["engine_channel"] = channel
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


def quality_min_score_pct(db) -> float:
    """Score global minimal attendu du referentiel clients (%, 0 = controle
    desactive : aucune alerte de qualite n'est emise)."""
    value = get_setting_with_source(db, SETTING_QUALITY_MIN_SCORE, 0.0)["value"]
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def notification_batch_settings(db) -> Dict[str, Any]:
    """
    Reglage du recapitulatif des evenements groupes : activation, expression
    cron (defaut horaire) et adresses supplementaires par categorie (boite du
    service conformite en copie, en plus du routage par role).
    """
    value = get_setting_with_source(db, SETTING_NOTIFICATION_BATCH,
                                    dict(DEFAULT_NOTIFICATION_BATCH))["value"]
    out = {"enabled": DEFAULT_NOTIFICATION_BATCH["enabled"],
           "cron": DEFAULT_NOTIFICATION_BATCH["cron"],
           "extra_recipients": {}}
    if isinstance(value, dict):
        out["enabled"] = bool(value.get("enabled", out["enabled"]))
        cron_expr = str(value.get("cron") or "").strip()
        if cron_expr:
            out["cron"] = cron_expr
        extras = value.get("extra_recipients")
        if isinstance(extras, dict):
            for category, addresses in extras.items():
                if isinstance(addresses, list):
                    clean = [str(a).strip() for a in addresses if str(a).strip()]
                    if clean:
                        out["extra_recipients"][str(category)] = clean
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


def scoring_config_with_thresholds(db, channel: str = "SCREENING") -> Dict[str, Any]:
    """Copie de la config globale avec les seuils a chaud injectes — a passer
    au moteur de scoring pour que le reglage prenne effet sans redemarrage.

    `engine_channel` voyage avec la config pour la meme raison que dans
    `blocking_config_for` : les capacites du moteur se reglent par canal, et
    `match_entities` doit savoir lequel s'applique sans changement de
    signature. Absent = criblage, qui est le comportement historique.
    """
    thresholds = score_thresholds(db)
    cfg = dict(config)
    scoring_cfg = dict(config.get("scoring", {}) or {})
    scoring_cfg["cut_off_threshold"] = thresholds["cut_off_threshold"]
    scoring_cfg["cut_off_overrides"] = dict(thresholds["cut_off_overrides"])
    cfg["scoring"] = scoring_cfg
    cfg["engine_channel"] = channel
    return cfg


def investigation_checklist(db) -> list:
    """Points de controle de l'instruction d'une alerte (dossier), a chaud."""
    value = get_setting(db, SETTING_CHECKLIST, None)
    if isinstance(value, list) and value and all(isinstance(i, str) and i.strip() for i in value):
        return [i.strip() for i in value]
    return list(DEFAULT_CHECKLIST)


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


def engine_capabilities(db, channel: str) -> Dict[str, bool]:
    """
    Capacites du moteur actives sur un canal (criblage ou filtrage).

    Fusionne TOUJOURS sur les defauts du catalogue : une cle inconnue en base
    est ignoree, et une capacite nouvellement declaree apparait avec son
    defaut. Ajouter une entree au catalogue n'invalide donc aucune
    installation existante.

    Sans session, on en ouvre une : le moteur appelle ce chemin depuis des
    contextes varies (API, batch, re-criblage). Jamais bloquant — au pire on
    retombe sur les defauts, c'est-a-dire sur le moteur au complet.
    """
    from fiskr.capabilities import defaults_for_channel

    out = defaults_for_channel(channel)
    try:
        session, owned = db, False
        if session is None:
            from fiskr.database import SessionLocal
            if SessionLocal is None:
                return out
            session, owned = SessionLocal(), True
        try:
            stored = get_setting(session, SETTING_ENGINE_CAPABILITIES, None)
        finally:
            if owned:
                session.close()
    except Exception as e:  # jamais bloquant : au pire, le moteur au complet
        logger.debug(f"Reglage des capacites indisponible : {e}")
        return out

    per_channel = (stored or {}).get(channel) if isinstance(stored, dict) else None
    if isinstance(per_channel, dict):
        for cap_id in out:
            if cap_id in per_channel:
                out[cap_id] = bool(per_channel[cap_id])
    return out


def resource_fields(db) -> Dict[str, bool]:
    """
    Types de champ pour lesquels les equivalences linguistiques s'appliquent.

    Prenoms et noms de famille sont actifs par defaut — mesure a l'appui, cf.
    DEFAULT_RESOURCE_FIELDS et Documentation/MESURE_RESSOURCES.md. Les trois
    autres types sont inactifs : une table d'equivalences change le perimetre
    des alertes, elle doit etre activee sciemment et mesuree au cahier de
    tests avant mise en production.
    """
    out = dict(DEFAULT_RESOURCE_FIELDS)
    value = get_setting(db, SETTING_RESOURCE_FIELDS, None)
    if isinstance(value, dict):
        for field in DEFAULT_RESOURCE_FIELDS:
            if field in value:
                out[field] = bool(value[field])
    return out


def resources_active(db) -> bool:
    """Vrai si au moins un type de champ est active."""
    return any(resource_fields(db).values())


def mining_settings(db) -> Dict[str, Any]:
    """
    Reglage de la fouille quotidienne d'homonymes.

    `auto_approve_confidence` a 0 = aucune application automatique, la fouille
    se contente de proposer. Le defaut applique les decouvertes tres sures
    (0.85) : elles proviennent des alias declares par les sources officielles
    elles-memes, et n'atteignent le criblage que si le type de champ
    correspondant est par ailleurs active — ce qui n'est jamais le cas par
    defaut. Une installation qui exige une mesure avant chaque elargissement
    du perimetre met ce seuil a 0.
    """
    out = dict(DEFAULT_MINING)
    value = get_setting_with_source(db, SETTING_MINING, None)["value"]
    if isinstance(value, dict):
        if "enabled" in value:
            out["enabled"] = bool(value["enabled"])
        cron_expr = str(value.get("cron") or "").strip()
        if cron_expr:
            out["cron"] = cron_expr
        for key, caster, low, high in (
            ("min_occurrences", int, 1, 1000),
            ("min_similarity", float, 0.0, 1.0),
            ("auto_approve_confidence", float, 0.0, 1.0),
        ):
            if key in value:
                try:
                    out[key] = max(low, min(high, caster(value[key])))
                except (TypeError, ValueError):
                    pass
        if isinstance(value.get("sources"), list):
            allowed = [s for s in value["sources"] if s in DEFAULT_MINING["sources"]]
            if allowed:
                out["sources"] = allowed
    return out

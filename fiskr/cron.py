"""
Mini-moteur cron sans dépendance externe : expressions 5 champs
(minute heure jour-du-mois mois jour-de-semaine), évaluées en heure locale.

Syntaxe supportée par champ : `*`, valeurs (`5`), listes (`1,15`),
plages (`8-18`), pas (`*/15`, `8-18/2`). Jour de semaine : 0-7
(0 et 7 = dimanche, convention cron classique). Comme cron : si jour-du-mois
ET jour-de-semaine sont tous deux restreints, l'un OU l'autre suffit.
"""
from datetime import datetime, timedelta
from typing import List, Optional, Set, Tuple

_FIELD_BOUNDS = (
    ("minute", 0, 59),
    ("heure", 0, 23),
    ("jour du mois", 1, 31),
    ("mois", 1, 12),
    ("jour de semaine", 0, 7),
)


class CronError(ValueError):
    """Expression cron invalide (message destiné à l'utilisateur)."""


def _parse_field(raw: str, label: str, low: int, high: int) -> Set[int]:
    values: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"Champ {label} : élément vide.")
        step = 1
        if "/" in part:
            part, step_raw = part.split("/", 1)
            try:
                step = int(step_raw)
            except ValueError:
                raise CronError(f"Champ {label} : pas invalide « {step_raw} ».")
            if step < 1:
                raise CronError(f"Champ {label} : le pas doit être ≥ 1.")
        if part == "*":
            start, end = low, high
        elif "-" in part:
            try:
                start_raw, end_raw = part.split("-", 1)
                start, end = int(start_raw), int(end_raw)
            except ValueError:
                raise CronError(f"Champ {label} : plage invalide « {part} ».")
        else:
            try:
                start = end = int(part)
            except ValueError:
                raise CronError(f"Champ {label} : valeur invalide « {part} ».")
        if start > end:
            raise CronError(f"Champ {label} : plage inversée « {part} ».")
        if start < low or end > high:
            raise CronError(f"Champ {label} : hors bornes [{low}-{high}].")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expr: str) -> Tuple[Set[int], Set[int], Set[int], Set[int], Set[int], bool, bool]:
    """
    Valide et compile une expression cron 5 champs. Retourne les jeux de
    valeurs (minutes, heures, jours, mois, jours-de-semaine) plus deux
    drapeaux indiquant si jour-du-mois / jour-de-semaine sont restreints.
    """
    fields = (expr or "").split()
    if len(fields) != 5:
        raise CronError("Une expression cron comporte 5 champs : minute heure jour mois jour-de-semaine.")
    parsed: List[Set[int]] = []
    for raw, (label, low, high) in zip(fields, _FIELD_BOUNDS):
        parsed.append(_parse_field(raw, label, low, high))
    minutes, hours, doms, months, dows = parsed
    # 7 = dimanche = 0 (convention cron)
    if 7 in dows:
        dows.discard(7)
        dows.add(0)
    dom_restricted = fields[2] != "*"
    dow_restricted = fields[4] != "*"
    return minutes, hours, doms, months, dows, dom_restricted, dow_restricted


def cron_matches(expr: str, moment: datetime) -> bool:
    """True si l'expression matche la minute donnée."""
    minutes, hours, doms, months, dows, dom_r, dow_r = parse_cron(expr)
    if moment.minute not in minutes or moment.hour not in hours or moment.month not in months:
        return False
    # cron : dimanche = 0 ; datetime.weekday() : lundi = 0
    cron_dow = (moment.weekday() + 1) % 7
    dom_ok = moment.day in doms
    dow_ok = cron_dow in dows
    if dom_r and dow_r:
        return dom_ok or dow_ok  # comme cron : l'un OU l'autre
    return dom_ok and dow_ok


def next_run(expr: str, after: Optional[datetime] = None, horizon_days: int = 400) -> Optional[datetime]:
    """
    Prochaine occurrence STRICTEMENT après `after` (défaut : maintenant),
    ou None si aucune dans l'horizon. Parcourt jours puis heures puis minutes
    (rapide même pour les motifs rares).
    """
    minutes, hours, doms, months, dows, dom_r, dow_r = parse_cron(expr)
    start = (after or datetime.now()).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = start.replace(hour=0, minute=0)
    for _ in range(horizon_days):
        if day.month in months:
            cron_dow = (day.weekday() + 1) % 7
            dom_ok = day.day in doms
            dow_ok = cron_dow in dows
            day_matches = (dom_ok or dow_ok) if (dom_r and dow_r) else (dom_ok and dow_ok)
            if day_matches:
                for hour in sorted(hours):
                    for minute in sorted(minutes):
                        candidate = day.replace(hour=hour, minute=minute)
                        if candidate >= start:
                            return candidate
        day += timedelta(days=1)
    return None

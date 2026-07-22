"""
Regles Python anti-faux positifs (canaux SCREENING = criblage clients,
FILTERING = filtrage transactionnel).

Contrat : le code d'une regle doit definir `def rule(ctx) -> bool`.
True = supprimer l'alerte candidate. La suppression n'est JAMAIS silencieuse :
l'alerte est creee puis auto-cloturee CLOSED_BY_RULE et la decision est tracee
dans le journal d'audit immuable (fp_rule_applied dans le decision_tree) —
exigence ACPR/FED.

Securite/gouvernance : le code est du Python volontairement complet (choix
produit : pas de DSL, pas de zone d'ombre). Ce n'est PAS un bac a sable —
l'acces est reserve au role `rules` (ou admin), chaque modification est
journalisee de facon immuable (fp_rule_changes), une regle ne s'applique en
production qu'apres tests unitaires verts et validation 4-yeux.

Fail-open conformite : une regle qui leve une exception en production est
ignoree (l'alerte est CONSERVEE) et l'erreur loggee.
"""
import logging
import math
import re
import unicodedata
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fiskr.database import FpRule

logger = logging.getLogger("fiskr.fprules")

FP_RULE_CHANNELS = ("SCREENING", "FILTERING")

# Espace d'execution des regles : builtins utiles + modules standards surs.
# Volontairement du Python complet (role-gated), pas un bac a sable.
_RULE_BUILTINS = {
    "abs": abs, "all": all, "any": any, "bool": bool, "dict": dict,
    "enumerate": enumerate, "float": float, "int": int, "len": len,
    "list": list, "max": max, "min": min, "range": range, "round": round,
    "set": set, "sorted": sorted, "str": str, "sum": sum, "tuple": tuple,
    "zip": zip, "isinstance": isinstance, "print": print,
    "True": True, "False": False, "None": None,
}


def _rule_globals() -> Dict[str, Any]:
    return {
        "__builtins__": dict(_RULE_BUILTINS),
        "re": re,
        "math": math,
        "datetime": datetime,
        "date": date,
        "timedelta": timedelta,
        "unicodedata": unicodedata,
    }


def compile_rule(code: str):
    """
    Compile le code d'une regle et retourne la fonction `rule`.
    Leve ValueError si le code est invalide (syntaxe, fonction absente).
    """
    if not (code or "").strip():
        raise ValueError("Le code de la règle est vide.")
    namespace = _rule_globals()
    try:
        exec(compile(code, "<fp_rule>", "exec"), namespace)
    except SyntaxError as e:
        raise ValueError(f"Erreur de syntaxe Python : {e}")
    except Exception as e:
        raise ValueError(f"Erreur à l'initialisation du code : {e}")
    fn = namespace.get("rule")
    if not callable(fn):
        raise ValueError("Le code doit définir une fonction `rule(ctx)` retournant True/False.")
    return fn


def run_rule(code: str, ctx: Dict[str, Any]) -> Tuple[Optional[bool], Optional[str]]:
    """Execute une regle sur un contexte. Retourne (resultat, erreur)."""
    try:
        fn = compile_rule(code)
        return bool(fn(dict(ctx))), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def active_rules(db, channel: str) -> List[FpRule]:
    """Regles appliquees en production : ACTIVE + activees, dans l'ordre."""
    return db.query(FpRule).filter(
        FpRule.channel == channel,
        FpRule.status == "ACTIVE",
        FpRule.enabled.is_(True)
    ).order_by(FpRule.run_order.asc(), FpRule.id.asc()).all()


def evaluate_fp_rules(db, channel: str, ctx: Dict[str, Any],
                      dry_run: bool = False) -> Optional[FpRule]:
    """
    Applique les regles ACTIVE du canal au contexte d'alerte : retourne la
    premiere regle qui matche (ordre run_order), ou None. Fail-open : une
    regle en erreur est ignoree (l'alerte est conservee). Hors dry-run, le
    compteur de hits de la regle est incremente (commit par l'appelant).
    """
    for db_rule in active_rules(db, channel):
        result, error = run_rule(db_rule.code, ctx)
        if error:
            logger.error(
                f"Règle anti-FP #{db_rule.id} « {db_rule.name} » (v{db_rule.version}) en erreur "
                f"— alerte CONSERVÉE (fail-open) : {error}"
            )
            continue
        if result:
            if not dry_run:
                db_rule.hit_count = (db_rule.hit_count or 0) + 1
            return db_rule
    return None


def build_screening_ctx(client: Dict[str, Any], entity: Dict[str, Any],
                        best_match: Dict[str, Any]) -> Dict[str, Any]:
    """Contexte d'alerte du canal criblage clients, passe a rule(ctx)."""
    return {
        "channel": "SCREENING",
        "client_id": client.get("client_id"),
        "client_name": " ".join(
            p for p in (client.get("client_first_name"), client.get("client_last_name")) if p
        ).strip() or client.get("client_company_name") or "",
        "entity_id": entity.get("entity_id"),
        "entity_name": entity.get("primary_name"),
        "list_type": entity.get("_list_type"),
        "final_score": float(best_match.get("final_score", 0.0)),
        "base_score": float(best_match.get("base_score", 0.0)),
        "hard_match": bool(best_match.get("hard_match_triggered", False)),
        "adjustments": best_match.get("adjustments") or {},
        "client": dict(client),
        "entity": {k: v for k, v in entity.items()},
        "party": None,
        "message": None,
    }


def build_filtering_ctx(party: Dict[str, Any], entity: Dict[str, Any],
                        best_match: Dict[str, Any], parsed_message: Dict[str, Any],
                        client_id: str) -> Dict[str, Any]:
    """Contexte d'alerte du canal filtrage transactionnel, passe a rule(ctx)."""
    return {
        "channel": "FILTERING",
        "client_id": client_id,
        "client_name": party.get("name") or "",
        "entity_id": entity.get("entity_id"),
        "entity_name": entity.get("primary_name"),
        "list_type": entity.get("_list_type"),
        "final_score": float(best_match.get("final_score", 0.0)),
        "base_score": float(best_match.get("base_score", 0.0)),
        "hard_match": bool(best_match.get("hard_match_triggered", False)),
        "adjustments": best_match.get("adjustments") or {},
        "client": None,
        "entity": {k: v for k, v in entity.items()},
        "party": {
            "name": party.get("name"),
            "roles": party.get("roles") or [party.get("role")],
            "country": party.get("country"),
            "bic": party.get("bic"),
            "is_agent": bool(party.get("is_agent")),
            "address": party.get("address"),
            "birth_date": party.get("birth_date"),
        },
        "message": {
            "type": parsed_message.get("message_type"),
            "msg_id": parsed_message.get("msg_id"),
        },
    }


def annotate_suppression(best_match: Dict[str, Any], db_rule: FpRule) -> None:
    """Marque la decision comme supprimee par regle, pour le journal immuable."""
    best_match["fp_rule_applied"] = {
        "id": db_rule.id,
        "name": db_rule.name,
        "version": db_rule.version,
        "channel": db_rule.channel,
    }


# Squelette propose dans l'editeur du mode DEV
RULE_TEMPLATE = '''def rule(ctx):
    """
    Retourne True pour SUPPRIMER l'alerte (auto-clôture CLOSED_BY_RULE, tracée
    à l'audit), False pour la CONSERVER. Clés disponibles dans ctx :
      channel, client_id, client_name, entity_id, entity_name, list_type,
      final_score, base_score, hard_match, adjustments,
      client (profil complet, criblage), entity (fiche listée complète),
      party (name/roles/country/bic/is_agent, filtrage), message (type/msg_id).
    Modules disponibles : re, math, datetime, date, timedelta, unicodedata.
    """
    # Exemple : supprimer les scores faibles sans correspondance exacte
    # return ctx["final_score"] < 80 and not ctx["hard_match"]
    return False
'''

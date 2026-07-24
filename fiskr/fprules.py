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
import os
import re
import unicodedata
from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fiskr.config import config
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


def validate_rule_code(code: str) -> Dict[str, Any]:
    """
    Validation detaillee du code d'une regle, pour l'aide a l'edition :
    retourne {valid, error, line, offset} — la ligne/colonne d'une erreur de
    syntaxe permet au front de positionner le curseur sur la faute.
    """
    if not (code or "").strip():
        return {"valid": False, "error": "Le code de la règle est vide.",
                "line": None, "offset": None}
    try:
        compile(code, "<fp_rule>", "exec")
    except SyntaxError as e:
        return {"valid": False, "error": e.msg or "syntaxe invalide",
                "line": e.lineno, "offset": e.offset}
    try:
        compile_rule(code)
    except ValueError as e:
        return {"valid": False, "error": str(e), "line": None, "offset": None}
    return {"valid": True, "error": None, "line": None, "offset": None}


# ---------------------------------------------------------------------------
# Generation de regle en langage naturel (IA optionnelle, cle Anthropic)
# ---------------------------------------------------------------------------

def get_fprules_llm_config() -> Dict[str, Any]:
    cfg = config.get("fprules", {}) or {}
    return {
        "llm_enabled": bool(cfg.get("llm_enabled", False)),
        "llm_model": cfg.get("llm_model", "claude-sonnet-5"),
    }


class RuleGenerationUnavailable(RuntimeError):
    """La generation IA n'est pas configuree (flag ou cle absents) : le front
    doit proposer le formulaire structure a la place."""


class RuleGenerationFailed(RuntimeError):
    """Le modele a produit un code invalide malgre la relance. `raw_code`
    contient la derniere sortie brute pour correction manuelle."""

    def __init__(self, message: str, raw_code: str = ""):
        super().__init__(message)
        self.raw_code = raw_code


_GENERATION_SYSTEM_PROMPT = """Tu es un assistant de conformité LCB-FT qui écrit des règles Python \
anti-faux positifs pour le moteur de criblage Fiskr.

Contrat STRICT :
- Le code doit définir exactement `def rule(ctx):` retournant un booléen.
- True = SUPPRIMER l'alerte candidate (auto-clôture tracée à l'audit), False = la CONSERVER.
- Modules disponibles (déjà importés, n'ajoute AUCUN import) : re, math, datetime, date, timedelta, unicodedata.
- Clés de ctx (canal SCREENING = criblage clients) : channel, client_id, client_name, entity_id, \
entity_name, list_type, final_score (float 0-100), base_score (float), hard_match (bool), \
adjustments (dict), client (dict profil complet ou None), entity (dict fiche listée), \
party (None en criblage), message (None en criblage).
- Canal FILTERING (filtrage transactionnel) : client vaut None ; party est un dict \
{name, roles, country, bic, is_agent, address, birth_date} ; message est {type, msg_id}.
- Accède aux sous-dictionnaires de façon sûre : (ctx.get("client") or {}).get("...").
- GARDE-FOU : ne supprime JAMAIS une alerte dont ctx["hard_match"] est True, sauf si \
l'instruction le demande explicitement.
- Reste conservateur : en cas de doute sur un champ, retourne False (l'alerte est conservée).

Format de réponse OBLIGATOIRE, sans texte autour :
# EXPLICATION: <une phrase en français décrivant ce que fait la règle>
def rule(ctx):
    ..."""


def _extract_generated_code(text: str) -> Tuple[str, str]:
    """Extrait (code, explication) d'une reponse du modele : retire les
    eventuelles clotures Markdown et la ligne # EXPLICATION:."""
    body = (text or "").strip()
    fence = re.search(r"```(?:python)?\s*\n(.*?)```", body, re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    explanation = ""
    lines = []
    for line in body.splitlines():
        m = re.match(r"\s*#\s*EXPLICATION\s*:\s*(.*)", line, re.IGNORECASE)
        if m and not explanation:
            explanation = m.group(1).strip()
            continue
        lines.append(line)
    return "\n".join(lines).strip(), explanation


def generate_rule_code(instruction: str, channel: str,
                       model: Optional[str] = None) -> Dict[str, Any]:
    """
    Genere le code d'une regle depuis une instruction en langage naturel via
    l'API Claude. Erreurs EXPLICITES (pas de repli silencieux : c'est un clic
    utilisateur) : RuleGenerationUnavailable si non configure,
    RuleGenerationFailed (avec le code brut) si la sortie reste invalide apres
    une relance. Le code retourne n'est qu'un BROUILLON : le circuit normal
    (tests unitaires, soumission, validation 4-yeux) s'applique inchange.
    """
    cfg = get_fprules_llm_config()
    if not cfg["llm_enabled"]:
        raise RuleGenerationUnavailable(
            "La génération par IA est désactivée (fprules.llm_enabled dans config.yaml). "
            "Utilisez le formulaire structuré ou l'éditeur Python."
        )
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuleGenerationUnavailable(
            "ANTHROPIC_API_KEY n'est pas configurée sur le serveur. "
            "Utilisez le formulaire structuré ou l'éditeur Python."
        )
    try:
        import anthropic
    except ImportError:
        raise RuleGenerationUnavailable(
            "Le paquet Python 'anthropic' n'est pas installé sur le serveur "
            "(pip install anthropic). Utilisez le formulaire structuré."
        )
    model = model or cfg["llm_model"]
    client = anthropic.Anthropic()
    user_prompt = (
        f"Canal de la règle : {channel}\n"
        f"Instruction du responsable conformité :\n{instruction.strip()}"
    )
    messages = [{"role": "user", "content": user_prompt}]
    raw_code = ""
    for attempt in range(2):
        response = client.messages.create(
            model=model,
            max_tokens=2048,
            system=_GENERATION_SYSTEM_PROMPT,
            messages=messages,
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        raw_code, explanation = _extract_generated_code(text)
        try:
            compile_rule(raw_code)
            return {"code": raw_code, "explanation": explanation, "model": model}
        except ValueError as e:
            if attempt == 0:
                # Une seule relance, avec l'erreur en contexte
                messages.append({"role": "assistant", "content": text})
                messages.append({"role": "user", "content": (
                    f"Ce code est invalide ({e}). Corrige-le et renvoie "
                    "uniquement le format demandé (# EXPLICATION: puis def rule)."
                )})
            else:
                raise RuleGenerationFailed(
                    f"Le code généré reste invalide après relance : {e}",
                    raw_code=raw_code,
                )
    raise RuleGenerationFailed("Génération impossible.", raw_code=raw_code)


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

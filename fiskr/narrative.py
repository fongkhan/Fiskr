"""
Narratifs d'alertes human-in-the-loop (roadmap P3-3).

Genere un PROJET de narratif d'investigation a partir des donnees tracees de
l'alerte : le decision_tree du journal d'audit immuable (scores, hard match,
ajustements contextuels), l'identite des parties et l'historique des actions.

Deux niveaux :
1. Composeur deterministe (toujours disponible) : le narratif est assemble
   uniquement depuis des donnees calculees et tracees — aucun contenu invente,
   contexte fiable par construction (exigence d'explicabilite, EU AI Act).
2. Reformulation LLM optionnelle (Claude) : si `narrative.llm_enabled` est
   actif et qu'une cle ANTHROPIC_API_KEY est configuree, le brouillon
   deterministe est reformule en prose fluide. En cas d'erreur ou d'absence
   de configuration, repli silencieux sur le texte deterministe.

Le narratif reste un brouillon : il n'entraine JAMAIS de decision automatique.
La proposition et la validation 4-yeux restent des actes humains.
"""
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from fiskr.config import config

logger = logging.getLogger("fiskr.narrative")

ALERT_STATUS_LABELS = {
    "OPEN": "ouverte",
    "IN_PROGRESS": "en cours d'analyse",
    "PENDING_VALIDATION": "en attente de validation 4-yeux",
    "ESCALATED": "escaladée",
    "CLOSED_CONFIRMED": "close — vrai positif confirmé",
    "CLOSED_FALSE_POSITIVE": "close — faux positif",
}


def get_narrative_config() -> Dict[str, Any]:
    cfg = config.get("narrative", {}) or {}
    return {
        "llm_enabled": bool(cfg.get("llm_enabled", False)),
        "llm_model": cfg.get("llm_model", "claude-opus-4-8"),
    }


def _fmt_date(value) -> str:
    if value is None:
        return "date inconnue"
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y à %H:%M UTC")
    return str(value)


def compose_deterministic_narrative(alert, audit, events: List[Any]) -> str:
    """
    Assemble le narratif depuis les seules donnees tracees (alerte + audit +
    historique). Chaque phrase est justifiable par un champ en base.
    """
    tree: Dict[str, Any] = (audit.decision_tree if audit is not None else None) or {}
    wl_entity = tree.get("watchlist_entity") or {}
    lines: List[str] = []

    # --- Contexte ---
    lines.append(f"PROJET DE NARRATIF — Alerte n°{alert.id}")
    lines.append("")
    lines.append(
        f"L'alerte n°{alert.id} a été générée le {_fmt_date(alert.created_at)} lors du criblage "
        f"du tiers « {alert.client_name} » (identifiant {alert.client_id or 'non renseigné'}) "
        f"contre les listes de surveillance en production."
    )
    origin = wl_entity.get("_list_type") or ""
    origin_txt = f" (liste {origin})" if origin else ""
    lines.append(
        f"Le profil a présenté une correspondance avec la personne listée "
        f"« {alert.watchlist_name} » (identifiant {alert.watchlist_entity_id}){origin_txt}, "
        f"avec un score final de {alert.final_score:.1f} %."
    )
    if audit is not None and audit.watchlist_version:
        lines.append(f"Version des listes au moment de la décision : {audit.watchlist_version}.")

    # --- Analyse du matching ---
    lines.append("")
    lines.append("Analyse de la correspondance :")
    if tree.get("hard_match_triggered"):
        detail = tree.get("hard_match_details") or "correspondance exacte d'identifiant"
        lines.append(
            f"- Correspondance exacte d'identifiant (hard match) : {detail}. "
            f"Le score est verrouillé à 100 % conformément à la règle de priorité absolue."
        )
    else:
        base = tree.get("base_score")
        if base is not None:
            lines.append(
                f"- Score textuel de base : {base:.1f} % entre "
                f"« {tree.get('best_client_name', alert.client_name)} » et "
                f"« {tree.get('best_watchlist_name', alert.watchlist_name)} » "
                f"(moyenne pondérée Jaro-Winkler / Damerau-Levenshtein / Token Sort)."
            )
        adjustments = tree.get("adjustments") or {}
        adj_labels = {"dob": "Date de naissance", "gender": "Genre", "geography": "Géographie"}
        for key, label in adj_labels.items():
            adj = adjustments.get(key) or {}
            desc = adj.get("description")
            score = adj.get("score", 0.0)
            if desc and desc != "N/A":
                sign = f"+{score:g}" if score > 0 else f"{score:g}"
                lines.append(f"- {label} : {desc} ({sign} point(s)).")
    cut_off = tree.get("cut_off_applied")
    if cut_off is not None:
        lines.append(
            f"- Seuil réglementaire appliqué : {cut_off:g} % — le score final de "
            f"{alert.final_score:.1f} % {'dépasse' if alert.final_score >= cut_off else 'est inférieur à'} ce seuil."
        )

    # --- Historique de traitement ---
    redetections = [e for e in events if getattr(e, "action", "") == "REDETECTED"]
    lines.append("")
    lines.append("Traitement de l'alerte :")
    lines.append(f"- Statut actuel : {ALERT_STATUS_LABELS.get(alert.status, alert.status)}.")
    if alert.assigned_to:
        lines.append(f"- Assignée à : {alert.assigned_to}.")
    if redetections:
        lines.append(
            f"- La correspondance a été re-détectée {len(redetections)} fois lors de criblages "
            f"ultérieurs (dernière : {_fmt_date(getattr(redetections[-1], 'timestamp', None))})."
        )
    if alert.decided_by:
        lines.append(
            f"- Décision : {ALERT_STATUS_LABELS.get(alert.status, alert.status)}, par {alert.decided_by} "
            f"le {_fmt_date(alert.decided_at)}."
            + (f" Motif : {alert.decision_comment}" if alert.decision_comment else "")
        )

    lines.append("")
    lines.append(
        "Ce narratif est un projet généré automatiquement à partir des seules données "
        "tracées dans le journal d'audit. Il doit être relu, complété et validé par un "
        "analyste : la décision finale (vrai/faux positif) reste humaine et soumise à la "
        "validation 4-yeux."
    )
    return "\n".join(lines)


def _llm_rewrite(draft: str, model: str) -> Optional[str]:
    """
    Reformule le brouillon deterministe en prose fluide via l'API Claude.
    Retourne None (repli deterministe) si le SDK n'est pas installe, si la
    cle est absente, ou si l'appel echoue — sans jamais bloquer la generation.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("narrative.llm_enabled est actif mais le paquet 'anthropic' n'est pas installé.")
        return None
    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=(
                "Tu es un assistant de conformité LCB-FT. On te fournit un projet de narratif "
                "d'alerte de criblage assemblé automatiquement depuis des données d'audit. "
                "Reformule-le en prose française professionnelle et fluide destinée à un dossier "
                "d'investigation. Règles strictes : n'ajoute AUCUN fait, chiffre ou hypothèse qui "
                "ne figure pas dans le brouillon ; conserve tous les scores, seuils, dates et "
                "identifiants exactement ; conserve l'avertissement final indiquant que la "
                "décision reste humaine. Réponds uniquement avec le narratif reformulé."
            ),
            messages=[{"role": "user", "content": draft}],
        )
        if response.stop_reason == "refusal":
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        return text or None
    except Exception as e:
        logger.warning(f"Reformulation LLM du narratif impossible ({e}) — repli déterministe.")
        return None


def generate_alert_narrative(alert, audit, events: List[Any]) -> Tuple[str, bool]:
    """
    Genere le narratif d'une alerte. Retourne (texte, llm_utilise).
    Le composeur deterministe fournit toujours un resultat ; la reformulation
    LLM n'est tentee que si elle est activee et configuree.
    """
    draft = compose_deterministic_narrative(alert, audit, events)
    cfg = get_narrative_config()
    if cfg["llm_enabled"]:
        rewritten = _llm_rewrite(draft, cfg["llm_model"])
        if rewritten:
            return rewritten, True
    return draft, False

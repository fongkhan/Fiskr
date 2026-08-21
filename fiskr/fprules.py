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
from functools import lru_cache
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


@lru_cache(maxsize=256)
def _compiled(code: str):
    """Fonction `rule` compilee, memoisee sur le TEXTE de la regle.

    Une regle est desormais evaluee une fois par correspondance, pas une fois
    par criblage : jusqu'a plusieurs milliers de fois pour un homonyme de nom
    tres courant. Recompiler le code a chaque appel etait invisible tant qu'il
    n'y en avait qu'un. Modifier une regle produit un texte different, donc une
    autre entree : aucune invalidation explicite necessaire.
    """
    return compile_rule(code)


def run_rule(code: str, ctx: Dict[str, Any]) -> Tuple[Optional[bool], Optional[str]]:
    """Execute une regle sur un contexte. Retourne (resultat, erreur)."""
    try:
        fn = _compiled(code)
        return bool(fn(dict(ctx))), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def rule_perimeters(db_rule) -> List[str]:
    """Perimetres declares d'une regle. Vide = tous (regles anterieures a la
    colonne : leur portee ne change pas)."""
    from fiskr.perimeters import PERIMETRES
    brut = getattr(db_rule, "perimeters", None)
    if not brut:
        return []
    if isinstance(brut, str):
        brut = [v for v in brut.split(",")]
    valeurs = [str(v).strip().upper() for v in brut if str(v).strip()]
    retenues = [v for v in valeurs if v in PERIMETRES]
    # Une declaration entierement invalide ne doit pas ELARGIR la portee en
    # silence : elle vaut alors « aucun perimetre reconnu », donc la regle ne
    # s'applique nulle part plutot que partout.
    return retenues if retenues else ["__AUCUN__"]


def active_rules(db, channel: str) -> List[FpRule]:
    """Regles appliquees en production : ACTIVE + activees, dans l'ordre."""
    return db.query(FpRule).filter(
        FpRule.channel == channel,
        FpRule.status == "ACTIVE",
        FpRule.enabled.is_(True)
    ).order_by(FpRule.run_order.asc(), FpRule.id.asc()).all()


def evaluate_fp_rules(db, channel: str, ctx: Dict[str, Any],
                      dry_run: bool = False, rules=None) -> Optional[FpRule]:
    """
    Applique les regles ACTIVE du canal au contexte d'alerte : retourne la
    premiere regle qui matche (ordre run_order), ou None. Fail-open : une
    regle en erreur est ignoree (l'alerte est conservee). Hors dry-run, le
    compteur de hits de la regle est incremente (commit par l'appelant).

    `rules` : jeu de regles deja charge. Un criblage evalue les regles une fois
    par correspondance — des milliers pour un homonyme de nom tres courant — et
    les relire a chaque fois remettait un N+1 sur le chemin le plus chaud.
    L'appelant qui boucle les charge UNE fois et les passe ici.
    """
    perimetre = str(ctx.get("perimeter") or "").strip().upper()
    for db_rule in (active_rules(db, channel) if rules is None else rules):
        # Portee de la regle : filtree par le MOTEUR, pas par le code de la
        # regle. Une regle limitee au hors-sanction ne peut pas cloturer une
        # correspondance de gel d'avoirs, meme si son code l'oublie — et un
        # controleur lit la portee sur la regle plutot que dans son code.
        portee = rule_perimeters(db_rule)
        if portee and perimetre and perimetre not in portee:
            continue
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


def corroboration_context(client: Dict[str, Any],
                          best_match: Dict[str, Any]) -> Dict[str, Any]:
    """
    De quoi le profil crible dispose-t-il POUR IDENTIFIER, au-dela du nom ?

    Un nom identique ne vaut pas une identification. Mesure en production :
    « Mohammed Ali » sans pays remonte 2 976 correspondances au-dessus du
    seuil, dont plusieurs dizaines a 100,00 — des homonymes reels, pas du
    bruit de score. Aucune metrique de chaine ne separe ces fiches ; ce qui
    manque est ailleurs : date de naissance, pays, piece d'identite.

    Ce bloc est passe aux regles anti-faux positifs pour qu'elles puissent
    ecrire cette distinction — « nom seul, sans element corroborant » — au
    lieu de la deduire d'un score.
    """
    dates = [d for d in ([client.get("client_dob")] if client.get("client_dob") else [])
             + list(client.get("dates_of_birth") or []) if d]
    pays_bruts = client.get("client_countries") or {}
    pays = [p for groupe in pays_bruts.values() if isinstance(groupe, (list, tuple))
            for p in groupe if p]
    pays += [p for p in (client.get("client_nationality") or []) if p]
    pays += [p for p in (client.get("client_residence") or []) if p]
    pieces = [
        client.get("client_passport"), client.get("passport_number"),
        client.get("client_national_id"), client.get("national_id"),
        client.get("client_tax_id"), client.get("lei_number"),
    ]
    pieces = [p for p in pieces if p and str(p).strip()]
    ajustements = best_match.get("adjustments") or {}

    def _score(cle: str) -> float:
        bloc = ajustements.get(cle) or {}
        try:
            return float(bloc.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    a_dob, a_pays, a_piece = bool(dates), bool(pays), bool(pieces)
    return {
        "has_dob": a_dob,
        "has_country": a_pays,
        "has_identity_document": a_piece,
        # Aucun element identifiant hors le nom : la correspondance ne peut
        # etre qu'une homonymie possible, quel que soit son score.
        "name_only": not (a_dob or a_pays or a_piece),
        "dob_score": _score("dob"),
        "gender_score": _score("gender"),
        "geography_score": _score("geography"),
        # Un element du profil a effectivement CORROBORE la fiche listee
        "corroborated": _score("dob") > 0 or _score("geography") > 0,
    }


def build_screening_ctx(client: Dict[str, Any], entity: Dict[str, Any],
                        best_match: Dict[str, Any],
                        hits_count: int = 1, hit_rank: int = 1,
                        perimeter_overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Contexte d'alerte du canal criblage clients, passe a rule(ctx).

    `hits_count` / `hit_rank` : volumetrie du criblage qui a produit cette
    correspondance, et rang de celle-ci par score decroissant (1 = la
    meilleure). Un criblage rend desormais TOUTES ses correspondances
    au-dessus du seuil : une regle doit pouvoir raisonner sur le lot, pas
    seulement sur la ligne.
    """
    from fiskr.perimeters import perimetre_de
    from fiskr import rarete
    return {
        "hits_count": int(hits_count),
        "hit_rank": int(hit_rank),
        # Frequence des mots du nom dans le corpus liste : un rapprochement sur
        # « MOHAMMED ALI » n'identifie personne, un rapprochement sur « TYURIN »
        # identifie presque surement. Cf. fiskr/rarete.py.
        # Deja calcule par le moteur sur les alertes (scoring.match_entities) :
        # on relit plutot que de recompter.
        "rarity": best_match.get("name_rarity") or rarete.profil(
            best_match.get("best_client_name") or "",
            best_match.get("best_watchlist_name") or "", "SCREENING"),
        # SANCTION (gel des avoirs) ou HORS_SANCTION (PEP, regulateurs,
        # exclusions). Manquer une sanction est constatable a l'audit et
        # sanctionnable ; manquer un PEP ne l'est pas de la meme facon.
        "perimeter": perimetre_de(entity.get("_list_type"), perimeter_overrides),
        "corroboration": corroboration_context(client, best_match),
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
                        client_id: str,
                        hits_count: int = 1, hit_rank: int = 1,
                        perimeter_overrides: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Contexte d'alerte du canal filtrage transactionnel, passe a rule(ctx).

    Meme enrichissement que le criblage : une partie de message de paiement
    porte rarement une date de naissance ou une piece d'identite, donc le cas
    « nom seul » y est la norme plutot que l'exception.
    """
    from fiskr.perimeters import perimetre_de
    from fiskr import rarete
    return {
        "hits_count": int(hits_count),
        "hit_rank": int(hit_rank),
        # Deja calcule par le moteur sur les alertes (scoring.match_entities) :
        # on relit plutot que de recompter.
        "rarity": best_match.get("name_rarity") or rarete.profil(
            best_match.get("best_client_name") or "",
            best_match.get("best_watchlist_name") or "", "FILTERING"),
        "perimeter": perimetre_de(entity.get("_list_type"), perimeter_overrides),
        "corroboration": corroboration_context(party, best_match),
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

def get_fprules_llm_config(db=None) -> Dict[str, Any]:
    """Etat effectif (reglage a chaud > config.yaml)."""
    from fiskr.settings import fprules_llm_settings
    return fprules_llm_settings(db)


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
            "La génération par IA est désactivée (Paramètres → Intégrations → IA). "
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
      party (name/roles/country/bic/is_agent, filtrage), message (type/msg_id),
      perimeter       : SANCTION ou HORS_SANCTION — une règle volumétrique n'a
                        rien à faire sur le premier,
      hits_count      : nombre de correspondances >= seuil de CE criblage,
      hit_rank        : rang de celle-ci par score décroissant (1 = meilleure),
      corroboration   : has_dob, has_country, has_identity_document,
                        name_only, corroborated, dob_score, gender_score,
                        geography_score,
      rarity          : fréquence des mots du nom dans le corpus listé —
                        disponible, nom_repandu, sans_token_commun, rarete,
                        df_min, df_max, seuil_repandu, couverture, tokens.
                        Sans table, disponible vaut False et tous les drapeaux
                        sont au repos : la règle ne clôture rien.
    Modules disponibles : re, math, datetime, date, timedelta, unicodedata.
    """
    # Exemple : supprimer les scores faibles sans correspondance exacte
    # return ctx["final_score"] < 80 and not ctx["hard_match"]
    return False
'''


# ------------------ MODELES DE REGLES PRETS A INSTALLER ------------------
#
# Le criblage conserve desormais TOUTES les correspondances au-dessus du seuil.
# C'est l'exigence d'audit ; c'en est aussi la consequence : un homonyme d'un
# nom tres courant en produit des milliers. Mesure en production, « Mohammed
# Ali » sans pays : 17 649 candidats, 2 976 correspondances >= seuil, dont
# plusieurs dizaines a 100,00 — des homonymes REELS, pas du bruit de score.
#
# Aucune metrique de chaine ne separe ces fiches : leurs noms SONT identiques.
# Ce qui manque n'est pas dans le score, c'est l'identification — date de
# naissance, pays, piece d'identite. Ces modeles ecrivent cette distinction.
#
# Aucun n'est actif par defaut : ce sont des arbitrages de conformite, pas des
# reglages de confort. Chacun cree les alertes PUIS les cloture par regle, avec
# le motif en clair — jamais de suppression silencieuse.
_CODE_NOM_SEUL_VOLUME = '\n'.join([
    'def rule(ctx):',
    '    """Nom seul + volumetrie : le criblage ne peut pas identifier."""',
    '    VOLUME = 25          # au-dela de N correspondances pour un meme criblage',
    '    c = ctx["corroboration"]',
    '    if ctx["hits_count"] < VOLUME:',
    '        return False     # peu de correspondances : elles restent a traiter',
    '    if ctx["hard_match"]:',
    '        return False     # identifiant officiel identique : jamais cloture',
    '    return c["name_only"]',
    '',
])

_CODE_HORS_SOMMET = '\n'.join([
    'def rule(ctx):',
    '    """Au-dela des premieres correspondances, sans corroboration."""',
    '    RANG_CONSERVE = 10',
    '    c = ctx["corroboration"]',
    '    if ctx["hard_match"] or ctx["hit_rank"] <= RANG_CONSERVE:',
    '        return False',
    '    return not c["corroborated"]',
    '',
])

_CODE_FILTRAGE_NOM_SEUL = '\n'.join([
    'def rule(ctx):',
    '    """Filtrage transactionnel : nom seul, en volume."""',
    '    VOLUME = 25',
    '    if ctx["hard_match"]:',
    '        return False',
    '    return ctx["hits_count"] >= VOLUME and ctx["corroboration"]["name_only"]',
    '',
])

_CODE_SANCTION_REPERE = '\n'.join([
    'def rule(ctx):',
    '    """Perimetre SANCTION : ne cloture rien par volumetrie.',
    '',
    '    Manquer un gel d avoirs est constatable a l audit et sanctionnable',
    '    financierement. Une regle de ce perimetre doit viser une famille de',
    '    faux positifs identifiee et justifiee, jamais trier par le nombre.',
    '    """',
    '    return False',
    '',
])

_CODE_NOM_REPANDU = '\n'.join([
    'def rule(ctx):',
    '    """Nom compose uniquement de mots tres repandus dans les listes.',
    '',
    '    Le rapprochement ne partage avec la fiche listee que des mots que des',
    '    milliers d autres fiches portent aussi (MOHAMMED, ALI, AL, BIN...).',
    '    Il ne designe donc personne en particulier. Un seul mot rare partage',
    '    suffit a faire echouer la regle, et l alerte reste ouverte.',
    '    """',
    '    if ctx["hard_match"]:',
    '        return False     # identifiant officiel identique : jamais cloture',
    '    if ctx["corroboration"]["corroborated"]:',
    '        return False     # date de naissance ou pays concordant : on garde',
    '    r = ctx.get("rarity") or {}',
    '    if not r.get("disponible") or r.get("sans_token_commun"):',
    '        return False     # sans mesure de rarete, on ne cloture rien',
    '    return bool(r.get("nom_repandu"))',
    '',
])

RULE_TEMPLATES = (
    {
        "key": "name_only_volume",
        "name": "Hors sanctions — nom seul, sans élément corroborant, en volume",
        "channel": "SCREENING",
        "perimeters": ["HORS_SANCTION"],
        "summary": ("Clôture les correspondances d'un criblage qui ne repose que sur le "
                    "nom — aucun élément d'identification au profil — et qui en produit "
                    "plus que le seuil de volumétrie."),
        "loss": ("Un PEP ou une exclusion réellement porteur de ce nom sera clôturé "
                 "comme les autres tant que le profil client ne porte ni date de "
                 "naissance, ni pays, ni pièce d'identité. Le périmètre SANCTION est "
                 "HORS DE PORTÉE de cette règle : un gel d'avoirs manqué est "
                 "constatable à l'audit, un PEP manqué ne l'est pas de la même façon."),
        "code": _CODE_NOM_SEUL_VOLUME,
    },
    {
        "key": "common_name_tokens",
        "name": "Hors sanctions — le nom ne partage que des mots très répandus",
        "channel": "SCREENING",
        "perimeters": ["HORS_SANCTION"],
        "summary": ("Clôture les correspondances dont TOUS les mots communs avec la fiche "
                    "listée sont portés par des milliers d'autres fiches — « MOHAMMED », "
                    "« ALI », « AL », « BIN » — et qu'aucun élément du profil ne corrobore. "
                    "Un seul mot rare partagé suffit à conserver l'alerte."),
        "loss": ("Une personne réellement listée dont le nom n'est composé que de mots "
                 "répandus sera clôturée tant que le profil client n'apporte ni date de "
                 "naissance ni pays concordant. C'est le même arbitrage que « nom seul », "
                 "mais fondé sur ce que le nom vaut dans le corpus plutôt que sur le "
                 "nombre de correspondances : il vise le nom banal, pas le criblage "
                 "volumineux. Périmètre SANCTION hors de portée."),
        "code": _CODE_NOM_REPANDU,
    },
    {
        "key": "no_corroboration_beyond_top",
        "name": "Hors sanctions — aucune corroboration au-delà des premières",
        "channel": "SCREENING",
        "perimeters": ["HORS_SANCTION"],
        "summary": ("Garde ouvertes les meilleures correspondances et clôture les "
                    "suivantes quand rien dans le profil ne corrobore la fiche listée "
                    "(ni date de naissance concordante, ni géographie)."),
        "loss": ("Une correspondance classée au-delà du rang retenu est clôturée même si "
                 "elle est la bonne. Convient quand le profil porte du contexte mais que "
                 "celui-ci ne corrobore aucune fiche."),
        "code": _CODE_HORS_SOMMET,
    },
    {
        "key": "filtering_name_only",
        "name": "Hors sanctions — filtrage, partie sans pays ni identifiant",
        "channel": "FILTERING",
        "perimeters": ["HORS_SANCTION"],
        "summary": ("Une partie de message ISO 20022 porte rarement une date de "
                    "naissance. Clôture les correspondances nom-seul en volume, en "
                    "gardant intactes les correspondances exactes d'identifiant."),
        "loss": ("Même arbitrage que pour le criblage, sur un canal où l'absence de "
                 "contexte est la norme plutôt que l'exception."),
        "code": _CODE_FILTRAGE_NOM_SEUL,
    },
    {
        "key": "sanction_never_auto_closed",
        "name": "Sanctions — repère : ce périmètre ne se clôture pas en volume",
        "channel": "SCREENING",
        "perimeters": ["SANCTION"],
        "summary": ("Modèle de référence, volontairement inerte : il ne clôture rien. "
                    "Il sert de point de départ à une règle de faux positif visant "
                    "précisément une famille de cas sur le périmètre sanctions, jamais "
                    "un tri par volumétrie."),
        "loss": ("Aucune : tel quel, ce modèle ne clôture aucune correspondance. Toute "
                 "modification qui le rendrait volumétrique ferait perdre, sur le "
                 "périmètre où le manquement est constatable à l'audit et sanctionnable "
                 "financièrement, ce que les deux autres modèles font perdre sur un "
                 "périmètre où il ne l'est pas."),
        "code": _CODE_SANCTION_REPERE,
    },
)


def rule_templates(channel: Optional[str] = None) -> List[Dict[str, Any]]:
    """Modeles de regles proposes, filtres par canal."""
    if not channel:
        return [dict(m) for m in RULE_TEMPLATES]
    canal = channel.strip().upper()
    return [dict(m) for m in RULE_TEMPLATES if m["channel"] == canal]

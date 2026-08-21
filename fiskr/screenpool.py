"""
Criblage a blanc, en serie ou en parallele par tranches de clients.

Ce module porte le corps du criblage (un client contre un index de blocking)
et son orchestration. La version parallele decoupe le panel en tranches par
bornes d'identifiants et les repartit sur un pool de processus `fork()` cree
APRES le chargement de l'univers : l'index est partage en copy-on-write, il
n'est jamais copie ni recharge. Les enfants sont en LECTURE SEULE — aucune
ecriture en base, et la seule connexion qu'ils ouvrent (chargement de leur
tranche) est refermee avant le calcul.

Pourquoi un pool de processus et pas des threads : les metriques de
comparaison sont du Python pur, un thread CPU-bound monopolise le GIL et
etouffe son processus hote (mesure sur l'API : p50 multiplie par 6 a 11).

PROJECTION MEMOIRE : a 750 000 fiches (cas PEP reel), un dict complet pese
~8,4 Ko/fiche soit 6,3 Go l'univers. La projection ENTITY_PROJECTION reduit a
~3,8 Ko/fiche (2,8 Go) en ne chargeant que ce que le moteur lit. Les regles
anti-faux positifs recevant la fiche entiere (fprules.build_screening_ctx),
`projection_for` re-inclut toute colonne que le code d'une regle mentionne.
"""
import gc
import logging
import multiprocessing
import os
import re
import sys
import time
import types
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

from fiskr.config import config
from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys
from fiskr.scoring import match_entities

logger = logging.getLogger("fiskr.screenpool")

MAX_PAIR_DETAILS = 200
_PROGRESS_EVERY = 500

# ------------------ PROJECTION ------------------

# Champs de WatchlistEntity reellement lus par le moteur, verifies dans
# blocking.generate_blocking_keys, scoring.check_hard_matches (priorites 1-6),
# scoring.match_entities, fprules.build_screening_ctx (champs directs) et les
# restitutions de paires (backtest, rescreen). Le test sentinelle de
# tests/test_projection.py echoue si un futur champ lu n'y figure pas.
ENTITY_PROJECTION: Tuple[str, ...] = (
    # identite / restitution / delta
    "entity_id", "entity_type", "primary_name", "snapshot_id", "entity_checksum",
    # blocking + scoring flou
    "individual_name_parsed", "aliases", "countries", "country",
    "dates_of_birth", "gender",
    # hard matches
    "lei_number", "bic_swift", "tax_id", "crypto_wallets",
    "passport_documents", "national_registry_ids", "national_id_documents",
    "imo_number", "aircraft_tail_number", "vessel_mmsi", "vessel_call_sign",
    "other_id_documents", "other_registration_ids",
)


def projection_for(rule_snaps: Sequence[Any]) -> Tuple[str, ...]:
    """
    Projection de base + toute colonne que le CODE d'une regle anti-FP
    mentionne. Une regle est du Python libre qui recoit la fiche entiere
    (ctx["entity"]) : projeter aveuglement changerait son verdict. Le code
    etant du texte, un scan lexical suffit et ne peut pas rater une colonne
    citee — au pire il en re-inclut une par exces, ce qui est sans danger.
    """
    from fiskr.database import WatchlistEntity

    cols = set(ENTITY_PROJECTION)
    all_cols = {c.name for c in WatchlistEntity.__table__.columns}
    for rule in rule_snaps or ():
        code = getattr(rule, "code", "") or ""
        for col in all_cols - cols:
            if re.search(rf"['\"]{re.escape(col)}['\"]", code):
                cols.add(col)
    return tuple(sorted(cols))


# ------------------ CORPS DU CRIBLAGE (un client) ------------------

def screen_one(client: Dict[str, Any], index: Dict[str, List[Dict[str, Any]]],
               screening_cfg, whitelist_keys: Set[Tuple[str, str]],
               rules: Sequence[Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """
    Crible UN client contre l'index. Retourne (categorie, detail) :
    ("alert", paire), ("whitelisted", None), ("rule", paire enrichie de la
    regle), ou None si aucun rapprochement. C'est LE code commun des chemins
    serie et parallele : ils ne peuvent pas diverger.
    """
    from fiskr.fprules import build_screening_ctx, run_rule

    candidates: Dict[str, Dict[str, Any]] = {}
    for key in lookup_blocking_keys(client, screening_cfg):
        for ent in index.get(key, []):
            candidates[ent["entity_id"]] = ent
    if not candidates:
        return None

    best = None
    best_ent = None
    # Nombre de correspondances au-dessus du seuil, pas seulement la meilleure.
    # La production en ouvre UNE ALERTE CHACUNE : sans ce compte, le cahier de
    # tests annoncerait un volume qui n'est pas celui qui sera produit — et
    # surtout, une regle volumetrique ne se declencherait jamais ici (elle
    # verrait toujours hits_count = 1) alors qu'elle se declenchera en
    # production. Le cahier doit predire la production, pas une autre chose.
    hits = 0
    for ent in candidates.values():
        score = match_entities(client, ent, config)
        if score.get("status") == "ALERT":
            hits += 1
        if best is None or score["final_score"] > best["final_score"]:
            best = score
            best_ent = ent

    if not best or best.get("status") != "ALERT":
        return None

    if (client.get("client_id"), best_ent.get("entity_id")) in whitelist_keys:
        return ("whitelisted", {"hits": hits})

    pair = {
        "client_id": client.get("client_id"),
        "client_name": _client_label(client),
        "entity_id": best_ent.get("entity_id"),
        "entity_name": best_ent.get("primary_name"),
        "list_type": best_ent.get("_list_type"),
        "score": round(float(best.get("final_score", 0)), 2),
        "hits": hits,
    }

    matched_rule = None
    if rules:
        # Meme contexte qu'en production : la meilleure correspondance est de
        # rang 1, et la volumetrie du criblage voyage avec elle.
        ctx = build_screening_ctx(client, best_ent, best,
                                  hits_count=hits, hit_rank=1)
        for rule in rules:
            result, error = run_rule(rule.code, ctx)
            if error:
                continue  # fail-open : une regle en erreur conserve l'alerte
            if result:
                matched_rule = rule
                break
    if matched_rule is not None:
        return ("rule", {**pair, "rule_id": matched_rule.id, "rule_name": matched_rule.name})
    return ("alert", pair)


def _client_label(client: Dict[str, Any]) -> str:
    if client.get("client_company_name"):
        return client["client_company_name"]
    return " ".join(p for p in (client.get("client_first_name"),
                                client.get("client_last_name")) if p).strip()


# ------------------ AGREGATION ------------------

def new_partial() -> Dict[str, Any]:
    # `pairs` compte les CLIENTS interceptes (une paire par client, la
    # meilleure correspondance) ; `hits` compte les CORRESPONDANCES, dont la
    # production ouvre une alerte chacune. Les deux chiffres repondent a deux
    # questions differentes : « quelle proportion du panel est interceptee ? »
    # et « combien d'alertes cette liste va-t-elle ouvrir ? ».
    return {"pairs": {}, "whitelisted_suppressed": 0, "alerts_before_rules": 0,
            "rule_suppressed": 0, "rule_suppressed_pairs": [], "hits": 0}


def apply_outcome(agg: Dict[str, Any], outcome) -> None:
    if outcome is None:
        return
    category, detail = outcome
    agg["hits"] += int((detail or {}).get("hits", 0))
    if category == "whitelisted":
        agg["whitelisted_suppressed"] += 1
        return
    agg["alerts_before_rules"] += 1
    if category == "rule":
        agg["rule_suppressed"] += 1
        if len(agg["rule_suppressed_pairs"]) < MAX_PAIR_DETAILS:
            agg["rule_suppressed_pairs"].append(detail)
        return
    agg["pairs"][(detail["client_id"], detail["entity_id"])] = detail


def merge_partials(partials: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Fusionne les tranches : les cles de `pairs` sont disjointes (un client
    vit dans une seule tranche), les compteurs s'additionnent, les listes se
    concatenent DANS L'ORDRE des tranches puis se tronquent — meme resultat
    que la passe sequentielle sur les memes clients dans le meme ordre."""
    merged = new_partial()
    for part in partials:
        merged["pairs"].update(part["pairs"])
        merged["whitelisted_suppressed"] += part["whitelisted_suppressed"]
        merged["alerts_before_rules"] += part["alerts_before_rules"]
        merged["rule_suppressed"] += part["rule_suppressed"]
        merged["hits"] += part.get("hits", 0)
        merged["rule_suppressed_pairs"].extend(part["rule_suppressed_pairs"])
    merged["rule_suppressed_pairs"] = merged["rule_suppressed_pairs"][:MAX_PAIR_DETAILS]
    merged["alerts"] = len(merged["pairs"])
    return merged


# ------------------ DIMENSIONNEMENT ------------------

def _mem_available_bytes() -> int:
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        pass
    return 4 * 1024**3  # estimation prudente si /proc indisponible


# Empreinte moyenne d'une fiche PROJETEE en memoire (mesuree : 3,8 Ko + index)
_PROJECTED_ENTITY_BYTES = 4200
# Fraction de l'univers salie par les refcounts malgre gc.freeze (copy-on-write)
_COW_DIRTY_FACTOR = 0.35


def resolve_processes(universe_count: int, requested: Optional[int] = None) -> int:
    """
    Nombre de processus de calcul : borne par la config, par les coeurs
    (criblage temps reel prioritaire : on laisse deux coeurs a l'API et au
    demon) et par un BUDGET MEMOIRE — chaque enfant salit une fraction de
    l'univers partage en copy-on-write, malgre gc.freeze.
    """
    if sys.platform != "linux":
        return 1  # pas de fork : repli sequentiel (comportement historique)
    if requested is None:
        requested = int((config.get("jobs") or {}).get("screen_processes", 0) or 0)
    if requested == 1:
        return 1
    cpu_cap = max(1, (os.cpu_count() or 2) - 2)
    dirty = max(1, int(_COW_DIRTY_FACTOR * max(1, universe_count) * _PROJECTED_ENTITY_BYTES))
    mem_cap = max(1, int(_mem_available_bytes() * 0.6 // dirty))
    auto = max(1, min(cpu_cap, mem_cap, 6))
    return min(requested, cpu_cap) if requested > 1 else auto


# ------------------ POOL FORK ------------------

class PoolStalled(RuntimeError):
    """
    Le pool de criblage n'avance plus : un enfant a ete tue (OOM killer,
    typiquement) et sa tranche est PERDUE — multiprocessing.Pool remplace
    l'enfant mais ne relance jamais la tranche, donc map_async ne se termine
    jamais — ou un enfant est fige (interblocage fork+threads).

    Sans ce signal, l'attente du parent etait INFINIE : le job gardait son
    battement de coeur (thread separe), la reprise sur battement perime ne le
    voyait jamais, et la serialisation des cahiers de tests bloquait tous les
    suivants — un slot du demon consomme a vie, la file en attente.
    """


def _pool_stall_timeout_s() -> float:
    raw = (config.get("jobs") or {}).get("screen_stall_timeout_s", 900)
    try:
        return max(60.0, float(raw))
    except (TypeError, ValueError):
        return 900.0


def _wait_with_watchdog(async_result, queue, pool, total_clients: int,
                        progress: Optional[Callable[[int, int], None]],
                        stall_timeout_s: Optional[float] = None) -> None:
    """
    Attend la fin du pool en surveillant DEUX signaux de blocage :
    - un enfant mort avec un code de sortie non nul (tue par l'OOM killer :
      exitcode -9) — detection opportuniste, le pool peut le remplacer avant
      qu'on le voie ;
    - AUCUN tick de progression pendant `stall_timeout_s` (la garantie) :
      les tranches saines tickent tous les 500 clients, un silence total
      prolonge signifie que seules des tranches perdues/figees restent.
    Leve PoolStalled — le `with` du pool le termine, l'appelant decide du
    repli. L'attente infinie n'existe plus.
    """
    stall = stall_timeout_s if stall_timeout_s is not None else _pool_stall_timeout_s()
    done = 0
    last_activity = time.monotonic()
    while not async_result.ready():
        drained = _drain_count(queue)
        if drained:
            done += drained
            last_activity = time.monotonic()
            if progress:
                try:
                    progress(min(done, total_clients), total_clients)
                except Exception:
                    pass
        dead = [p.exitcode for p in getattr(pool, "_pool", ())
                if p.exitcode not in (None, 0)]
        if dead:
            raise PoolStalled(
                f"un processus de criblage est mort (code {dead[0]} — tué par "
                f"l'OOM killer ?) et sa tranche est perdue.")
        if time.monotonic() - last_activity > stall:
            raise PoolStalled(
                f"aucune progression depuis {int(stall)} s : tranche(s) "
                f"perdue(s) ou processus figé(s).")
        time.sleep(0.2)

# Etat partage avec les enfants par HERITAGE de fork (jamais picklé) : pose
# avant la creation du pool, lu par les tranches.
_G = types.SimpleNamespace(index=None, cfg=None, wl=None, rules=None,
                           queue=None, caps_override=None, res_override=None,
                           snapshot_id=None, projection=None, clients=None)


def _child_init():
    """
    Initialisation de chaque enfant, apres fork :
    - jeter le pool de connexions herite SANS fermer les sockets du parent
      (close=False) — les connexions PostgreSQL ne se partagent pas ;
    - reposer les surcharges de contexte (capacites moteur, equivalences)
      capturees dans le parent : les thread-locals du parent ne font pas
      partie du contrat de fork, on les repose explicitement.
    """
    from fiskr import capabilities as caps
    from fiskr import database
    from fiskr import resources

    if database.engine is not None:
        database.engine.dispose(close=False)
    caps._local.override = _G.caps_override
    resources._local.override = _G.res_override
    gc.freeze()  # l'univers herite ne sera jamais collecte : le GC n'a pas a le parcourir


def _screen_chunk(bounds: Tuple[int, int, int]) -> Dict[str, Any]:
    """Crible une tranche de clients [lo, hi] (bornes d'id). Ouvre UNE
    connexion pour charger la tranche, la referme, puis calcule sans base."""
    from fiskr import database
    from fiskr.database import ClientEntity

    lo, hi, chunk_index = bounds
    session = database.SessionLocal()
    try:
        rows = session.query(ClientEntity).filter(
            ClientEntity.snapshot_id == _G.snapshot_id,
            ClientEntity.id >= lo, ClientEntity.id <= hi,
        ).order_by(ClientEntity.id).all()
        clients = [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]
    finally:
        session.close()

    agg = new_partial()
    for i, client in enumerate(clients, start=1):
        apply_outcome(agg, screen_one(client, _G.index, _G.cfg, _G.wl, _G.rules))
        if i % _PROGRESS_EVERY == 0:
            _G.queue.put(_PROGRESS_EVERY)
    if len(clients) % _PROGRESS_EVERY:
        _G.queue.put(len(clients) % _PROGRESS_EVERY)
    agg["_chunk_index"] = chunk_index
    return agg


def parallel_dry_run(db, panel_snapshot_id: str, index, screening_cfg,
                     whitelist_keys, rules, total_clients: int,
                     processes: int,
                     progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    """
    Orchestration du criblage parallele. Prerequis poses par l'appelant :
    index construit, whitelist chargee, regles detachees, `db.rollback()`
    fait (AUCUNE transaction ouverte au fork).
    """
    from fiskr import capabilities as caps
    from fiskr import resources
    from fiskr.database import ClientEntity

    ids = [r[0] for r in db.query(ClientEntity.id).filter(
        ClientEntity.snapshot_id == panel_snapshot_id
    ).order_by(ClientEntity.id).all()]
    db.rollback()
    if not ids:
        return merge_partials([])

    chunk_count = min(len(ids), max(processes * 4, processes))
    step = max(1, len(ids) // chunk_count)
    bounds = []
    for n, start in enumerate(range(0, len(ids), step)):
        chunk_ids = ids[start:start + step]
        bounds.append((chunk_ids[0], chunk_ids[-1], n))

    ctx = multiprocessing.get_context("fork")
    _G.index = index
    _G.cfg = screening_cfg
    _G.wl = whitelist_keys
    _G.rules = list(rules or ())
    _G.snapshot_id = panel_snapshot_id
    _G.caps_override = getattr(caps._local, "override", None)
    _G.res_override = getattr(resources._local, "override", None)
    _G.queue = ctx.SimpleQueue()

    gc.freeze()
    try:
        with ctx.Pool(processes=processes, initializer=_child_init) as pool:
            async_result = pool.map_async(_screen_chunk, bounds)
            _wait_with_watchdog(async_result, _G.queue, pool, total_clients, progress)
            partials = async_result.get()  # propage l'exception d'une tranche
        if progress:
            try:
                progress(total_clients, total_clients)
            except Exception:
                pass
    finally:
        gc.unfreeze()
        _G.index = _G.cfg = _G.wl = _G.rules = _G.queue = None
        _G.caps_override = _G.res_override = None

    partials.sort(key=lambda p: p.pop("_chunk_index"))
    return merge_partials(partials)


def _drain_count(queue) -> int:
    total = 0
    try:
        while not queue.empty():
            total += queue.get()
    except Exception:
        pass
    return total


# ------------------ PHASE DE CALCUL PUR (re-criblage) ------------------
# Le re-criblage post-delta, lui, ECRIT (journal d'audit, alertes). Il ne peut
# donc pas tourner entierement dans des enfants. Mais son travail se scinde
# proprement en deux :
#   1. trouver, pour chaque client, sa meilleure correspondance — du calcul
#      PUR, qui represente la quasi-totalite du temps (la plupart des clients
#      n'ont aucun candidat, et aucun n'ecrit quoi que ce soit) ;
#   2. pour les SEULS clients qui alertent — une poignee — ecrire en base.
# Seule la phase 1 est parallelisee ici ; la phase 2 reste sequentielle dans le
# processus parent, dans l'ordre des clients, donc au resultat identique.


def _match_chunk(bounds: Tuple[int, int, int]) -> Dict[str, Any]:
    """Calcule les correspondances d'une tranche de clients deja en memoire
    (heritee par fork). Ne touche JAMAIS la base. Retourne les seuls clients
    en ALERT, reperes par leur indice dans la liste du parent. Toutes leurs
    correspondances au-dessus du seuil sont remontees, pas seulement la
    meilleure."""
    start, end, chunk_index = bounds
    clients = _G.clients
    cfg = _G.cfg
    index = _G.index
    hits: List[Tuple[int, Dict[str, Any]]] = []
    seen = 0
    for i in range(start, end):
        client = clients[i]
        seen += 1
        if seen % _PROGRESS_EVERY == 0:
            _G.queue.put(_PROGRESS_EVERY)
        candidates: Dict[str, Dict[str, Any]] = {}
        for key in lookup_blocking_keys(client, cfg):
            for ent in index.get(key, []):
                candidates[ent["entity_id"]] = ent
        if not candidates:
            continue
        # TOUTES les correspondances au-dessus du seuil remontent au parent,
        # pas seulement la meilleure : le re-criblage doit laisser autant de
        # traces que le criblage unitaire, sinon une mise en production de
        # liste effacerait des correspondances que le criblage aurait gardees.
        trouvees = []
        for ent in candidates.values():
            score = match_entities(client, ent, config)
            if score.get("status") == "ALERT":
                # La fiche listee n'est PAS jointe : le parent a le meme index
                # en memoire (il l'a transmis par fork), il la rattache par son
                # identifiant. Depuis que toutes les correspondances remontent,
                # la joindre pesait 863 octets par correspondance au lieu de
                # 355 — 1,2 Go de pickle pour 500 clients tres homonymes, la ou
                # 504 Mo suffisent. C'est la meme donnee, transmise une fois.
                score["_entity_id"] = ent.get("entity_id")
                trouvees.append(score)
        if trouvees:
            trouvees.sort(key=lambda s: -s["final_score"])
            hits.append((i, trouvees))
    if seen % _PROGRESS_EVERY:
        _G.queue.put(seen % _PROGRESS_EVERY)
    return {"_chunk_index": chunk_index, "hits": hits}


def parallel_match(clients: Sequence[Dict[str, Any]], index, screening_cfg,
                   processes: int,
                   progress: Optional[Callable[[int, int], None]] = None
                   ) -> List[Tuple[int, Dict[str, Any]]]:
    """
    Phase de calcul du re-criblage, en parallele et EN LECTURE SEULE.

    `clients` et `index` sont deja en memoire : les enfants les heritent par
    fork (copy-on-write), rien n'est ni pickle ni recharge. Retourne la liste
    `(indice du client, correspondances en ALERT triees par score)`,
    **triee par indice** — l'appelant ecrit ensuite en base dans cet ordre,
    exactement comme le ferait la boucle sequentielle.

    L'appelant doit avoir libere sa transaction (`db.rollback()`) avant
    l'appel : aucune transaction ne doit etre ouverte au moment du fork.
    Leve PoolStalled si le pool meurt ou se fige — a l'appelant de replier en
    sequentiel.
    """
    from fiskr import capabilities as caps
    from fiskr import resources

    total = len(clients)
    if total == 0:
        return []

    chunk_count = min(total, max(processes * 4, processes))
    step = max(1, total // chunk_count)
    bounds = [(start, min(start + step, total), n)
              for n, start in enumerate(range(0, total, step))]

    ctx = multiprocessing.get_context("fork")
    _G.clients = clients
    _G.index = index
    _G.cfg = screening_cfg
    _G.caps_override = getattr(caps._local, "override", None)
    _G.res_override = getattr(resources._local, "override", None)
    _G.queue = ctx.SimpleQueue()

    gc.freeze()
    try:
        with ctx.Pool(processes=processes, initializer=_child_init) as pool:
            async_result = pool.map_async(_match_chunk, bounds)
            _wait_with_watchdog(async_result, _G.queue, pool, total, progress)
            partials = async_result.get()  # propage l'exception d'une tranche
    finally:
        gc.unfreeze()
        _G.clients = _G.index = _G.cfg = _G.queue = None
        _G.caps_override = _G.res_override = None

    # Rattachement des fiches listees : les enfants n'ont renvoye que leurs
    # identifiants (cf. `_match_chunk`). L'index du parent est le MEME objet
    # que celui herite par les enfants, donc la fiche rattachee est identique
    # a celle qui a servi au calcul — pas une relecture qui pourrait differer.
    par_id: Dict[str, Dict[str, Any]] = {}
    for bucket in index.values():
        for ent in bucket:
            par_id.setdefault(ent.get("entity_id"), ent)

    hits: List[Tuple[int, Dict[str, Any]]] = []
    for part in sorted(partials, key=lambda p: p["_chunk_index"]):
        for indice, trouvees in part["hits"]:
            for score in trouvees:
                identifiant = score.pop("_entity_id", None)
                fiche = par_id.get(identifiant)
                if fiche is None:
                    # Ne peut pas arriver (l'index est le meme), mais une
                    # correspondance sans sa fiche produirait une ligne d'audit
                    # muette : on la laisse tomber bruyamment plutot que
                    # silencieusement.
                    logger.error(
                        f"Re-criblage : fiche {identifiant!r} absente de l'index "
                        "du parent — correspondance ignorée.")
                    continue
                score["watchlist_entity"] = fiche
            hits.append((indice, [s for s in trouvees if "watchlist_entity" in s]))
    hits.sort(key=lambda h: h[0])
    return [(i, t) for i, t in hits if t]

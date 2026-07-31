"""
Cahier de tests d'homologation (backtest) : criblage A/B A BLANC d'un panel de
pseudo-clients contre la production actuelle ET contre l'univers candidat (le
snapshot en attente remplacant les listes du meme type), pour mesurer l'ecart
de taux d'interception AVANT la promotion. Dry-run strict : aucune alerte ni
ligne d'audit n'est ecrite — la production reste intacte.

Fournit aussi le generateur de panels de pseudo-clients (CLIENT_TEST_PANEL) :
copies exactes de listes (hits attendus), variantes (typos, inversions),
quasi-collisions (meme nom, date de naissance differente) et clients neutres.
Les panels generes sont isoles du referentiel clients reel (file_type dedie,
jamais repris par le re-criblage automatique).
"""
import logging
import random
import time
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from fiskr.config import config
from fiskr.database import Snapshot, ClientEntity, WhitelistPair, compute_checksum
from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys
from fiskr.scoring import match_entities
from fiskr.rescreen import _entity_dicts

logger = logging.getLogger("fiskr.backtest")

TEST_PANEL_FILE_TYPE = "CLIENT_TEST_PANEL"
PANEL_FILE_TYPES = ("CLIENT_BASE", TEST_PANEL_FILE_TYPE)
MAX_PAIR_DETAILS = 200

# Lexique embarque pour les clients neutres (pas de dependance externe)
_NEUTRAL_FIRST_NAMES = [
    "Alice", "Bruno", "Camille", "David", "Emma", "Felix", "Gabrielle", "Hugo",
    "Ines", "Julien", "Karim", "Lea", "Mathieu", "Nadia", "Olivier", "Pauline",
    "Quentin", "Rosa", "Simon", "Theo", "Ursula", "Victor", "William", "Yasmine",
    "Zoe", "Antoine", "Beatrice", "Clement", "Diane", "Etienne", "Fanny", "Gilles",
]
_NEUTRAL_LAST_NAMES = [
    "MARTIN", "BERNARD", "DUBOIS", "THOMAS", "ROBERT", "RICHARD", "PETIT",
    "DURAND", "LEROY", "MOREAU", "SIMON", "LAURENT", "LEFEBVRE", "MICHEL",
    "GARCIA", "ROUX", "FOURNIER", "GIRARD", "LAMBERT", "MERCIER", "BONNET",
    "SCHMIDT", "MUELLER", "JOHNSON", "WILLIAMS", "BROWN", "SILVA", "ROSSI",
]
_NEUTRAL_COMPANIES = [
    "ATELIER DU NORD", "BOULANGERIE CENTRALE", "CABINET HORIZON", "DELTA CONSEIL",
    "ETABLISSEMENTS RIVIERE", "FROMAGERIE DU PARC", "GARAGE SAINT-MICHEL",
    "HOTEL BELLEVUE", "IMPRIMERIE MODERNE", "JARDINS DE PROVENCE",
]
_NEUTRAL_COUNTRIES = ["FR", "DE", "BE", "CH", "ES", "IT", "GB", "US", "NL", "PT"]


# ------------------ UNIVERS A/B ------------------

def _universe_snapshot_ids(db, pending_snap: Snapshot) -> Tuple[List[str], List[str]]:
    """
    (ids production actuelle, ids univers candidat). L'univers candidat est le
    miroir exact d'une approbation : les snapshots READY du meme type sont
    remplaces par le candidat, le snapshot manuel et les autres types restent.
    """
    from fiskr.api import WATCHLIST_FILE_TYPES
    from fiskr.sync import MANUAL_SNAPSHOT_ID  # prefixe commun aux snapshots manuels
    prod = db.query(Snapshot).filter(
        Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
        Snapshot.status == "READY"
    ).all()
    current_ids = [s.snapshot_id for s in prod]
    candidate_ids = [
        s.snapshot_id for s in prod
        if s.file_type != pending_snap.file_type or s.snapshot_id.startswith(MANUAL_SNAPSHOT_ID)
    ]
    candidate_ids.append(pending_snap.snapshot_id)
    return current_ids, candidate_ids


def _panel_clients(db, panel_snapshot_id: str) -> List[Dict[str, Any]]:
    # Ordre deterministe par id : le chemin parallele decoupe le panel par
    # bornes d'id, l'ordre sequentiel doit etre le meme pour que les deux
    # chemins produisent des restitutions identiques (listes bornees a 200)
    rows = db.query(ClientEntity).filter(
        ClientEntity.snapshot_id == panel_snapshot_id
    ).order_by(ClientEntity.id).all()
    return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]


def _client_label(client: Dict[str, Any]) -> str:
    if client.get("client_company_name"):
        return client["client_company_name"]
    return " ".join(p for p in (client.get("client_first_name"), client.get("client_last_name")) if p).strip()


# ------------------ CRIBLAGE A BLANC (DRY-RUN) ------------------

# Cadence des remontees de progression : un tick tous les N clients criblés
_PROGRESS_EVERY = 500

# Cadence des cessions de GIL, DELIBEREMENT bien plus fine que celle de la
# progression : entre deux ticks de progression il peut s'ecouler plusieurs
# secondes, pendant lesquelles l'API ne repondrait pas. Une cession coute
# environ une microseconde ; toutes les 25 fiches, son cout est invisible dans
# la duree totale et le temps de reponse reste celui d'une application au repos.
_YIELD_EVERY = 25


class _RuleSnapshot(NamedTuple):
    """
    Copie detachee d'une regle anti-FP : le criblage a blanc n'a besoin que de
    ces trois champs, et une copie simple survit a la liberation de la session
    (un objet ORM, lui, serait expire par le rollback et rechargerait la base a
    chaque acces).
    """
    id: int
    name: str
    code: str


def _active_whitelist_keys(db) -> Set[Tuple[str, str]]:
    """
    Couples (client, entite) de liste blanche ACTIFS, charges EN UNE REQUETE.

    Le criblage a blanc interrogeait la base pour chaque client en alerte
    (`is_whitelisted`). Outre le cout — une requete par alerte —, ces lectures
    rouvraient une transaction en plein calcul et maintenaient la base occupee
    pendant toute la duree du cahier de tests. La table est une liste curee a la
    main : la charger entierement coute une requete et tient en memoire.

    Meme filtre que `is_whitelisted` (non revoquee, non expiree).
    """
    now = datetime.utcnow()
    rows = db.query(WhitelistPair.client_id, WhitelistPair.watchlist_entity_id).filter(
        WhitelistPair.revoked_at.is_(None),
        (WhitelistPair.expires_at.is_(None)) | (WhitelistPair.expires_at > now)
    ).all()
    return {(r[0], r[1]) for r in rows}


def _dry_run_screen(db, clients: Optional[List[Dict[str, Any]]],
                    entities: List[Dict[str, Any]],
                    rule_set: Optional[List[Any]] = None,
                    progress: Optional[Callable[[int, int], None]] = None,
                    panel_snapshot_id: Optional[str] = None,
                    processes: Optional[int] = None) -> Dict[str, Any]:
    """
    Crible le panel contre un univers d'entites via un index de blocking local.
    Memes seuils par liste, meme liste blanche, meme layout de blocking et
    memes regles anti-faux positifs que la production (match_entities +
    is_whitelisted + rule_set), mais AUCUNE ecriture. Les regles sont
    appliquees en boucle locale fail-open (pas evaluate_fp_rules : pas
    d'increment de hit_count en dry-run, et une regle candidate injectable).

    `progress(traites, total)` est appele tous les 500 clients : un cahier de
    tests sur une vraie base clients dure plusieurs minutes, l'utilisateur doit
    voir ou il en est. Jamais bloquant (meme motif que persist_pivot_items).

    PARALLELISME : avec `panel_snapshot_id` et un dimensionnement > 1
    (jobs.screen_processes, auto par defaut), le panel est decoupe en tranches
    de clients criblees par un pool de processus fork() — l'index est partage
    en copy-on-write, jamais copie. Le corps du criblage (screenpool.screen_one)
    est LE MEME dans les deux chemins : ils ne peuvent pas diverger, et un
    test d'egalite parallele == sequentiel le verrouille.

    DISPONIBILITE DE L'APPLICATION PENDANT LE CALCUL :
    - la base n'est pas interrogee dans la boucle (liste blanche prechargee)
      et la transaction de lecture est refermee avant de commencer ;
    - en chemin sequentiel execute dans le processus API (mode thread), le
      GIL est rendu periodiquement — en processus dedie, c'est sans objet
      mais sans cout.
    """
    from fiskr import screenpool
    from fiskr.settings import blocking_layout, blocking_config_for
    screening_cfg = blocking_config_for(blocking_layout(db, "SCREENING"))

    index: Dict[str, List[Dict[str, Any]]] = {}
    for ent in entities:
        for key in generate_blocking_keys(ent, screening_cfg):
            index.setdefault(key, []).append(ent)

    whitelist_keys = _active_whitelist_keys(db)
    # Les regles sont detachees de la session AVANT de la liberer : un rollback
    # expire les objets ORM, et la boucle rechargerait alors chaque regle depuis
    # la base a chaque client — exactement ce qu'on cherche a eviter.
    rules = [_RuleSnapshot(r.id, r.name, r.code) for r in (rule_set or [])]

    resolved = screenpool.resolve_processes(len(entities), processes)
    if resolved > 1 and panel_snapshot_id:
        # Tout ce dont les tranches ont besoin est en memoire ou recharge par
        # elles ; parallel_dry_run fait son propre rollback avant le fork.
        result = screenpool.parallel_dry_run(
            db, panel_snapshot_id, index, screening_cfg, whitelist_keys, rules,
            total_clients=(len(clients) if clients is not None else _panel_count(db, panel_snapshot_id)),
            processes=resolved, progress=progress)
        return result

    if clients is None:
        clients = _panel_clients(db, panel_snapshot_id)
    # Tout ce dont la boucle a besoin est en memoire : on rend la base a
    # l'application. Sans ce rollback, la transaction de lecture ouverte par les
    # requetes ci-dessus resterait ouverte pendant toute la duree du criblage.
    try:
        db.rollback()
    except Exception:  # session deja libre : sans consequence
        pass

    agg = screenpool.new_partial()
    total_clients = len(clients)
    for done, client in enumerate(clients, start=1):
        if progress and (done % _PROGRESS_EVERY == 0 or done == total_clients):
            try:
                progress(done, total_clients)
            except Exception:
                pass  # une progression cassee n'interrompt jamais un criblage
        # Cession explicite du GIL : utile quand cette boucle tourne dans le
        # processus API (mode thread) ; sans objet mais sans cout en processus
        # dedie.
        if done % _YIELD_EVERY == 0:
            time.sleep(0)
        screenpool.apply_outcome(
            agg, screenpool.screen_one(client, index, screening_cfg,
                                       whitelist_keys, rules))
    return screenpool.merge_partials([agg])


def _panel_count(db, panel_snapshot_id: str) -> int:
    return db.query(ClientEntity).filter(
        ClientEntity.snapshot_id == panel_snapshot_id).count()


def validate_candidate_rule(db, candidate_rule_id: Optional[int]):
    """
    Verifie qu'une regle candidate est evaluable au cahier de tests et la
    retourne (None si aucune n'est demandee). Leve ValueError sinon.

    Extraite de run_backtest pour que l'endpoint valide AVANT de lancer le job
    de fond : un identifiant invalide doit rester un 400 immediat, pas un job
    qui part puis echoue silencieusement.
    """
    from fiskr.database import FpRule

    if not candidate_rule_id:
        return None
    rule = db.query(FpRule).filter(FpRule.id == candidate_rule_id).first()
    if not rule:
        raise ValueError("Règle candidate introuvable.")
    if rule.channel != "SCREENING":
        raise ValueError("Seules les règles du canal criblage peuvent être évaluées au cahier de tests.")
    if rule.status not in ("DRAFT", "PENDING_VALIDATION", "ACTIVE"):
        raise ValueError("Statut de règle non évaluable : brouillon, en validation ou active attendus.")
    return rule


def run_backtest(db, pending_snap: Snapshot, panel_snapshot_id: str,
                 threshold_pct: float, executed_by: str,
                 candidate_rule_id: Optional[int] = None,
                 progress: Optional[Callable[[str, int, int], None]] = None) -> Dict[str, Any]:
    """
    Execute le cahier de tests A/B et retourne le rapport (non persiste ici).

    Les regles anti-FP ACTIVES du canal criblage sont appliquees des deux
    cotes (le cahier de tests reflete la production). Avec candidate_rule_id,
    la regle candidate (DRAFT/PENDING_VALIDATION/ACTIVE, canal SCREENING) est
    ajoutee cote candidat UNIQUEMENT : l'ecart chiffre montre l'effet de la
    regle avant de la soumettre a validation.

    `progress(phase, traites, total)` est relaye par les deux passes, avec les
    phases SCREEN_CURRENT puis SCREEN_CANDIDATE : la barre avance sur toute la
    duree du cahier de tests, pas seulement sur sa seconde moitie.

    Leve ValueError si la regle candidate est invalide (l'endpoint valide en
    amont pour repondre 400 avant de lancer le job).
    """
    import gc as _gc

    from fiskr import screenpool
    from fiskr.fprules import active_rules

    current_ids, candidate_ids = _universe_snapshot_ids(db, pending_snap)
    panel_size = _panel_count(db, panel_snapshot_id)

    current_rules = active_rules(db, "SCREENING")
    candidate_rules = list(current_rules)
    candidate_rule = validate_candidate_rule(db, candidate_rule_id)
    if candidate_rule is not None:
        if not any(r.id == candidate_rule.id for r in candidate_rules):
            candidate_rules.append(candidate_rule)
            candidate_rules.sort(key=lambda r: ((r.run_order if r.run_order is not None else 100), r.id))

    # Fiche de la regle candidate relevee MAINTENANT : les criblages ci-dessous
    # liberent la session, ce qui expire les objets ORM. Le rapport se construit
    # ensuite sans redemander la base.
    candidate_rule_info = ({
        "id": candidate_rule.id,
        "name": candidate_rule.name,
        "version": candidate_rule.version,
        "status": candidate_rule.status,
    } if candidate_rule else None)
    active_rules_count = len(current_rules)

    def _phase_progress(phase: str):
        if not progress:
            return None
        return lambda done, total: progress(phase, done, total)

    # Projection memoire derivee des regles reellement evaluees : les colonnes
    # que le moteur lit, plus celles que le code d'une regle mentionne. C'est
    # ce qui fait passer un univers de 750 000 fiches de 6,3 Go a ~2,8 Go.
    projection = screenpool.projection_for(list(current_rules) + list(candidate_rules))

    # PASSES SEQUENTIELLES : jamais deux univers en memoire en meme temps.
    # A 750 000 fiches, tenir production ET candidat simultanement (~12,6 Go)
    # etait la cause premiere du cahier de tests qui ne se terminait pas.
    current_entities = _entity_dicts(db, current_ids, projection=projection) if current_ids else []
    current = _dry_run_screen(db, None, current_entities, rule_set=current_rules,
                              progress=_phase_progress("SCREEN_CURRENT"),
                              panel_snapshot_id=panel_snapshot_id)
    del current_entities
    _gc.collect()

    candidate_entities = _entity_dicts(db, candidate_ids, projection=projection) if candidate_ids else []
    candidate = _dry_run_screen(db, None, candidate_entities, rule_set=candidate_rules,
                                progress=_phase_progress("SCREEN_CANDIDATE"),
                                panel_snapshot_id=panel_snapshot_id)
    del candidate_entities
    _gc.collect()

    def _rate(alerts: int) -> float:
        return round(alerts * 100.0 / panel_size, 2) if panel_size else 0.0

    new_keys = [k for k in candidate["pairs"] if k not in current["pairs"]]
    resolved_keys = [k for k in current["pairs"] if k not in candidate["pairs"]]

    # Ecart relatif du nombre d'alertes (100 % si on part de zero)
    if current["alerts"] == 0:
        gap_pct = 0.0 if candidate["alerts"] == 0 else 100.0
    else:
        gap_pct = round(abs(candidate["alerts"] - current["alerts"]) * 100.0 / current["alerts"], 2)

    # Meme ecart, calcule AVANT application des regles anti-FP : isole la part
    # de l'ecart imputable a la liste elle-meme vs aux regles
    if current["alerts_before_rules"] == 0:
        gap_pct_before_rules = 0.0 if candidate["alerts_before_rules"] == 0 else 100.0
    else:
        gap_pct_before_rules = round(
            abs(candidate["alerts_before_rules"] - current["alerts_before_rules"]) * 100.0
            / current["alerts_before_rules"], 2)

    return {
        # Cle additive : anciens rapports (sans "rules") toujours valides,
        # le gate d'approbation ne lit que "verdict"
        "rules": {
            "active_count": active_rules_count,
            "candidate_rule": candidate_rule_info,
            "current_suppressed": current["rule_suppressed"],
            "candidate_suppressed": candidate["rule_suppressed"],
            "suppressed_delta": candidate["rule_suppressed"] - current["rule_suppressed"],
            "candidate_suppressed_pairs": candidate["rule_suppressed_pairs"],
            "gap_pct_before_rules": gap_pct_before_rules,
        },
        "panel_snapshot_id": panel_snapshot_id,
        "panel_size": panel_size,
        "current": {
            "alerts": current["alerts"],
            "interception_rate_pct": _rate(current["alerts"]),
            "whitelisted_suppressed": current["whitelisted_suppressed"],
            "alerts_before_rules": current["alerts_before_rules"],
            "rule_suppressed": current["rule_suppressed"],
        },
        "candidate": {
            "alerts": candidate["alerts"],
            "interception_rate_pct": _rate(candidate["alerts"]),
            "whitelisted_suppressed": candidate["whitelisted_suppressed"],
            "alerts_before_rules": candidate["alerts_before_rules"],
            "rule_suppressed": candidate["rule_suppressed"],
        },
        "gap_pct": gap_pct,
        "threshold_pct": threshold_pct,
        "verdict": "WARN" if gap_pct > threshold_pct else "OK",
        "new_pairs_count": len(new_keys),
        "resolved_pairs_count": len(resolved_keys),
        "new_pairs": [candidate["pairs"][k] for k in new_keys[:MAX_PAIR_DETAILS]],
        "resolved_pairs": [current["pairs"][k] for k in resolved_keys[:MAX_PAIR_DETAILS]],
        "executed_by": executed_by,
        "executed_at": datetime.utcnow().isoformat() + "Z",
    }


# ------------------ GENERATEUR DE PANEL DE PSEUDO-CLIENTS ------------------

def _typo(rng: random.Random, name: str) -> str:
    """Inverse deux lettres adjacentes du nom (typo de saisie realiste)."""
    if len(name) < 4:
        return name + "E"
    i = rng.randint(1, len(name) - 3)
    return name[:i] + name[i + 1] + name[i] + name[i + 2:]


def _entity_to_client(rng: random.Random, ent: Dict[str, Any], idx: int,
                      variant: str) -> Dict[str, Any]:
    """Derive un pseudo-client d'une entite listee (hit exact, variante ou quasi-collision)."""
    parsed = ent.get("individual_name_parsed") or {}
    first = (parsed.get("first_name") or "").strip()
    last = (parsed.get("last_name") or "").strip()
    if not (first or last):
        parts = (ent.get("primary_name") or "").split(" ", 1)
        first, last = parts[0], (parts[1] if len(parts) > 1 else parts[0])

    is_individual = ent.get("entity_type") == "I"
    dobs = ent.get("dates_of_birth") or []
    dob = dobs[0] if dobs else None
    countries = ent.get("countries") or {}
    nationality = list(countries.get("citizenship") or [])

    if variant == "typo":
        last = _typo(rng, last or (ent.get("primary_name") or "X"))
    elif variant == "swap":
        first, last = last, first
    elif variant == "near":
        # Quasi-collision : meme nom, date de naissance decalee -> devrait
        # rester sous le seuil ou etre discrimine par le malus DOB
        if dob and len(dob) >= 4 and dob[:4].isdigit():
            dob = f"{int(dob[:4]) + 17}{dob[4:]}"
        else:
            dob = "1990-01-01"

    client = {
        "client_id": f"TEST-{variant.upper()}-{idx:05d}",
        "client_type": "PP" if is_individual else "PM",
        "client_first_name": first if is_individual else None,
        "client_last_name": last if is_individual else None,
        "client_maiden_name": None,
        "client_company_name": None if is_individual else (ent.get("primary_name") or last),
        "client_dob": dob if is_individual else None,
        "client_gender": ent.get("gender") or "U",
        "client_is_deceased": False,
        "client_countries": {
            "nationality": nationality, "residence": [],
            "birth_country": [], "registration_country": nationality if not is_individual else [],
        },
    }
    return client


def _neutral_client(rng: random.Random, idx: int) -> Dict[str, Any]:
    is_individual = rng.random() < 0.85
    country = rng.choice(_NEUTRAL_COUNTRIES)
    if is_individual:
        return {
            "client_id": f"TEST-NEUTRE-{idx:05d}",
            "client_type": "PP",
            "client_first_name": rng.choice(_NEUTRAL_FIRST_NAMES),
            "client_last_name": rng.choice(_NEUTRAL_LAST_NAMES),
            "client_maiden_name": None,
            "client_company_name": None,
            "client_dob": f"{rng.randint(1950, 2005)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
            "client_gender": rng.choice(["M", "F"]),
            "client_is_deceased": False,
            "client_countries": {"nationality": [country], "residence": [country],
                                 "birth_country": [], "registration_country": []},
        }
    return {
        "client_id": f"TEST-NEUTRE-{idx:05d}",
        "client_type": "PM",
        "client_first_name": None,
        "client_last_name": None,
        "client_maiden_name": None,
        "client_company_name": f"{rng.choice(_NEUTRAL_COMPANIES)} {rng.randint(1, 999)}",
        "client_dob": None,
        "client_gender": "U",
        "client_is_deceased": False,
        "client_countries": {"nationality": [], "residence": [],
                             "birth_country": [], "registration_country": [country]},
    }


def generate_test_panel(db, source_snapshot_ids: List[str], size: int = 500,
                        seed: Optional[int] = None, created_by: str = "reviewer") -> Snapshot:
    """
    Genere un panel de pseudo-clients derive des entites des snapshots sources
    (candidat + production) : ~10 % de copies exactes (hits attendus), ~10 % de
    variantes (typos, inversions prenom/nom), ~10 % de quasi-collisions (meme
    nom, DOB differente) et ~70 % de clients neutres. Stocke en
    CLIENT_TEST_PANEL : jamais repris par le re-criblage du referentiel reel.
    """
    rng = random.Random(seed)
    entities = [e for e in _entity_dicts(db, source_snapshot_ids) if e.get("primary_name")]
    if not entities:
        raise ValueError("Aucune entité exploitable dans les snapshots sources pour générer le panel.")

    n_hits = max(1, size // 10)
    n_typos = max(1, size // 10)
    n_near = max(1, size // 10)
    n_neutral = max(0, size - n_hits - n_typos - n_near)

    clients: List[Dict[str, Any]] = []
    for i in range(n_hits):
        clients.append(_entity_to_client(rng, rng.choice(entities), i, "hit"))
    for i in range(n_typos):
        variant = "typo" if i % 2 == 0 else "swap"
        clients.append(_entity_to_client(rng, rng.choice(entities), i, variant))
    for i in range(n_near):
        clients.append(_entity_to_client(rng, rng.choice(entities), i, "near"))
    for i in range(n_neutral):
        clients.append(_neutral_client(rng, i))

    snap = Snapshot(
        snapshot_id=str(uuid.uuid4()),
        file_type=TEST_PANEL_FILE_TYPE,
        file_name=f"panel-test-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{len(clients)}.gen",
        file_hash=uuid.uuid4().hex,
        record_count=len(clients),
        status="READY",
    )
    db.add(snap)
    db.flush()

    for c in clients:
        db.add(ClientEntity(snapshot_id=snap.snapshot_id, entity_checksum=compute_checksum(c), **c))
    db.commit()
    db.refresh(snap)
    logger.info(
        f"Panel de test genere par {created_by} : {len(clients)} pseudo-clients "
        f"({n_hits} hits, {n_typos} variantes, {n_near} quasi-collisions, {n_neutral} neutres)."
    )
    return snap

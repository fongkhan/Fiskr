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
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fiskr.config import config
from fiskr.database import Snapshot, ClientEntity, compute_checksum
from fiskr.blocking import generate_blocking_keys
from fiskr.scoring import match_entities
from fiskr.alerts import is_whitelisted
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
    from fiskr.sync import MANUAL_SNAPSHOT_ID
    prod = db.query(Snapshot).filter(
        Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
        Snapshot.status == "READY"
    ).all()
    current_ids = [s.snapshot_id for s in prod]
    candidate_ids = [
        s.snapshot_id for s in prod
        if s.file_type != pending_snap.file_type or s.snapshot_id == MANUAL_SNAPSHOT_ID
    ]
    candidate_ids.append(pending_snap.snapshot_id)
    return current_ids, candidate_ids


def _panel_clients(db, panel_snapshot_id: str) -> List[Dict[str, Any]]:
    rows = db.query(ClientEntity).filter(ClientEntity.snapshot_id == panel_snapshot_id).all()
    return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]


def _client_label(client: Dict[str, Any]) -> str:
    if client.get("client_company_name"):
        return client["client_company_name"]
    return " ".join(p for p in (client.get("client_first_name"), client.get("client_last_name")) if p).strip()


# ------------------ CRIBLAGE A BLANC (DRY-RUN) ------------------

def _dry_run_screen(db, clients: List[Dict[str, Any]],
                    entities: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Crible le panel contre un univers d'entites via un index de blocking local.
    Memes seuils par liste et meme liste blanche que la production
    (match_entities + is_whitelisted), mais AUCUNE ecriture.
    """
    index: Dict[str, List[Dict[str, Any]]] = {}
    for ent in entities:
        for key in generate_blocking_keys(ent, config):
            index.setdefault(key, []).append(ent)

    pairs: Dict[Tuple[str, str], Dict[str, Any]] = {}
    whitelisted_suppressed = 0

    for client in clients:
        candidates: Dict[str, Dict[str, Any]] = {}
        for key in generate_blocking_keys(client, config):
            for ent in index.get(key, []):
                candidates[ent["entity_id"]] = ent
        if not candidates:
            continue

        best = None
        best_ent = None
        for ent in candidates.values():
            score = match_entities(client, ent, config)
            if best is None or score["final_score"] > best["final_score"]:
                best = score
                best_ent = ent

        if not best or best.get("status") != "ALERT":
            continue

        if is_whitelisted(db, client.get("client_id"), best_ent.get("entity_id")):
            whitelisted_suppressed += 1
            continue

        pairs[(client.get("client_id"), best_ent.get("entity_id"))] = {
            "client_id": client.get("client_id"),
            "client_name": _client_label(client),
            "entity_id": best_ent.get("entity_id"),
            "entity_name": best_ent.get("primary_name"),
            "list_type": best_ent.get("_list_type"),
            "score": round(float(best.get("final_score", 0)), 2),
        }

    return {"alerts": len(pairs), "pairs": pairs, "whitelisted_suppressed": whitelisted_suppressed}


def run_backtest(db, pending_snap: Snapshot, panel_snapshot_id: str,
                 threshold_pct: float, executed_by: str) -> Dict[str, Any]:
    """
    Execute le cahier de tests A/B et retourne le rapport (non persiste ici).
    """
    clients = _panel_clients(db, panel_snapshot_id)
    current_ids, candidate_ids = _universe_snapshot_ids(db, pending_snap)
    current_entities = _entity_dicts(db, current_ids) if current_ids else []
    candidate_entities = _entity_dicts(db, candidate_ids) if candidate_ids else []

    current = _dry_run_screen(db, clients, current_entities)
    candidate = _dry_run_screen(db, clients, candidate_entities)

    panel_size = len(clients)

    def _rate(alerts: int) -> float:
        return round(alerts * 100.0 / panel_size, 2) if panel_size else 0.0

    new_keys = [k for k in candidate["pairs"] if k not in current["pairs"]]
    resolved_keys = [k for k in current["pairs"] if k not in candidate["pairs"]]

    # Ecart relatif du nombre d'alertes (100 % si on part de zero)
    if current["alerts"] == 0:
        gap_pct = 0.0 if candidate["alerts"] == 0 else 100.0
    else:
        gap_pct = round(abs(candidate["alerts"] - current["alerts"]) * 100.0 / current["alerts"], 2)

    return {
        "panel_snapshot_id": panel_snapshot_id,
        "panel_size": panel_size,
        "current": {
            "alerts": current["alerts"],
            "interception_rate_pct": _rate(current["alerts"]),
            "whitelisted_suppressed": current["whitelisted_suppressed"],
        },
        "candidate": {
            "alerts": candidate["alerts"],
            "interception_rate_pct": _rate(candidate["alerts"]),
            "whitelisted_suppressed": candidate["whitelisted_suppressed"],
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

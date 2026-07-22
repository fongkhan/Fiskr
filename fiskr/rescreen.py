"""
Re-criblage automatique post-delta : des qu'un snapshot de liste entre en
production (sync, upload manuel ou approbation d'homologation), le referentiel
clients est re-crible contre les SEULES entites nouvelles ou modifiees.
Les nouveaux hits ouvrent des alertes de travail (dedupliquees) ; les paires
en liste blanche sont supprimees de facon tracee (statut WHITELISTED dans le
journal d'audit). Fournit aussi le lookback manuel (guidance Wolfsberg).
"""
import logging
from typing import Any, Dict, List, Optional

from fiskr.config import config
from fiskr.database import (
    Snapshot, WatchlistEntity, ClientEntity, log_compliance_decision
)
from fiskr.blocking import generate_blocking_keys
from fiskr.scoring import match_entities
from fiskr.alerts import open_or_redetect_alert, is_whitelisted

logger = logging.getLogger("fiskr.rescreen")

RESCREEN_USERNAME = "rescreen-auto"


def _entity_dicts(db, snapshot_ids: List[str]) -> List[Dict[str, Any]]:
    snapshot_types = {
        s.snapshot_id: s.file_type
        for s in db.query(Snapshot).filter(Snapshot.snapshot_id.in_(snapshot_ids)).all()
    }
    rows = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_(snapshot_ids),
        WatchlistEntity.excluded.isnot(True)
    ).all()
    out = []
    for r in rows:
        d = {c.name: getattr(r, c.name) for c in r.__table__.columns}
        # Type de liste d'origine : seuils de cut-off par liste
        d["_list_type"] = snapshot_types.get(r.snapshot_id)
        out.append(d)
    return out


def _client_dicts(db) -> List[Dict[str, Any]]:
    """Referentiel clients : entites des snapshots CLIENT_BASE en production."""
    snap_ids = [
        s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_type == "CLIENT_BASE",
            Snapshot.status == "READY"
        ).all()
    ]
    if not snap_ids:
        return []
    rows = db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(snap_ids)).all()
    return [{c.name: getattr(r, c.name) for c in r.__table__.columns} for r in rows]


def _screen_clients_against(db, changed_entities: List[Dict[str, Any]],
                            trigger_detail: str) -> Dict[str, int]:
    """
    Crible le referentiel clients contre un ensemble borne d'entites (index de
    blocking local). Retourne les compteurs du run.
    """
    result = {
        "changed_entities": len(changed_entities),
        "clients_screened": 0,
        "new_alerts": 0,
        "whitelisted_suppressed": 0,
    }
    if not changed_entities:
        return result

    # Layout de blocking du canal criblage (parametrable a chaud)
    from fiskr.settings import blocking_layout, blocking_config_for
    screening_cfg = blocking_config_for(blocking_layout(db, "SCREENING"))

    index: Dict[str, List[Dict[str, Any]]] = {}
    for ent in changed_entities:
        for key in generate_blocking_keys(ent, screening_cfg):
            index.setdefault(key, []).append(ent)

    from fiskr.api import watchlist_version, watchlist_hash  # valeurs de version du cache actif

    for client in _client_dicts(db):
        result["clients_screened"] += 1
        candidates: Dict[str, Dict[str, Any]] = {}
        for key in generate_blocking_keys(client, screening_cfg):
            for ent in index.get(key, []):
                candidates[ent["entity_id"]] = ent
        if not candidates:
            continue

        best = None
        for ent in candidates.values():
            score = match_entities(client, ent, config)
            score["watchlist_entity"] = ent
            if best is None or score["final_score"] > best["final_score"]:
                best = score

        if not best or best.get("status") != "ALERT":
            continue

        pair = is_whitelisted(db, client.get("client_id"), best["watchlist_entity"].get("entity_id"))
        if pair:
            best["status"] = "WHITELISTED"
            best["whitelist_pair_id"] = pair.id
            log_compliance_decision(db, client, best["watchlist_entity"], best,
                                    watchlist_version, watchlist_hash)
            result["whitelisted_suppressed"] += 1
            continue

        # Regles anti-faux positifs du canal SCREENING
        from fiskr.fprules import evaluate_fp_rules, build_screening_ctx, annotate_suppression
        ctx = build_screening_ctx(client, best["watchlist_entity"], best)
        suppressed_by_rule = evaluate_fp_rules(db, "SCREENING", ctx)
        if suppressed_by_rule is not None:
            annotate_suppression(best, suppressed_by_rule)

        audit = log_compliance_decision(db, client, best["watchlist_entity"], best,
                                        watchlist_version, watchlist_hash)
        open_or_redetect_alert(
            db, audit, client.get("client_id"), best, RESCREEN_USERNAME,
            channel="SCREENING", suppressed_by_rule=suppressed_by_rule,
            detail_suffix=f" {trigger_detail}"
        )
        if suppressed_by_rule is not None:
            result["rule_suppressed"] = result.get("rule_suppressed", 0) + 1
        else:
            result["new_alerts"] += 1

    logger.info(
        f"Re-criblage ({trigger_detail}) : {result['changed_entities']} entités changées, "
        f"{result['clients_screened']} clients criblés, {result['new_alerts']} nouvelle(s) alerte(s), "
        f"{result['whitelisted_suppressed']} supprimée(s) par liste blanche."
    )
    return result


def rescreen_after_snapshot_change(db, file_type: str, new_snapshot_id: str,
                                   previous_snapshot_id: Optional[str] = None) -> Dict[str, int]:
    """
    Re-crible le referentiel clients contre les entites du nouveau snapshot
    qui sont nouvelles ou modifiees par rapport au precedent (comparaison des
    checksums). Sans snapshot precedent, tout le snapshot est considere.
    """
    new_entities = _entity_dicts(db, [new_snapshot_id])
    if previous_snapshot_id:
        previous_checksums = {
            row[0] for row in db.query(WatchlistEntity.entity_checksum).filter(
                WatchlistEntity.snapshot_id == previous_snapshot_id
            ).all()
        }
        changed = [e for e in new_entities if e["entity_checksum"] not in previous_checksums]
    else:
        changed = new_entities
    return _screen_clients_against(
        db, changed,
        trigger_detail=f"[Re-criblage automatique après mise à jour {file_type}]"
    )


def rescreen_lookback(db, file_type: Optional[str] = None) -> Dict[str, int]:
    """
    Lookback manuel : re-crible le referentiel clients contre TOUTES les
    entites en production (d'un type de liste, ou de tous les types watchlist).
    """
    from fiskr.api import WATCHLIST_FILE_TYPES
    types = [file_type] if file_type else WATCHLIST_FILE_TYPES
    snap_ids = [
        s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_type.in_(types),
            Snapshot.status == "READY"
        ).all()
    ]
    if not snap_ids:
        return {"changed_entities": 0, "clients_screened": 0, "new_alerts": 0, "whitelisted_suppressed": 0}
    entities = _entity_dicts(db, snap_ids)
    label = file_type or "toutes listes"
    return _screen_clients_against(db, entities, trigger_detail=f"[Lookback manuel {label}]")

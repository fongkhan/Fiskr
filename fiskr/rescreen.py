"""
Re-criblage automatique post-delta : des qu'un snapshot de liste entre en
production (sync, upload manuel ou approbation d'homologation), le referentiel
clients est re-crible contre les SEULES entites nouvelles ou modifiees.
Les nouveaux hits ouvrent des alertes de travail (dedupliquees) ; les paires
en liste blanche sont supprimees de facon tracee (statut WHITELISTED dans le
journal d'audit). Fournit aussi le lookback manuel (guidance Wolfsberg).
"""
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Set

from fiskr.config import config
from fiskr.database import (
    Snapshot, WatchlistEntity, ClientEntity, log_compliance_decision
)
from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys
from fiskr.scoring import match_entities
from fiskr.alerts import open_or_redetect_alert, is_whitelisted
from fiskr import screenpool

logger = logging.getLogger("fiskr.rescreen")

RESCREEN_USERNAME = "rescreen-auto"

# Cadence des remontees de progression (un tick tous les N clients criblés)
_PROGRESS_EVERY = 500

# Cadence des cessions de GIL (voir backtest.py) : bien plus fine que celle de
# la progression, sans quoi l'API reste muette entre deux ticks.
_YIELD_EVERY = 25


def _entity_dicts(db, snapshot_ids: List[str],
                  projection: Optional[tuple] = None,
                  entity_ids: Optional[Set[str]] = None) -> List[Dict[str, Any]]:
    """
    Fiches listees des snapshots demandes, en dicts.

    `projection` : liste de colonnes a charger (cf. screenpool.ENTITY_PROJECTION).
    A 750 000 fiches, le dict complet pese ~8,4 Ko/fiche (6,3 Go l'univers) ;
    la projection reduit a ~3,8 Ko/fiche en ne chargeant que ce que le moteur
    lit — ET evite la materialisation d'objets ORM (tuples directs).
    None = comportement historique, toutes colonnes.

    `entity_ids` : restreint aux entity_id demandes (passes DELTA du cahier de
    tests : seules les fiches ajoutees/modifiees/retirees sont chargees). Le
    IN est decoupe en tranches — SQLite plafonne les parametres d'une requete.
    """
    snapshot_types = {
        s.snapshot_id: s.file_type
        for s in db.query(Snapshot).filter(Snapshot.snapshot_id.in_(snapshot_ids)).all()
    }
    id_chunks: List[Optional[List[str]]] = [None]
    if entity_ids is not None:
        if not entity_ids:
            return []
        ordered = sorted(entity_ids)
        id_chunks = [ordered[i:i + 800] for i in range(0, len(ordered), 800)]

    out = []
    if projection:
        fields = [f for f in projection if f != "_list_type"]
        if "snapshot_id" not in fields:
            fields.append("snapshot_id")
        columns = [getattr(WatchlistEntity, f) for f in fields]
        for chunk in id_chunks:
            query = db.query(*columns).filter(
                WatchlistEntity.snapshot_id.in_(snapshot_ids),
                WatchlistEntity.excluded.isnot(True)
            )
            if chunk is not None:
                query = query.filter(WatchlistEntity.entity_id.in_(chunk))
            for row in query.yield_per(5000):
                d = dict(zip(fields, row))
                d["_list_type"] = snapshot_types.get(d.get("snapshot_id"))
                out.append(d)
        return out
    for chunk in id_chunks:
        query = db.query(WatchlistEntity).filter(
            WatchlistEntity.snapshot_id.in_(snapshot_ids),
            WatchlistEntity.excluded.isnot(True)
        )
        if chunk is not None:
            query = query.filter(WatchlistEntity.entity_id.in_(chunk))
        for r in query.all():
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
                            trigger_detail: str,
                            progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, int]:
    """
    Crible le referentiel clients contre un ensemble borne d'entites (index de
    blocking local). Retourne les compteurs du run.

    `progress(traites, total)` est appele tous les 500 clients : le re-criblage
    qui suit une approbation d'homologation peut durer plusieurs minutes sur un
    gros referentiel. Jamais bloquant.
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

    # Reference des listes en production DERIVEE DE LA BASE : ce re-criblage
    # tourne desormais dans le demon travailleur, qui ne voit pas le cache
    # memoire du processus API — importer ses globales y trace « N/A » au
    # journal d'audit immuable, ce qui est faux. La base fait foi partout.
    from fiskr.database import production_watchlist_reference
    watchlist_version, watchlist_hash = production_watchlist_reference(db)

    # Rarete des mots de nom : construite ICI, dans le parent, AVANT le fork du
    # pool — les enfants en heritent. Sans cet appel le demon travailleur n'a
    # pas de table, et une regle fondee sur la rarete cloturerait au criblage
    # unitaire (processus API) mais pas au re-criblage : deux verites.
    from fiskr import rarete
    rarete.table_pour(db)

    clients = _client_dicts(db)
    total_clients = len(clients)
    result["clients_screened"] = total_clients

    def _persist_hits(client: Dict[str, Any], hits: List[Dict[str, Any]]) -> None:
        """Ecrit en base la suite de TOUTES les correspondances en ALERT d'un
        client : liste blanche, regles anti-FP, journal d'audit immuable,
        alertes de travail. Toujours appelee dans le processus PARENT, dans
        l'ordre des clients — les enfants du pool ne font que calculer.

        Le re-criblage ne gardait que la MEILLEURE correspondance par client.
        Une mise en production de liste effacait donc des correspondances que
        le criblage unitaire, lui, conserve : deux canaux, deux verites. Elles
        sont desormais toutes ecrites, ici comme ailleurs.
        """
        from fiskr.fprules import evaluate_fp_rules, build_screening_ctx, annotate_suppression
        from fiskr.alerts import whitelisted_pairs, open_or_redetect_alerts
        from fiskr.database import log_compliance_decisions

        client_ref = client.get("client_id")
        hits = sorted(hits, key=lambda h: -h["final_score"])
        paires = whitelisted_pairs(
            db, client_ref,
            [(h.get("watchlist_entity") or {}).get("entity_id") for h in hits])

        from fiskr.fprules import active_rules
        from fiskr.settings import perimeter_overrides
        regles_actives = active_rules(db, "SCREENING")
        classement = perimeter_overrides(db)

        a_journaliser = []
        for position, hit in enumerate(hits, start=1):
            fiche = hit.get("watchlist_entity") or {}
            paire = paires.get(fiche.get("entity_id"))
            if paire is not None:
                hit["status"] = "WHITELISTED"
                hit["whitelist_pair_id"] = paire.id
                result["whitelisted_suppressed"] += 1
                a_journaliser.append((hit, None))
                continue
            ctx = build_screening_ctx(client, fiche, hit,
                                      hits_count=len(hits), hit_rank=position,
                                      perimeter_overrides=classement)
            hit["screening_perimeter"] = ctx["perimeter"]
            regle = evaluate_fp_rules(db, "SCREENING", ctx, rules=regles_actives)
            if regle is not None:
                annotate_suppression(hit, regle)
                result["rule_suppressed"] = result.get("rule_suppressed", 0) + 1
            else:
                result["new_alerts"] += 1
            a_journaliser.append((hit, regle))

        lignes = log_compliance_decisions(
            db, client,
            [(h.get("watchlist_entity") or {}, h) for h, _r in a_journaliser],
            watchlist_version, watchlist_hash, commit=False)
        a_alerter = [{"audit": ligne, "match": h, "rule": r}
                     for (h, r), ligne in zip(a_journaliser, lignes)
                     if h.get("status") == "ALERT"]
        if not a_alerter:
            db.commit()
        if a_alerter:
            open_or_redetect_alerts(
                db, a_alerter, client_id=client_ref, username=RESCREEN_USERNAME,
                channel="SCREENING", detail_suffix=f" {trigger_detail}")

    # ---- Phase 1 : trouver les correspondances (calcul pur, parallelisable) ----
    # Le re-criblage repasse TOUTE la base clients apres chaque mise en
    # production : c'est le criblage le plus frequent, et il etait entierement
    # sequentiel. Le calcul part maintenant sur un pool de processus forkes ;
    # seules les rares correspondances en ALERT reviennent au parent, qui garde
    # l'ecriture pour lui. Resultat identique, dans le meme ordre.
    resolved = screenpool.resolve_processes(len(clients) + len(changed_entities))
    hits = None
    if resolved > 1 and total_clients:
        try:
            db.rollback()  # aucune transaction ouverte au moment du fork
        except Exception:
            pass
        try:
            hits = screenpool.parallel_match(clients, index, screening_cfg,
                                             processes=resolved, progress=progress)
        except screenpool.PoolStalled as e:
            # Auto-guerison : pool mort (OOM) ou fige — on recalcule en
            # sequentiel dans CE processus plutot que d'abandonner le run.
            logger.warning(f"Re-criblage parallèle interrompu : {e} Repli séquentiel.")
            hits = None

    if hits is not None:
        # ---- Phase 2 : ecriture, sequentielle, dans l'ordre des clients ----
        for i, trouvees in hits:
            _persist_hits(clients[i], trouvees)
        if progress:
            try:
                progress(total_clients, total_clients)
            except Exception:
                pass
    else:
        # Chemin sequentiel historique (un seul coeur, pool indisponible ou
        # replie) : calcul et ecriture entrelaces, meme resultat.
        done = 0
        for client in clients:
            done += 1
            if progress and (done % _PROGRESS_EVERY == 0 or done == total_clients):
                try:
                    progress(done, total_clients)
                except Exception:
                    pass  # une progression cassee n'interrompt jamais un re-criblage
            # Cession explicite du GIL : sans elle, ce calcul en Python pur affame
            # la boucle d'evenements et l'API cesse de repondre pendant tout le
            # re-criblage (meme motif que le criblage a blanc).
            if done % _YIELD_EVERY == 0:
                time.sleep(0)
            candidates: Dict[str, Dict[str, Any]] = {}
            for key in lookup_blocking_keys(client, screening_cfg):
                for ent in index.get(key, []):
                    candidates[ent["entity_id"]] = ent
            if not candidates:
                continue

            trouvees = []
            for ent in candidates.values():
                score = match_entities(client, ent, config)
                score["watchlist_entity"] = ent
                if score.get("status") == "ALERT":
                    trouvees.append(score)

            if not trouvees:
                continue
            _persist_hits(client, trouvees)

    logger.info(
        f"Re-criblage ({trigger_detail}) : {result['changed_entities']} entités changées, "
        f"{result['clients_screened']} clients criblés, {result['new_alerts']} nouvelle(s) alerte(s), "
        f"{result['whitelisted_suppressed']} supprimée(s) par liste blanche."
    )
    return result


def rescreen_after_snapshot_change(db, file_type: str, new_snapshot_id: str,
                                   previous_snapshot_id: Optional[str] = None,
                                   progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, int]:
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
        trigger_detail=f"[Re-criblage automatique après mise à jour {file_type}]",
        progress=progress,
    )


def rescreen_lookback(db, file_type: Optional[str] = None,
                      progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, int]:
    """
    Lookback manuel : re-crible le referentiel clients contre TOUTES les
    entites en production (d'un type de liste, ou de tous les types watchlist).
    Instrumente (`progress`) : c'est l'operation la plus lourde du produit,
    elle est desormais executee en tache de fond et suivie par jeton.
    """
    from fiskr.database import WATCHLIST_FILE_TYPES
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
    return _screen_clients_against(db, entities,
                                   trigger_detail=f"[Lookback manuel {label}]",
                                   progress=progress)

"""
Re-criblage automatique post-delta : des qu'un snapshot de liste entre en
production (sync, upload manuel ou approbation d'homologation), le referentiel
clients est re-crible contre les SEULES entites nouvelles ou modifiees.
Les nouveaux hits ouvrent des alertes de travail (dedupliquees) ; les paires
en liste blanche sont supprimees de facon tracee (statut WHITELISTED dans le
journal d'audit) TANT QUE la fiche et le client sont ceux de leur decision —
une fiche modifiee rouvre l'alerte (fiskr.empreintes). Fournit aussi le
lookback manuel (guidance Wolfsberg) et le criblage des clients nouveaux ou
modifies apres un import de referentiel.

CHAQUE PASSAGE LAISSE UNE TRACE PAR CLIENT
------------------------------------------
Le journal d'audit n'ecrit une ligne que pour une correspondance. Un lookback
sur trois clients propres annoncait donc « 3 clients criblés » sans laisser une
seule preuve, et la couverture — mesuree sur ce journal — restait « 3 jamais
criblés » apres chaque relance. Ecrire une ligne NO_MATCH par client n'est pas
tenable : mesure, 5,5 Ko la ligne (dont 4,7 Ko d'etat de configuration),
soit 4,2 Go et plus de trois minutes d'ecriture par passage a 750 000 clients.

Un passage ecrit donc UNE ligne (`ScreeningPass` : reference de l'univers,
perimetre, effectifs, horodatages) et date chaque fiche criblee par des mises
a jour en masse (`ClientEntity.screened_at`, `screening_pass_id`). Les
correspondances, elles, gardent leur ligne d'audit complete, comme avant.
"""
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from fiskr.config import config
from fiskr.database import (
    Snapshot, WatchlistEntity, ClientEntity, ScreeningPass,
    CLIENT_SCREENING_TRACE_COLUMNS, log_compliance_decision
)
from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys
from fiskr.scoring import match_entities
from fiskr.alerts import is_whitelisted
from fiskr import screenpool

logger = logging.getLogger("fiskr.rescreen")

RESCREEN_USERNAME = "rescreen-auto"

# Cadence des remontees de progression (un tick tous les N clients criblés)
_PROGRESS_EVERY = 500

# Cadence des cessions de GIL (voir backtest.py) : bien plus fine que celle de
# la progression, sans quoi l'API reste muette entre deux ticks.
_YIELD_EVERY = 25


def reference_de_l_univers(db) -> tuple:
    """
    (version, empreinte, composition) de l'univers en production : CONTRE QUOI
    un passage crible. POINT UNIQUE de lecture pour les passages, leurs lignes
    d'audit, la couverture et les references « avant mise a jour » relevees par
    l'approbation, la synchronisation et l'import.

    La definition est celle de fiskr/univers.py, que lisent aussi le journal
    d'audit, l'index de filtrage et la table de rarete : un passage cite le
    meme univers que les decisions qu'il ecrit.
    """
    from fiskr import univers
    reference = univers.reference_en_production(db)
    return reference.version, reference.empreinte, reference.composition_journal()


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


def _client_dicts(db, snapshot_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Referentiel clients : entites des snapshots CLIENT_BASE en production
    (ou des seuls `snapshot_ids`). Sans les colonnes de trace : elles disent
    quand la fiche a ete criblee, elles ne font pas partie de ce qu'on crible."""
    snap_ids = snapshot_ids if snapshot_ids is not None else [
        s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_type == "CLIENT_BASE",
            Snapshot.status == "READY"
        ).all()
    ]
    if not snap_ids:
        return []
    rows = db.query(ClientEntity).filter(
        ClientEntity.snapshot_id.in_(snap_ids)).order_by(ClientEntity.id).all()
    return [{c.name: getattr(r, c.name) for c in r.__table__.columns
             if c.name not in CLIENT_SCREENING_TRACE_COLUMNS} for r in rows]


def compteurs_de_recriblage(changed_entities: int = 0) -> Dict[str, int]:
    """
    Forme UNIQUE du compte rendu d'un re-criblage.

    Elle est definie ici et nulle part ailleurs parce qu'elle l'etait a deux
    endroits qui avaient deja diverge : le retour a vide de `rescreen_lookback`
    ne portait pas `rule_suppressed`, et le compteur lui-meme n'apparaissait
    dans le resultat QUE si une regle avait tranche. Un destinataire ne pouvait
    pas distinguer « aucune regle n'a joue » de « personne ne me l'a dit ».

    Les trois comptes d'alertes viennent de ce que `open_or_redetect_alerts` a
    reellement fait, jamais d'un decompte parallele :
      - `new_alerts`        : alertes OUVERTES par ce passage ;
      - `redetected_alerts` : alertes deja existantes, re-confirmees ;
      - `closed_by_rule`    : ouvertes puis closes par une regle, en clair.

    `whitelist_stale` : correspondances qu'une paire de liste blanche n'a PAS
    absorbees parce que la fiche ou le client a change depuis sa decision
    (fiskr.empreintes) — elles ont suivi le chemin des autres.

    Et la trace du passage : `screening_pass_id` (None : aucun passage n'a eu
    lieu, faute de clients) et `clients_covered`, les fiches dont il a etabli
    ou prolonge la couverture contre l'univers en production.

    `lapsed_alerts` : alertes ouvertes dont la fiche vient d'etre re-criblee et
    dont le couple ne franchit plus le seuil, rendues caduques
    (fiskr.caducite). Zero hors d'une mise en production de liste.
    """
    return {
        "changed_entities": int(changed_entities),
        "clients_screened": 0,
        "new_alerts": 0,
        "redetected_alerts": 0,
        "closed_by_rule": 0,
        "rule_suppressed": 0,
        "whitelisted_suppressed": 0,
        "whitelist_stale": 0,
        "screening_pass_id": None,
        "clients_covered": 0,
        "lapsed_alerts": 0,
    }


def _screen_clients_against(db, changed_entities: List[Dict[str, Any]],
                            trigger_detail: str,
                            progress: Optional[Callable[[int, int], None]] = None,
                            clients: Optional[List[Dict[str, Any]]] = None,
                            reference: Optional[tuple] = None,
                            paires_franchies: Optional[Set] = None) -> Dict[str, int]:
    """
    Crible le referentiel clients (ou les seuls `clients` donnes) contre un
    ensemble borne d'entites (index de blocking local). Retourne les compteurs
    du run.

    `reference` : (version, empreinte) de l'univers, celle que le passage a lue
    en s'ouvrant — les lignes d'audit des correspondances et la ligne du
    passage portent ainsi la MEME valeur, lue une seule fois.

    `paires_franchies` : s'il est fourni, recoit chaque couple (client, fiche)
    trouve au-dessus du seuil — liste blanche et regles comprises, puisqu'ils
    le franchissent. C'est ce qui permet de dire, ensuite, quelles alertes
    ouvertes ne le franchissent plus (fiskr.caducite).

    `progress(traites, total)` est appele tous les 500 clients : le re-criblage
    qui suit une approbation d'homologation peut durer plusieurs minutes sur un
    gros referentiel. Jamais bloquant.
    """
    result = compteurs_de_recriblage(len(changed_entities))
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
    watchlist_version, watchlist_hash = reference or reference_de_l_univers(db)[:2]

    # Rarete des mots de nom : construite ICI, dans le parent, AVANT le fork du
    # pool — les enfants en heritent. Sans cet appel le demon travailleur n'a
    # pas de table, et une regle fondee sur la rarete cloturerait au criblage
    # unitaire (processus API) mais pas au re-criblage : deux verites.
    from fiskr import rarete
    rarete.table_pour(db)

    if clients is None:
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
        from fiskr.empreintes import empreinte_client, examiner_la_paire, notifier_les_bascules

        client_ref = client.get("client_id")
        hits = sorted(hits, key=lambda h: -h["final_score"])
        if paires_franchies is not None:
            paires_franchies.update(
                (client_ref, (h.get("watchlist_entity") or {}).get("entity_id")) for h in hits)
        paires = whitelisted_pairs(
            db, client_ref,
            [(h.get("watchlist_entity") or {}).get("entity_id") for h in hits])
        # Identification du client telle que CE criblage l'a vue : journalisee
        # avec chaque decision, c'est ce qu'une paire posee depuis cette
        # alerte couvrira (fiskr.empreintes).
        empreinte = empreinte_client(client)

        from fiskr.fprules import active_rules
        from fiskr.settings import perimeter_overrides
        regles_actives = active_rules(db, "SCREENING")
        classement = perimeter_overrides(db)

        a_journaliser = []
        reexamens: Dict[int, Dict[str, Any]] = {}
        for position, hit in enumerate(hits, start=1):
            fiche = hit.get("watchlist_entity") or {}
            hit["client_fingerprint"] = empreinte
            paire = paires.get(fiche.get("entity_id"))
            if paire is not None:
                reexamen = examiner_la_paire(db, paire, fiche, empreinte)
                if reexamen is None:
                    hit["status"] = "WHITELISTED"
                    hit["whitelist_pair_id"] = paire.id
                    result["whitelisted_suppressed"] += 1
                    a_journaliser.append((hit, None))
                    continue
                # La fiche ou le client a change depuis la decision : la paire
                # ne couvre plus cette correspondance, qui suit le chemin de
                # toutes les autres (regles comprises).
                hit["whitelist_stale"] = {"pair_id": paire.id, "reason": reexamen["motif"]}
                reexamens[id(hit)] = reexamen
                result["whitelist_stale"] += 1
            ctx = build_screening_ctx(client, fiche, hit,
                                      hits_count=len(hits), hit_rank=position,
                                      perimeter_overrides=classement)
            hit["screening_perimeter"] = ctx["perimeter"]
            regle = evaluate_fp_rules(db, "SCREENING", ctx, rules=regles_actives)
            if regle is not None:
                annotate_suppression(hit, regle)
                result["rule_suppressed"] += 1
            a_journaliser.append((hit, regle))

        lignes = log_compliance_decisions(
            db, client,
            [(h.get("watchlist_entity") or {}, h) for h, _r in a_journaliser],
            watchlist_version, watchlist_hash, commit=False)
        a_alerter = [{"audit": ligne, "match": h, "rule": r,
                      "whitelist_stale": reexamens.get(id(h))}
                     for (h, r), ligne in zip(a_journaliser, lignes)
                     if h.get("status") == "ALERT"]
        if not a_alerter:
            db.commit()
        if a_alerter:
            # `open_or_redetect_alerts` sait exactement ce qu'il a fait de chaque
            # correspondance — ouverte, deja existante et re-confirmee, ou close
            # par une regle. Ce compte-la est le seul juste : une correspondance
            # qui retombe sur une alerte deja ouverte n'ouvre rien. Compte a la
            # main, un lookback — qui repasse TOUTE la production — annoncait
            # autant de « nouvelles alertes » qu'il trouvait de correspondances,
            # sans en creer une seule.
            compte = open_or_redetect_alerts(
                db, a_alerter, client_id=client_ref, username=RESCREEN_USERNAME,
                channel="SCREENING", detail_suffix=f" {trigger_detail}")
            result["new_alerts"] += int(compte.get("opened", 0))
            result["redetected_alerts"] += int(compte.get("redetected", 0))
            result["closed_by_rule"] += int(compte.get("closed_by_rule", 0))
        notifier_les_bascules(db, reexamens.values())

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
        f"{result['clients_screened']} clients criblés, "
        f"{result['new_alerts']} alerte(s) ouverte(s), "
        f"{result['redetected_alerts']} re-détectée(s), "
        f"{result['closed_by_rule']} close(s) par règle, "
        f"{result['whitelisted_suppressed']} supprimée(s) par liste blanche, "
        f"{result['whitelist_stale']} non absorbée(s) (paire à réexaminer)."
    )
    return result


def rescreen_after_snapshot_change(db, file_type: str, new_snapshot_id: str,
                                   previous_snapshot_id: Optional[str] = None,
                                   progress: Optional[Callable[[int, int], None]] = None,
                                   reference_avant: Optional[str] = None,
                                   started_by: Optional[str] = None) -> Dict[str, int]:
    """
    Re-crible le referentiel clients contre les entites du nouveau snapshot
    qui sont nouvelles ou modifiees par rapport au precedent (comparaison des
    checksums). Sans snapshot precedent, tout le snapshot est considere.

    `reference_avant` : empreinte de l'univers en production AVANT cette mise
    a jour, lue par l'appelant juste avant de la faire. C'est elle qui permet
    de dire ensuite qu'un client est couvert contre la nouvelle version : il
    l'etait contre l'ancienne, et il vient d'etre confronte a tout ce qui a
    change. Inconnue, les fiches sont datees mais aucune couverture n'est
    prolongee — mieux vaut un client signale « non crible contre la version
    en production » qu'un client declare couvert sans l'etre.
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
    # Rien de nouveau ni de modifie (retraits seuls) : personne a confronter,
    # mais la couverture se prolonge — inutile de charger les profils.
    clients = _client_dicts(db) if changed else _identifiants_en_production(db)
    if not clients:
        return compteurs_de_recriblage(len(changed))
    # Premiere liste en production, sans precedent : le « delta » est l'univers
    # entier, et le passage est complet. C'est l'ordre naturel d'une
    # installation neuve — clients d'abord, listes ensuite — et sans cela ses
    # clients, confrontes a tout, resteraient « non criblés contre la version
    # en production » faute d'avoir ete couverts contre l'univers vide d'avant.
    complet = previous_snapshot_id is None and lots_en_production(db) == [new_snapshot_id]
    franchies: Set = set()
    result = _executer_le_passage(
        db, NATURE_MISE_A_JOUR, changed, clients,
        trigger_detail=f"[Re-criblage automatique après mise à jour {file_type}]",
        progress=progress, complet=complet, reference_avant=reference_avant,
        scope={"listes": [file_type], "complet": complet, "snapshot_id": new_snapshot_id,
               "snapshot_precedent": previous_snapshot_id},
        started_by=started_by, paires_franchies=franchies,
    )
    # Les alertes ouvertes sur ces fiches que ce passage n'a PAS retrouvees
    # au-dessus du seuil : le cahier de tests les annoncait « resolues par la
    # liste candidate », et elles restaient en file comme si de rien n'etait.
    from fiskr.caducite import examiner_les_caduques
    result["lapsed_alerts"] = examiner_les_caduques(
        db, file_type, changed, franchies, version=new_snapshot_id)
    return result


class UniversAbsent(RuntimeError):
    """Aucune liste en production dans le perimetre demande : un passage rendu
    maintenant ecrirait que des clients ont ete cribles contre rien."""


def lots_en_production(db, types=None) -> List[str]:
    """Les lots de listes en production (des seuls `types`, ou de tous)."""
    from fiskr.database import WATCHLIST_FILE_TYPES
    return [s.snapshot_id for s in db.query(Snapshot.snapshot_id).filter(
        Snapshot.file_type.in_(types or WATCHLIST_FILE_TYPES),
        Snapshot.status == "READY").all()]


def rescreen_lookback(db, file_type: Optional[str] = None,
                      progress: Optional[Callable[[int, int], None]] = None,
                      started_by: Optional[str] = None) -> Dict[str, int]:
    """
    Lookback manuel : re-crible le referentiel clients contre TOUTES les
    entites en production (d'un type de liste, ou de tous les types watchlist).
    Instrumente (`progress`) : c'est l'operation la plus lourde du produit,
    elle est desormais executee en tache de fond et suivie par jeton.

    Un perimetre sans liste en production est REFUSE (UniversAbsent). Il
    rendait « Lookback exécuté », zero client, zero alerte : un compte rendu
    de conformite pour une comparaison qui n'avait pas eu lieu.
    """
    from fiskr.database import WATCHLIST_FILE_TYPES
    types = [file_type] if file_type else WATCHLIST_FILE_TYPES
    snap_ids = lots_en_production(db, types)
    if not snap_ids:
        raise UniversAbsent(
            f"Aucune liste {file_type} n'est en production : le lookback n'a rien "
            f"à comparer." if file_type else
            "Aucune liste n'est en production : le lookback n'a rien à comparer.")
    clients = _client_dicts(db)
    if not clients:
        return compteurs_de_recriblage(0)
    entities = _entity_dicts(db, snap_ids)
    label = file_type or "toutes listes"
    return _executer_le_passage(
        db, NATURE_LOOKBACK, entities, clients,
        trigger_detail=f"[Lookback manuel {label}]", progress=progress,
        # Restreint a un type de liste, il crible pour de bon — les fiches sont
        # datees — mais ne couvre pas l'univers : il n'en a vu qu'une partie.
        complet=file_type is None,
        scope={"listes": [file_type] if file_type else "TOUTES",
               "complet": file_type is None},
        started_by=started_by,
    )


def cribler_les_clients_nouveaux_ou_modifies(
        db, snapshot_id: str,
        progress: Optional[Callable[[int, int], None]] = None,
        started_by: Optional[str] = None) -> Dict[str, Any]:
    """
    Apres la mise en production d'un referentiel clients : crible, contre tout
    l'univers en production, les clients AJOUTES ou dont un element
    d'identification a CHANGE. Les autres ne sont pas recribles.

    Un import ne criblait personne : il lancait un controle de completude,
    l'interface annonçait en anglais 406 éléments importés, l'accueil « Rien ne
    vous attend ». Et le re-criblage ne compare qu'aux fiches LISTEES qui
    changent : un client arrive apres la derniere mise en production d'une
    liste n'etait confronte a rien. Lignes directrices conjointes DGT/ACPR sur
    le gel des avoirs (18 mars 2026) : cribler avant toute entree en relation
    (§122) et « a chaque mise a jour des elements d'identification » (§141).

    Ce qui est « inchange » se decide sur l'empreinte des elements
    d'identification (fiskr/identification.py, relevee sur le moteur), pas sur
    celle de la fiche entiere : un telephone modifie ne recrible personne. Une
    fiche identique a celle, deja criblee, que portait le referentiel precedent
    HERITE de sa trace — quand et contre quoi elle l'a ete : sans heritage, un
    reimport identique remettait tout le referentiel a « jamais crible ».
    """
    from fiskr.identification import empreinte_d_identification

    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == snapshot_id).first()
    if snap is None or snap.file_type != "CLIENT_BASE":
        raise ValueError(f"Référentiel clients introuvable : {snapshot_id}.")
    bilan = {**compteurs_de_recriblage(0), "snapshot_id": snapshot_id,
             "clients": 0, "unchanged_clients": 0, "inherited_clients": 0}
    if snap.status != "READY":
        # Retire de la production entre l'import et ce travail : ses fiches ne
        # sont plus le referentiel, il n'y a rien a cribler en leur nom.
        return bilan

    fiches = _client_dicts(db, [snapshot_id])
    bilan["clients"] = len(fiches)
    empreintes = {f["id"]: empreinte_d_identification(f) for f in fiches}
    criblees = dict(db.query(ClientEntity.id, ClientEntity.screened_identity_sha256)
                    .filter(ClientEntity.snapshot_id == snapshot_id).all())

    sans_trace = [f for f in fiches if criblees.get(f["id"]) is None]
    heritees = _heriter_des_traces(db, snapshot_id, sans_trace, empreintes)
    for identifiant in heritees:
        criblees[identifiant] = empreintes[identifiant]
    bilan["inherited_clients"] = len(heritees)

    a_cribler = [f for f in fiches if criblees.get(f["id"]) != empreintes[f["id"]]]
    bilan["unchanged_clients"] = len(fiches) - len(a_cribler)
    if not a_cribler:
        return bilan

    lots = lots_en_production(db)
    if not lots:
        raise UniversAbsent(
            f"Aucune liste n'est en production : les {len(a_cribler)} client(s) "
            f"nouveaux ou modifiés n'ont été comparés à rien et restent NON criblés.")
    resultat = _executer_le_passage(
        db, NATURE_REFERENTIEL, _entity_dicts(db, lots), a_cribler,
        trigger_detail="[Criblage des clients nouveaux ou modifiés]",
        progress=progress, complet=True,
        scope={"listes": "TOUTES", "complet": True, "snapshot_id": snapshot_id},
        started_by=started_by,
    )
    return {**bilan, **resultat}


def _heriter_des_traces(db, snapshot_id: str, fiches: List[Dict[str, Any]],
                        empreintes: Dict[int, str]) -> List[int]:
    """
    Reporte sur les fiches du nouveau referentiel la trace de criblage de la
    MEME fiche dans un referentiel precedent : meme client, memes elements
    d'identification. La plus recente l'emporte. Rend les fiches heritieres.

    Un client dont un element d'identification a change n'herite de rien : sa
    trace d'hier ne dit rien de qui il est aujourd'hui.
    """
    if not fiches:
        return []
    par_client: Dict[str, List[Dict[str, Any]]] = {}
    for f in fiches:
        if f.get("client_id"):
            par_client.setdefault(f["client_id"], []).append(f)
    references = sorted(par_client)
    anciennes: Dict[str, Any] = {}
    for i in range(0, len(references), 800):
        tranche = references[i:i + 800]
        lignes = db.query(ClientEntity.client_id, ClientEntity.screened_at,
                          ClientEntity.screening_pass_id,
                          ClientEntity.screened_identity_sha256,
                          ClientEntity.covered_universe_hash) \
            .join(Snapshot, ClientEntity.snapshot_id == Snapshot.snapshot_id) \
            .filter(Snapshot.file_type == "CLIENT_BASE",
                    ClientEntity.snapshot_id != snapshot_id,
                    ClientEntity.client_id.in_(tranche),
                    ClientEntity.screened_identity_sha256.isnot(None)) \
            .order_by(Snapshot.uploaded_at.asc(), ClientEntity.id.asc()).all()
        for ligne in lignes:
            anciennes[ligne.client_id] = ligne   # la plus recente ecrase
    reports = []
    for reference, nouvelles in par_client.items():
        ancienne = anciennes.get(reference)
        if ancienne is None:
            continue
        for f in nouvelles:
            if ancienne.screened_identity_sha256 == empreintes[f["id"]]:
                reports.append({"id": f["id"], "screened_at": ancienne.screened_at,
                                "screening_pass_id": ancienne.screening_pass_id,
                                "screened_identity_sha256": ancienne.screened_identity_sha256,
                                "covered_universe_hash": ancienne.covered_universe_hash})
    if reports:
        _mettre_a_jour_par_fiche(db, reports)
        db.commit()
    return [r["id"] for r in reports]


# ------------------ PASSAGES DE CRIBLAGE ------------------

NATURE_LOOKBACK = "LOOKBACK"
NATURE_MISE_A_JOUR = "MISE_A_JOUR_LISTE"
NATURE_REFERENTIEL = "REFERENTIEL_CLIENTS"

# SQLite plafonne les parametres d'une requete : les mises a jour en masse
# procedent par tranches d'identifiants, comme `_entity_dicts`.
_TRANCHE = 800


def _identifiants_en_production(db) -> List[Dict[str, Any]]:
    """Les seuls identifiants des fiches du referentiel en production."""
    snap_ids = [s.snapshot_id for s in db.query(Snapshot.snapshot_id).filter(
        Snapshot.file_type == "CLIENT_BASE", Snapshot.status == "READY").all()]
    if not snap_ids:
        return []
    return [{"id": i} for (i,) in db.query(ClientEntity.id).filter(
        ClientEntity.snapshot_id.in_(snap_ids)).order_by(ClientEntity.id).all()]


def _mettre_a_jour_par_fiche(db, valeurs: List[Dict[str, Any]]) -> None:
    """Mise a jour par cle primaire, en lots (une valeur differente par fiche)."""
    from sqlalchemy import update
    for i in range(0, len(valeurs), 5000):
        db.execute(update(ClientEntity), valeurs[i:i + 5000])


def _executer_le_passage(db, nature: str, entites: List[Dict[str, Any]],
                         clients: List[Dict[str, Any]], trigger_detail: str,
                         progress: Optional[Callable[[int, int], None]] = None, *,
                         complet: bool, reference_avant: Optional[str] = None,
                         scope: Optional[Dict[str, Any]] = None,
                         started_by: Optional[str] = None,
                         paires_franchies: Optional[Set] = None) -> Dict[str, Any]:
    """
    Un passage, de bout en bout : la ligne qui le decrit, le criblage, la
    trace sur chaque fiche. Un echec laisse le passage en ECHEC avec sa cause,
    et aucune fiche datee — une trace a moitie ecrite affirmerait un criblage
    qui n'a pas abouti.
    """
    # La reference est lue UNE fois, au point unique, et transmise au
    # criblage : le passage et les lignes d'audit de ses correspondances
    # portent la meme.
    version, empreinte, composition = reference_de_l_univers(db)
    passage = ScreeningPass(
        nature=nature, trigger_detail=trigger_detail[:255], status="EN_COURS",
        watchlist_version=version, watchlist_hash=empreinte,
        watchlist_composition=composition,
        previous_watchlist_hash=reference_avant, scope=scope,
        entities_compared=len(entites), started_by=started_by,
        started_at=datetime.utcnow())
    db.add(passage)
    db.commit()
    passage_id = passage.id
    try:
        resultat = _screen_clients_against(db, entites, trigger_detail, progress,
                                           clients=clients, reference=(version, empreinte),
                                           paires_franchies=paires_franchies)
        couverts = _dater_les_fiches(db, passage_id, empreinte,
                                     clients if entites else [], clients,
                                     complet=complet, reference_avant=reference_avant)
        passage = db.get(ScreeningPass, passage_id)
        passage.status = "TERMINE"
        passage.clients_screened = int(resultat.get("clients_screened") or 0)
        passage.clients_covered = couverts
        passage.counters = dict(resultat)
        passage.finished_at = datetime.utcnow()
        db.commit()
    except Exception as e:
        db.rollback()
        echec = db.get(ScreeningPass, passage_id)
        if echec is not None:
            echec.status = "ECHEC"
            echec.error = str(e)[:2000]
            echec.finished_at = datetime.utcnow()
            db.commit()
        raise
    return {**resultat, "screening_pass_id": passage_id, "clients_covered": couverts}


def _dater_les_fiches(db, passage_id: int, empreinte: str,
                      cribles: List[Dict[str, Any]], confrontes: List[Dict[str, Any]], *,
                      complet: bool, reference_avant: Optional[str]) -> int:
    """
    Ecrit la trace du passage sur les fiches, en masse. Rend le nombre de
    fiches dont il etablit ou prolonge la couverture.

    - `cribles` : fiches reellement confrontees a des fiches listees — elles
      recoivent la date et le passage ;
    - `complet` : le passage a vu tout l'univers — ses fiches sont couvertes
      contre `empreinte`, et leur empreinte d'identification est relevee (par
      fiche, seulement quand elle differe : un lookback quotidien ne reecrit
      pas 750 000 empreintes identiques) ;
    - sinon, avec `reference_avant` : un delta ne prolonge la couverture que
      des fiches couvertes contre l'univers d'avant. Un client que personne
      n'avait crible contre lui ne devient pas couvert pour avoir ete compare
      aux seules fiches nouvelles.
    """
    from fiskr.identification import empreinte_d_identification

    maintenant = datetime.utcnow()
    ids = [c["id"] for c in cribles]
    for i in range(0, len(ids), _TRANCHE):
        db.query(ClientEntity).filter(ClientEntity.id.in_(ids[i:i + _TRANCHE])).update(
            {"screened_at": maintenant, "screening_pass_id": passage_id},
            synchronize_session=False)

    couverts = 0
    if complet:
        for i in range(0, len(ids), _TRANCHE):
            couverts += db.query(ClientEntity).filter(
                ClientEntity.id.in_(ids[i:i + _TRANCHE])).update(
                {"covered_universe_hash": empreinte}, synchronize_session=False)
        actuelles: Dict[int, Optional[str]] = {}
        for i in range(0, len(ids), _TRANCHE):
            actuelles.update(db.query(ClientEntity.id, ClientEntity.screened_identity_sha256)
                             .filter(ClientEntity.id.in_(ids[i:i + _TRANCHE])).all())
        nouvelles = []
        for c in cribles:
            identite = empreinte_d_identification(c)
            if actuelles.get(c["id"]) != identite:
                nouvelles.append({"id": c["id"], "screened_identity_sha256": identite})
        _mettre_a_jour_par_fiche(db, nouvelles)
    elif reference_avant:
        tous = [c["id"] for c in confrontes]
        for i in range(0, len(tous), _TRANCHE):
            couverts += db.query(ClientEntity).filter(
                ClientEntity.id.in_(tous[i:i + _TRANCHE]),
                ClientEntity.covered_universe_hash == reference_avant).update(
                {"covered_universe_hash": empreinte}, synchronize_session=False)
    db.commit()
    return int(couverts)


def dater_un_criblage_unitaire(db, fiche: ClientEntity, audit_id: Optional[int]) -> None:
    """
    Trace d'un criblage unitaire (webhook d'upsert, « recribler ce client ») :
    sa piece est sa ligne d'audit — elle porte toujours une decision, meme sans
    correspondance — et la fiche en reprend la reference de l'univers. Sans
    cela, un client cree et crible par le SI amont compterait parmi les « non
    criblés contre la version en production ». L'appelant valide.
    """
    from fiskr.database import AuditTrail
    from fiskr.identification import empreinte_d_identification

    ligne = db.get(AuditTrail, audit_id) if audit_id else None
    if ligne is None:
        return
    profil = {c.name: getattr(fiche, c.name) for c in fiche.__table__.columns
              if c.name not in CLIENT_SCREENING_TRACE_COLUMNS}
    fiche.screened_at = ligne.timestamp or datetime.utcnow()
    fiche.screening_pass_id = None
    fiche.screened_identity_sha256 = empreinte_d_identification(profil)
    fiche.covered_universe_hash = ligne.watchlist_hash

"""
Mesure de l'impact des equivalences linguistiques sur le criblage.

Pourquoi ce module existe
-------------------------
Activer une table d'equivalences elargit mecaniquement le perimetre des
alertes : des rapprochements aujourd'hui impossibles en deviennent. C'est
l'effet recherche, mais il doit etre CHIFFRE avant la production, pas subi.

Or ni le cahier de tests ni le simulateur de seuils ne savaient le faire :
- `backtest.run_backtest` compare deux UNIVERS DE LISTES (production actuelle
  contre snapshot candidat) en appliquant le meme parametrage de scoring des
  deux cotes — il ne fait pas varier le parametrage ;
- `/api/settings/scoring/simulate` rejoue des `final_score` DEJA STOCKES contre
  des seuils candidats. Les equivalences changent les scores eux-memes ET
  l'ensemble des candidats retenus au blocking : rejouer un score fige n'aurait
  aucun sens.

Ce module comble ce trou : meme panel, meme univers de listes, deux passes de
criblage a blanc sous DEUX PARAMETRAGES d'equivalences differents, et l'ecart
mesure.

Ce que la mesure dit — et ce qu'elle ne dit pas
-----------------------------------------------
Elle donne le VOLUME d'alertes gagnees et perdues, avec les paires concernees
et l'equivalence qui les a produites. Sur un panel de pseudo-clients dont les
correspondances attendues sont connues, elle donne aussi le taux
d'interception des deux cotes.

Elle ne dit PAS combien des alertes gagnees sont des faux positifs : cela
suppose une verite terrain qu'aucune simulation ne possede. Le chiffre a lire
est « voila combien d'alertes en plus vos analystes traiteront », et les
exemples permettent d'en juger la qualite a la main.
"""
import logging
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("fiskr.resource_impact")

# Nombre d'exemples de paires restitues par sens (gagnees / perdues)
MAX_EXAMPLES = 25


def _pair_key(pair: Dict[str, Any]) -> str:
    return f"{pair.get('client_id')}|{pair.get('entity_id')}"


def _explain(client_name: str, entity_name: str) -> List[Dict[str, str]]:
    """Equivalences qui expliquent un rapprochement, dans le contexte courant."""
    from fiskr.scoring import collect_name_equivalences

    return collect_name_equivalences(
        (client_name or "").upper(), (entity_name or "").upper())


def build_candidate_index(db, fields: Set[str],
                          include_pending_ids: Optional[List[int]] = None):
    """
    Index a utiliser pour la passe candidate.

    Sans equivalence apprise en attente, c'est l'index actif (fichiers +
    apprises deja approuvees). Avec, on repart d'un index FRAIS charge depuis
    les fichiers et on y fusionne les approuvees PLUS les propositions
    designees : la mesure repond alors a « qu'est-ce qui se passerait si
    j'approuvais celles-ci ? » sans rien approuver.
    """
    from fiskr import resources
    from fiskr.database import LearnedEquivalence
    from fiskr.resource_mining import STATUS_APPROVED, approved_groups

    if not fields:
        return None
    if not include_pending_ids:
        return resources.get_index()

    index = resources.load_index(resources.default_directory())
    groups = approved_groups(db)
    pending = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.id.in_(include_pending_ids),
        LearnedEquivalence.status != STATUS_APPROVED,
    ).all()
    for row in pending:
        field_groups = groups.setdefault(row.field, {})
        terms = field_groups.setdefault(row.class_id, [])
        for term in (row.term_a, row.term_b):
            if term not in terms:
                terms.append(term)
    index.merge_learned(groups)
    return index


def simulate_resource_impact(
    db,
    panel_snapshot_id: str,
    candidate_fields: Set[str],
    baseline_fields: Optional[Set[str]] = None,
    include_pending_ids: Optional[List[int]] = None,
    progress: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """
    Crible deux fois le meme panel contre le meme univers, sous deux
    parametrages d'equivalences, et retourne l'ecart.

    `baseline_fields` a None = le parametrage actuellement en vigueur : la
    question posee est « qu'est-ce que mon changement ajoute a ce que je fais
    deja ? », pas « qu'est-ce que les ressources ajoutent au neant ».

    Les deux passes tournent sous `resources.use_context`, une surcharge
    limitee au thread courant : le criblage de production servi en parallele
    ne voit rien changer.
    """
    from fiskr import resources
    from fiskr.backtest import _dry_run_screen, _panel_clients, _universe_snapshot_ids
    from fiskr.database import Snapshot
    from fiskr.fprules import active_rules
    from fiskr.rescreen import _entity_dicts
    from fiskr.settings import resource_fields

    clients = _panel_clients(db, panel_snapshot_id)
    if not clients:
        raise ValueError("Panel introuvable ou vide.")

    # Univers = la production telle qu'elle est aujourd'hui. On ne fait varier
    # QUE le parametrage des equivalences : deux variables a la fois rendraient
    # l'ecart ininterpretable.
    from fiskr.api import WATCHLIST_FILE_TYPES

    snapshot_ids = [
        s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
            Snapshot.status == "READY").all()
    ]
    from fiskr import screenpool
    rules = active_rules(db, "SCREENING")
    # Projection memoire derivee des regles evaluees (cf. run_backtest)
    projection = screenpool.projection_for(rules)
    entities = _entity_dicts(db, snapshot_ids, projection=projection) if snapshot_ids else []
    if not entities:
        raise ValueError("Aucune liste en production : rien à cribler.")
    if baseline_fields is None:
        baseline_fields = {f for f, on in resource_fields(db).items() if on}
    baseline_fields = set(baseline_fields)
    candidate_fields = set(candidate_fields)

    candidate_index = build_candidate_index(db, candidate_fields, include_pending_ids)

    def _phase(name: str):
        if not progress:
            return None
        return lambda done, total: progress(name, done, total)

    with resources.use_context(baseline_fields):
        before = _dry_run_screen(db, clients, entities, rule_set=rules,
                                 panel_snapshot_id=panel_snapshot_id,
                                 progress=_phase("SCREEN_CURRENT"))
    with resources.use_context(candidate_fields, candidate_index):
        after = _dry_run_screen(db, clients, entities, rule_set=rules,
                                panel_snapshot_id=panel_snapshot_id,
                                progress=_phase("SCREEN_CANDIDATE"))
        # Les explications se calculent DANS le contexte candidat : hors de lui,
        # aucune equivalence ne s'applique et la trace serait vide
        gained_keys = set(map(_pair_key, after["pairs"].values())) - \
            set(map(_pair_key, before["pairs"].values()))
        gained = []
        for pair in after["pairs"].values():
            if _pair_key(pair) in gained_keys and len(gained) < MAX_EXAMPLES:
                gained.append({**pair, "equivalences": _explain(
                    pair.get("client_name"), pair.get("entity_name"))})

    lost_keys = set(map(_pair_key, before["pairs"].values())) - \
        set(map(_pair_key, after["pairs"].values()))
    lost = [p for p in before["pairs"].values() if _pair_key(p) in lost_keys][:MAX_EXAMPLES]

    by_list: Dict[str, Dict[str, int]] = {}
    for source, key in ((before["pairs"].values(), "before"),
                        (after["pairs"].values(), "after")):
        for pair in source:
            bucket = by_list.setdefault(pair.get("list_type") or "UNKNOWN",
                                        {"before": 0, "after": 0})
            bucket[key] += 1
    for bucket in by_list.values():
        bucket["delta"] = bucket["after"] - bucket["before"]

    total_clients = len(clients)
    report = {
        "panel_snapshot_id": panel_snapshot_id,
        "panel_size": total_clients,
        "universe_size": len(entities),
        "baseline_fields": sorted(baseline_fields),
        "candidate_fields": sorted(candidate_fields),
        "pending_included": list(include_pending_ids or []),
        "alerts_before": before["alerts"],
        "alerts_after": after["alerts"],
        "delta": after["alerts"] - before["alerts"],
        "delta_pct": (round((after["alerts"] - before["alerts"]) * 100.0 / before["alerts"], 2)
                      if before["alerts"] else None),
        "interception_before_pct": round(before["alerts"] * 100.0 / total_clients, 2),
        "interception_after_pct": round(after["alerts"] * 100.0 / total_clients, 2),
        "gained_count": len(gained_keys),
        "lost_count": len(lost_keys),
        "gained_examples": gained,
        "lost_examples": lost,
        "by_list": by_list,
        # Ce que la mesure ne dit pas : a inscrire dans le rapport, pas dans une
        # note de bas de page que personne ne lit
        "caveat": ("Le volume d'alertes gagnées est mesuré, pas leur qualité : "
                   "aucune simulation ne connaît la vérité terrain. Les exemples "
                   "ci-dessus servent à en juger à la main."),
    }
    logger.info(f"Impact des ressources : {before['alerts']} -> {after['alerts']} alertes "
                f"({report['delta']:+d}) sur {total_clients} pseudo-clients.")
    return report

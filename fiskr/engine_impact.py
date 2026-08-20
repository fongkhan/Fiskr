"""
Mesure de l'impact d'un changement de capacites du moteur, AVANT application.

Pourquoi ce module existe
-------------------------
Le catalogue des capacites permet de rendre le moteur aveugle : couper la
translitteration, c'est perdre tous les alias non latins de l'OFAC et de
l'ONU. Pouvoir legitime — un etablissement sans exposition non latine paie
aujourd'hui ce cout sans benefice — mais dangereux. Le chiffrer avant de
decider est la contrepartie de ce pouvoir.

Aucun outil existant ne savait le faire :
- `backtest.run_backtest` fait varier l'UNIVERS DE LISTES (production contre
  snapshot candidat) en gardant le meme parametrage des deux cotes ;
- `/api/settings/scoring/simulate` rejoue des `final_score` DEJA STOCKES contre
  des seuils candidats. Or une capacite change les scores EUX-MEMES et
  l'ensemble des candidats retenus au blocking : rejouer un score fige n'aurait
  aucun sens.

Meme forme que `resource_impact` : meme panel, meme univers de listes, deux
passes de criblage a blanc sous DEUX PARAMETRAGES de capacites, et l'ecart
mesure.

Pourquoi la mesure ne contamine pas la production
-------------------------------------------------
Les deux passes tournent sous `capabilities.use_context`, une surcharge
limitee au THREAD courant. Une mesure lancee en tache de fond pendant que
l'API sert des criblages reels ne change rien pour eux — propriete verrouillee
par test, comme pour les ressources.

Ce que la mesure dit — et ce qu'elle ne dit pas
-----------------------------------------------
Elle donne le VOLUME d'alertes gagnees et perdues, avec les paires concernees.
Le sens qui compte ici est generalement l'INVERSE de celui des ressources :
couper une capacite fait PERDRE des alertes, et ce sont les paires perdues
qu'un responsable doit regarder une a une avant de valider.

Elle ne dit PAS si une alerte perdue etait un vrai positif : cela suppose une
verite terrain qu'aucune simulation ne possede. Sur un panel de pseudo-clients
dont les correspondances attendues sont connues, le taux d'interception des
deux cotes donne un reperage plus solide qu'un simple volume.

Ce qu'elle ne couvre pas non plus, et il faut le savoir en lisant le rapport :
les fiches listees deja ingerees gardent la forme sous laquelle elles ont ete
normalisees a l'import. Une capacite d'ecriture agit immediatement sur la
sonde client, et sur les fiches au prochain rechargement complet de leur
liste. La mesure reflete donc l'etat des listes AU MOMENT ou elle tourne.
"""
import logging
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

logger = logging.getLogger("fiskr.engine_impact")

# Nombre d'exemples de paires restitues par sens (gagnees / perdues)
MAX_EXAMPLES = 25


def _pair_key(pair: Dict[str, Any]) -> str:
    return f"{pair.get('client_id')}|{pair.get('entity_id')}"


def describe_change(baseline: Iterable[str], candidate: Iterable[str]) -> Dict[str, List[str]]:
    """
    Ce que le parametrage candidat coupe et rallume par rapport a la reference.

    Restitue avec le rapport : un ecart de volume ne se lit pas sans savoir
    quelle bascule l'a produit.
    """
    from fiskr import capabilities as caps

    base, cand = set(baseline), set(candidate)
    return {
        "disabled": sorted(c for c in base - cand if c in caps.CAPABILITY_CATALOG),
        "enabled": sorted(c for c in cand - base if c in caps.CAPABILITY_CATALOG),
        "inert": sorted(caps.resolve_inactive_dependencies(cand)),
    }


def simulate_engine_impact(
    db,
    panel_snapshot_id: str,
    candidate_capabilities: Set[str],
    baseline_capabilities: Optional[Set[str]] = None,
    channel: str = "SCREENING",
    progress: Optional[Callable[[str, int, int], None]] = None,
) -> Dict[str, Any]:
    """
    Crible deux fois le meme panel contre le meme univers, sous deux
    parametrages de capacites, et retourne l'ecart.

    `baseline_capabilities` a None = le parametrage EN VIGUEUR : la question
    posee est « qu'est-ce que mon changement enleve a ce que je fais deja ? »,
    pas « qu'est-ce que le moteur complet ajoute au neant ».
    """
    from fiskr import capabilities as caps
    from fiskr.backtest import _dry_run_screen, _panel_clients
    from fiskr.database import Snapshot
    from fiskr.fprules import active_rules
    from fiskr.rescreen import _entity_dicts
    from fiskr.settings import engine_capabilities

    clients = _panel_clients(db, panel_snapshot_id)
    if not clients:
        raise ValueError("Panel introuvable ou vide.")

    # Univers = la production telle qu'elle est aujourd'hui. On ne fait varier
    # QUE le parametrage du moteur : deux variables a la fois rendraient
    # l'ecart ininterpretable.
    from fiskr.api import WATCHLIST_FILE_TYPES

    snapshot_ids = [
        s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
            Snapshot.status == "READY").all()
    ]
    from fiskr import screenpool
    rules = active_rules(db, channel)
    # Projection memoire derivee des regles evaluees (cf. run_backtest)
    projection = screenpool.projection_for(rules)
    entities = _entity_dicts(db, snapshot_ids, projection=projection) if snapshot_ids else []
    if not entities:
        raise ValueError("Aucune liste en production : rien à cribler.")
    if baseline_capabilities is None:
        baseline_capabilities = {c for c, on in engine_capabilities(db, channel).items() if on}
    baseline_capabilities = set(baseline_capabilities)
    candidate_capabilities = set(candidate_capabilities)

    def _phase(name: str):
        if not progress:
            return None
        return lambda done, total: progress(name, done, total)

    # L'index de blocking est reconstruit A L'INTERIEUR de chaque passe par
    # `_dry_run_screen` : indispensable ici, car une capacite comme la
    # translitteration change les cles des DEUX cotes. Un index partage entre
    # les deux passes ne mesurerait que la moitie de l'effet.
    with caps.use_context(channel, baseline_capabilities):
        before = _dry_run_screen(db, clients, entities, rule_set=rules,
                                 panel_snapshot_id=panel_snapshot_id,
                                 progress=_phase("SCREEN_CURRENT"))
    with caps.use_context(channel, candidate_capabilities):
        after = _dry_run_screen(db, clients, entities, rule_set=rules,
                                panel_snapshot_id=panel_snapshot_id,
                                progress=_phase("SCREEN_CANDIDATE"))

    before_keys = {_pair_key(p) for p in before["pairs"].values()}
    after_keys = {_pair_key(p) for p in after["pairs"].values()}
    gained_keys = after_keys - before_keys
    lost_keys = before_keys - after_keys
    gained = [p for p in after["pairs"].values()
              if _pair_key(p) in gained_keys][:MAX_EXAMPLES]
    lost = [p for p in before["pairs"].values()
            if _pair_key(p) in lost_keys][:MAX_EXAMPLES]

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
    change = describe_change(baseline_capabilities, candidate_capabilities)
    report = {
        "panel_snapshot_id": panel_snapshot_id,
        "panel_size": total_clients,
        "universe_size": len(entities),
        "channel": channel,
        "baseline_capabilities": sorted(baseline_capabilities),
        "candidate_capabilities": sorted(candidate_capabilities),
        "change": change,
        "alerts_before": before["alerts"],
        "alerts_after": after["alerts"],
        "delta": after["alerts"] - before["alerts"],
        "delta_pct": (round((after["alerts"] - before["alerts"]) * 100.0 / before["alerts"], 2)
                      if before["alerts"] else None),
        "interception_before_pct": round(before["alerts"] * 100.0 / total_clients, 2),
        "interception_after_pct": round(after["alerts"] * 100.0 / total_clients, 2),
        # `alerts_*` compte les CLIENTS interceptes (une paire par client) ;
        # `hits_*` compte les CORRESPONDANCES, dont la production ouvre une
        # alerte chacune. Couper la translitteration ne change pas seulement
        # le nombre de clients pris : cela change le volume de travail, et
        # c'est ce second chiffre qui le dit.
        "hits_before": before.get("hits", 0),
        "hits_after": after.get("hits", 0),
        "hits_delta": after.get("hits", 0) - before.get("hits", 0),
        "gained_count": len(gained_keys),
        "lost_count": len(lost_keys),
        "gained_examples": gained,
        "lost_examples": lost,
        "by_list": by_list,
        # Ce que la mesure ne dit pas : dans le rapport, pas dans une note de
        # bas de page que personne ne lit
        "caveat": ("Les alertes perdues sont comptées, pas qualifiées : aucune "
                   "simulation ne connaît la vérité terrain. Les exemples "
                   "ci-dessus servent à en juger à la main. La mesure reflète "
                   "aussi l'état des listes au moment où elle tourne — les "
                   "fiches déjà ingérées gardent la normalisation de leur "
                   "import jusqu'au prochain rechargement complet."),
    }
    logger.info(f"Impact du moteur ({channel}) : {before['alerts']} -> {after['alerts']} "
                f"alertes ({report['delta']:+d}) sur {total_clients} pseudo-clients ; "
                f"coupées : {', '.join(change['disabled']) or '—'}.")
    return report

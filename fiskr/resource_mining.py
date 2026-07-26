"""
Moteur de fouille d'homonymes : decouverte automatique de nouvelles
equivalences linguistiques.

D'ou viennent les homonymes ?
----------------------------
Pas d'une source externe : d'une donnee que l'installation possede deja et
dont la valeur probante est superieure a n'importe quel dictionnaire achete.

1. `ALIAS` — LE GRAPHE D'ALIAS DES LISTES OFFICIELLES. Quand l'OFAC declare
   qu'une fiche « Muhammad AL-ASSAD » porte l'alias « Mohammed AL-ASAD »,
   l'autorite elle-meme affirme que ces deux graphies designent la meme
   personne. Extraire de la la paire (MUHAMMAD, MOHAMMED) n'est pas une
   inference : c'est une lecture de la donnee officielle.

2. `ANALYST` — LES ALERTES CONFIRMEES EN REVUE. Quand un analyste cloture une
   alerte en « vrai positif », il a valide humainement que le nom du client et
   le nom liste designent la meme personne. C'est la preuve la plus forte qui
   existe dans le systeme.

Le garde-fou qui rend la fouille utilisable
-------------------------------------------
Le piege evident : « Ali HASSAN » alias « Abu MUHAMMAD » est un surnom, pas
une variante d'ecriture. Aligner les tokens produirait les paires absurdes
Ali=Abu et Hassan=Muhammad.

La regle retenue elimine ce cas par construction : **les deux noms doivent
avoir le meme nombre de tokens et ne differer que sur UN SEUL token**. Tout le
reste etant identique, le token divergent est necessairement une autre
ecriture du meme element. « Ali Hassan » vs « Abu Muhammad » differe sur deux
tokens : rejete. « Mohammad Al Assad » vs « Mohammed Al Assad » differe sur un
seul : la paire (MOHAMMAD, MOHAMMED) est retenue.

S'y ajoutent : exclusion des particules (AL, BIN, DE, VAN...), longueur
minimale, proximite phonetique ou de chaine exigee, et un nombre minimal de
fiches DISTINCTES portant la paire — une coquille unique dans un seul
enregistrement ne devient pas une regle de criblage.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("fiskr.resource_mining")

SOURCE_ALIAS = "ALIAS"
SOURCE_ANALYST = "ANALYST"
SOURCES = (SOURCE_ALIAS, SOURCE_ANALYST)

SOURCE_LABELS = {
    SOURCE_ALIAS: "Alias déclarés par la source officielle",
    SOURCE_ANALYST: "Alerte confirmée par un analyste",
}

STATUS_PROPOSED = "PROPOSED"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUSES = (STATUS_PROPOSED, STATUS_APPROVED, STATUS_REJECTED)

# Particules et affixes : ils se repetent dans des milliers de noms sans jamais
# constituer un prenom ni un nom. Les apparier produirait du bruit massif.
PARTICLES = {
    "AL", "EL", "AS", "ABU", "ABOU", "ABD", "ABDEL", "BIN", "BEN", "IBN", "BINT",
    "DE", "DEL", "DELLA", "DI", "DA", "DOS", "DAS", "DU", "DES", "LA", "LE", "LES",
    "VAN", "VON", "DER", "DEN", "TER", "TEN", "MC", "MAC", "SAN", "SANTA", "SAINT",
    "ST", "AND", "THE", "OF", "BEY", "SHEIKH", "SHAYKH", "HAJ", "HAJJ", "SIDI",
    "MR", "MRS", "DR", "JR", "SR", "II", "III",
}

MIN_TOKEN_LENGTH = 3


def _evidence_cap() -> int:
    """Nombre d'exemples conserves par equivalence (preuve, pas archive)."""
    return 5


def signature_of(field: str, term_a: str, term_b: str) -> str:
    """Cle de deduplication stable : le couple est trie, l'ordre n'a pas de sens."""
    left, right = sorted([term_a, term_b])
    return f"{field}|{left}|{right}"


def _is_minable_token(token: str) -> bool:
    return (
        len(token) >= MIN_TOKEN_LENGTH
        and token not in PARTICLES
        and not token.isdigit()
    )


def align_single_divergence(left: str, right: str) -> Optional[Tuple[int, str, str, int]]:
    """
    Aligne deux noms et retourne l'unique token divergent.

    Retourne `(position, token_gauche, token_droit, nombre_de_tokens)` quand
    les deux noms ont le meme nombre de tokens et ne different que sur un
    seul — sinon None. C'est le garde-fou central du moteur : il elimine par
    construction les surnoms et les noms de guerre, qui divergent sur
    plusieurs tokens.
    """
    from fiskr.resources import normalize_term

    lt = normalize_term(left).split()
    rt = normalize_term(right).split()
    if not lt or len(lt) != len(rt) or len(lt) < 2:
        # Un nom d'un seul token ne permet aucun alignement : la paire serait
        # une simple juxtaposition de deux noms sans element commun
        return None
    diffs = [i for i in range(len(lt)) if lt[i] != rt[i]]
    if len(diffs) != 1:
        return None
    pos = diffs[0]
    return pos, lt[pos], rt[pos], len(lt)


def field_for_position(position: int, total: int) -> Optional[str]:
    """
    Type de champ deduit de la position du token divergent.

    Premier token -> prenom, dernier -> nom de famille. Les positions
    intermediaires sont ambigues (deuxieme prenom, particule composee, nom
    compose) : on ne propose rien plutot que de proposer faux.
    """
    from fiskr import resources

    if position == 0:
        return resources.FIELD_GIVEN_NAME
    if position == total - 1:
        return resources.FIELD_SURNAME
    return None


def pair_proximity(term_a: str, term_b: str) -> Tuple[float, bool]:
    """
    Similarite de chaine et concordance phonetique d'une paire.

    Une variante d'ecriture du meme element est proche graphiquement
    (Mohammad/Mohammed) ou phonetiquement (Zhang/Chang). Un couple qui n'est ni
    l'un ni l'autre n'est pas une variante : c'est deux mots differents que
    l'alignement a rapproches par accident.
    """
    from fiskr.phonetics import double_metaphone
    from fiskr.scoring import damerau_levenshtein_similarity, jaro_wink_similarity

    similarity = max(
        jaro_wink_similarity(term_a, term_b),
        damerau_levenshtein_similarity(term_a, term_b),
    ) / 100.0
    pa, sa = double_metaphone(term_a)
    pb, sb = double_metaphone(term_b)
    keys_a = {k for k in (pa, sa) if k}
    keys_b = {k for k in (pb, sb) if k}
    return similarity, bool(keys_a & keys_b)


def confidence_of(occurrences: int, similarity: float, phonetic: bool, source: str) -> float:
    """
    Confiance d'une equivalence, entre 0 et 1.

    Trois facteurs, tous explicables a un controleur :
    - la REPETITION (une paire vue dans dix fiches independantes n'est pas une
      coquille), plafonnee a cinq occurrences pour eviter qu'un gros programme
      de sanctions n'ecrase tout le reste ;
    - la PROXIMITE de chaine, plus la concordance phonetique en bonus ;
    - la SOURCE : une alerte confirmee par un analyste porte une validation
      humaine, l'alias officiel porte l'autorite de l'emetteur de la liste.
    """
    repetition = min(occurrences, 5) / 5.0
    proximity = min(1.0, similarity + (0.1 if phonetic else 0.0))
    source_weight = 1.0 if source == SOURCE_ANALYST else 0.95
    return round((0.45 * repetition + 0.55 * proximity) * source_weight, 4)


class Candidate:
    """Paire candidate en cours d'agregation sur une passe de fouille."""

    def __init__(self, field: str, term_a: str, term_b: str, source: str):
        self.field = field
        self.term_a, self.term_b = sorted([term_a, term_b])
        self.source = source
        self.occurrences = 0
        self.evidence: List[Dict[str, Any]] = []
        self.similarity, self.phonetic = pair_proximity(self.term_a, self.term_b)

    @property
    def signature(self) -> str:
        return signature_of(self.field, self.term_a, self.term_b)

    def add(self, evidence: Dict[str, Any]) -> None:
        self.occurrences += 1
        if len(self.evidence) < _evidence_cap():
            self.evidence.append(evidence)

    @property
    def confidence(self) -> float:
        return confidence_of(self.occurrences, self.similarity, self.phonetic, self.source)


def _accept_pair(field: Optional[str], term_a: str, term_b: str, min_similarity: float) -> bool:
    if not field or term_a == term_b:
        return False
    if not (_is_minable_token(term_a) and _is_minable_token(term_b)):
        return False
    similarity, phonetic = pair_proximity(term_a, term_b)
    return phonetic or similarity >= min_similarity


def _register(bucket: Dict[str, Candidate], field: str, term_a: str, term_b: str,
              source: str, evidence: Dict[str, Any]) -> None:
    key = signature_of(field, term_a, term_b)
    cand = bucket.get(key)
    if cand is None:
        cand = Candidate(field, term_a, term_b, source)
        bucket[key] = cand
    cand.add(evidence)


# ------------------ SOURCE 1 : GRAPHE D'ALIAS DES LISTES ------------------

def _entity_alias_names(entity) -> List[str]:
    raw = entity.aliases or []
    if isinstance(raw, dict):
        names = list(raw.get("high_priority") or []) + list(raw.get("low_priority") or [])
    elif isinstance(raw, list):
        names = list(raw)
    else:
        names = []
    return [str(n) for n in names if n and str(n).strip()]


def mine_alias_graph(db, min_similarity: float, progress=None) -> Dict[str, Candidate]:
    """
    Fouille les fiches listees en production : chaque alias est confronte au
    nom principal. C'est la source la plus volumineuse et la plus defendable.
    """
    from fiskr.database import Snapshot, WatchlistEntity

    bucket: Dict[str, Candidate] = {}
    snapshots = db.query(Snapshot).filter(Snapshot.status.in_(["READY", "PRODUCTION"])).all()
    snapshot_ids = [s.snapshot_id for s in snapshots]
    if not snapshot_ids:
        return bucket
    types = {s.snapshot_id: s.file_type for s in snapshots}

    seen = 0
    query = db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_(snapshot_ids),
        WatchlistEntity.excluded.isnot(True),
    )
    for entity in query.yield_per(1000):
        seen += 1
        if progress and seen % 2000 == 0:
            progress(seen)
        # Les raisons sociales n'ont ni prenom ni nom de famille : les aligner
        # produirait des paires de mots de vocabulaire (« Trading »/« Trade »)
        if (entity.entity_type or "").upper() != "I":
            continue
        primary = entity.primary_name or ""
        if not primary.strip():
            continue
        for alias in _entity_alias_names(entity):
            aligned = align_single_divergence(primary, alias)
            if not aligned:
                continue
            position, term_a, term_b, total = aligned
            field = field_for_position(position, total)
            if not _accept_pair(field, term_a, term_b, min_similarity):
                continue
            _register(bucket, field, term_a, term_b, SOURCE_ALIAS, {
                "entity_id": entity.entity_id,
                "primary_name": primary,
                "alias": alias,
                "list_type": types.get(entity.snapshot_id),
            })
    return bucket


# ------------------ SOURCE 2 : ALERTES CONFIRMEES EN REVUE ------------------

def mine_confirmed_alerts(db, min_similarity: float) -> Dict[str, Candidate]:
    """
    Fouille les alertes cloturees « vrai positif » : un analyste a valide que
    le nom du client et le nom liste designent la meme personne.
    """
    from fiskr.database import Alert

    bucket: Dict[str, Candidate] = {}
    alerts = db.query(Alert).filter(Alert.status == "CLOSED_CONFIRMED").all()
    for alert in alerts:
        aligned = align_single_divergence(alert.client_name or "", alert.watchlist_name or "")
        if not aligned:
            continue
        position, term_a, term_b, total = aligned
        field = field_for_position(position, total)
        if not _accept_pair(field, term_a, term_b, min_similarity):
            continue
        _register(bucket, field, term_a, term_b, SOURCE_ANALYST, {
            "alert_id": alert.id,
            "client_name": alert.client_name,
            "watchlist_name": alert.watchlist_name,
            "list_type": alert.list_type,
        })
    return bucket


# ------------------ CLASSEMENT ET PERSISTANCE ------------------

def resolve_class(index, field: str, term_a: str, term_b: str) -> Optional[str]:
    """
    Classe canonique que la paire doit rejoindre.

    - aucun des deux termes connu -> classe neuve, nommee d'apres le premier
      terme dans l'ordre alphabetique (deterministe, reproductible) ;
    - un seul connu -> l'autre rejoint sa classe ;
    - les deux connus dans LA MEME classe -> deja acquis, rien a faire (None) ;
    - les deux connus dans DEUX classes differentes -> COLLISION. Fusionner
      deux classes existantes sur la foi d'une decouverte automatique
      reunirait des univers que quelqu'un a deliberement separes : on refuse.
    """
    cls_a = index.canonical(term_a, field) if index else None
    cls_b = index.canonical(term_b, field) if index else None
    if cls_a and cls_b:
        return None                      # deja equivalents, ou collision : rien
    if cls_a:
        return cls_a
    if cls_b:
        return cls_b
    return term_a if term_a < term_b else term_b


def run_mining(db, settings: Dict[str, Any], progress=None) -> Dict[str, Any]:
    """
    Passe complete de fouille : decouverte, agregation, classement, ecriture.

    Idempotente : une paire deja connue voit ses compteurs et ses preuves mis a
    jour sans jamais repasser d'APPROVED ou de REJECTED a PROPOSED — une
    decision humaine n'est pas defaite par une passe automatique.
    """
    from fiskr import resources
    from fiskr.database import LearnedEquivalence

    min_similarity = float(settings.get("min_similarity", 0.75))
    min_occurrences = int(settings.get("min_occurrences", 2))
    auto_approve = float(settings.get("auto_approve_confidence", 0.0))
    sources = list(settings.get("sources") or SOURCES)

    buckets: Dict[str, Candidate] = {}
    if SOURCE_ALIAS in sources:
        buckets.update(mine_alias_graph(db, min_similarity, progress=progress))
    if SOURCE_ANALYST in sources:
        for key, cand in mine_confirmed_alerts(db, min_similarity).items():
            existing = buckets.get(key)
            if existing is None:
                buckets[key] = cand
            else:
                # Une paire vue des deux cotes est plus solide : on conserve le
                # cumul des occurrences et la source la plus probante
                existing.occurrences += cand.occurrences
                existing.source = SOURCE_ANALYST
                for ev in cand.evidence:
                    if len(existing.evidence) < _evidence_cap():
                        existing.evidence.append(ev)

    index = resources.get_index()
    now = datetime.utcnow()
    created = updated = approved = skipped_known = skipped_thin = 0

    existing_rows = {
        row.signature: row
        for row in db.query(LearnedEquivalence).filter(
            LearnedEquivalence.signature.in_(list(buckets.keys()))
        ).all()
    } if buckets else {}

    for signature, cand in buckets.items():
        if cand.occurrences < min_occurrences:
            skipped_thin += 1
            continue
        row = existing_rows.get(signature)
        if row is None:
            class_id = resolve_class(index, cand.field, cand.term_a, cand.term_b)
            if class_id is None:
                skipped_known += 1     # deja equivalents, ou collision de classes
                continue
            row = LearnedEquivalence(
                field=cand.field, class_id=class_id,
                term_a=cand.term_a, term_b=cand.term_b, signature=signature,
                source=cand.source, discovered_at=now, status=STATUS_PROPOSED,
            )
            db.add(row)
            created += 1
        else:
            updated += 1
        row.occurrences = cand.occurrences
        row.similarity = cand.similarity
        row.phonetic_match = cand.phonetic
        row.confidence = cand.confidence
        row.evidence = cand.evidence
        row.last_seen_at = now
        # L'auto-approbation ne touche QUE les propositions : une equivalence
        # rejetee par un humain ne revient jamais par la fouille
        if (row.status == STATUS_PROPOSED and auto_approve > 0
                and row.confidence >= auto_approve):
            row.status = STATUS_APPROVED
            row.decided_by = "système"
            row.decided_at = now
            row.decision_comment = (
                f"Auto-approbation : confiance {row.confidence:.2f} ≥ seuil "
                f"{auto_approve:.2f}, {row.occurrences} occurrence(s).")
            approved += 1

    db.commit()
    report = {
        "at": now.isoformat(),
        "candidates": len(buckets),
        "created": created,
        "updated": updated,
        "auto_approved": approved,
        "skipped_already_known": skipped_known,
        "skipped_too_few_occurrences": skipped_thin,
        "min_occurrences": min_occurrences,
        "min_similarity": min_similarity,
        "auto_approve_confidence": auto_approve,
        "sources": sources,
    }
    logger.info(f"Fouille d'homonymes : {report}")
    return report


def approved_groups(db) -> Dict[str, Dict[str, List[str]]]:
    """
    Equivalences approuvees, au format attendu par l'index : {champ: {classe:
    [termes]}}. Fusionnees a l'index des fichiers au chargement.
    """
    from fiskr.database import LearnedEquivalence

    out: Dict[str, Dict[str, List[str]]] = {}
    rows = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.status == STATUS_APPROVED).all()
    for row in rows:
        field_groups = out.setdefault(row.field, {})
        terms = field_groups.setdefault(row.class_id, [])
        for term in (row.term_a, row.term_b):
            if term not in terms:
                terms.append(term)
    return out

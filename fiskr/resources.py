"""
Fichiers de ressources linguistiques : base de connaissance d'equivalences.

Le criblage ne sait comparer que des CHAINES : Jaro-Winkler, Damerau-
Levenshtein et token sort rattrapent une faute de frappe (Mohammad /
Mohammed = une lettre, une transposition), mais aucune metrique de chaine ne
peut deduire que « Henri » et « Harry » designent le meme prenom, ni que
« Londres » et « London » sont la meme ville. C'est une CONNAISSANCE, pas un
calcul : elle se declare.

Un fichier de ressources declare des GROUPES D'EQUIVALENCE par type de champ.
Tous les termes d'un groupe partagent une CLASSE CANONIQUE ; deux termes de la
meme classe sont traites comme identiques par le moteur.

    type: given_name
    groups:
      - id: HENRY
        terms: [Henri, Henry, Harry, Heinrich, Enrique]

Les equivalences agissent a DEUX endroits, et les deux sont indispensables :
- au BLOCKING (fiskr/blocking.py), sans quoi Henri et Harry ne tombent jamais
  dans le meme seau de candidats et ne sont donc JAMAIS compares ;
- au SCORING (fiskr/scoring.py), pour la comparaison elle-meme.
"""
import hashlib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger("fiskr.resources")

# Types de champ couverts. Chaque type a son propre index : « Nice » est une
# ville, jamais un prenom — melanger les univers creerait des faux positifs.
FIELD_GIVEN_NAME = "given_name"
FIELD_SURNAME = "surname"
FIELD_CITY = "city"
FIELD_COUNTRY = "country"
FIELD_STATE = "state"
FIELD_TYPES = (FIELD_GIVEN_NAME, FIELD_SURNAME, FIELD_CITY, FIELD_COUNTRY, FIELD_STATE)

FIELD_LABELS = {
    FIELD_GIVEN_NAME: "Prénoms",
    FIELD_SURNAME: "Noms de famille",
    FIELD_CITY: "Villes",
    FIELD_COUNTRY: "Pays",
    FIELD_STATE: "États et régions",
}


class ResourceError(RuntimeError):
    """Ressource invalide : le chargement est refuse, jamais applique a moitie."""


def normalize_term(term: str) -> str:
    """
    Cle de recherche d'un terme : meme normalisation que le criblage
    (translitteration des ecritures non latines puis suppression des
    diacritiques, cf. quality.strip_accents), majuscules, espaces reduits.

    « Müller », « MULLER » et « Мюллер » donnent ainsi la meme cle : la
    ressource se declare dans l'ecriture qu'on veut, elle reste trouvable.
    """
    from fiskr.quality import strip_accents

    if not term:
        return ""
    cleaned = strip_accents(str(term)).upper()
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    return re.sub(r"\s+", " ", cleaned).strip()


class ResourceIndex:
    """
    Index en memoire des equivalences chargees, interroge a chaque criblage.

    Immuable une fois construit : le rechargement a chaud remplace l'instance
    d'un bloc, aucun criblage ne voit un index a moitie mis a jour.
    """

    def __init__(self) -> None:
        # type de champ -> {terme normalise: classe}
        self._by_field: Dict[str, Dict[str, str]] = {f: {} for f in FIELD_TYPES}
        # type de champ -> {classe: [termes d'origine]}
        self._classes: Dict[str, Dict[str, List[str]]] = {f: {} for f in FIELD_TYPES}
        self.files: List[Dict[str, Any]] = []
        self.collisions: List[Dict[str, str]] = []
        self.content_hash: str = ""

    # ---------------- Interrogation ----------------

    def canonical(self, term: str, field: str) -> Optional[str]:
        """Classe canonique d'un terme, ou None s'il est inconnu."""
        if not term or field not in self._by_field:
            return None
        return self._by_field[field].get(normalize_term(term))

    def variants(self, term: str, field: str) -> List[str]:
        """Tous les equivalents declares d'un terme (lui compris), ou []."""
        cls = self.canonical(term, field)
        if not cls:
            return []
        return list(self._classes[field].get(cls, []))

    def canonicalize_tokens(self, text: str, field: str) -> str:
        """
        Remplace chaque token connu par sa classe. « Harry Dupont » et
        « Henri Dupont » donnent tous deux « HENRY DUPONT » : les metriques de
        chaine, inchangees, retournent alors 100.
        """
        if not text or field not in self._by_field:
            return text or ""
        table = self._by_field[field]
        out = []
        for token in normalize_term(text).split():
            out.append(table.get(token, token))
        return " ".join(out)

    def applied_equivalences(self, left: str, right: str, field: str) -> List[Dict[str, str]]:
        """
        Equivalences qui ont rapproche deux textes : uniquement les tokens
        DIFFERENTS ramenes a la meme classe. Sert la tracabilite du decision
        tree — un analyste doit pouvoir lire pourquoi deux noms dissemblables
        ont matche.
        """
        if field not in self._by_field:
            return []
        table = self._by_field[field]
        left_tokens = normalize_term(left).split()
        right_tokens = normalize_term(right).split()
        seen: Set[str] = set()
        applied = []
        for lt in left_tokens:
            lc = table.get(lt)
            if not lc:
                continue
            for rt in right_tokens:
                if rt == lt or table.get(rt) != lc:
                    continue
                key = f"{lt}|{rt}"
                if key in seen:
                    continue
                seen.add(key)
                applied.append({"source": lt, "target": rt, "class": lc, "field": field})
        return applied

    def stats(self) -> Dict[str, Any]:
        return {
            "content_hash": self.content_hash,
            "files": list(self.files),
            "collisions": list(self.collisions),
            "by_field": {
                field: {
                    "label": FIELD_LABELS[field],
                    "classes": len(self._classes[field]),
                    "terms": len(self._by_field[field]),
                }
                for field in FIELD_TYPES
            },
            "total_terms": sum(len(t) for t in self._by_field.values()),
        }

    # ---------------- Construction ----------------

    def _add_group(self, field: str, class_id: str, terms: List[str], source: str) -> int:
        added = 0
        bucket = self._by_field[field]
        for term in terms:
            key = normalize_term(term)
            if not key:
                continue
            existing = bucket.get(key)
            if existing and existing != class_id:
                # Un terme dans deux classes rendrait le criblage non
                # deterministe : on le signale au lieu de choisir au hasard
                self.collisions.append({
                    "field": field, "term": key, "classes": f"{existing} / {class_id}",
                    "file": source,
                })
                continue
            bucket[key] = class_id
            added += 1
        known = self._classes[field].setdefault(class_id, [])
        for term in terms:
            if term not in known:
                known.append(term)
        return added


def _parse_resource_document(doc: Any, source: str) -> Dict[str, Any]:
    """Valide la structure d'un fichier de ressources et la retourne."""
    if not isinstance(doc, dict):
        raise ResourceError(f"{source} : le fichier doit contenir un objet YAML.")
    field = str(doc.get("type") or "").strip()
    if field not in FIELD_TYPES:
        raise ResourceError(
            f"{source} : type de champ « {field or 'absent'} » inconnu "
            f"(attendus : {', '.join(FIELD_TYPES)}).")
    groups = doc.get("groups")
    if not isinstance(groups, list) or not groups:
        raise ResourceError(f"{source} : aucun groupe d'équivalence déclaré.")
    parsed_groups = []
    for index, group in enumerate(groups, start=1):
        if not isinstance(group, dict):
            raise ResourceError(f"{source} : groupe #{index} invalide (objet attendu).")
        class_id = str(group.get("id") or "").strip().upper()
        terms = group.get("terms")
        if not class_id:
            raise ResourceError(f"{source} : groupe #{index} sans identifiant `id`.")
        if not isinstance(terms, list) or len(terms) < 2:
            raise ResourceError(
                f"{source} : groupe « {class_id} » — au moins deux termes sont "
                "nécessaires pour déclarer une équivalence.")
        parsed_groups.append((class_id, [str(t) for t in terms if str(t).strip()]))
    return {"field": field, "groups": parsed_groups,
            "label": str(doc.get("label") or "").strip(),
            "source_note": str(doc.get("source") or "").strip()}


def load_index(directory: Path) -> ResourceIndex:
    """
    Charge tous les fichiers `.yaml`/`.yml` d'un repertoire en un index.

    Un fichier invalide leve ResourceError : mieux vaut un refus explicite
    qu'un criblage qui tourne avec une ressource partielle sans que personne
    ne le sache.
    """
    import yaml

    index = ResourceIndex()
    directory = Path(directory)
    if not directory.exists():
        logger.info(f"Répertoire de ressources absent ({directory}) : aucune équivalence chargée.")
        index.content_hash = hashlib.sha256(b"").hexdigest()[:16]
        return index

    digest = hashlib.sha256()
    for path in sorted(directory.glob("*.y*ml")):
        raw = path.read_text(encoding="utf-8")
        try:
            doc = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ResourceError(f"{path.name} : YAML invalide — {e}")
        parsed = _parse_resource_document(doc, path.name)
        terms_added = 0
        for class_id, terms in parsed["groups"]:
            terms_added += index._add_group(parsed["field"], class_id, terms, path.name)
        digest.update(raw.encode("utf-8"))
        index.files.append({
            "file": path.name,
            "field": parsed["field"],
            "label": parsed["label"] or FIELD_LABELS[parsed["field"]],
            "source": parsed["source_note"],
            "classes": len(parsed["groups"]),
            "terms": terms_added,
        })

    index.content_hash = digest.hexdigest()[:16]
    if index.collisions:
        # Signale mais ne bloque pas : la collision est tracee et visible dans
        # l'ecran de diagnostic, le premier declarant l'emporte
        logger.warning(f"Ressources : {len(index.collisions)} terme(s) en collision de classe.")
    logger.info(f"Ressources linguistiques chargées : {index.stats()['total_terms']} terme(s), "
                f"empreinte {index.content_hash}.")
    return index


# ------------------ INDEX ACTIF (partage par le criblage) ------------------

_lock = threading.Lock()
_active: Optional[ResourceIndex] = None
_active_dir: Optional[Path] = None


def default_directory() -> Path:
    from fiskr.config import config

    configured = ((config.get("resources") or {}).get("directory") or "").strip()
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / "resources"


def get_index() -> ResourceIndex:
    """Index actif, charge paresseusement au premier criblage."""
    global _active, _active_dir
    with _lock:
        if _active is None:
            _active_dir = default_directory()
            try:
                _active = load_index(_active_dir)
            except ResourceError as e:
                # Une ressource invalide ne doit pas empecher de cribler : on
                # repart sur un index vide (comportement d'avant la fonction)
                logger.error(f"Ressources non chargées : {e}")
                _active = ResourceIndex()
                _active.collisions.append({"field": "-", "term": "-", "classes": str(e), "file": "-"})
        return _active


def reload_index(directory: Optional[Path] = None) -> ResourceIndex:
    """Recharge l'index a chaud (remplacement atomique de l'instance)."""
    global _active, _active_dir
    target = Path(directory) if directory else default_directory()
    fresh = load_index(target)
    with _lock:
        _active = fresh
        _active_dir = target
    invalidate_context()
    return fresh


def set_index(index: Optional[ResourceIndex]) -> None:
    """Force l'index actif (tests). None = rechargement paresseux."""
    global _active
    with _lock:
        _active = index
    invalidate_context()


def active_fields(db=None) -> Set[str]:
    """
    Types de champ sur lesquels les equivalences s'appliquent effectivement.

    Sans session, on relit le reglage a chaud avec une session propre : le
    criblage appelle ce chemin depuis des contextes varies (API, batch,
    re-criblage). Tout est desactive par defaut — une installation existante
    ne change pas de comportement tant qu'un responsable n'a pas active un
    type et mesure l'ecart au cahier de tests.
    """
    from fiskr.settings import resource_fields

    try:
        if db is not None:
            return {f for f, on in resource_fields(db).items() if on}
        from fiskr.database import SessionLocal

        if SessionLocal is None:
            return set()
        session = SessionLocal()
        try:
            return {f for f, on in resource_fields(session).items() if on}
        finally:
            session.close()
    except Exception as e:  # jamais bloquant : au pire, aucune equivalence
        logger.debug(f"Champs de ressources indisponibles : {e}")
        return set()


_context_cache: Optional[Dict[str, Any]] = None


def current_context() -> Dict[str, Any]:
    """
    Contexte du criblage : index actif + types de champ actives.

    MIS EN CACHE : le criblage compare des milliers de paires par seconde,
    relire le reglage a chaque comparaison couterait une requete par candidat.
    Le cache est invalide par `invalidate_context()` — appele au rechargement
    des ressources et a toute modification du reglage d'activation.
    """
    global _context_cache
    if _context_cache is None:
        fields = active_fields()
        _context_cache = {"index": get_index() if fields else None, "fields": fields}
    return _context_cache


def invalidate_context() -> None:
    """Force la relecture du reglage au prochain criblage."""
    global _context_cache
    _context_cache = None


def canonical_for(text: str, field: str) -> Optional[str]:
    """Forme canonicalisee d'un texte si le type de champ est actif, sinon None."""
    ctx = current_context()
    if field not in ctx["fields"] or ctx["index"] is None:
        return None
    return ctx["index"].canonicalize_tokens(text, field)


def equivalences_for(left: str, right: str, field: str) -> List[Dict[str, str]]:
    """Equivalences ayant rapproche deux textes (trace du decision tree)."""
    ctx = current_context()
    if field not in ctx["fields"] or ctx["index"] is None:
        return []
    return ctx["index"].applied_equivalences(left, right, field)


def index_from_mapping(mapping: Dict[str, Dict[str, List[str]]]) -> ResourceIndex:
    """Index construit en memoire : {champ: {classe: [termes]}} (tests, essais)."""
    index = ResourceIndex()
    digest = hashlib.sha256()
    for field, groups in mapping.items():
        if field not in FIELD_TYPES:
            raise ResourceError(f"Type de champ inconnu : {field}")
        for class_id, terms in groups.items():
            index._add_group(field, class_id.upper(), list(terms), "<mémoire>")
        digest.update(json.dumps({field: groups}, sort_keys=True).encode("utf-8"))
    index.content_hash = digest.hexdigest()[:16]
    return index

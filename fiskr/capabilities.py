"""
Catalogue des capacites du moteur de rapprochement.

Ce module est volontairement SANS DEPENDANCE interne (il n'importe rien de
fiskr, sauf le contexte thread-local en fin de fichier) : il est la source de
verite lue par
- fiskr/settings.py -> activation par defaut et lecture du reglage,
- fiskr/blocking.py et fiskr/scoring.py -> gardes d'execution,
- l'API et le dashboard -> ecran de reglages genere depuis le catalogue.

Ajouter une capacite pilotable = ajouter UNE entree ici. Rien d'autre n'est a
declarer : elle devient activable, tracable, mesurable, traduisible et
affichee partout.

POURQUOI CE MODULE EXISTE
-------------------------
Le moteur applique une quarantaine de mecanismes de rapprochement dont la
quasi-totalite etait cablee en dur. Un responsable conformite ne pouvait ni
les voir, ni les desactiver, ni mesurer ce que chacun apporte — alors que
l'ACPR attend un dispositif de criblage *documente et justifie*.

CE QUI N'EST VOLONTAIREMENT PAS PILOTABLE ICI
---------------------------------------------
Les POIDS des metriques de similarite (`scoring.weights.*`). Ils ne sont pas
normalises : `compute_base_score` en fait une somme simple. Mettre un poids a
zero ne neutralise donc pas la metrique, cela CHANGE L'ECHELLE du score et
invalide tous les seuils. Les offrir en interrupteurs serait un piege ; ils
restent dans config.yaml.

Les equivalences linguistiques par type de champ (`resources.enabled_fields`)
sont deja pilotees et mesurees par leur propre ecran : ce catalogue ne les
duplique pas. Il pilote en revanche leur PREREQUIS au blocking, sans lequel
elles sont inertes (cf. CAP_BLOCKING_EQUIVALENCES).

OU LES CAPACITES AGISSENT, ET OU ELLES N'AGISSENT PAS
-----------------------------------------------------
Elles pilotent la COMPARAISON : generation des cles de blocking, normalisation
des noms compares, ajustements, rapprochement sur identifiants. Elles ne
pilotent PAS la normalisation faite a l'ingestion : ce qui est stocke est
verse au dossier reglementaire avec son instantane de liste, et le faire
dependre d'un reglage a chaud normaliserait deux fiches de la meme liste
differemment selon l'heure de leur import.

Consequence a connaitre : les capacites d'ecriture et de nettoyage
(translitteration, diacritiques, suffixes juridiques) agissent immediatement
sur la sonde CLIENT ; les fiches deja ingerees gardent la forme sous laquelle
elles ont ete stockees, jusqu'au prochain rechargement complet de leur liste.

CONVENTIONS
-----------
- `loss` est OBLIGATOIRE : c'est ce que l'etablissement perd en coupant la
  capacite, affiche comme avertissement dans l'ecran. On ne peut pas ajouter
  une bascule en oubliant d'expliquer son risque.
- `depends_on` declare les prerequis : une capacite dont un prerequis est
  coupe est INERTE, et l'ecran doit le dire plutot que de laisser croire
  qu'elle agit.
- Une capacite absente du reglage stocke reprend son defaut : ajouter une
  entree n'invalide aucune installation existante.
"""
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

# Canaux de criblage. Le filtrage transactionnel travaille sur des donnees de
# paiement bien plus pauvres (le pays est souvent absent, le nom est un champ
# libre) : les memes mecanismes n'y ont pas le meme sens, d'ou un reglage
# distinct — comme c'est deja le cas des layouts de blocking.
CHANNEL_SCREENING = "SCREENING"
CHANNEL_FILTERING = "FILTERING"
CHANNELS = (CHANNEL_SCREENING, CHANNEL_FILTERING)

# Familles d'affichage
FAMILY_SCRIPTS = "ecritures"
FAMILY_CANDIDATES = "candidats"
FAMILY_NAMES = "noms"
FAMILY_ADJUSTMENTS = "ajustements"
FAMILY_IDENTIFIERS = "identifiants"

FAMILY_LABELS = {
    FAMILY_SCRIPTS: "Écritures et normalisation",
    FAMILY_CANDIDATES: "Sélection des candidats",
    FAMILY_NAMES: "Variantes de noms comparées",
    FAMILY_ADJUSTMENTS: "Ajustements contextuels",
    FAMILY_IDENTIFIERS: "Rapprochement sur identifiants",
}

FAMILY_ORDER = (FAMILY_SCRIPTS, FAMILY_CANDIDATES, FAMILY_NAMES,
                FAMILY_ADJUSTMENTS, FAMILY_IDENTIFIERS)


@dataclass(frozen=True)
class Capability:
    """Un mecanisme du moteur qu'un responsable conformite peut arbitrer."""
    label: str
    family: str
    # Ce que l'etablissement PERD en coupant la capacite. Obligatoire.
    loss: str
    default_screening: bool = True
    default_filtering: bool = True
    channels: Tuple[str, ...] = CHANNELS
    # Capacites dont celle-ci depend : si l'une est coupee, celle-ci est inerte
    depends_on: Tuple[str, ...] = ()

    def default_for(self, channel: str) -> bool:
        if channel not in self.channels:
            return False
        return self.default_filtering if channel == CHANNEL_FILTERING else self.default_screening


# ------------------ IDENTIFIANTS DES CAPACITES ------------------
# Constantes plutot que chaines libres : une faute de frappe dans une garde du
# moteur passerait sinon inapercue (la capacite serait vue comme inactive).

CAP_TRANSLIT = "translit"
CAP_DIACRITICS = "diacritics"
CAP_NOISE_WORDS = "noise_words"

CAP_BLOCKING_PHONETIC = "blocking.phonetic"
CAP_BLOCKING_EQUIVALENCES = "blocking.equivalences"
CAP_BLOCKING_COUNTRY_WILDCARD = "blocking.country_wildcard"

CAP_NAMES_REVERSED = "names.reversed_order"
CAP_NAMES_MAIDEN = "names.maiden"
CAP_NAMES_ALIASES_LISTED = "names.aliases_listed"
CAP_NAMES_ALIASES_CLIENT = "names.aliases_client"

CAP_ADJUST_DOB = "adjust.dob"
CAP_ADJUST_GENDER = "adjust.gender"
CAP_ADJUST_GEOGRAPHY = "adjust.geography"
CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL = "adjust.geography.missing_is_neutral"

CAP_HARD_LEI = "hard.lei"
CAP_HARD_BIC = "hard.bic"
CAP_HARD_TAX_ID = "hard.tax_id"
CAP_HARD_CRYPTO = "hard.crypto"
CAP_HARD_PASSPORT = "hard.passport"
CAP_HARD_NATIONAL_REGISTRY = "hard.national_registry"
CAP_HARD_NATIONAL_ID = "hard.national_id"
CAP_HARD_VESSEL = "hard.vessel"
CAP_HARD_AIRCRAFT = "hard.aircraft"
CAP_HARD_OTHER_DOCUMENTS = "hard.other_documents"

# Sous-bascules par ECRITURE. La detection d'ecriture (quality.detect_scripts)
# nomme l'ecriture rencontree ; ces capacites decident, ecriture par ecriture,
# si le texte est translittere avant comparaison.
SCRIPT_CYRILLIC = "cyrillic"
SCRIPT_HAN = "han"
SCRIPT_ARABIC = "arabic"
SCRIPT_HANGUL = "hangul"
SCRIPT_KANA = "kana"
SCRIPT_HEBREW = "hebrew"
SCRIPT_GREEK = "greek"
SCRIPT_THAI = "thai"
SCRIPT_DEVANAGARI = "devanagari"
SCRIPT_OTHER = "other"

SCRIPTS = (SCRIPT_CYRILLIC, SCRIPT_HAN, SCRIPT_ARABIC, SCRIPT_HANGUL, SCRIPT_KANA,
           SCRIPT_HEBREW, SCRIPT_GREEK, SCRIPT_THAI, SCRIPT_DEVANAGARI, SCRIPT_OTHER)

SCRIPT_LABELS = {
    SCRIPT_CYRILLIC: "cyrillique",
    SCRIPT_HAN: "han (chinois, kanji japonais)",
    SCRIPT_ARABIC: "arabe",
    SCRIPT_HANGUL: "hangul (coréen)",
    SCRIPT_KANA: "kana (hiragana, katakana)",
    SCRIPT_HEBREW: "hébreu",
    SCRIPT_GREEK: "grec",
    SCRIPT_THAI: "thaï",
    SCRIPT_DEVANAGARI: "devanagari",
    SCRIPT_OTHER: "autres écritures",
}


def script_capability(script: str) -> str:
    """Identifiant de la capacite de translitteration d'une ecriture donnee."""
    return f"{CAP_TRANSLIT}.{script}"


CAPABILITY_CATALOG: Dict[str, Capability] = {
    # ------------------ ÉCRITURES ET NORMALISATION ------------------
    CAP_TRANSLIT: Capability(
        label="Translittération des écritures non latines",
        family=FAMILY_SCRIPTS,
        loss="Les noms écrits en cyrillique, chinois, arabe, coréen ou japonais "
             "n'atteignent plus aucune métrique de comparaison : le double "
             "métaphone ne connaît que l'alphabet latin et rend une clé vide. "
             "Tous les alias en écriture d'origine de l'OFAC et de l'ONU "
             "deviennent invisibles.",
    ),
    CAP_DIACRITICS: Capability(
        label="Aplatissement des diacritiques (Müller → Muller)",
        family=FAMILY_SCRIPTS,
        loss="« Müller » cesse de rapprocher « MULLER », « Ibáñez » cesse de "
             "rapprocher « IBANEZ » : les métriques de chaîne sont sensibles "
             "aux accents.",
    ),
    CAP_NOISE_WORDS: Capability(
        label="Suppression des suffixes juridiques (SA, SARL, LLC, GMBH…)",
        family=FAMILY_SCRIPTS,
        loss="« ACME SARL » et « ACME LLC » restent comparés avec leur suffixe, "
             "qui pèse dans la distance d'édition et fait baisser le score de "
             "deux raisons sociales pourtant identiques.",
    ),

    # ------------------ SÉLECTION DES CANDIDATS ------------------
    CAP_BLOCKING_PHONETIC: Capability(
        label="Clés phonétiques (double métaphone)",
        family=FAMILY_CANDIDATES,
        loss="Deux graphies proches à l'oreille — « Shmit » et « Schmidt » — "
             "ne tombent plus dans le même seau et ne sont donc JAMAIS "
             "comparées : le scoring ne les voit même pas.",
    ),
    CAP_BLOCKING_EQUIVALENCES: Capability(
        label="Clés d'équivalence linguistique au blocking",
        family=FAMILY_CANDIDATES,
        loss="Les tables d'équivalences deviennent INERTES même si elles sont "
             "activées : sans clé commune, « Henri » et « Harry » ne sont pas "
             "candidats l'un pour l'autre et la table n'a rien à rapprocher.",
    ),
    CAP_BLOCKING_COUNTRY_WILDCARD: Capability(
        label="Joker « pays inconnu » à l'interrogation",
        family=FAMILY_CANDIDATES,
        loss="Toute fiche listée dont la source ne publie aucun pays redevient "
             "structurellement inatteignable : elle tombe dans la partition "
             "« pays inconnu » que ne rejoint aucun client ayant un pays. Les "
             "listes d'alerte de régulateurs n'en publient presque jamais.",
    ),

    # ------------------ VARIANTES DE NOMS ------------------
    CAP_NAMES_REVERSED: Capability(
        label="Ordre de nom inversé (NOM Prénom)",
        family=FAMILY_NAMES,
        loss="Les listes officielles écrivent les noms d'Asie de l'Est "
             "patronyme en tête ; le référentiel client concatène « prénom nom ». "
             "Sans cette variante, les deux chaînes sont systématiquement "
             "inversées et le score s'effondre — seul le token sort (20 % du "
             "poids) résiste, ce qui ne suffit à franchir aucun seuil.",
        # Le filtrage compare des champs libres de paiement, ou l'ordre des mots
        # n'est de toute facon pas fiable : le token sort y joue deja ce role.
        default_filtering=False,
    ),
    CAP_NAMES_MAIDEN: Capability(
        label="Noms de jeune fille (client et fiche listée)",
        family=FAMILY_NAMES,
        loss="Une personne listée sous son nom de naissance cesse d'être "
             "rapprochée d'un client connu sous son nom d'usage, et "
             "réciproquement.",
    ),
    CAP_NAMES_ALIASES_LISTED: Capability(
        label="Alias des fiches listées (haute priorité)",
        family=FAMILY_NAMES,
        loss="Seul le nom principal de chaque fiche est comparé. Les alias "
             "officiels — souvent la graphie sous laquelle la contrepartie se "
             "présente réellement — ne sont plus criblés du tout.",
    ),
    CAP_NAMES_ALIASES_CLIENT: Capability(
        label="Alias du référentiel client",
        family=FAMILY_NAMES,
        loss="Les dénominations alternatives déclarées au référentiel (nom "
             "commercial, ancienne raison sociale) ne sont plus criblées.",
    ),

    # ------------------ AJUSTEMENTS CONTEXTUELS ------------------
    CAP_ADJUST_DOB: Capability(
        label="Ajustement par date de naissance",
        family=FAMILY_ADJUSTMENTS,
        loss="Perte du discriminant le plus fort sur les homonymes : deux "
             "« Mohamed Ali » nés à quarante ans d'écart cessent d'être "
             "départagés et repassent tous deux au-dessus du seuil.",
        # Un message de paiement ne porte quasiment jamais de date de naissance.
        default_filtering=False,
    ),
    CAP_ADJUST_GENDER: Capability(
        label="Malus de conflit de genre",
        family=FAMILY_ADJUSTMENTS,
        loss="Les couples homonymes de genres opposés ne sont plus écartés.",
        default_filtering=False,
    ),
    CAP_ADJUST_GEOGRAPHY: Capability(
        label="Ajustement géographique (bonus/malus pays)",
        family=FAMILY_ADJUSTMENTS,
        loss="La confirmation géographique disparaît : un client russe et un "
             "listé russe ne sont plus rapprochés par leur pays commun, et un "
             "client sans lien géographique n'est plus écarté.",
        default_filtering=False,
    ),
    CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL: Capability(
        label="Un pays manquant est neutre (au lieu d'un malus)",
        family=FAMILY_ADJUSTMENTS,
        loss="Comportement historique rétabli : l'absence de pays d'un côté "
             "vaut MALUS et non neutre. Un référentiel client mal renseigné "
             "voit alors ses scores baisser sans qu'aucune information ne le "
             "justifie — risque de faux négatifs.",
        # Defaut OFF : le comportement historique (pays manquant = malus) est
        # conserve tel quel. L'activer ELARGIT le perimetre d'alertes, donc il
        # se mesure avant de s'appliquer.
        default_screening=False,
        default_filtering=False,
        depends_on=(CAP_ADJUST_GEOGRAPHY,),
    ),

    # ------------------ RAPPROCHEMENT SUR IDENTIFIANTS ------------------
    # Un hit force ALERT a 100/100 et CONTOURNE le seuil de coupure : ce sont
    # les capacites dont la desactivation est la plus lourde de consequences.
    CAP_HARD_LEI: Capability(
        label="Identifiant d'entité juridique (LEI)",
        family=FAMILY_IDENTIFIERS,
        loss="Un client et une fiche portant le MÊME LEI mais des raisons "
             "sociales différentes (« ACME HOLDING SA » vs « ACME HLDG ») "
             "retombent sous le seuil : faux négatif réglementaire sur une "
             "identité pourtant certaine.",
    ),
    CAP_HARD_BIC: Capability(
        label="Code BIC/SWIFT",
        family=FAMILY_IDENTIFIERS,
        loss="Un établissement listé cesse d'être identifié par son code BIC, "
             "alors que c'est l'identifiant le plus fiable d'un message de "
             "paiement.",
    ),
    CAP_HARD_TAX_ID: Capability(
        label="Numéro fiscal (INN, TVA…)",
        family=FAMILY_IDENTIFIERS,
        loss="Perte du rapprochement certain par numéro fiscal.",
    ),
    CAP_HARD_CRYPTO: Capability(
        label="Adresse de portefeuille crypto",
        family=FAMILY_IDENTIFIERS,
        loss="Les portefeuilles désignés par l'OFAC ne sont plus reconnus, "
             "alors que l'adresse est un identifiant exact et non ambigu.",
    ),
    CAP_HARD_PASSPORT: Capability(
        label="Passeport (numéro + pays émetteur)",
        family=FAMILY_IDENTIFIERS,
        loss="Perte du rapprochement certain par document de voyage.",
    ),
    CAP_HARD_NATIONAL_REGISTRY: Capability(
        label="Registre national d'entreprises (SIREN, TVA…)",
        family=FAMILY_IDENTIFIERS,
        loss="Perte du rapprochement certain par immatriculation.",
    ),
    CAP_HARD_NATIONAL_ID: Capability(
        label="Pièce d'identité nationale",
        family=FAMILY_IDENTIFIERS,
        loss="Perte du rapprochement certain par carte d'identité.",
    ),
    CAP_HARD_VESSEL: Capability(
        label="Navire (IMO, MMSI, indicatif radio)",
        family=FAMILY_IDENTIFIERS,
        loss="Un navire désigné n'est plus identifiable que par son nom — or "
             "un navire change de nom bien plus souvent que de numéro IMO. "
             "C'est le SEUL discriminant fiable de cette nature d'entité.",
    ),
    CAP_HARD_AIRCRAFT: Capability(
        label="Aéronef (immatriculation)",
        family=FAMILY_IDENTIFIERS,
        loss="Même conséquence que pour les navires : l'immatriculation est le "
             "seul discriminant fiable d'un aéronef.",
    ),
    CAP_HARD_OTHER_DOCUMENTS: Capability(
        label="Autres documents et enregistrements",
        family=FAMILY_IDENTIFIERS,
        loss="Perte du rapprochement certain sur les documents hors passeport "
             "et pièce d'identité.",
    ),
}

# Sous-bascules par ecriture, derivees pour ne pas ecrire dix fois la meme
# entree : leur libelle et leur perte se deduisent du nom de l'ecriture.
for _script in SCRIPTS:
    CAPABILITY_CATALOG[script_capability(_script)] = Capability(
        label=f"Translittérer le {SCRIPT_LABELS[_script]}"
              if _script != SCRIPT_OTHER else "Translittérer les autres écritures",
        family=FAMILY_SCRIPTS,
        loss=f"Les noms écrits en {SCRIPT_LABELS[_script]} ne sont plus "
             f"translittérés : ils n'atteignent aucune métrique de comparaison "
             f"et sortent du périmètre de criblage — sauf ceux que les tables "
             f"d'équivalences connaissent déjà, qui restent rapprochés par "
             f"elles. La perte est donc réelle mais inégale : elle se mesure.",
        depends_on=(CAP_TRANSLIT,),
    )
del _script


def capabilities_for_channel(channel: str) -> Tuple[str, ...]:
    """Identifiants des capacites ayant un sens sur ce canal, dans l'ordre."""
    return tuple(cap_id for cap_id, cap in CAPABILITY_CATALOG.items()
                 if channel in cap.channels)


def defaults_for_channel(channel: str) -> Dict[str, bool]:
    """Activation par defaut de chaque capacite sur ce canal."""
    return {cap_id: CAPABILITY_CATALOG[cap_id].default_for(channel)
            for cap_id in capabilities_for_channel(channel)}


def resolve_inactive_dependencies(active: Iterable[str]) -> Dict[str, Tuple[str, ...]]:
    """
    Capacites actives mais INERTES faute d'un prerequis.

    Sert a l'ecran : une bascule cochee dont le prerequis est coupe ne fait
    rien, et il vaut mieux le dire que laisser croire qu'elle agit.
    """
    active_set = set(active)
    inert: Dict[str, Tuple[str, ...]] = {}
    for cap_id in active_set:
        cap = CAPABILITY_CATALOG.get(cap_id)
        if cap is None:
            continue
        missing = tuple(dep for dep in cap.depends_on if dep not in active_set)
        if missing:
            inert[cap_id] = missing
    return inert


# ------------------ CONTEXTE D'EXECUTION ------------------
# Meme mecanique que fiskr/resources.py : cache global relu paresseusement, et
# surcharge THREAD-LOCAL pour qu'une mesure d'impact tourne en tache de fond
# sans que les criblages servis en parallele ne voient quoi que ce soit
# changer. Regle d'or : le moteur lit le CONTEXTE, jamais le reglage en base
# dans une boucle de comparaison — ce serait une requete SQL par candidat.

_context_cache: Dict[str, FrozenSet[str]] = {}
_local = threading.local()


def _load_active(channel: str) -> FrozenSet[str]:
    """Lit le reglage en base. Jamais bloquant : degrade sur les defauts."""
    try:
        from fiskr.settings import engine_capabilities
        return frozenset(cap_id for cap_id, on in engine_capabilities(None, channel).items() if on)
    except Exception:  # pragma: no cover - degradation defensive
        return frozenset(cap_id for cap_id, on in defaults_for_channel(channel).items() if on)


def current_context(channel: str = CHANNEL_SCREENING) -> FrozenSet[str]:
    """
    Capacites actives sur ce canal.

    Une surcharge posee par `use_context()` sur le thread courant l'emporte,
    sans jamais toucher les autres threads.
    """
    override = getattr(_local, "override", None)
    if override is not None and channel in override:
        return override[channel]
    if channel not in _context_cache:
        _context_cache[channel] = _load_active(channel)
    return _context_cache[channel]


@lru_cache(maxsize=256)
def _effective_capabilities(active: FrozenSet[str], channel: str) -> FrozenSet[str]:
    """
    Capacites REELLEMENT actives : presentes ET dont tous les prerequis directs
    le sont aussi. Memoise sur (contexte, canal) — le nombre de contextes
    distincts vus dans un processus est minuscule (souvent 1), donc ceci
    transforme la resolution des prerequis, faite des centaines de milliers de
    fois par criblage, en une seule intersection par contexte. Un changement de
    reglage produit un nouveau frozenset, donc une nouvelle entree : aucune
    invalidation explicite necessaire.
    """
    return frozenset(
        c for c in active
        if (cap := CAPABILITY_CATALOG.get(c)) is not None
        and all(dep in active for dep in cap.depends_on)
    )


def is_active(capability: str, channel: str = CHANNEL_SCREENING) -> bool:
    """
    Vrai si la capacite est active ET tous ses prerequis le sont.

    La verification des prerequis est faite une fois par contexte
    (cf. `_effective_capabilities`), puis chaque appel n'est plus qu'un test
    d'appartenance a un frozenset.
    """
    return capability in _effective_capabilities(current_context(channel), channel)


def describe_context(channel: str = CHANNEL_SCREENING) -> Optional[Dict[str, object]]:
    """
    Ecart entre le parametrage EFFECTIF et les defauts du catalogue.

    Destine au decision_tree du journal d'audit. Une alerte doit rester
    explicable des annees plus tard, or le reglage des capacites vit en base :
    il n'est pas recopie dans le `config_state` fige au criblage. Sans cette
    trace, un controleur relisant une alerte de 2026 en 2029 ne peut pas
    savoir quels mecanismes tournaient ce jour-la.

    On n'ecrit QUE l'ecart aux defauts, pour deux raisons :
    - une installation au parametrage standard produit exactement le meme
      arbre de decision qu'avant l'introduction du catalogue (la cle est
      absente), comme le fait deja `resource_equivalences` ;
    - l'ecart est precisement l'information que la lecture du code ne donne
      pas. Enumerer les trente-quatre capacites a chaque alerte n'apprendrait
      rien de plus et alourdirait chaque ligne du journal.

    Renvoie None quand le moteur tourne au parametrage standard.
    """
    disabled, enabled, inert = _context_delta(current_context(channel), channel)
    if not (disabled or enabled or inert):
        return None
    trace: Dict[str, object] = {"channel": channel}
    # Listes fraiches a chaque appel : le resultat memoise (tuples immuables)
    # n'est jamais partage ni mutable par l'appelant, qui le range dans un
    # decision_tree.
    if disabled:
        trace["disabled"] = list(disabled)
    if enabled:
        trace["enabled"] = list(enabled)
    if inert:
        trace["inert"] = list(inert)
    return trace


@lru_cache(maxsize=256)
def _context_delta(active: FrozenSet[str], channel: str):
    """Ecart aux defauts (desactivees / activees / inertes), memoise par
    (contexte, canal) — appele une fois par rapprochement, invariant sur tout
    un criblage. Renvoie des tuples immuables (surs a mettre en cache)."""
    defaults = defaults_for_channel(channel)
    disabled = tuple(sorted(cap_id for cap_id, on in defaults.items()
                            if on and cap_id not in active))
    enabled = tuple(sorted(cap_id for cap_id, on in defaults.items()
                           if not on and cap_id in active))
    inert = tuple(sorted(resolve_inactive_dependencies(active)))
    return disabled, enabled, inert


@contextmanager
def use_context(channel: str, active: Iterable[str]):
    """
    Force les capacites actives d'un canal sur le thread courant.

    Sert a mesurer : cribler un panel une fois sous le parametrage actuel et
    une fois sous un parametrage candidat, sans que la production servie en
    parallele ne voie quoi que ce soit changer. Restaure toujours l'etat
    anterieur, y compris sur exception, et s'imbrique.
    """
    previous = getattr(_local, "override", None)
    merged = dict(previous or {})
    merged[channel] = frozenset(active or ())
    _local.override = merged
    try:
        yield merged
    finally:
        _local.override = previous


def invalidate_context() -> None:
    """Force la relecture du reglage au prochain criblage."""
    _context_cache.clear()

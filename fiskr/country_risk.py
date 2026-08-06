"""
Risque géographique GAFI/FATF — juridictions à haut risque.

Le criblage de Fiskr rapproche des NOMS de listes de sanctions. Il lui
manquait la lentille COMPLEMENTAIRE qu'attend tout dispositif LCB-FT : le
risque porté par les JURIDICTIONS rattachées à un client ou à une partie de
paiement, indépendamment de tout nom listé. Le GAFI publie deux listes,
révisées à chaque plénière (~3 fois par an) :

- « Appel à l'action » (black list) : déficiences stratégiques graves —
  contre-mesures / vigilance renforcée obligatoire (Iran, Corée du Nord,
  Myanmar au 19 juin 2026).
- « Surveillance renforcée » (grey list) : juridictions coopérant avec le
  GAFI pour corriger leurs déficiences — vigilance accrue recommandée.

Ces listes bougent : elles sont donc SURCHARGEABLES à chaud par config.yaml
(bloc `country_risk`) sans toucher au code. Le référentiel intégré ci-dessous
sert de valeur par défaut et porte sa date de plénière (`as_of`) pour que
personne ne l'applique en croyant qu'il est à jour alors qu'une plénière a eu
lieu depuis. AUCUN accès réseau : c'est une donnée de référence, pas une
source à synchroniser.

Volontairement HORS du moteur de score : ce module n'altère ni le
`decision_tree`, ni le `final_score`, ni le verdict — il ajoute une lecture
de risque à côté. Le criblage par nom et le cahier de tests restent
strictement inchangés.
"""
from typing import Dict, List, Optional, Any, Iterable

# ------------------ REFERENTIEL INTEGRE (valeur par defaut) ------------------
# Source : GAFI/FATF, plénière du 19 juin 2026. À réviser après chaque
# plénière — soit ici, soit (sans redéploiement) via config.yaml.
BUILTIN_AS_OF = "2026-06-19"

# Appel à l'action (contre-mesures / vigilance renforcée obligatoire).
BUILTIN_BLACKLIST: Dict[str, Dict[str, str]] = {
    "IR": {"en": "Iran", "fr": "Iran"},
    "KP": {"en": "North Korea (DPRK)", "fr": "Corée du Nord (RPDC)"},
    "MM": {"en": "Myanmar", "fr": "Myanmar (Birmanie)"},
}

# Surveillance renforcée (grey list) — 22 juridictions au 19 juin 2026.
BUILTIN_GREYLIST: Dict[str, Dict[str, str]] = {
    "AO": {"en": "Angola", "fr": "Angola"},
    "BO": {"en": "Bolivia", "fr": "Bolivie"},
    "BA": {"en": "Bosnia and Herzegovina", "fr": "Bosnie-Herzégovine"},
    "BG": {"en": "Bulgaria", "fr": "Bulgarie"},
    "CM": {"en": "Cameroon", "fr": "Cameroun"},
    "CI": {"en": "Côte d'Ivoire", "fr": "Côte d'Ivoire"},
    "CD": {"en": "DR Congo", "fr": "RD Congo"},
    "HT": {"en": "Haiti", "fr": "Haïti"},
    "IQ": {"en": "Iraq", "fr": "Irak"},
    "KE": {"en": "Kenya", "fr": "Kenya"},
    "KW": {"en": "Kuwait", "fr": "Koweït"},
    "LA": {"en": "Lao PDR", "fr": "Laos"},
    "LB": {"en": "Lebanon", "fr": "Liban"},
    "MC": {"en": "Monaco", "fr": "Monaco"},
    "NP": {"en": "Nepal", "fr": "Népal"},
    "PG": {"en": "Papua New Guinea", "fr": "Papouasie-Nouvelle-Guinée"},
    "SS": {"en": "South Sudan", "fr": "Soudan du Sud"},
    "SY": {"en": "Syria", "fr": "Syrie"},
    "VE": {"en": "Venezuela", "fr": "Venezuela"},
    "VN": {"en": "Vietnam", "fr": "Viêt Nam"},
    "VG": {"en": "British Virgin Islands", "fr": "Îles Vierges britanniques"},
    "YE": {"en": "Yemen", "fr": "Yémen"},
}

# Le noir prime sur le gris : « appel à l'action » est plus sévère.
TIER_BLACKLIST = "BLACKLIST"
TIER_GREYLIST = "GREYLIST"
_TIER_RANK = {TIER_BLACKLIST: 2, TIER_GREYLIST: 1}


def _normalise_iso2(value: Any) -> Optional[str]:
    """Code pays ISO 3166-1 alpha-2 en majuscules, ou None si non exploitable."""
    if not value:
        return None
    code = str(value).strip().upper()
    return code if len(code) == 2 and code.isalpha() else None


def _reference() -> Dict[str, Any]:
    """
    Référentiel effectif : intégré par défaut, surchargé à chaud par le bloc
    `country_risk` de config.yaml quand il est présent (le fichier est relu
    à chaud par ailleurs). Un bloc partiel remplace uniquement ce qu'il
    définit — un `blacklist` fourni remplace le blacklist intégré, etc.
    """
    from fiskr.config import config
    cfg = (config.get("country_risk") or {})

    def _merge(builtin: Dict[str, Dict[str, str]], key: str) -> Dict[str, Dict[str, str]]:
        raw = cfg.get(key)
        if raw is None:
            return dict(builtin)
        # Accepte une liste d'ISO2 (les noms retombent sur l'intégré si connu)
        # ou un mapping {ISO2: {en, fr}}.
        out: Dict[str, Dict[str, str]] = {}
        if isinstance(raw, dict):
            for code, names in raw.items():
                iso = _normalise_iso2(code)
                if iso:
                    out[iso] = names if isinstance(names, dict) else builtin.get(iso, {"en": iso, "fr": iso})
        elif isinstance(raw, (list, tuple)):
            for code in raw:
                iso = _normalise_iso2(code)
                if iso:
                    out[iso] = builtin.get(iso, {"en": iso, "fr": iso})
        return out

    return {
        "as_of": str(cfg.get("as_of") or BUILTIN_AS_OF),
        "blacklist": _merge(BUILTIN_BLACKLIST, "blacklist"),
        "greylist": _merge(BUILTIN_GREYLIST, "greylist"),
        "overridden": bool(cfg),
    }


def reference() -> Dict[str, Any]:
    """Référentiel effectif pour affichage : as_of + listes nommées triées."""
    ref = _reference()

    def _rows(mapping, tier):
        return sorted(
            ({"country": iso, "tier": tier, **names} for iso, names in mapping.items()),
            key=lambda r: r["en"],
        )

    return {
        "as_of": ref["as_of"],
        "overridden": ref["overridden"],
        "source": "GAFI/FATF — jurisdictions under increased monitoring & call for action",
        "blacklist": _rows(ref["blacklist"], TIER_BLACKLIST),
        "greylist": _rows(ref["greylist"], TIER_GREYLIST),
    }


def classify(country: Any) -> Optional[Dict[str, str]]:
    """
    Classe un pays. Retourne None si non listé, sinon
    {country, tier, en, fr}. Le noir prime : un pays présent sur les deux
    listes (ne devrait pas arriver) serait rendu comme BLACKLIST.
    """
    iso = _normalise_iso2(country)
    if not iso:
        return None
    ref = _reference()
    if iso in ref["blacklist"]:
        return {"country": iso, "tier": TIER_BLACKLIST, **ref["blacklist"][iso]}
    if iso in ref["greylist"]:
        return {"country": iso, "tier": TIER_GREYLIST, **ref["greylist"][iso]}
    return None


def assess(countries: Iterable[Any]) -> Optional[Dict[str, Any]]:
    """
    Évalue un ensemble de pays (nationalité, résidence, naissance,
    immatriculation…). Retourne None si aucun n'est listé, sinon un résumé :
    - `tier` : le niveau le plus sévère rencontré (BLACKLIST > GREYLIST) ;
    - `matches` : la liste dédupliquée des pays listés, noir d'abord.
    Idempotent et sans effet de bord — utilisable en lecture (rendu d'alerte)
    comme dans la réponse de criblage.
    """
    seen: Dict[str, Dict[str, str]] = {}
    for c in countries or []:
        hit = classify(c)
        if hit and hit["country"] not in seen:
            seen[hit["country"]] = hit
    if not seen:
        return None
    matches = sorted(seen.values(),
                     key=lambda h: (-_TIER_RANK[h["tier"]], h["en"]))
    return {
        "tier": matches[0]["tier"],
        "matches": matches,
        "as_of": _reference()["as_of"],
    }


def client_countries(client: Dict[str, Any]) -> List[str]:
    """
    Rassemble tous les codes pays ISO2 rattachés à un profil client, quel que
    soit le schéma d'entrée (sonde temps réel, ligne CLIENT_BASE, partie de
    paiement) : nationalité, résidence, naissance, immatriculation, et les
    variantes historiques de champ.
    """
    out: List[str] = []

    def _add(v):
        iso = _normalise_iso2(v)
        if iso:
            out.append(iso)

    # Structure imbriquée { nationality: [...], residence: [...], ... }
    nested = client.get("client_countries") or client.get("countries") or {}
    if isinstance(nested, dict):
        for key in ("nationality", "residence", "birth_country",
                    "registration_country", "citizenship",
                    "jurisdiction_country"):
            for v in (nested.get(key) or []):
                _add(v)
    # Champs plats
    for key in ("nationality", "client_country", "country",
                "residence_country", "birth_country", "registration_country",
                "jurisdiction_country"):
        _add(client.get(key))
    # Dédup en conservant l'ordre
    return list(dict.fromkeys(out))


def assess_client(client: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Résumé de risque géographique pour un profil client (ou None)."""
    return assess(client_countries(client))

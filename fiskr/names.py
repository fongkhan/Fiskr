"""
Moteur de detection et de decoupage des noms d'individus.

Utilise par tous les connecteurs d'import (EUR-Lex, SSIE, CSV, PDF, ajout
manuel) pour structurer un nom complet en prenom(s) / nom de famille.

Regles de detection, par ordre de priorite :
1. Format "NOM, Prenoms"        -> la virgule separe famille et prenoms.
2. Signal typographique         -> les listes officielles (EUR-Lex, ONU)
   ecrivent le nom de famille en CAPITALES et les prenoms en casse mixte
   ("Igor Yuryevich BABKIN"). Tous les tokens en capitales forment le nom
   de famille (avec leurs particules adjacentes : bin, van, Le, al...),
   le reste forme les prenoms, quel que soit l'ordre des blocs.
3. Repli                        -> premier token = prenom, reste = nom.
"""
import re
from typing import Any, Dict

# Particules onomastiques rattachees au nom de famille lorsqu'elles jouxtent
# un bloc en capitales (Usama bin LADIN, Jean-Marie Le PEN, Aicha al ASSAD...)
_FAMILY_PARTICLES = {
    "de", "da", "das", "dos", "del", "della", "di", "du", "der", "den", "ter",
    "van", "von", "le", "la", "les", "mac", "mc", "saint", "st",
    "bin", "ben", "ibn", "abu", "abou", "al", "el", "ould", "oul",
}


def _is_family_cased(token: str) -> bool:
    """Vrai si le token est ecrit en capitales (au moins 2 lettres, aucune minuscule)."""
    letters = [c for c in token if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


def parse_individual_name(full_name: str) -> Dict[str, str]:
    """
    Decoupe un nom complet d'individu en {first_name, last_name, maiden_name}.
    Gere les prenoms multiples grace au signal typographique des listes
    officielles ("Aleksandr Vladimirovich GUTSAN" -> prenoms "Aleksandr
    Vladimirovich", famille "GUTSAN").
    """
    name = re.sub(r"\s+", " ", (full_name or "")).strip()
    if not name:
        return {"first_name": "", "last_name": "", "maiden_name": ""}

    # 1. Format "NOM, Prenoms"
    if "," in name:
        last, _, first = name.partition(",")
        return {"first_name": first.strip(), "last_name": last.strip(), "maiden_name": ""}

    tokens = name.split()
    if len(tokens) == 1:
        return {"first_name": "", "last_name": name, "maiden_name": ""}

    # 2. Signal typographique : nom de famille en CAPITALES, prenoms en casse mixte
    family_flags = [_is_family_cased(t) for t in tokens]
    if any(family_flags) and not all(family_flags):
        family_idx = {i for i, flagged in enumerate(family_flags) if flagged}
        # Rattache les particules adjacentes au bloc famille (propagation)
        changed = True
        while changed:
            changed = False
            for i, token in enumerate(tokens):
                if i in family_idx:
                    continue
                if token.lower().strip("'-.") in _FAMILY_PARTICLES and \
                   ((i + 1) in family_idx or (i - 1) in family_idx):
                    family_idx.add(i)
                    changed = True
        first = " ".join(t for i, t in enumerate(tokens) if i not in family_idx)
        last = " ".join(t for i, t in enumerate(tokens) if i in family_idx)
        if first and last:
            return {"first_name": first, "last_name": last, "maiden_name": ""}

    # 3. Repli : premier token = prenom, reste = nom de famille
    return {"first_name": tokens[0], "last_name": " ".join(tokens[1:]), "maiden_name": ""}


def ensure_parsed_name(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    Complete individual_name_parsed sur un enregistrement pivot d'individu :
    - conserve tel quel un decoupage deja fourni par la source (OFAC XML...),
    - sinon utilise les colonnes explicites first_name / last_name (CSV),
    - sinon applique le moteur de detection sur le nom principal.
    Sans effet pour les entites, navires et aeronefs.
    """
    if item.get("entity_type") not in ("I", "PP"):
        return item

    parsed = item.get("individual_name_parsed") or {}
    if (parsed.get("first_name") or "").strip() or (parsed.get("last_name") or "").strip():
        return item

    first = (item.get("first_name") or "").strip()
    last = (item.get("last_name") or "").strip()
    maiden = (parsed.get("maiden_name") or item.get("maiden_name") or "").strip()
    if not (first or last):
        auto = parse_individual_name(item.get("primary_name") or item.get("name") or "")
        first, last = auto["first_name"], auto["last_name"]

    item["individual_name_parsed"] = {"first_name": first, "last_name": last, "maiden_name": maiden}
    return item

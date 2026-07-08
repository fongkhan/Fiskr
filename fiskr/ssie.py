"""
Smart Sanctions Ingestion Engine (SSIE) Connector.

Porte le pipeline SSIE dans l'import de listes Fiskr, en 3 phases sequentielles :
1. DECOUVERTE  : streaming du XML pour construire le dictionnaire de reference
                 (ID de type de caracteristique -> Libelle).
2. RESOLUTION  : streaming des listes (entites) et jointure dynamique de leurs
                 caracteristiques (Features) avec le dictionnaire de reference.
3. RESTITUTION : pivot des caracteristiques resolues vers le schema de criblage
                 Fiskr (25 champs reglementaires AML/CFT).

Le parser est structurellement agnostique : les balises pivots sont definies par
des selecteurs externes (section `ssie` de config.yaml, surchargables a l'import),
ce qui permet de supporter OFAC Advanced, SWIFT SLD ou tout autre flux XML
reference-croise sans codage en dur.
"""
import re
import logging
from typing import Any, Dict, Generator, List, Optional, Tuple
import xml.etree.ElementTree as ET

from fiskr.ingest import categorize_aliases

logger = logging.getLogger("fiskr.ssie")

# Selecteurs par defaut (format OFAC Advanced), identiques au config.json du SSIE
DEFAULT_SSIE_SELECTORS: Dict[str, str] = {
    "reference_root_tag": ".//ReferenceValueList",
    "reference_item_tag": "ReferenceValue",
    "entity_root_tag": ".//DistinctParty",
    "entity_feature_tag": "Feature",
    "mapping_id_attr": "ID",
    "mapping_link_attr": "FeatureTypeID",
}
DEFAULT_SOURCE_FORMAT = "OFAC_ADVANCED_v1"


def get_local_name(tag: Any) -> str:
    """Retourne le nom local d'une balise en retirant tout prefixe de namespace."""
    if not isinstance(tag, str) or not tag:
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _selector_local(selector: str) -> str:
    """Extrait le nom local d'un selecteur XPath simplifie (ex: './/DistinctParty' -> 'DistinctParty')."""
    return selector.split("/")[-1].split(".")[-1]


def _get_attr_insensitive(elem: ET.Element, attr_name: str) -> Optional[str]:
    """Recherche un attribut en ignorant la casse et les namespaces."""
    target = attr_name.lower()
    for k, v in elem.attrib.items():
        local_key = k.split("}")[-1].lower() if "}" in k else k.lower()
        if local_key == target:
            return v
    return None


def _element_value(elem: ET.Element) -> str:
    """
    Retourne la valeur textuelle d'un element : son texte direct, sinon la
    concatenation des textes de ses descendants (Features imbriquees).
    """
    if elem.text and elem.text.strip():
        return elem.text.strip()
    parts = [t.strip() for t in elem.itertext() if t.strip()]
    return " ".join(parts)


def merge_ssie_selectors(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Fusionne les selecteurs par defaut avec des surcharges partielles."""
    merged = dict(DEFAULT_SSIE_SELECTORS)
    if overrides:
        for key, value in overrides.items():
            if key in merged and value:
                merged[key] = value
    return merged


# ------------------ STREAMING EVENEMENTIEL (iterparse) ------------------

def _stream_target_elements(xml_path: str, target_local: str) -> Generator[ET.Element, None, None]:
    """
    Streame le XML via iterparse et produit chaque element cible entierement
    construit. Les elements hors des sous-arbres cibles sont liberes au fil de
    l'eau (elem.clear()) pour maintenir une consommation RAM constante,
    y compris sur des fichiers de plusieurs Go.
    """
    depth_in_target = 0
    root = None
    for event, elem in ET.iterparse(xml_path, events=("start", "end")):
        local_name = get_local_name(elem.tag)
        if event == "start":
            if root is None:
                root = elem
            if depth_in_target > 0 or local_name == target_local:
                depth_in_target += 1
            continue

        if depth_in_target > 0:
            depth_in_target -= 1
            if depth_in_target == 0 and local_name == target_local:
                yield elem
                elem.clear()
            # Les descendants d'une cible restent intacts jusqu'a son propre end
            continue

        # Element termine hors de tout sous-arbre cible : liberation memoire
        elem.clear()
        if root is not None:
            root.clear()


# ------------------ PHASE 1 : DECOUVERTE ------------------

def discover_reference_types(xml_path: str, selectors: Dict[str, str]) -> Dict[str, str]:
    """
    Streame le fichier XML (iterparse) pour extraire le dictionnaire de reference
    des types de caracteristiques : {id_type_carac: libelle}.
    """
    ref_item_local = _selector_local(selectors["reference_item_tag"])
    mapping_id_attr = selectors["mapping_id_attr"]

    references: Dict[str, str] = {}
    for elem in _stream_target_elements(xml_path, ref_item_local):
        ref_id = _get_attr_insensitive(elem, mapping_id_attr)
        ref_val = _element_value(elem)
        if ref_id and ref_val:
            references[str(ref_id)] = ref_val

    logger.info(f"SSIE Phase 1 (Decouverte) : {len(references)} types de caracteristiques references.")
    return references


# ------------------ PHASE 2 : RESOLUTION ------------------

def resolve_entities(xml_path: str, selectors: Dict[str, str]) -> Generator[Dict[str, Any], None, None]:
    """
    Streame le fichier XML pour extraire les listes (entites) et leurs
    caracteristiques liees par ID : {"entity_id", "nom_principal", "features": [(type_id, valeur), ...]}.
    """
    entity_root_local = _selector_local(selectors["entity_root_tag"])
    entity_feature_local = _selector_local(selectors["entity_feature_tag"])
    mapping_id_attr = selectors["mapping_id_attr"]
    mapping_link_attr = selectors["mapping_link_attr"]

    for elem in _stream_target_elements(xml_path, entity_root_local):
        entity_id = _get_attr_insensitive(elem, mapping_id_attr)
        if not entity_id:
            for child in elem.iter():
                if get_local_name(child.tag).lower() == mapping_id_attr.lower() and child.text:
                    entity_id = child.text.strip()
                    break
        if not entity_id:
            # Sans identifiant, le liste n'est pas exploitable pour le criblage
            continue

        # Nom principal : attribut Name, sinon premier noeud Name/NomPrincipal descendant
        nom_principal = _get_attr_insensitive(elem, "Name") or _get_attr_insensitive(elem, "nom_principal")
        if not nom_principal:
            for child in elem.iter():
                if get_local_name(child.tag) in ("Name", "NomPrincipal", "nom_principal") and child.text:
                    nom_principal = child.text
                    break
        nom_principal = (nom_principal or f"Entity {entity_id}").strip()

        # Caracteristiques liees par ID (jointure dynamique en Phase 3)
        features: List[Tuple[str, str]] = []
        for child in elem.iter():
            if get_local_name(child.tag) != entity_feature_local:
                continue
            feat_type_id = _get_attr_insensitive(child, mapping_link_attr)
            feat_value = _element_value(child)
            if feat_type_id and feat_value:
                features.append((str(feat_type_id), feat_value))

        yield {
            "entity_id": str(entity_id),
            "nom_principal": nom_principal,
            "features": features,
        }


# ------------------ PHASE 3 : RESTITUTION (PIVOT) ------------------

def _normalize_date(value: str) -> str:
    """Ramene les dates DD/MM/YYYY ou DD-MM-YYYY vers le format YYYY-MM-DD."""
    value = value.strip()
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})$", value)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return value


def _parse_individual_name(primary_name: str) -> Dict[str, str]:
    """
    Decoupe heuristique du nom principal d'un individu :
    'NOM, Prenom' -> (Prenom, NOM), sinon premier mot = prenom, reste = nom.
    """
    name = primary_name.strip()
    if "," in name:
        last, _, first = name.partition(",")
        return {"first_name": first.strip(), "last_name": last.strip(), "maiden_name": ""}
    tokens = name.split()
    if len(tokens) >= 2:
        return {"first_name": tokens[0], "last_name": " ".join(tokens[1:]), "maiden_name": ""}
    return {"first_name": "", "last_name": name, "maiden_name": ""}


def pivot_to_watchlist_schema(
    record: Dict[str, Any],
    references: Dict[str, str],
    source_format: str = DEFAULT_SOURCE_FORMAT,
) -> Dict[str, Any]:
    """
    Resout les libelles des caracteristiques via le dictionnaire de reference
    et pivote dynamiquement le resultat vers le schema watchlist Fiskr (25 champs).
    Les caracteristiques non mappees sont conservees dans additional_informations.
    """
    dobs: List[str] = []
    date_of_death = None
    is_deceased = False
    gender = "U"
    citizenships: List[str] = []
    residences: List[str] = []
    birth_countries: List[str] = []
    jurisdictions: List[str] = []
    aliases_raw: List[Dict[str, str]] = []
    passports: List[Dict[str, Any]] = []
    national_ids: List[Dict[str, Any]] = []
    other_ids: List[Dict[str, Any]] = []
    national_registry: List[Dict[str, Any]] = []
    other_registrations: List[Dict[str, Any]] = []
    addresses: List[str] = []
    unmapped: List[str] = []
    imo_number = None
    aircraft_tail = None
    lei = None
    place_of_birth = None
    city = None
    state = None
    country_field = None
    designation = None
    maiden_name = ""
    entity_type_hint = None

    for feat_type_id, value in record.get("features", []):
        label = references.get(feat_type_id, f"FeatureType {feat_type_id}")
        ll = label.lower()
        val = value.strip()

        if "gender" in ll or ll == "sex":
            vlow = val.lower()
            if "female" in vlow or vlow == "f":
                gender = "F"
            elif "male" in vlow or vlow == "m":
                gender = "M"
        elif "birth" in ll and "date" in ll or ll in ("dob", "birthdate"):
            dobs.append(_normalize_date(val))
        elif "death" in ll or "deceased" in ll:
            is_deceased = True
            normalized = _normalize_date(val)
            if re.search(r"\d", normalized):
                date_of_death = normalized
        elif "maiden" in ll:
            maiden_name = val
        elif "citizenship" in ll or "nationality" in ll:
            citizenships.append(val)
        elif "residence" in ll:
            residences.append(val)
        elif "birth" in ll and ("place" in ll or "location" in ll):
            place_of_birth = val
        elif "birth" in ll and "country" in ll:
            birth_countries.append(val)
        elif "jurisdiction" in ll or "registration country" in ll:
            jurisdictions.append(val)
        elif "alias" in ll or "a.k.a" in ll or "aka" == ll:
            aliases_raw.append({"name": val, "type": "Strong"})
        elif "passport" in ll:
            passports.append({"number": val, "issuing_country": "XX", "expiration_date": None})
        elif "national id" in ll or "identity card" in ll:
            national_ids.append({"number": val, "issuing_country": "XX"})
        elif ll == "lei" or "legal entity identifier" in ll:
            lei = val
        elif "imo" in ll or "vessel" in ll:
            digits = re.sub(r"\D", "", val)
            if digits:
                imo_number = digits[:7]
            entity_type_hint = "V"
        elif "aircraft" in ll or "tail" in ll:
            aircraft_tail = val
            entity_type_hint = "O"
        elif "tax" in ll or "commercial registry" in ll or "registration number" in ll:
            national_registry.append({"number": val, "country": "XX", "registry_name": label})
        elif "swift" in ll or "bic" in ll:
            other_ids.append({"doc_type": label, "number": val, "issuing_country": "XX"})
        elif "address" in ll:
            addresses.append(val)
        elif ll == "city":
            city = val
        elif ll in ("state", "region", "province"):
            state = val
        elif ll == "country":
            country_field = val
        elif "title" in ll or "designation" in ll or "function" in ll or "position" in ll:
            designation = val
        elif "vital status" in ll and "deceased" in val.lower():
            is_deceased = True
        else:
            # Caracteristique decouverte dynamiquement mais non pivotable : conservee
            unmapped.append(f"{label}: {val}")

    # Determination heuristique du type d'entite (I, E, V, O)
    if entity_type_hint:
        entity_type = entity_type_hint
    elif gender != "U" or dobs or passports or national_ids or maiden_name:
        entity_type = "I"
    else:
        entity_type = "E"

    primary_name = record.get("nom_principal", "")
    if entity_type == "I":
        parsed_name = _parse_individual_name(primary_name)
        parsed_name["maiden_name"] = maiden_name
    else:
        parsed_name = {"first_name": "", "last_name": "", "maiden_name": maiden_name}

    return {
        "entity_id": record.get("entity_id"),
        "entity_type": entity_type,
        "primary_name": primary_name,
        "individual_name_parsed": parsed_name,
        "aliases": categorize_aliases(aliases_raw),
        "dates_of_birth": sorted(set(dobs)),
        "date_of_death": date_of_death,
        "is_deceased": is_deceased,
        "gender": gender,
        "countries": {
            "citizenship": sorted(set(citizenships)),
            "residence": sorted(set(residences)),
            "birth_country": sorted(set(birth_countries)),
            "jurisdiction_country": sorted(set(jurisdictions)),
        },
        "place_of_birth": place_of_birth,
        "address": addresses[0] if addresses else None,
        "alternative_addresses": addresses[1:],
        "city": city,
        "state": state,
        "country": country_field,
        "origin": source_format,
        "designation": designation,
        "additional_informations": "; ".join(unmapped) if unmapped else None,
        "imo_number": imo_number,
        "aircraft_tail_number": aircraft_tail,
        "lei_number": lei,
        "national_registry_ids": national_registry,
        "other_registration_ids": other_registrations,
        "passport_documents": passports,
        "national_id_documents": national_ids,
        "other_id_documents": other_ids,
    }


# ------------------ PIPELINE COMPLET ------------------

def parse_ssie_xml(
    xml_path: str,
    selectors: Optional[Dict[str, str]] = None,
    source_format: str = DEFAULT_SOURCE_FORMAT,
) -> Generator[Dict[str, Any], None, None]:
    """
    Execute le pipeline SSIE complet (Decouverte -> Resolution -> Restitution)
    et genere des enregistrements au schema pivot watchlist Fiskr.
    """
    merged_selectors = merge_ssie_selectors(selectors)

    references = discover_reference_types(xml_path, merged_selectors)

    resolved_count = 0
    for record in resolve_entities(xml_path, merged_selectors):
        resolved_count += 1
        yield pivot_to_watchlist_schema(record, references, source_format)

    logger.info(f"SSIE Phases 2-3 (Resolution/Restitution) : {resolved_count} entites resolues.")

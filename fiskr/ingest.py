import re
import csv
import json
import logging
from typing import List, Dict, Any, Generator, Optional, Set, Tuple
import xml.etree.ElementTree as ET

# We try to import pypdf to extract PDF text, fallback to empty text if unavailable.
try:
    import pypdf
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

logger = logging.getLogger("fiskr.ingest")

# ------------------ ALIAS RISK CATEGORIZATION (Section 5.6) ------------------

def qualify_alias_priority(alias: str, alias_type_attr: str = "") -> str:
    """
    Qualifies an alias as HIGH (screened) or LOW (consultation only).
    Uses native attributes (Strong/Weak) if available, or fallback heuristics.
    """
    if alias_type_attr:
        cleaned_attr = alias_type_attr.lower().strip()
        if "strong" in cleaned_attr:
            return "high"
        elif "weak" in cleaned_attr:
            return "low"
            
    # Fallback Heuristics
    clean_a = re.sub(r"[\._\-]", " ", alias).strip()
    words = clean_a.split()
    
    # 1. Contains only a single word
    if len(words) <= 1:
        return "low"
        
    # 2. Total length is less than or equal to 4 characters
    if len(clean_a) <= 4:
        return "low"
        
    # 3. Composed only of Noise Words (SA, SARL, LLC, GMBH, LTD, SOCIETE, etc.)
    noise_pattern = r"^(SA|SARL|LLC|LTD|GMBH|SOCIETE|\s)+$"
    if re.match(noise_pattern, clean_a, re.IGNORECASE):
        return "low"
        
    return "high"

def categorize_aliases(alias_list: List[Dict[str, str]]) -> Dict[str, List[str]]:
    """
    Takes a list of alias objects: [{"name": "...", "type": "Strong/Weak/..."}]
    Returns {"high_priority": [...], "low_priority": [...]}
    """
    high = []
    low = []
    for a in alias_list:
        name = a.get("name", "")
        if not name:
            continue
        priority = qualify_alias_priority(name, a.get("type", ""))
        if priority == "high":
            high.append(name)
        else:
            low.append(name)
    return {"high_priority": high, "low_priority": low}


# ------------------ XML OFAC CONNECTOR (iterparse) ------------------

def get_attrib_insensitive(elem: Any, attr_name: str) -> Any:
    """
    Looks up an attribute in elem.attrib ignoring namespaces and case.
    """
    if elem is None or not hasattr(elem, "attrib"):
        return None
    target = attr_name.lower()
    for k, v in elem.attrib.items():
        local_key = k.split("}")[-1].lower() if "}" in k else k.lower()
        if local_key == target:
            return v
    return None

class OFACParserContext:
    def __init__(self):
        self.references = {}
        self.ref_links = {}
        self.locations = {}  # location_id -> {"full", "parts", "iso2", "country_name"}
        self.location_countries = {} # location_id -> Country ISO2
        self.id_documents = {}  # identity_id -> [doc_dict, ...]
        self.sanctions_programs = {}  # profile_id -> [program names]


def _local_ns(elem: ET.Element) -> str:
    """Prefixe de namespace ('{uri}') de l'element, ou chaine vide."""
    return elem.tag.split('}')[0] + '}' if '}' in elem.tag else ''


def _stream_target_elements(file_path: str, target_locals: Set[str]) -> Generator[Tuple[str, ET.Element], None, None]:
    """
    Streame le XML via iterparse et produit chaque element cible entierement
    construit, sous la forme (nom_local, element). Seuls les elements termines
    HORS de tout sous-arbre cible sont liberes au fil de l'eau : les descendants
    d'une cible restent intacts jusqu'au 'end' de la cible elle-meme (un clear
    inconditionnel viderait les valeurs des referentiels avant leur lecture,
    car les evenements 'end' remontent du bas vers le haut).
    """
    depth_in_target = 0
    root = None
    for event, elem in ET.iterparse(file_path, events=("start", "end")):
        local_name = elem.tag.split('}')[-1]
        if event == "start":
            if root is None:
                root = elem
            if depth_in_target > 0 or local_name in target_locals:
                depth_in_target += 1
            continue

        if depth_in_target > 0:
            depth_in_target -= 1
            if depth_in_target == 0 and local_name in target_locals:
                yield local_name, elem
                elem.clear()
            continue

        # Element termine hors de tout sous-arbre cible : liberation memoire
        elem.clear()
        if root is not None:
            root.clear()


def _extract_date_from_period_elem(elem: ET.Element, ns: str) -> Optional[str]:
    """Extrait une date YYYY-MM-DD depuis un sous-arbre contenant DatePeriod/Start/From."""
    frm = elem.find(f".//{ns}Start/{ns}From")
    if frm is None:
        frm = elem.find(f".//{ns}From")
    if frm is None:
        return None
    def _txt(tag):
        child = frm.find(f"{ns}{tag}")
        return child.text.strip() if (child is not None and child.text) else ""
    y, m, d = _txt("Year"), _txt("Month"), _txt("Day")
    if not y:
        return None
    return f"{y}-{(m or '01').zfill(2)}-{(d or '01').zfill(2)}"

def dict_get_insensitive(d, key):
    if not isinstance(d, dict):
        return None
    target = key.lower()
    for k, v in d.items():
        if k.lower() == target:
            return v
    return None

def find_nested_in_dict(data, target_key):
    results = []
    if isinstance(data, dict):
        for k, v in data.items():
            if k.lower() == target_key.lower():
                if isinstance(v, list):
                    results.extend(v)
                else:
                    results.append(v)
            else:
                results.extend(find_nested_in_dict(v, target_key))
    elif isinstance(data, list):
        for item in data:
            results.extend(find_nested_in_dict(item, target_key))
    return results

def get_reference_value(references, ref_type, val_id):
    for rk, rvals in references.items():
        if rk.lower() == ref_type.lower():
            return rvals.get(val_id, '')
    return ''

def elem_to_dict(elem, references):
    d = {}
    if elem.text and elem.text.strip():
        d['text'] = elem.text.strip()
    for k, v in elem.attrib.items():
        local_k = k.split('}')[-1] if '}' in k else k
        local_k_lower = local_k.lower()
        if local_k_lower.endswith('id') and not local_k_lower == 'id':
            ref_type = local_k[:-2]
            matched_ref_type = None
            for rk in references.keys():
                if rk.lower() == ref_type.lower():
                    matched_ref_type = rk
                    break
            if matched_ref_type and v in references[matched_ref_type]:
                d[local_k] = {"id": v, "value": references[matched_ref_type][v]}
            else:
                d[local_k] = v
        else:
            d[local_k] = v
            
    for child in elem:
        tag = child.tag.split('}')[-1]
        child_dict = elem_to_dict(child, references)
        if tag not in d:
            d[tag] = []
        d[tag].append(child_dict)
    return d

def _harvest_reference_sets(elem: ET.Element, parser_ctx: OFACParserContext) -> None:
    """Charge tous les jeux de valeurs de reference (PartyType, Country, FeatureType...)."""
    for value_set in list(elem):
        vs_tag = value_set.tag.split('}')[-1]
        base_tag = vs_tag.replace('Values', '')
        if base_tag not in parser_ctx.references:
            parser_ctx.references[base_tag] = {}
        if base_tag not in parser_ctx.ref_links:
            parser_ctx.ref_links[base_tag] = {}
        for child in list(value_set):
            if 'ID' in child.attrib:
                id_val = child.attrib['ID']
                if child.text and child.text.strip():
                    val = child.text.strip()
                elif 'Description' in child.attrib:
                    val = child.attrib['Description']
                else:
                    val = str(child.attrib)
                parser_ctx.references[base_tag][id_val] = val
                extra = {k: v for k, v in child.attrib.items() if k != 'ID'}
                if child.text and child.text.strip():
                    extra['_text'] = child.text.strip()
                if extra:
                    parser_ctx.ref_links[base_tag][id_val] = extra


def _harvest_location(elem: ET.Element, parser_ctx: OFACParserContext) -> None:
    """
    Indexe une localisation par ID : adresse complete, parties structurees
    (via LocPartType : ADDRESS1, CITY, STATE/PROVINCE, POSTAL CODE, REGION...)
    et pays (nom + ISO2).
    """
    if 'ID' not in elem.attrib:
        return
    id_val = elem.attrib['ID']
    ns = _local_ns(elem)

    parts = {}
    loc_texts = []
    for lp in elem.iter(f"{ns}LocationPart"):
        lp_type_id = get_attrib_insensitive(lp, 'LocPartTypeID')
        lp_type = parser_ctx.references.get('LocPartType', {}).get(str(lp_type_id or ''), '')
        texts = [v.text.strip() for v in lp.iter(f"{ns}Value") if v.text and v.text.strip()]
        if texts:
            val = ", ".join(texts)
            loc_texts.append(val)
            if lp_type:
                parts[lp_type.upper()] = val
    if not loc_texts:
        # Fichiers sans structure LocationPart : toutes les valeurs texte
        loc_texts = [p.text.strip() for p in elem.iter(f"{ns}Value") if p.text and p.text.strip()]

    country_name = None
    iso2 = None
    cid = None
    for p in elem.iter(f"{ns}LocationCountry"):
        cid = p.attrib.get('CountryID')
        if cid and cid in parser_ctx.references.get('Country', {}):
            country_name = parser_ctx.references['Country'][cid]

    full_parts = list(loc_texts)
    if country_name:
        full_parts.append(country_name)
    if cid:
        c_links = parser_ctx.ref_links.get('Country', {}).get(cid, {})
        iso2 = c_links.get('ISO2') or c_links.get('Code')
        if not iso2 and country_name:
            iso2 = country_name[:2].upper()
        if iso2:
            parser_ctx.location_countries[id_val] = iso2

    parser_ctx.locations[id_val] = {
        "full": ", ".join(full_parts),
        "parts": parts,
        "iso2": iso2,
        "country_name": country_name,
    }


def _harvest_id_document(elem: ET.Element, parser_ctx: OFACParserContext) -> None:
    """Indexe un document d'identite par IdentityID (numero, pays emetteur, expiration)."""
    identity_id = elem.attrib.get('IdentityID')
    if not identity_id:
        return
    ns = _local_ns(elem)
    doc_type_id = elem.attrib.get('IDRegDocTypeID')
    doc_num = ""
    doc_num_el = elem.find(f".//{ns}IDRegistrationNo")
    if doc_num_el is not None and doc_num_el.text:
        doc_num = doc_num_el.text.strip()

    issued_by_el = elem.find(f".//{ns}IssuedBy")
    issuing_country = "XX"
    if issued_by_el is not None:
        cid = issued_by_el.attrib.get('CountryID')
        if cid and cid in parser_ctx.references.get('Country', {}):
            country_name = parser_ctx.references['Country'][cid]
            c_links = parser_ctx.ref_links.get('Country', {}).get(cid, {})
            iso2 = c_links.get('ISO2') or c_links.get('Code') or country_name[:2].upper()
            issuing_country = iso2

    # Date d'expiration (DocumentDate type "Expiration Date" du referentiel)
    expiration = None
    for dd in elem.iter(f"{ns}DocumentDate"):
        dtype_id = get_attrib_insensitive(dd, 'IDRegDocDateTypeID')
        dtype_name = parser_ctx.references.get('IDRegDocDateType', {}).get(str(dtype_id or ''), '').lower()
        date_val = _extract_date_from_period_elem(dd, ns)
        if date_val and ("expir" in dtype_name):
            expiration = date_val

    doc_dict = {
        "doc_type_id": doc_type_id,
        "number": doc_num,
        "issuing_country": issuing_country,
        "expiration_date": expiration
    }
    if identity_id not in parser_ctx.id_documents:
        parser_ctx.id_documents[identity_id] = []
    parser_ctx.id_documents[identity_id].append(doc_dict)


def _harvest_sanctions_entry(elem: ET.Element, parser_ctx: OFACParserContext) -> None:
    """
    Recolte les programmes de sanctions d'une SanctionsEntry (liee par ProfileID).
    Les mesures dont le type se resout en "Program" portent le nom du programme
    dans leur Comment ; sans referentiel charge, tout Comment est conserve.
    """
    profile_id = get_attrib_insensitive(elem, 'ProfileID')
    if not profile_id:
        return
    ns = _local_ns(elem)
    sanctions_types = parser_ctx.references.get('SanctionsType', {})
    programs = []
    for measure in elem.iter(f"{ns}SanctionsMeasure"):
        type_id = get_attrib_insensitive(measure, 'SanctionsTypeID')
        type_name = sanctions_types.get(str(type_id or ''), '').lower()
        comment_el = measure.find(f"{ns}Comment")
        text = comment_el.text.strip() if (comment_el is not None and comment_el.text) else ""
        if not text:
            continue
        if "program" in type_name or not sanctions_types:
            programs.append(text)
    if programs:
        existing = parser_ctx.sanctions_programs.setdefault(str(profile_id), [])
        for p in programs:
            if p not in existing:
                existing.append(p)


def _classify_id_document(doc_type_id, doc_type_name, doc_num, issued_country, expiration_date, buckets):
    """
    Route un document d'identite vers le bon compartiment du schema pivot.
    Les IDs numeriques codes en dur couvrent les fichiers simplifies ; les
    correspondances par nom (referentiel IDRegDocType) couvrent le fichier
    officiel dont les IDs varient.
    """
    doc_type_id = str(doc_type_id or "")
    doc_type_name = (doc_type_name or "").lower()
    if doc_type_id == "392" or "passport" in doc_type_name:
        buckets["passports"].append({"number": doc_num, "issuing_country": issued_country, "expiration_date": expiration_date})
    elif doc_type_id == "391" or "national id" in doc_type_name:
        buckets["national_ids"].append({"number": doc_num, "issuing_country": issued_country})
    elif doc_type_id in ("386", "390", "394") or "driver" in doc_type_name:
        buckets["other_ids"].append({"doc_type": "DriverLicense" if doc_type_id == "386" or "driver" in doc_type_name else "Other", "number": doc_num, "issuing_country": issued_country})
    elif doc_type_id == "15502" or "lei" in doc_type_name:
        buckets["lei"] = doc_num
    elif doc_type_id in ("9436", "376", "384") or "tax" in doc_type_name or "commercial" in doc_type_name or "business registration" in doc_type_name:
        buckets["national_registry"].append({"number": doc_num, "country": issued_country, "registry_name": "CommercialRegistry" if doc_type_id == "9436" or "commercial" in doc_type_name or "business registration" in doc_type_name else "TaxRegistry"})
    elif doc_type_id == "13886" or "imo" in doc_type_name or "vessel registration" in doc_type_name:
        digits = re.sub(r"\D", "", doc_num)
        buckets["imo_number"] = digits[:7]
    elif doc_type_id == "13887" or "aircraft" in doc_type_name:
        buckets["aircraft_tail"] = doc_num
    else:
        buckets["other_registrations"].append({"id_type": doc_type_name or "OtherRegistration", "number": doc_num})


def resolve_party_type(profile, parser_ctx):
    # Try child element style first (from mock XML or simplified schemas)
    pst_list = dict_get_insensitive(profile, 'PartySubType')
    if pst_list and isinstance(pst_list, list) and len(pst_list) > 0:
        pst_elem = pst_list[0]
        ptype = dict_get_insensitive(pst_elem, 'PartyTypeID')
        if isinstance(ptype, dict):
            ptype = ptype.get('id')
        if ptype == "151":
            return "I"
        elif ptype == "152":
            return "E"
        elif ptype == "154":
            return "V"
        elif ptype == "153":
            return "O"
            
    # Try attribute style (from standard Advanced XML)
    pst = dict_get_insensitive(profile, 'PartySubTypeID')
    if not pst:
        return None
    pst_value = ""
    if isinstance(pst, dict):
        pst_id = pst.get('id', '')
        pst_value = str(pst.get('value') or '')
    elif isinstance(pst, list) and pst:
        pst_id = pst[0].get('id', '') if isinstance(pst[0], dict) else ''
        pst_value = str(pst[0].get('value') or '') if isinstance(pst[0], dict) else ''
    else:
        pst_id = str(pst)

    links = parser_ctx.ref_links.get('PartySubType', {}).get(pst_id, {})
    pt_id = links.get('PartyTypeID', '')
    pt_name = get_reference_value(parser_ctx.references, 'PartyType', pt_id).lower()
    # Le nom du sous-type lui-meme (ex: "Individual") est aussi discriminant
    combined = f"{pst_value.lower()} {get_reference_value(parser_ctx.references, 'PartySubType', pst_id).lower()} {pt_name}"

    if "individual" in combined:
        return "I"
    elif "vessel" in combined:
        return "V"
    elif "aircraft" in combined:
        return "O"
    elif "entity" in combined:
        return "E"
    # Referentiel absent ou irresoluble : on laisse l'heuristique decider
    return None

def _feature_version_text(fv) -> str:
    """
    Extrait le texte d'une version de feature : valeurs resolues des
    DetailReferenceID, contenus des DetailReference et texte brut du detail.
    """
    out = []
    version_details = dict_get_insensitive(fv, 'VersionDetail') or []
    for vd in version_details:
        ref_obj = dict_get_insensitive(vd, 'DetailReferenceID')
        if ref_obj:
            if isinstance(ref_obj, dict):
                out.append(str(ref_obj.get('value') or ref_obj.get('id') or ''))
            elif isinstance(ref_obj, list):
                for child in ref_obj:
                    if isinstance(child, dict):
                        out.append(str(child.get('value') or child.get('text') or ''))
            else:
                out.append(str(ref_obj))
        ref_ref = dict_get_insensitive(vd, 'DetailReference')
        if ref_ref:
            if isinstance(ref_ref, list):
                for child in ref_ref:
                    if isinstance(child, dict):
                        out.append(str(child.get('value') or child.get('text') or ''))
            elif isinstance(ref_ref, dict):
                out.append(str(ref_ref.get('value') or ref_ref.get('text') or ''))
            else:
                out.append(str(ref_ref))
        if isinstance(vd, dict) and vd.get('text'):
            out.append(vd['text'])
    return " ".join(t.strip() for t in out if t and t.strip()).strip()


def format_alias_name(alias_dict, identity_dict):
    group_map = {}
    name_part_groups = dict_get_insensitive(identity_dict, 'NamePartGroups') or []
    for groups in name_part_groups:
        master_groups = dict_get_insensitive(groups, 'MasterNamePartGroup') or []
        for mg in master_groups:
            ng_list = dict_get_insensitive(mg, 'NamePartGroup') or []
            for ng in ng_list:
                gid = dict_get_insensitive(ng, 'ID')
                tid = dict_get_insensitive(ng, 'NamePartTypeID')
                ty = tid.get('value') if isinstance(tid, dict) else str(tid)
                group_map[gid] = ty
                
    order_map = {
        "First Name": 1,
        "Middle Name": 2,
        "Patronymic": 3,
        "Matronymic": 4,
        "Last Name": 5,
        "Entity Name": 10,
        "Nickname": 11,
        "Vessel Name": 12,
        "Aircraft Name": 13
    }
    
    parts_list = []
    documented_names = dict_get_insensitive(alias_dict, 'DocumentedName') or []
    for dn in documented_names:
        name_parts = dict_get_insensitive(dn, 'DocumentedNamePart') or []
        for pt in name_parts:
            name_part_values = dict_get_insensitive(pt, 'NamePartValue') or []
            for nv in name_part_values:
                if 'text' in nv:
                    gid = dict_get_insensitive(nv, 'NamePartGroupID')
                    ty = group_map.get(gid, "Unknown")
                    weight = order_map.get(ty, 99)
                    parts_list.append((weight, nv['text']))
                    
    parts_list.sort(key=lambda x: x[0])
    return " ".join([x[1] for x in parts_list])

def parse_ofac_advanced_xml(file_path: str) -> Generator[Dict[str, Any], None, None]:
    """
    Sequentially parses the OFAC Advanced XML using ElementTree.iterparse
    to prevent memory ballooning. Yields Pivot Schema dicts.
    """
    parser_ctx = OFACParserContext()

    # Pass 1: recolte des referentiels, localisations, documents d'identite et
    # programmes de sanctions (SanctionsEntries suit DistinctParties dans le
    # fichier officiel, d'ou la necessite de deux passes). Le streaming a suivi
    # de profondeur garantit que les enfants d'une cible ne sont jamais vides
    # avant la lecture de la cible.
    for local_name, elem in _stream_target_elements(
        file_path, {'ReferenceValueSets', 'Location', 'IDRegDocument', 'SanctionsEntry'}
    ):
        if local_name == 'ReferenceValueSets':
            _harvest_reference_sets(elem, parser_ctx)
        elif local_name == 'Location':
            _harvest_location(elem, parser_ctx)
        elif local_name == 'IDRegDocument':
            _harvest_id_document(elem, parser_ctx)
        elif local_name == 'SanctionsEntry':
            _harvest_sanctions_entry(elem, parser_ctx)

    # Pass 2: Parse DistinctParties
    for _, elem in _stream_target_elements(file_path, {'DistinctParty'}):
        ns = _local_ns(elem)
        prof_elem = elem.find(f'{ns}Profile')
        if prof_elem is None:
            continue

        pid = (
            get_attrib_insensitive(elem, "fixedRef")
            or get_attrib_insensitive(elem, "ID")
            or (get_attrib_insensitive(prof_elem, "ID") if prof_elem is not None else None)
        )
        if not pid:
            continue
            
        profile = elem_to_dict(prof_elem, parser_ctx.references)
        
        # Extract basic fields
        entity_type_id = resolve_party_type(profile, parser_ctx)
        primary_name = ""
        first_name = ""
        last_name = ""
        maiden_name = ""
        aliases_raw = []
        
        # Extract names & aliases
        for identity in profile.get('Identity', []):
            for alias in identity.get('Alias', []):
                is_primary = alias.get('Primary') == 'true'
                formatted_name = format_alias_name(alias, identity)
                if not formatted_name:
                    continue
                    
                alias_type_obj = alias.get('AliasTypeID')
                alias_type_str = "Strong"
                if isinstance(alias_type_obj, dict):
                    alias_type_str = alias_type_obj.get('value', 'Strong')
                elif alias_type_obj:
                    alias_type_str = str(alias_type_obj)
                    
                if is_primary:
                    primary_name = formatted_name
                    
                    # Extract first, last, maiden name
                    group_map = {}
                    for groups in identity.get('NamePartGroups', []):
                        for mg in groups.get('MasterNamePartGroup', []):
                            for ng in mg.get('NamePartGroup', []):
                                gid = ng.get('ID')
                                tid = ng.get('NamePartTypeID', {})
                                group_map[gid] = tid.get('value') if isinstance(tid, dict) else str(tid)
                                
                    for dn in alias.get('DocumentedName', []):
                        for pt in dn.get('DocumentedNamePart', []):
                            for nv in pt.get('NamePartValue', []):
                                if 'text' in nv:
                                    gid = nv.get('NamePartGroupID')
                                    ty = group_map.get(gid, "Unknown")
                                    if ty == "First Name":
                                        first_name = nv['text']
                                    elif ty == "Last Name":
                                        last_name = nv['text']
                                    elif "maiden" in ty.lower():
                                        maiden_name = nv['text']
                else:
                    aliases_raw.append({"name": formatted_name, "type": alias_type_str})
        
        # Nested DocumentedName fallback
        if not primary_name:
            nested_doc_names = find_nested_in_dict(profile, 'DocumentedName')
            for doc_name in nested_doc_names:
                status_id = dict_get_insensitive(doc_name, "DocNameStatusID")
                if isinstance(status_id, dict):
                    status_id = status_id.get('id', '')
                is_primary = str(status_id) == "1"
                
                name_parts = []
                parts = find_nested_in_dict(doc_name, 'DocumentedNamePart')
                for part in parts:
                    part_type = dict_get_insensitive(part, "NamePartTypeID")
                    if isinstance(part_type, dict):
                        part_type = part_type.get('id', '')
                    else:
                        part_type = str(part_type or '')
                        
                    part_vals = find_nested_in_dict(part, 'Value')
                    for pv in part_vals:
                        text = pv.get('text', '') if isinstance(pv, dict) else str(pv)
                        if text:
                            text_clean = text.strip()
                            name_parts.append(text_clean)
                            if is_primary:
                                if part_type == "1360":
                                    first_name = text_clean
                                elif part_type == "1361":
                                    last_name = text_clean
                                    
                full_name_resolved = " ".join(name_parts)
                if is_primary:
                    primary_name = full_name_resolved
                else:
                    alias_type = dict_get_insensitive(doc_name, "AliasTypeID")
                    if isinstance(alias_type, dict):
                        alias_type = alias_type.get('id', '')
                    type_str = "Strong" if str(alias_type) == "1" else "Weak"
                    aliases_raw.append({"name": full_name_resolved, "type": type_str})
        
        # Extract features (DOB, Gender, Death/Deceased, countries, POB, addresses...)
        dobs = []
        date_of_death = None
        is_deceased = False
        gender = "U"
        citizenships = []
        residences = []
        birth_countries = []
        jurisdictions = []
        place_of_birth = None
        addresses = []       # [{"full", "parts", ...}] dans l'ordre du fichier
        designation = None
        unmapped_features = []  # features non pivotables -> additional_informations

        features = dict_get_insensitive(profile, 'Feature') or []
        for f in features:
            ftype_obj = dict_get_insensitive(f, 'FeatureTypeID')
            if not ftype_obj:
                continue
            ftype_str = ftype_obj.get('value', '') if isinstance(ftype_obj, dict) else str(ftype_obj)
            ftype_str_lower = ftype_str.lower()

            is_gender = "gender" in ftype_str_lower or ftype_str_lower == "25"
            is_birth = ("birth" in ftype_str_lower and "date" in ftype_str_lower) or ftype_str_lower in ["8", "12"]
            is_death = "death" in ftype_str_lower or "deceased" in ftype_str_lower or ftype_str_lower == "24"
            # "place of birth" avant la branche generique "birth" (pays de naissance)
            is_pob = "place of birth" in ftype_str_lower
            is_address = "address" in ftype_str_lower or ftype_str_lower == "location"
            is_designation = any(k in ftype_str_lower for k in ("title", "position", "function", "occupation"))

            feature_versions = dict_get_insensitive(f, 'FeatureVersion') or []
            for fv in feature_versions:
                # Gender
                if is_gender:
                     ref_val_lower = _feature_version_text(fv).lower()
                     if "female" in ref_val_lower:
                         gender = "F"
                     elif "male" in ref_val_lower:
                         gender = "M"

                # Birth
                elif is_birth:
                    date_periods = dict_get_insensitive(fv, 'DatePeriod') or []
                    for dp in date_periods:
                        start = dict_get_insensitive(dp, 'Start') or []
                        if start and 'From' in start[0]:
                            from_date = start[0]['From'][0]
                            y_el = dict_get_insensitive(from_date, 'Year')
                            m_el = dict_get_insensitive(from_date, 'Month')
                            d_el = dict_get_insensitive(from_date, 'Day')
                            y = y_el[0].get('text', '') if (y_el and isinstance(y_el, list)) else ''
                            m = m_el[0].get('text', '') if (m_el and isinstance(m_el, list)) else ''
                            d = d_el[0].get('text', '') if (d_el and isinstance(d_el, list)) else ''
                            if y:
                                m_str = m.strip() if m else "01"
                                d_str = d.strip() if d else "01"
                                dobs.append(f"{y.strip()}-{m_str.zfill(2)}-{d_str.zfill(2)}")
                                
                # Death
                elif is_death:
                    is_deceased = True
                    date_periods = dict_get_insensitive(fv, 'DatePeriod') or []
                    for dp in date_periods:
                        start = dict_get_insensitive(dp, 'Start') or []
                        if start and 'From' in start[0]:
                            from_date = start[0]['From'][0]
                            y_el = dict_get_insensitive(from_date, 'Year')
                            m_el = dict_get_insensitive(from_date, 'Month')
                            d_el = dict_get_insensitive(from_date, 'Day')
                            y = y_el[0].get('text', '') if (y_el and isinstance(y_el, list)) else ''
                            m = m_el[0].get('text', '') if (m_el and isinstance(m_el, list)) else ''
                            d = d_el[0].get('text', '') if (d_el and isinstance(d_el, list)) else ''
                            if y:
                                m_str = m.strip() if m else "01"
                                d_str = d.strip() if d else "01"
                                date_of_death = f"{y.strip()}-{m_str.zfill(2)}-{d_str.zfill(2)}"
                
                # Localisations liees a la feature (pays, lieu de naissance, adresses)
                version_locations = dict_get_insensitive(fv, 'VersionLocation') or []
                for vl in version_locations:
                    lid_obj = dict_get_insensitive(vl, 'LocationID')
                    lid = lid_obj.get('id') if isinstance(lid_obj, dict) else str(lid_obj)
                    if not lid:
                        continue
                    loc_info = parser_ctx.locations.get(lid) or {}
                    country_code = parser_ctx.location_countries.get(lid)
                    if is_pob:
                        if loc_info.get("full") and not place_of_birth:
                            place_of_birth = loc_info["full"]
                        if country_code:
                            birth_countries.append(country_code)
                    elif is_address:
                        if loc_info.get("full"):
                            addresses.append(loc_info)
                    elif country_code:
                        if "citizenship" in ftype_str_lower or "nationality" in ftype_str_lower:
                            citizenships.append(country_code)
                        elif "residence" in ftype_str_lower:
                            residences.append(country_code)
                        elif "birth" in ftype_str_lower:
                            birth_countries.append(country_code)
                        else:
                            jurisdictions.append(country_code)

                # Features non pivotables (call sign, pavillon, tonnage, site web,
                # email, telephone, modele d'aeronef...) : conservees pour
                # consultation humaine au lieu d'etre perdues
                if not (is_gender or is_birth or is_death or is_pob or is_address) and not version_locations:
                    text = _feature_version_text(fv)
                    if text:
                        if is_designation and not designation:
                            designation = text
                        else:
                            unmapped_features.append(f"{ftype_str}: {text}")

        # Fallback to nested locations
        if not citizenships and not residences and not birth_countries and not jurisdictions:
            nested_locations = find_nested_in_dict(profile, 'Location')
            for loc in nested_locations:
                loc_type_list = find_nested_in_dict(loc, 'LocationType')
                loc_type = ""
                if loc_type_list:
                    loc_type = loc_type_list[0].get('text', '') if isinstance(loc_type_list[0], dict) else str(loc_type_list[0])
                    
                country_list = find_nested_in_dict(loc, 'LocationCountry')
                country_code = ""
                if country_list:
                    country_el = country_list[0]
                    if isinstance(country_el, dict):
                        country_code = (dict_get_insensitive(country_el, 'CountryISO2') 
                                        or dict_get_insensitive(country_el, 'CountryID'))
                        if isinstance(country_code, dict):
                            country_code = country_code.get('id') or country_code.get('value')
                    else:
                        country_code = str(country_el)
                        
                if country_code:
                    lt_str = str(loc_type).lower()
                    if "citizenship" in lt_str:
                        citizenships.append(country_code)
                    elif "residence" in lt_str:
                        residences.append(country_code)
                    elif "birth" in lt_str:
                        birth_countries.append(country_code)
                    else:
                        residences.append(country_code)
                        
        # Extract ID registration documents
        imo_number = None
        aircraft_tail = None
        lei = None
        national_registry = []
        other_registrations = []
        passports = []
        national_ids = []
        other_ids = []

        doc_buckets = {
            "passports": passports,
            "national_ids": national_ids,
            "other_ids": other_ids,
            "national_registry": national_registry,
            "other_registrations": other_registrations,
            "lei": None,
            "imo_number": None,
            "aircraft_tail": None,
        }

        # Load documents linked to any identity in the profile
        for identity in profile.get('Identity', []):
            ident_id = identity.get('ID')
            if not ident_id:
                continue
            docs = parser_ctx.id_documents.get(ident_id, [])
            for doc in docs:
                doc_type_name = parser_ctx.references.get('IDRegDocType', {}).get(doc["doc_type_id"], "")
                _classify_id_document(
                    doc["doc_type_id"], doc_type_name, doc["number"],
                    doc["issuing_country"], doc.get("expiration_date"), doc_buckets
                )
        lei = doc_buckets["lei"]
        imo_number = doc_buckets["imo_number"]
        aircraft_tail = doc_buckets["aircraft_tail"]

        # Fallback to nested IDRegistrationDocument / IDRegDocument elements
        if not passports and not national_ids and not other_ids and not lei and not national_registry and not imo_number and not aircraft_tail:
            nested_docs = find_nested_in_dict(profile, 'IDRegistrationDocument') + find_nested_in_dict(profile, 'IDRegDocument')
            for doc_elem in nested_docs:
                doc_type_id = (dict_get_insensitive(doc_elem, "IDRegistrationDocTypeID") 
                               or dict_get_insensitive(doc_elem, "IDRegDocTypeID"))
                if isinstance(doc_type_id, dict):
                    doc_type_id = doc_type_id.get('id', '')
                else:
                    doc_type_id = str(doc_type_id or '')
                    
                doc_num_el_list = (find_nested_in_dict(doc_elem, "IDRegistrationDocElement") 
                                   or find_nested_in_dict(doc_elem, "IDRegistrationNo"))
                doc_num = ""
                if doc_num_el_list:
                    if isinstance(doc_num_el_list[0], dict):
                        doc_num = doc_num_el_list[0].get('text', '')
                    else:
                        doc_num = str(doc_num_el_list[0])
                
                issuing_el_list = find_nested_in_dict(doc_elem, "IssuedBy")
                issued_country = "XX"
                if issuing_el_list:
                    issuing_el = issuing_el_list[0]
                    country_el_list = find_nested_in_dict(issuing_el, "CountryISO2")
                    if country_el_list:
                        country_el = country_el_list[0]
                        if isinstance(country_el, dict):
                            issued_country = country_el.get('text') or country_el.get('CountryID') or "XX"
                            if isinstance(issued_country, dict):
                                issued_country = issued_country.get('id') or "XX"
                        else:
                            issued_country = str(country_el)
                            
                if doc_num:
                    doc_type_name = parser_ctx.references.get('IDRegDocType', {}).get(doc_type_id, "")
                    _classify_id_document(doc_type_id, doc_type_name, doc_num, issued_country, None, doc_buckets)
            lei = doc_buckets["lei"]
            imo_number = doc_buckets["imo_number"]
            aircraft_tail = doc_buckets["aircraft_tail"]

        # Repli heuristique quand ni le style enfant ni le referentiel n'ont
        # permis de typer le liste (fichiers simplifies ou referentiel absent)
        if not entity_type_id:
            if imo_number:
                entity_type_id = "V"
            elif aircraft_tail:
                entity_type_id = "O"
            elif gender != "U" or dobs or passports or national_ids or first_name or maiden_name:
                entity_type_id = "I"
            else:
                entity_type_id = "E"

        # Adresses structurees : premiere adresse = principale, le reste en alternatives
        primary_addr = addresses[0] if addresses else {}
        addr_parts = primary_addr.get("parts", {})

        # Build Pivot structure
        aliases_categorized = categorize_aliases(aliases_raw)
        current_party = {
            "entity_id": pid,
            "entity_type": entity_type_id,
            "primary_name": primary_name or "NOM INCONNU",
            "individual_name_parsed": {
                "first_name": first_name,
                "last_name": last_name,
                "maiden_name": maiden_name
            },
            "aliases": aliases_categorized,
            "dates_of_birth": list(set(dobs)),
            "date_of_death": date_of_death,
            "is_deceased": is_deceased,
            "gender": gender,
            "countries": {
                "citizenship": list(set(citizenships)),
                "residence": list(set(residences)),
                "birth_country": list(set(birth_countries)),
                "jurisdiction_country": list(set(jurisdictions))
            },
            "place_of_birth": place_of_birth,
            "address": primary_addr.get("full"),
            "alternative_addresses": [a["full"] for a in addresses[1:] if a.get("full")],
            "city": addr_parts.get("CITY"),
            "state": addr_parts.get("STATE/PROVINCE") or addr_parts.get("REGION"),
            "country": primary_addr.get("country_name"),
            "designation": designation,
            "designation_reasons": "; ".join(parser_ctx.sanctions_programs.get(str(pid), [])) or None,
            "additional_informations": "; ".join(unmapped_features) or None,
            "origin": "OFAC SDN_ADVANCED",
            "imo_number": imo_number,
            "aircraft_tail_number": aircraft_tail,
            "lei_number": lei,
            "national_registry_ids": national_registry,
            "other_registration_ids": other_registrations,
            "passport_documents": passports,
            "national_id_documents": national_ids,
            "other_id_documents": other_ids
        }

        yield current_party
        
                



# ------------------ REGISTRE NATIONAL DES GELS (DGT) ------------------
# Connecteur du registre national des gels des avoirs publie par la Direction
# generale du Tresor (gels-avoirs.dgtresor.gouv.fr, API publique ENGEL).
# Structure du fichier JSON : Publications.PublicationDetail[] avec IdRegistre,
# Nature (Personne physique / Personne morale / Navire), Nom et RegistreDetail[]
# (paires TypeChamp / Valeur[]). Le parseur est tolerant aux variations de cles
# des objets Valeur (recherche insensible a la casse, repli sur toute valeur texte).

DGT_NATURE_TO_TYPE = {
    "personne physique": "I",
    "personne morale": "E",
    "navire": "V",
}

# Normalisation des pays/nationalites du registre DGT (libelles francais) vers
# ISO2, indispensable pour que les cles de blocking coincident avec celles du
# referentiel clients (codes ISO). Radicaux sans accents, minuscules : ils
# couvrent le nom du pays ET l'adjectif de nationalite (masculin/feminin/pluriel).
_DGT_COUNTRY_STEMS = [
    ("coree du nord", "KP"), ("nord-coreen", "KP"), ("nord coreen", "KP"),
    ("russ", "RU"), ("bielorus", "BY"), ("belarus", "BY"),
    ("syrie", "SY"), ("syrien", "SY"),
    ("iranien", "IR"), ("iran", "IR"),
    ("birman", "MM"), ("myanmar", "MM"), ("birmanie", "MM"),
    ("libye", "LY"), ("libyen", "LY"),
    ("malien", "ML"), ("mali", "ML"),
    ("venezuel", "VE"), ("chin", "CN"),
    ("irakien", "IQ"), ("irak", "IQ"),
    ("afghan", "AF"), ("yemen", "YE"), ("liban", "LB"),
    ("soudan du sud", "SS"), ("sud-soudan", "SS"),
    ("soudan", "SD"), ("congolais", "CD"),
    ("republique democratique du congo", "CD"), ("rdc", "CD"), ("congo", "CD"),
    ("centrafri", "CF"), ("somal", "SO"), ("nicaragua", "NI"),
    ("guinee-bissau", "GW"), ("bissau", "GW"), ("guine", "GN"),
    ("zimbabw", "ZW"), ("haiti", "HT"), ("hait", "HT"),
    ("turc", "TR"), ("turq", "TR"), ("ukrain", "UA"), ("moldav", "MD"),
    ("tunis", "TN"), ("egypt", "EG"), ("pakistan", "PK"),
    ("saoudien", "SA"), ("arabie saoudite", "SA"),
    ("jordan", "JO"), ("israel", "IL"), ("palestin", "PS"),
    ("franc", "FR"), ("algeri", "DZ"), ("marocain", "MA"), ("maroc", "MA"),
    ("burundi", "BI"), ("erythre", "ER"), ("ethiopi", "ET"),
    ("kirghiz", "KG"), ("tadjik", "TJ"), ("ouzbek", "UZ"), ("kazakh", "KZ"),
    ("armeni", "AM"), ("azerbaidjan", "AZ"), ("georgi", "GE"),
    ("serbe", "RS"), ("serbie", "RS"), ("bosni", "BA"), ("kosov", "XK"),
    ("indien", "IN"), ("inde", "IN"), ("indonesi", "ID"),
    ("philippin", "PH"), ("sri lank", "LK"), ("bangladesh", "BD"),
    ("nigeria", "NG"), ("nigerian", "NG"), ("nigerien", "NE"), ("niger", "NE"),
    ("burkin", "BF"), ("tchad", "TD"), ("tchadien", "TD"),
    ("camerou", "CM"), ("senegal", "SN"), ("mauritani", "MR"),
    ("kowei", "KW"), ("qatar", "QA"), ("emirat", "AE"), ("bahrein", "BH"),
    ("britanni", "GB"), ("royaume-uni", "GB"), ("americain", "US"), ("etats-unis", "US"),
    ("allemand", "DE"), ("allemagne", "DE"), ("belge", "BE"), ("belgique", "BE"),
    ("espagnol", "ES"), ("espagne", "ES"), ("italien", "IT"), ("italie", "IT"),
]


def _strip_accents_lower(text: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", str(text or "").lower())
        if unicodedata.category(c) != "Mn"
    ).strip()


def dgt_country_to_iso2(value: str) -> str:
    """
    Convertit un pays / une nationalite du registre DGT (libelle francais,
    ex. "Russe", "Russie") en code ISO2. Repli sur la valeur d'origine si
    aucun radical connu ne correspond (la cle de blocking reste coherente
    en interne, meme si elle ne croisera pas les codes ISO clients).
    """
    normalized = _strip_accents_lower(value)
    if re.fullmatch(r"[a-z]{2}", normalized):
        return normalized.upper()
    for stem, iso2 in _DGT_COUNTRY_STEMS:
        if normalized.startswith(stem) or f" {stem}" in f" {normalized}":
            return iso2
    return str(value).strip()


def _dgt_value_text(value_obj: Any, *preferred_keys: str) -> str:
    """
    Extrait le texte d'un objet Valeur du registre DGT : cherche d'abord les
    cles preferees (insensible a la casse), sinon joint toutes les valeurs
    texte non vides de l'objet.
    """
    if not isinstance(value_obj, dict):
        return str(value_obj or "").strip()
    for key in preferred_keys:
        val = dict_get_insensitive(value_obj, key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return " ".join(
        str(v).strip() for v in value_obj.values()
        if v is not None and isinstance(v, (str, int, float)) and str(v).strip()
    ).strip()


def _dgt_details_by_type(record: Dict[str, Any]) -> Dict[str, List[Any]]:
    """Indexe les RegistreDetail par TypeChamp -> liste d'objets Valeur."""
    indexed: Dict[str, List[Any]] = {}
    for detail in record.get("RegistreDetail") or []:
        type_champ = str(detail.get("TypeChamp") or "").strip().upper()
        if not type_champ:
            continue
        values = detail.get("Valeur")
        if values is None:
            continue
        if not isinstance(values, list):
            values = [values]
        indexed.setdefault(type_champ, []).extend(values)
    return indexed


def _dgt_date(value_obj: Any) -> Optional[str]:
    """Assemble une date YYYY-MM-DD depuis un objet {Jour, Mois, Annee} (jour/mois optionnels)."""
    if not isinstance(value_obj, dict):
        return None
    year = _dgt_value_text(value_obj, "Annee", "Year")
    if not year or not re.fullmatch(r"\d{4}", year):
        # Certains enregistrements portent la date complete dans un seul champ
        raw = _dgt_value_text(value_obj)
        match = re.search(r"(\d{4})(?:-(\d{1,2})-(\d{1,2}))?", raw)
        if not match:
            return None
        year, month, day = match.group(1), match.group(2) or "01", match.group(3) or "01"
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    month = _dgt_value_text(value_obj, "Mois", "Month") or "01"
    day = _dgt_value_text(value_obj, "Jour", "Day") or "01"
    if not month.isdigit():
        month = "01"
    if not day.isdigit():
        day = "01"
    return f"{year}-{month.zfill(2)}-{day.zfill(2)}"


def parse_dgt_gels_json(file_path: str) -> Generator[Dict[str, Any], None, None]:
    """
    Parse le fichier JSON du registre national des gels (DGT) et produit des
    enregistrements au schema pivot Fiskr.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    publications = dict_get_insensitive(data, "Publications") or {}
    if isinstance(publications, list):
        publications = publications[0] if publications else {}
    records = dict_get_insensitive(publications, "PublicationDetail") or []

    for record in records:
        id_registre = record.get("IdRegistre")
        if id_registre is None:
            continue
        nature = str(record.get("Nature") or "").strip().lower()
        entity_type = DGT_NATURE_TO_TYPE.get(nature, "E")
        last_name = str(record.get("Nom") or "").strip()

        details = _dgt_details_by_type(record)

        first_name = ""
        for v in details.get("PRENOM", []):
            first_name = _dgt_value_text(v, "Prenom")
            if first_name:
                break

        if entity_type == "I":
            primary_name = f"{first_name} {last_name}".strip()
        else:
            primary_name = last_name
        if not primary_name:
            continue

        gender = "U"
        for v in details.get("SEXE", []):
            sexe = _dgt_value_text(v, "Sexe").lower()
            if sexe.startswith("f"):
                gender = "F"
            elif sexe.startswith("m"):
                gender = "M"

        dobs = []
        for v in details.get("DATE_DE_NAISSANCE", []):
            date_val = _dgt_date(v)
            if date_val:
                dobs.append(date_val)

        place_of_birth = None
        birth_countries = []
        for v in details.get("LIEU_DE_NAISSANCE", []):
            lieu = _dgt_value_text(v, "Lieu")
            pays = _dgt_value_text(v, "Pays")
            if not place_of_birth and (lieu or pays):
                place_of_birth = ", ".join(p for p in (lieu, pays) if p)
            if pays:
                birth_countries.append(dgt_country_to_iso2(pays))

        citizenships = []
        for v in details.get("NATIONALITE", []):
            pays = _dgt_value_text(v, "Pays", "Nationalite")
            if pays:
                citizenships.append(dgt_country_to_iso2(pays))

        aliases_raw = [
            {"name": alias, "type": "Strong"}
            for alias in (_dgt_value_text(v, "Alias") for v in details.get("ALIAS", []))
            if alias
        ]

        designation = None
        for v in details.get("TITRE", []):
            titre = _dgt_value_text(v, "Titre")
            if titre:
                designation = titre
                break

        addresses = []
        address_countries = []
        for type_champ in ("ADRESSE_PP", "ADRESSE_PM"):
            for v in details.get(type_champ, []):
                adresse = _dgt_value_text(v, "Adresse")
                pays = _dgt_value_text(v, "Pays")
                full = ", ".join(p for p in (adresse, pays) if p)
                if full:
                    addresses.append(full)
                if pays:
                    address_countries.append(pays)

        passports = []
        for v in details.get("PASSEPORT", []):
            numero = _dgt_value_text(v, "NumeroPasseport", "Numero")
            if numero:
                passports.append({"number": numero, "issuing_country": "XX", "expiration_date": None})

        other_registrations = []
        for type_champ in ("IDENTIFICATION", "AUTRE_IDENTITE"):
            for v in details.get(type_champ, []):
                ident = _dgt_value_text(v, "Identification", "NumeroCarte", "Numero")
                if ident:
                    other_registrations.append({"id_type": type_champ.title(), "number": ident})

        motifs = []
        for v in details.get("MOTIFS", []):
            motif = _dgt_value_text(v, "Motifs", "Motif")
            if motif:
                motifs.append(motif)

        extra_info = []
        for type_champ, keys in (
            ("FONDEMENT_JURIDIQUE", ("FondementJuridiqueLabel", "FondementJuridique")),
            ("REFERENCE_UE", ("ReferenceUe",)),
            ("REFERENCE_ONU", ("ReferenceOnu",)),
        ):
            for v in details.get(type_champ, []):
                text = _dgt_value_text(v, *keys)
                if text:
                    extra_info.append(f"{type_champ.replace('_', ' ').title()}: {text}")

        yield {
            "entity_id": f"DGT-{id_registre}",
            "entity_type": entity_type,
            "primary_name": primary_name,
            "individual_name_parsed": {
                "first_name": first_name,
                "last_name": last_name if entity_type == "I" else "",
                "maiden_name": ""
            },
            "aliases": categorize_aliases(aliases_raw),
            "dates_of_birth": sorted(set(dobs)),
            "date_of_death": None,
            "is_deceased": False,
            "gender": gender,
            "countries": {
                "citizenship": sorted(set(citizenships)),
                "residence": [],
                "birth_country": sorted(set(birth_countries)),
                "jurisdiction_country": sorted({dgt_country_to_iso2(c) for c in address_countries}) if entity_type != "I" else []
            },
            "place_of_birth": place_of_birth,
            "address": addresses[0] if addresses else None,
            "alternative_addresses": addresses[1:],
            "country": address_countries[0] if address_countries else None,
            "designation": designation,
            "designation_reasons": "; ".join(motifs) or None,
            "additional_informations": "; ".join(extra_info) or None,
            "origin": "DGT Registre national des gels",
            "imo_number": None,
            "aircraft_tail_number": None,
            "lei_number": None,
            "national_registry_ids": [],
            "other_registration_ids": other_registrations,
            "passport_documents": passports,
            "national_id_documents": [],
            "other_id_documents": []
        }


# ------------------ CSV CONNECTOR ------------------

def parse_csv_file(file_path: str, delimiter: str = ",", mapping_dict: dict = None) -> Generator[Dict[str, Any], None, None]:
    """
    Parses Client or Watchlist CSV dataset dynamically.
    Uses custom delimiters and maps columns according to config.
    """
    with open(file_path, mode="r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            # Map columns
            mapped_row = {}
            if mapping_dict:
                for target, source in mapping_dict.items():
                    mapped_row[target] = row.get(source, "")
            else:
                # Direct mirror map based on headers
                for k, v in row.items():
                    mapped_row[k] = v
                    
            yield mapped_row


# ------------------ PDF IA PARSING CONNECTOR ------------------

def parse_pdf_watchlist(file_path: str) -> List[Dict[str, Any]]:
    """
    Ingests publications/PDF files:
    1. Extracts text via pypdf.
    2. Runs NER heuristic to structure entities.
    3. Simulates LLM schema verification.
    """
    text = ""
    if PDF_AVAILABLE:
        try:
            with open(file_path, "rb") as f:
                pdf = pypdf.PdfReader(f)
                for page in pdf.pages:
                    text += page.extract_text() or ""
        except Exception as e:
            logger.error(f"Error reading PDF: {e}")
    else:
        # Fallback if library missing
        logger.warning("pypdf not installed, simulating text extraction")
        text = "COMMISSION REGULATION - Gels d'avoirs - AL-MANSOUR SHIPPING (IMO 99412) - pays résidence: RU."

    # Step 2: Simulated LLM Named Entity Recognition (NER)
    # Scan text for names, countries, dates and IDs
    entities_extracted = []
    
    # We parse the text using regular expressions to mimic a structured LLM parser.
    # Ex: AL-MANSOUR SHIPPING, RESIDENCE: RU, IMO: 99412
    vessels = re.findall(r"([A-Z\-\s]+)\s*\(IMO\s*(\d+)\)", text)
    for name, imo in vessels:
        entities_extracted.append({
            "entity_id": f"PDF-VES-{imo}",
            "entity_type": "V",
            "primary_name": name.strip(),
            "imo_number": imo,
            "countries": {"jurisdiction_country": ["RU"]},
            "extraction_confidence": 95.0 # High confidence
        })
        
    # Standard warning if no clear patterns found (confidence < 85%)
    if not entities_extracted:
        # Generate a warning mock entry with low confidence
        entities_extracted.append({
            "entity_id": "PDF-LOW-CONF",
            "entity_type": "I",
            "primary_name": "INCONNU EXTRAIT",
            "extraction_confidence": 75.0, # Will trigger Rule_M08 warning
            "countries": {}
        })
        
    return entities_extracted

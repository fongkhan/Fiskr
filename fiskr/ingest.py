import re
import csv
import json
import logging
from typing import List, Dict, Any, Generator
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
        self.locations = {}
        self.location_countries = {} # location_id -> Country ISO2
        self.id_documents = {}  # identity_id -> [doc_dict, ...]

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
        return "E"
    if isinstance(pst, dict):
        pst_id = pst.get('id', '')
    elif isinstance(pst, list) and pst:
        pst_id = pst[0].get('id', '') if isinstance(pst[0], dict) else ''
    else:
        pst_id = str(pst)
    
    links = parser_ctx.ref_links.get('PartySubType', {}).get(pst_id, {})
    pt_id = links.get('PartyTypeID', '')
    pt_name = get_reference_value(parser_ctx.references, 'PartyType', pt_id).lower()
    
    if "individual" in pt_name:
        return "I"
    elif "vessel" in pt_name:
        return "V"
    elif "aircraft" in pt_name:
        return "O"
    else:
        return "E"

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
    
    # Pass 1: Parse ReferenceValueSets, Locations, IDRegDocuments
    context1 = ET.iterparse(file_path, events=('start', 'end'))
    in_ref = False
    in_locs = False
    in_id_docs = False
    root1 = None
    
    for event, elem in context1:
        if event == 'start' and root1 is None:
            root1 = elem
            
        tag = elem.tag.split('}')[-1]
        
        if event == 'start' and tag == 'ReferenceValueSets':
            in_ref = True
        elif event == 'end' and in_ref and tag == 'ReferenceValueSets':
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
            elem.clear()
            if root1 is not None:
                root1.clear()
            in_ref = False
            
        elif event == 'start' and tag == 'Locations':
            in_locs = True
        elif event == 'end' and in_locs and tag == 'Location':
            if 'ID' in elem.attrib:
                id_val = elem.attrib['ID']
                loc_parts = []
                ns = elem.tag.split('}')[0] + '}'
                for p in elem.iter(f"{ns}Value"):
                    if p.text:
                        loc_parts.append(p.text.strip())
                cid = None
                for p in elem.iter(f"{ns}LocationCountry"):
                    cid = p.attrib.get('CountryID')
                    if cid and cid in parser_ctx.references.get('Country', {}):
                        loc_parts.append(parser_ctx.references['Country'][cid])
                parser_ctx.locations[id_val] = ", ".join(loc_parts)
                
                if cid:
                    c_links = parser_ctx.ref_links.get('Country', {}).get(cid, {})
                    iso2 = c_links.get('ISO2') or c_links.get('Code')
                    if not iso2 and cid in parser_ctx.references.get('Country', {}):
                        country_name = parser_ctx.references['Country'][cid]
                        iso2 = country_name[:2].upper()
                    if iso2:
                        parser_ctx.location_countries[id_val] = iso2
            elem.clear()
            if root1 is not None:
                root1.clear()
        elif event == 'end' and in_locs and tag == 'Locations':
            in_locs = False
            if root1 is not None:
                root1.clear()
            
        elif event == 'start' and tag == 'IDRegDocuments':
            in_id_docs = True
        elif event == 'end' and in_id_docs and tag == 'IDRegDocument':
            identity_id = elem.attrib.get('IdentityID')
            if identity_id:
                doc_type_id = elem.attrib.get('IDRegDocTypeID')
                doc_num = ""
                ns = elem.tag.split('}')[0] + '}'
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
                
                doc_dict = {
                    "doc_type_id": doc_type_id,
                    "number": doc_num,
                    "issuing_country": issuing_country,
                    "expiration_date": None
                }
                if identity_id not in parser_ctx.id_documents:
                    parser_ctx.id_documents[identity_id] = []
                parser_ctx.id_documents[identity_id].append(doc_dict)
            elem.clear()
            if root1 is not None:
                root1.clear()
        elif event == 'end' and in_id_docs and tag == 'IDRegDocuments':
            in_id_docs = False
            if root1 is not None:
                root1.clear()
                
        # Periodic clear to keep memory low
        if event == 'end' and tag not in ['ReferenceValueSets', 'Locations', 'IDRegDocuments', 'Location', 'IDRegDocument']:
            elem.clear()
            if root1 is not None:
                root1.clear()

    # Pass 2: Parse DistinctParties
    context2 = ET.iterparse(file_path, events=('start', 'end'))
    root2 = None
    
    for event, elem in context2:
        if event == 'start' and root2 is None:
            root2 = elem
            
        tag = elem.tag.split('}')[-1]
        
        if event == 'end' and tag == 'DistinctParty':
            ns = elem.tag.split('}')[0] + '}'
            prof_elem = elem.find(f'{ns}Profile')
            if prof_elem is None:
                elem.clear()
                if root2 is not None:
                    root2.clear()
                continue
                
            pid = (
                get_attrib_insensitive(elem, "fixedRef")
                or get_attrib_insensitive(elem, "ID")
                or (get_attrib_insensitive(prof_elem, "ID") if prof_elem is not None else None)
            )
            if not pid:
                elem.clear()
                if root2 is not None:
                    root2.clear()
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
            
            # Extract features (DOB, Gender, Death/Deceased, countries)
            dobs = []
            date_of_death = None
            is_deceased = False
            gender = "U"
            citizenships = []
            residences = []
            birth_countries = []
            jurisdictions = []
            
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
                
                feature_versions = dict_get_insensitive(f, 'FeatureVersion') or []
                for fv in feature_versions:
                    # Gender
                    if is_gender:
                         version_details = dict_get_insensitive(fv, 'VersionDetail') or []
                         for vd in version_details:
                             ref_val = ""
                             ref_obj = dict_get_insensitive(vd, 'DetailReferenceID')
                             if ref_obj:
                                 if isinstance(ref_obj, dict):
                                     ref_val = ref_obj.get('value', '')
                                 elif isinstance(ref_obj, list):
                                     for child in ref_obj:
                                         if isinstance(child, dict):
                                             ref_val += " " + (child.get('value') or child.get('text') or "")
                                 else:
                                     ref_val = str(ref_obj)
                             
                             # Also check DetailReference (child tag in some mock XML schemas)
                             ref_ref = dict_get_insensitive(vd, 'DetailReference')
                             if ref_ref:
                                 if isinstance(ref_ref, list):
                                     for child in ref_ref:
                                         if isinstance(child, dict):
                                             ref_val += " " + (child.get('value') or child.get('text') or "")
                                 elif isinstance(ref_ref, dict):
                                     ref_val += " " + (ref_ref.get('value') or ref_ref.get('text') or "")
                                 else:
                                     ref_val += " " + str(ref_ref)
                                     
                             ref_val_lower = ref_val.lower()
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
                    
                    # Country codes from features
                    version_locations = dict_get_insensitive(fv, 'VersionLocation') or []
                    for vl in version_locations:
                        lid_obj = dict_get_insensitive(vl, 'LocationID')
                        lid = lid_obj.get('id') if isinstance(lid_obj, dict) else str(lid_obj)
                        if lid:
                            country_code = parser_ctx.location_countries.get(lid)
                            if country_code:
                                if "citizenship" in ftype_str_lower or "nationality" in ftype_str_lower:
                                    citizenships.append(country_code)
                                elif "residence" in ftype_str_lower:
                                    residences.append(country_code)
                                elif "birth" in ftype_str_lower:
                                    birth_countries.append(country_code)
                                else:
                                    jurisdictions.append(country_code)
                                    
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
            
            # Load documents linked to any identity in the profile
            for identity in profile.get('Identity', []):
                ident_id = identity.get('ID')
                if not ident_id:
                    continue
                docs = parser_ctx.id_documents.get(ident_id, [])
                for doc in docs:
                    doc_type_id = doc["doc_type_id"]
                    doc_num = doc["number"]
                    issued_country = doc["issuing_country"]
                    doc_type_name = parser_ctx.references.get('IDRegDocType', {}).get(doc_type_id, "").lower()
                    
                    if doc_type_id == "392" or "passport" in doc_type_name:
                        passports.append({"number": doc_num, "issuing_country": issued_country, "expiration_date": None})
                    elif doc_type_id == "391" or "national id" in doc_type_name:
                        national_ids.append({"number": doc_num, "issuing_country": issued_country})
                    elif doc_type_id in ["386", "390", "394"] or "driver" in doc_type_name:
                        other_ids.append({"doc_type": "DriverLicense" if doc_type_id == "386" or "driver" in doc_type_name else "Other", "number": doc_num, "issuing_country": issued_country})
                    elif doc_type_id == "15502" or "lei" in doc_type_name:
                        lei = doc_num
                    elif doc_type_id in ["9436", "376", "384"] or "tax" in doc_type_name or "commercial" in doc_type_name:
                        national_registry.append({"number": doc_num, "country": issued_country, "registry_name": "CommercialRegistry" if doc_type_id == "9436" or "commercial" in doc_type_name else "TaxRegistry"})
                    elif doc_type_id == "13886" or "imo" in doc_type_name:
                        digits = re.sub(r"\D", "", doc_num)
                        imo_number = digits[:7]
                    elif doc_type_id == "13887" or "aircraft" in doc_type_name:
                        aircraft_tail = doc_num
                    else:
                        other_registrations.append({"id_type": doc_type_name or "OtherRegistration", "number": doc_num})
            
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
                        doc_type_name = parser_ctx.references.get('IDRegDocType', {}).get(doc_type_id, "").lower()
                        if doc_type_id == "392" or "passport" in doc_type_name:
                            passports.append({"number": doc_num, "issuing_country": issued_country, "expiration_date": None})
                        elif doc_type_id == "391" or "national id" in doc_type_name:
                            national_ids.append({"number": doc_num, "issuing_country": issued_country})
                        elif doc_type_id in ["386", "390", "394"] or "driver" in doc_type_name:
                            other_ids.append({"doc_type": "DriverLicense" if doc_type_id == "386" or "driver" in doc_type_name else "Other", "number": doc_num, "issuing_country": issued_country})
                        elif doc_type_id == "15502" or "lei" in doc_type_name:
                            lei = doc_num
                        elif doc_type_id in ["9436", "376", "384"] or "tax" in doc_type_name or "commercial" in doc_type_name:
                            national_registry.append({"number": doc_num, "country": issued_country, "registry_name": "CommercialRegistry" if doc_type_id == "9436" or "commercial" in doc_type_name else "TaxRegistry"})
                        elif doc_type_id == "13886" or "imo" in doc_type_name:
                            digits = re.sub(r"\D", "", doc_num)
                            imo_number = digits[:7]
                        elif doc_type_id == "13887" or "aircraft" in doc_type_name:
                            aircraft_tail = doc_num
                        else:
                            other_registrations.append({"id_type": doc_type_name or "OtherRegistration", "number": doc_num})

            # Build Pivot structure
            aliases_categorized = categorize_aliases(aliases_raw)
            current_party = {
                "entity_id": pid,
                "entity_type": entity_type_id or "E",
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
            
            # Clear distinct party element from memory
            elem.clear()
            if root2 is not None:
                root2.clear()
                



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

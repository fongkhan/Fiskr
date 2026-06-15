import re
from typing import Set, List
from fiskr.phonetics import double_metaphone

def generate_blocking_keys(entity: dict, config: dict) -> Set[str]:
    """
    Generates a set of blocking keys for an entity based on the configured layout.
    Supports both listed entities (I, E, V, O) and client base records (PP, PM).
    """
    blocking_config = config.get("blocking", {})
    layout = blocking_config.get("custom_key_layout", ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])
    
    is_client = "client_type" in entity or "client_id" in entity
    
    components_values = {}
    
    for item in layout:
        if item == "COUNTRY_ISO":
            all_countries = []
            if is_client:
                countries_dict = entity.get("client_countries", {}) or {}
                nationality = countries_dict.get("nationality", []) or []
                residence = countries_dict.get("residence", []) or []
                birth = countries_dict.get("birth_country", []) or []
                reg = countries_dict.get("registration_country", []) or []
                all_countries = list(set(nationality + residence + birth + reg))
            else:
                countries_dict = entity.get("countries", {}) or {}
                citizenship = countries_dict.get("citizenship", []) or []
                residence = countries_dict.get("residence", []) or []
                birth = countries_dict.get("birth_country", []) or []
                jurisdiction = countries_dict.get("jurisdiction_country", []) or []
                all_countries = list(set(citizenship + residence + birth + jurisdiction))
                
            all_countries = [c.upper().strip() for c in all_countries if c and str(c).strip()]
            
            if not all_countries:
                components_values[item] = ["XX"]
            else:
                components_values[item] = all_countries
                
        elif item == "ENTITY_TYPE":
            if is_client:
                ctype = entity.get("client_type", "") or ""
                ctype = ctype.upper().strip()
                # Map client PP -> PP, PM -> PM
                if ctype not in ["PP", "PM"]:
                    components_values[item] = ["XX"]
                else:
                    components_values[item] = [ctype]
            else:
                etype = entity.get("entity_type", "") or ""
                etype = etype.upper().strip()
                # Map watchlist I -> PP (Individual), E/V/O -> PM (Non-Individual)
                if etype == "I":
                    components_values[item] = ["PP"]
                elif etype in ["E", "V", "O"]:
                    components_values[item] = ["PM"]
                else:
                    components_values[item] = ["XX"]
                    
        elif item == "PHONETIC_FIRST":
            names = []
            if is_client:
                client_type = entity.get("client_type", "")
                if client_type == "PP":
                    first = entity.get("client_first_name", "") or ""
                    last = entity.get("client_last_name", "") or ""
                    maiden = entity.get("client_maiden_name", "") or ""
                    if first.strip():
                        names.append(first)
                    if last.strip():
                        names.append(last)
                    if maiden.strip():
                        names.append(maiden)
                else:
                    company = entity.get("client_company_name", "") or ""
                    if company.strip():
                        names.append(company)
            else:
                primary_name = entity.get("primary_name", "") or ""
                if primary_name.strip():
                    names.append(primary_name)
                
                # Check parsed maiden name
                parsed = entity.get("individual_name_parsed", {}) or {}
                maiden = parsed.get("maiden_name", "") or ""
                if maiden.strip():
                    names.append(maiden)
                
                # Check aliases (Only high priority ones!)
                raw_aliases = entity.get("aliases", []) or []
                if isinstance(raw_aliases, dict):
                    aliases = raw_aliases.get("high_priority", []) or []
                else:
                    # If it's a flat list, we filter it dynamically using the qualification logic in ingest.py
                    # To avoid import loops, we implement a simple local filter:
                    aliases = []
                    for alias in raw_aliases:
                        if not alias:
                            continue
                        clean_a = re.sub(r"[\._\-]", " ", alias).strip()
                        words = clean_a.split()
                        # Low priority if single word, or len <= 4
                        if len(words) <= 1 or len(clean_a) <= 4:
                            continue
                        aliases.append(alias)
                
                for alias in aliases:
                    if alias and str(alias).strip():
                        names.append(alias)
            
            phonetics = set()
            for name in names:
                name_clean = str(name).strip()
                if not name_clean:
                    continue
                words = re.split(r"[\s\-]+", name_clean)
                first_word = words[0] if words else ""
                if first_word:
                    p_key, s_key = double_metaphone(first_word)
                    if p_key:
                        phonetics.add(p_key)
                    if s_key:
                        phonetics.add(s_key)
                        
            if not phonetics:
                components_values[item] = ["XX"]
            else:
                components_values[item] = list(phonetics)
        else:
            components_values[item] = ["XX"]
            
    # Compute Cartesian Product
    keys = {""}
    for item in layout:
        new_keys = set()
        values = components_values[item]
        for val in values:
            for k in keys:
                new_keys.add(f"{k}_{val}" if k else val)
        keys = new_keys
        
    return keys

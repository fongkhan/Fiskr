from typing import Set, List
from fiskr.phonetics import double_metaphone

def generate_blocking_keys(entity: dict, config: dict) -> Set[str]:
    """
    Generates a set of blocking keys for an entity based on the configured layout.
    Example key layout: ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
    
    If attributes are missing, defaults to 'XX'.
    Uses a Cartesian product if fields have multiple values (like countries or aliases).
    """
    blocking_config = config.get("blocking", {})
    layout = blocking_config.get("custom_key_layout", ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])
    
    # 1. Resolve components
    components_values = {}
    
    for item in layout:
        if item == "COUNTRY_ISO":
            # Extract countries from citizenship, residence, and birth_country
            countries_dict = entity.get("countries", {}) or {}
            citizenship = countries_dict.get("citizenship", []) or []
            residence = countries_dict.get("residence", []) or []
            birth_country = countries_dict.get("birth_country", []) or []
            
            # Combine and clean
            all_countries = list(set(citizenship + residence + birth_country))
            all_countries = [c.upper().strip() for c in all_countries if c and c.strip()]
            
            if not all_countries:
                components_values[item] = ["XX"]
            else:
                components_values[item] = all_countries
                
        elif item == "ENTITY_TYPE":
            etype = entity.get("entity_type", "") or ""
            etype = etype.upper().strip()
            if etype not in ["PP", "PM"]:
                components_values[item] = ["XX"]
            else:
                components_values[item] = [etype]
                
        elif item == "PHONETIC_FIRST":
            # Get primary name and aliases
            names = []
            primary_name = entity.get("primary_name", "") or ""
            if primary_name.strip():
                names.append(primary_name)
            
            aliases = entity.get("aliases", []) or []
            for alias in aliases:
                if alias and alias.strip():
                    names.append(alias)
            
            import re
            phonetics = set()
            for name in names:
                name_clean = name.strip()
                if not name_clean:
                    continue
                # Split and take the first word (handling spaces and hyphens)
                words = re.split(r"[\s\-]+", name_clean)
                first_word = words[0] if words else ""
                if first_word:
                    # Get primary and secondary metaphone codes
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
            # Unknown custom layout variable fallback
            components_values[item] = ["XX"]
            
    # 2. Compute Cartesian Product of layout components
    keys = {""}
    for item in layout:
        new_keys = set()
        values = components_values[item]
        for val in values:
            for k in keys:
                new_keys.add(f"{k}_{val}" if k else val)
        keys = new_keys
        
    return keys

import re
import unicodedata

def has_non_latin_chars(text: str) -> bool:
    """Checks if text contains characters outside ASCII/extended Latin."""
    for char in text:
        # Check unicode block name or if it's beyond standard Latin
        try:
            name = unicodedata.name(char)
            # If it belongs to Cyrillic, Arabic, Han, etc.
            if any(block in name for block in ["CYRILLIC", "ARABIC", "CJK", "HEBREW", "THAI", "GREEK"]):
                return True
        except ValueError:
            # If unicode name is unknown, check if outside basic Latin-1 range
            if ord(char) > 255:
                return True
    return False

def strip_accents(text: str) -> str:
    """Removes accents and diacritics, e.g., Müller -> Muller."""
    nfkd_form = unicodedata.normalize('NFKD', text)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def clean_noise_words(text: str) -> str:
    """Removes corporate noise suffixes (SA, SARL, LLC, LTD, GMBH, SOCIETE) for PMs."""
    # Case-insensitive replacement on word boundaries
    pattern = r"\b(SA|SARL|LLC|LTD|GMBH|SOCIETE)\b"
    cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE)
    # Remove any extra space created
    return re.sub(r"\s+", " ", cleaned).strip()

def evaluate_and_clean(entity: dict) -> dict:
    """
    Evaluates data quality rules on the incoming entity and returns the evaluation report.
    Returns:
        dict: {
            "is_valid": bool,
            "status": "OK" | "DEGRADED" | "REJECT",
            "errors": list[str],
            "warnings": list[str],
            "cleansed_name": str,
            "cleansed_aliases": list[str]
        }
    """
    name = entity.get("primary_name", "") or ""
    entity_type = entity.get("entity_type", "") or ""
    dob_list = entity.get("dates_of_birth", []) or []
    countries = entity.get("countries", {}) or {}
    
    # Extract countries from nested structure
    citizenship = countries.get("citizenship", []) or []
    residence = countries.get("residence", []) or []
    birth_country = countries.get("birth_country", []) or []
    all_countries = list(set(citizenship + residence + birth_country))
    
    errors = []
    warnings = []
    
    # ------------------ LEVEL 1: CRITICAL / REJECT ------------------
    # Rule_B01: Champ Nom Vide
    trimmed_name = name.strip()
    if len(trimmed_name) == 0:
        errors.append("Rule_B01: Champ Nom Vide")
        
    # Rule_B02: Longueur Insuffisante (Moins de 2 caractères alphanumériques)
    alphanumeric_chars = "".join([c for c in name if c.isalnum()])
    if len(trimmed_name) > 0 and len(alphanumeric_chars) < 2:
        errors.append("Rule_B02: Longueur Insuffisante (Moins de 2 caractères alphanumériques de base)")
        
    # Rule_B03: Type d'Entité Inconnu (Doit être PP ou PM)
    if entity_type not in ["PP", "PM"]:
        errors.append("Rule_B03: Type d'Entité Inconnu (doit être PP ou PM)")
        
    if errors:
        return {
            "is_valid": False,
            "status": "REJECT",
            "errors": errors,
            "warnings": warnings,
            "cleansed_name": name,
            "cleansed_aliases": entity.get("aliases", []) or []
        }
        
    # ------------------ LEVEL 2: WARNING / DEGRADED ------------------
    # Rule_M01: Absence de Pays
    if not all_countries:
        warnings.append("Rule_M01: Absence de Pays")
        
    # Rule_M02: DOB Absente pour PP
    if entity_type == "PP" and not dob_list:
        warnings.append("Rule_M02: DOB Absente pour PP")
        
    # Rule_M03: Caractères Non Translittérés (Hors ASCII/Latin)
    if has_non_latin_chars(name):
        warnings.append("Rule_M03: Présence de caractères hors blocs ASCII étendu/Latin")
        
    status = "DEGRADED" if warnings else "OK"
    
    # ------------------ LEVEL 3: INFO / AUTO-CLEAN & Cleansing ------------------
    def cleanse_single_name(n: str, is_pm: bool) -> str:
        # Rule_I01: Espaces multiples -> espace simple
        n = re.sub(r"\s+", " ", n).strip()
        
        # Rule_I02: Caractères Spéciaux Isolés (Suppression des symboles @, #, $, etc.)
        # Keep letters, numbers, spaces, and hyphens/apostrophes
        n = re.sub(r"[@#\$%\^&\*\(\)\+=\{\}\[\]\|\\:;\"<>\?,]", "", n)
        
        # 1. Passage en Majuscules
        n = n.upper()
        
        # 2. Aplatissement ASCII (Stripping)
        n = strip_accents(n)
        
        # 3. Suppression des "Noise Words" (Pour PM uniquement)
        if is_pm:
            n = clean_noise_words(n)
            
        # Final trim and space collapse
        n = re.sub(r"\s+", " ", n).strip()
        return n
        
    cleansed_name = cleanse_single_name(name, entity_type == "PM")
    
    # Cleanse aliases as well using the same rules
    raw_aliases = entity.get("aliases", []) or []
    cleansed_aliases = [cleanse_single_name(alias, entity_type == "PM") for alias in raw_aliases if alias and alias.strip()]
    
    return {
        "is_valid": True,
        "status": status,
        "errors": [],
        "warnings": warnings,
        "cleansed_name": cleansed_name,
        "cleansed_aliases": cleansed_aliases
    }

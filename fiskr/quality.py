import re
import unicodedata
from datetime import datetime

# Translitteration multi-ecritures (cyrillique, arabe, CJK, grec, hebreu...)
# vers le latin : indispensable pour que les alias non latins des listes
# officielles (OFAC, ONU) matchent les noms latins du referentiel clients.
# Repli silencieux sur l'aplatissement de diacritiques si absent.
try:
    from anyascii import anyascii as _transliterate
    TRANSLIT_AVAILABLE = True
except ImportError:
    _transliterate = None
    TRANSLIT_AVAILABLE = False

def has_non_latin_chars(text: str) -> bool:
    """Checks if text contains characters outside ASCII/extended Latin."""
    for char in text:
        try:
            name = unicodedata.name(char)
            if any(block in name for block in ["CYRILLIC", "ARABIC", "CJK", "HEBREW", "THAI", "GREEK"]):
                return True
        except ValueError:
            if ord(char) > 255:
                return True
    return False

def strip_accents(text: str) -> str:
    """
    Normalise un nom vers le latin ASCII : translittere d'abord les ecritures
    non latines (cyrillique Владимир -> Vladimir, arabe, CJK...) quand le
    texte en contient, puis retire accents et diacritiques (Müller -> Muller).
    Utilise partout (nettoyage a l'ingestion, scoring des deux cotes).
    """
    if TRANSLIT_AVAILABLE and text and has_non_latin_chars(text):
        text = _transliterate(text)
    nfkd_form = unicodedata.normalize('NFKD', text)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])

def clean_noise_words(text: str) -> str:
    """Removes corporate noise suffixes (SA, SARL, LLC, LTD, GMBH, SOCIETE) for PMs."""
    pattern = r"\b(SA|SARL|LLC|LTD|GMBH|SOCIETE)\b"
    cleaned = re.sub(pattern, "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", cleaned).strip()

def validate_date(date_str: str) -> bool:
    """Validates if date is in strict YYYY-MM-DD format."""
    if not date_str:
        return False
    try:
        datetime.strptime(date_str.strip(), "%Y-%m-%d")
        return True
    except ValueError:
        return False

def validate_lei(lei: str) -> bool:
    """Checks if LEI is 20-character alphanumeric."""
    if not lei:
        return False
    lei_clean = lei.strip()
    return len(lei_clean) == 20 and lei_clean.isalnum()

def evaluate_and_clean(entity: dict) -> dict:
    """
    Evaluates upgraded data quality rules on Watchlist or Client entries.
    Identifies Level 1 REJECT, Level 2 WARNING/DEGRADED, and Level 3 AUTO-CLEAN.
    """
    errors = []
    warnings = []
    
    # 1. Determine if this is a Client base record or a Watchlist entry
    is_client = "client_type" in entity or "client_id" in entity
    
    if is_client:
        # Client Referentials
        client_id = entity.get("client_id", "")
        client_type = entity.get("client_type", "")
        first_name = entity.get("client_first_name", "") or ""
        last_name = entity.get("client_last_name", "") or ""
        company_name = entity.get("client_company_name", "") or ""
        maiden_name = entity.get("client_maiden_name", "") or ""
        dob = entity.get("client_dob", "") or ""
        gender = entity.get("client_gender", "U") or "U"
        is_deceased = str(entity.get("client_is_deceased", "False")).lower() == "true"
        
        # Countries
        countries_dict = entity.get("client_countries", {}) or {}
        nationality = countries_dict.get("nationality", []) or []
        residence = countries_dict.get("residence", []) or []
        birth = countries_dict.get("birth_country", []) or []
        reg = countries_dict.get("registration_country", []) or []
        all_countries = list(set(nationality + residence + birth + reg))
        
        # LEI
        lei = entity.get("client_lei_number", "") or ""
        
        # Primary Name resolution
        if client_type == "PP":
            primary_name = f"{first_name} {last_name}".strip()
        else:
            primary_name = company_name.strip()
            
    else:
        # Watchlist Entities
        entity_id = entity.get("entity_id", "")
        entity_type = entity.get("entity_type", "")
        primary_name = entity.get("primary_name", "") or ""
        
        parsed_name = entity.get("individual_name_parsed", {}) or {}
        first_name = parsed_name.get("first_name", "") or ""
        last_name = parsed_name.get("last_name", "") or ""
        maiden_name = parsed_name.get("maiden_name", "") or ""
        
        dob_list = entity.get("dates_of_birth", []) or []
        dob = dob_list[0] if dob_list else ""
        
        gender = entity.get("gender", "U") or "U"
        is_deceased = str(entity.get("is_deceased", "False")).lower() == "true"
        date_of_death = entity.get("date_of_death", "") or ""
        
        # Countries
        countries_dict = entity.get("countries", {}) or {}
        citizenship = countries_dict.get("citizenship", []) or []
        residence = countries_dict.get("residence", []) or []
        birth = countries_dict.get("birth_country", []) or []
        jurisdiction = countries_dict.get("jurisdiction_country", []) or []
        all_countries = list(set(citizenship + residence + birth + jurisdiction))
        
        # LEI
        lei = entity.get("lei_number", "") or ""

    # ------------------ LEVEL 1: CRITICAL / REJECT ------------------
    # Rule_B01: Champ Nom Principal Vide
    if is_client:
        if client_type == "PP" and not last_name.strip():
            errors.append("Rule_B01: Champ Nom Principal Vide (client_last_name manquant)")
        elif client_type == "PM" and not company_name.strip():
            errors.append("Rule_B01: Champ Nom Principal Vide (client_company_name manquant)")
        elif not client_type and not primary_name.strip():
            errors.append("Rule_B01: Champ Nom Principal Vide")
    else:
        if not primary_name.strip():
            errors.append("Rule_B01: Champ Nom Principal Vide")

    # Rule_B02: Type d'Entité Invalide ou Incohérent
    if is_client:
        if client_type not in ["PP", "PM"]:
            errors.append(f"Rule_B02: Type d'Entité Invalide côté Client ({client_type} - doit être PP ou PM)")
    else:
        if entity_type not in ["I", "E", "V", "O"]:
            errors.append(f"Rule_B02: Type d'Entité Invalide côté Watchlist ({entity_type} - doit être I, E, V, ou O)")

    # Rule_B04: Incohérence Nom/Structure Individu (PP/I must have first and last names)
    current_type = client_type if is_client else entity_type
    if current_type in ["PP", "I"]:
        if not first_name.strip() and not last_name.strip():
            errors.append("Rule_B04: Incohérence Nom/Structure Individu (Prénom et Nom de famille absents)")

    # Rule_B05: Longueur Nom Insuffisante (Moins de 2 caractères alphanumériques nettoyés)
    alphanumeric_chars = "".join([c for c in primary_name if c.isalnum()])
    if primary_name.strip() and len(alphanumeric_chars) < 2:
        errors.append("Rule_B05: Longueur Nom Insuffisante (Moins de 2 caractères alphanumériques de base)")

    if errors:
        return {
            "is_valid": False,
            "status": "REJECT",
            "errors": errors,
            "warnings": warnings,
            "cleansed_name": primary_name,
            "cleansed_aliases": entity.get("aliases", []) or []
        }

    # ------------------ LEVEL 2: WARNING / DEGRADED ------------------
    # Rule_M01: Absence totale de géographie
    if not all_countries:
        warnings.append("Rule_M01: Absence totale de Géographie")

    # Rule_M02: Absence d'identifiant d'âge/existence
    if current_type in ["PP", "I"] and not is_deceased:
        if is_client and not dob:
            warnings.append("Rule_M02: Absence d'identifiant d'âge/existence (DOB manquante)")
        elif not is_client and not entity.get("dates_of_birth", []):
            warnings.append("Rule_M02: Absence d'identifiant d'âge/existence (DOB manquante)")

    # Rule_M03: Caractères Non Translittérés (Hors ASCII/Latin)
    if has_non_latin_chars(primary_name):
        warnings.append("Rule_M03: Présence de caractères hors blocs ASCII étendu/Latin")

    # Rule_M04: Contradiction Statut Vital Prémédité
    if not is_client and entity.get("date_of_death") and not is_deceased:
        warnings.append("Rule_M04: Contradiction Statut Vital Prémédité (date de décès fournie mais is_deceased est False)")
        # Force is_deceased for scoring downstream
        entity["is_deceased"] = True

    # Rule_M05: Format Date Invalide
    dates_to_test = []
    if is_client and dob:
        dates_to_test.append(dob)
    elif not is_client:
        dates_to_test.extend(entity.get("dates_of_birth", []))
        if entity.get("date_of_death"):
            dates_to_test.append(entity.get("date_of_death"))
            
    for d in dates_to_test:
        if d and not validate_date(d):
            warnings.append(f"Rule_M05: Format Date Invalide ({d} - doit respecter YYYY-MM-DD)")

    # Rule_M06: Format Numéro Passeport Suspect
    passports = []
    if is_client:
        passports = entity.get("client_passport_documents", []) or []
    else:
        passports = entity.get("passport_documents", []) or []
    for p in passports:
        pnum = p.get("number", "") if isinstance(p, dict) else ""
        if pnum:
            if len(pnum.strip()) < 4 or any(c in pnum for c in [" ", "-", "_", "@", "#"]):
                warnings.append(f"Rule_M06: Format Numéro Passeport Suspect ({pnum})")

    # Rule_M07: Structure LEI Invalide
    if lei:
        if not validate_lei(lei):
            warnings.append(f"Rule_M07: Structure LEI Invalide ({lei} - doit faire 20 caractères alphanumériques)")

    # Rule_M08: Échec d'Extraction PDF (si score de confiance < 85%)
    confidence = entity.get("extraction_confidence")
    if confidence is not None and float(confidence) < 85.0:
        warnings.append(f"Rule_M08: Échec d'Extraction de Confiance PDF (score de {confidence}%)")

    status = "DEGRADED" if warnings else "OK"

    # ------------------ LEVEL 3: AUTO-CLEAN & Cleansing ------------------
    # Rule_I03: Incohérence de Genre Multi-valué (Fallback to U)
    resolved_gender = "U"
    raw_genders = entity.get("genders", []) or []
    if not is_client and not raw_genders and entity.get("gender"):
        raw_genders = [entity.get("gender")]
    elif is_client and entity.get("client_gender"):
        raw_genders = [entity.get("client_gender")]
        
    g_clean = list(set(str(g).upper().strip() for g in raw_genders if g))
    if len(g_clean) == 1 and g_clean[0] in ["M", "F"]:
        resolved_gender = g_clean[0]
    elif len(g_clean) > 1:
        warnings.append("Rule_I03: Incohérence de Genre Multi-valué (Force le repli sur U)")
        resolved_gender = "U"

    def cleanse_text(t: str, is_pm: bool) -> str:
        # Rule_I01: Espaces multiples
        t = re.sub(r"\s+", " ", t).strip()
        
        # Rule_I02: Caractères Spéciaux de Saisie (Replace points, underscores with spaces)
        t = re.sub(r"[\._\-]", " ", t)
        t = re.sub(r"[@#\$%\^&\*\(\)\+=\{\}\[\]\|\\:;\"<>\?,]", "", t)
        
        # Standard casing
        t = t.upper()
        
        # Accent stripping
        t = strip_accents(t)
        
        # PM Noise Suffixes
        if is_pm:
            t = clean_noise_words(t)
            
        return re.sub(r"\s+", " ", t).strip()

    # Clean primary name
    is_pm_type = current_type in ["PM", "E", "V"]
    cleansed_primary_name = cleanse_text(primary_name, is_pm_type)
    
    # Clean aliases
    raw_aliases = entity.get("aliases", []) or []
    if isinstance(raw_aliases, dict):
        # Already structured as {"high_priority": [], "low_priority": []}
        high = [cleanse_text(a, is_pm_type) for a in raw_aliases.get("high_priority", []) if a]
        low = [cleanse_text(a, is_pm_type) for a in raw_aliases.get("low_priority", []) if a]
        cleansed_aliases = {"high_priority": high, "low_priority": low}
    else:
        # List of strings, clean all
        cleansed_aliases = [cleanse_text(a, is_pm_type) for a in raw_aliases if a]

    # Clean maiden name
    cleansed_maiden_name = cleanse_text(maiden_name, False) if maiden_name else ""

    return {
        "is_valid": True,
        "status": status,
        "errors": [],
        "warnings": warnings,
        "cleansed_name": cleansed_primary_name,
        "cleansed_maiden_name": cleansed_maiden_name,
        "cleansed_aliases": cleansed_aliases,
        "resolved_gender": resolved_gender
    }

import re
from datetime import datetime
from typing import List, Tuple, Dict, Any

# ------------------ STRING METRICS ------------------

def jaro_similarity(s1: str, s2: str) -> float:
    """Computes Jaro similarity between s1 and s2 (returns value between 0 and 100)."""
    if not s1 and not s2:
        return 100.0
    if not s1 or not s2:
        return 0.0

    len1, len2 = len(s1), len(s2)
    match_bound = max(1, max(len1, len2) // 2 - 1)

    s1_matches = [False] * len1
    s2_matches = [False] * len2

    matches = 0
    for i in range(len1):
        start = max(0, i - match_bound)
        end = min(len2, i + match_bound + 1)
        for j in range(start, end):
            if not s2_matches[j] and s1[i] == s2[j]:
                s1_matches[i] = True
                s2_matches[j] = True
                matches += 1
                break

    if matches == 0:
        return 0.0

    # Count transpositions
    t = 0
    k = 0
    for i in range(len1):
        if s1_matches[i]:
            while not s2_matches[k]:
                k += 1
            if s1[i] != s2[k]:
                t += 1
            k += 1
    t //= 2

    m = float(matches)
    jaro = (m / len1 + m / len2 + (m - t) / m) / 3.0
    return jaro * 100.0


def jaro_wink_similarity(s1: str, s2: str, p: float = 0.1, max_l: int = 4) -> float:
    """Computes Jaro-Winkler similarity (returns value between 0 and 100)."""
    jaro = jaro_similarity(s1, s2)
    
    # Calculate prefix length
    l = 0
    for c1, c2 in zip(s1[:max_l], s2[:max_l]):
        if c1 == c2:
            l += 1
        else:
            break
            
    return jaro + l * p * (100.0 - jaro)


def damerau_levenshtein_similarity(s1: str, s2: str) -> float:
    """Computes Damerau-Levenshtein similarity (returns value between 0 and 100)."""
    if not s1 and not s2:
        return 100.0
    if not s1 or not s2:
        return 0.0
        
    len1, len2 = len(s1), len(s2)
    d = {}
    
    # Initialize matrix
    for i in range(-1, len1 + 1):
        d[(i, -1)] = i + 1
    for j in range(-1, len2 + 1):
        d[(-1, j)] = j + 1
        
    for i in range(len1):
        for j in range(len2):
            cost = 0 if s1[i] == s2[j] else 1
            d[(i, j)] = min(
                d[(i - 1, j)] + 1,        # deletion
                d[(i, j - 1)] + 1,        # insertion
                d[(i - 1, j - 1)] + cost,  # substitution
            )
            # Transposition check
            if i > 0 and j > 0 and s1[i] == s2[j - 1] and s1[i - 1] == s2[j]:
                d[(i, j)] = min(d[(i, j)], d[(i - 2, j - 2)] + cost)
                
    distance = d[(len1 - 1, len2 - 1)]
    max_len = max(len1, len2)
    return (1.0 - (distance / max_len)) * 100.0


def token_sort_similarity(s1: str, s2: str) -> float:
    """Sorts string tokens alphabetically and calculates JW similarity."""
    tokens1 = sorted(s1.split())
    tokens2 = sorted(s2.split())
    
    sorted_s1 = " ".join(tokens1)
    sorted_s2 = " ".join(tokens2)
    
    return jaro_wink_similarity(sorted_s1, sorted_s2)


def compute_base_score(s1: str, s2: str, config: dict) -> float:
    """
    Computes S_base = (w_jw * JW) + (w_dl * DL) + (w_ts * TS)
    """
    from fiskr.quality import strip_accents
    s1_norm = strip_accents(s1.upper().strip())
    s2_norm = strip_accents(s2.upper().strip())
    
    weights = config.get("scoring", {}).get("weights", {})
    w_jw = weights.get("jaro_winkler", 0.4)
    w_dl = weights.get("damerau_levenshtein", 0.4)
    w_ts = weights.get("token_sort", 0.2)
    
    jw = jaro_wink_similarity(s1_norm, s2_norm)
    dl = damerau_levenshtein_similarity(s1_norm, s2_norm)
    ts = token_sort_similarity(s1_norm, s2_norm)
    
    return (w_jw * jw) + (w_dl * dl) + (w_ts * ts)


# ------------------ HARD MATCH SÉQUENCE ------------------

def check_hard_matches(client: dict, watchlist: dict) -> Tuple[bool, str]:
    """
    Checks the exact ID matching sequence (Section 5.5).
    Returns (True, reason) if any match is verified, else (False, "").
    """
    def clean_doc_num(num: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", str(num)).upper()

    # Priority 1: LEI (Legal Entity Identifier - Corporates)
    clei = (client.get("client_lei_number") or "").strip().upper()
    wlei = (watchlist.get("lei_number") or "").strip().upper()
    # Confirm structural validity of LEI (20 chars alphanumeric)
    if clei and wlei and len(clei) == 20 and clei.isalnum() and clei == wlei:
        return True, f"Hard Match Priorité 1 : Numéro LEI identique ({clei})"

    # Priority 2: Passport (Individuals)
    c_passports = client.get("client_passport_documents") or []
    w_passports = watchlist.get("passport_documents") or []
    if not isinstance(c_passports, list): c_passports = []
    if not isinstance(w_passports, list): w_passports = []
    for cp in c_passports:
        cp_num = clean_doc_num(cp.get("number", ""))
        cp_country = (cp.get("issuing_country") or "").strip().upper()
        if not cp_num:
            continue
        for wp in w_passports:
            wp_num = clean_doc_num(wp.get("number", ""))
            wp_country = (wp.get("issuing_country") or "").strip().upper()
            if cp_num == wp_num and cp_country == wp_country:
                return True, f"Hard Match Priorité 2 : Passeport identique ({cp_num} - {cp_country})"

    # Priority 3: National Registry IDs (SIREN, VAT, etc.)
    c_reg = client.get("client_national_registry_ids") or []
    w_reg = watchlist.get("national_registry_ids") or []
    if not isinstance(c_reg, list): c_reg = []
    if not isinstance(w_reg, list): w_reg = []
    for cr in c_reg:
        cr_num = clean_doc_num(cr.get("number", ""))
        cr_country = (cr.get("country") or "").strip().upper()
        if not cr_num:
            continue
        for wr in w_reg:
            wr_num = clean_doc_num(wr.get("number", ""))
            wr_country = (wr.get("country") or "").strip().upper()
            if cr_num == wr_num and cr_country == wr_country:
                return True, f"Hard Match Priorité 3 : Registre national identique ({cr_num} - {cr_country})"

    # Priority 4: National ID (CNI)
    c_nid = client.get("client_national_id_documents") or []
    w_nid = watchlist.get("national_id_documents") or []
    if not isinstance(c_nid, list): c_nid = []
    if not isinstance(w_nid, list): w_nid = []
    for cn in c_nid:
        cn_num = clean_doc_num(cn.get("number", ""))
        cn_country = (cn.get("issuing_country") or "").strip().upper()
        if not cn_num:
            continue
        for wn in w_nid:
            wn_num = clean_doc_num(wn.get("number", ""))
            wn_country = (wn.get("issuing_country") or "").strip().upper()
            if cn_num == wn_num and cn_country == wn_country:
                return True, f"Hard Match Priorité 4 : Carte Nationale d'Identité identique ({cn_num} - {cn_country})"

    # Priority 5: Transports (Vessels / Aircraft)
    c_imo = (client.get("transaction_vessel_imo") or "").strip()
    w_imo = (watchlist.get("imo_number") or "").strip()
    if c_imo and w_imo and clean_doc_num(c_imo) == clean_doc_num(w_imo):
        return True, f"Hard Match Priorité 5 : IMO Navire identique ({c_imo})"

    c_tail = (client.get("transaction_aircraft_registration") or "").strip().upper()
    w_tail = (watchlist.get("aircraft_tail_number") or "").strip().upper()
    if c_tail and w_tail and clean_doc_num(c_tail) == clean_doc_num(w_tail):
        return True, f"Hard Match Priorité 5 : Immatriculation Aéronef identique ({c_tail})"

    # Priority 6: Other IDs / Other Registrations
    c_oid = client.get("client_other_id_documents") or []
    w_oid = watchlist.get("other_id_documents") or []
    if not isinstance(c_oid, list): c_oid = []
    if not isinstance(w_oid, list): w_oid = []
    for co in c_oid:
        co_num = clean_doc_num(co.get("number", ""))
        co_type = (co.get("doc_type") or "").strip().upper()
        if not co_num:
            continue
        for wo in w_oid:
            wo_num = clean_doc_num(wo.get("number", ""))
            wo_type = (wo.get("doc_type") or "").strip().upper()
            if co_num == wo_num and co_type == wo_type:
                return True, f"Hard Match Priorité 6 : Autre ID identique ({co_num} - Type: {co_type})"

    c_oreg = client.get("client_other_registration_ids") or []
    w_oreg = watchlist.get("other_registration_ids") or []
    if not isinstance(c_oreg, list): c_oreg = []
    if not isinstance(w_oreg, list): w_oreg = []
    for co in c_oreg:
        co_num = clean_doc_num(co.get("number", ""))
        co_type = (co.get("id_type") or "").strip().upper()
        if not co_num:
            continue
        for wo in w_oreg:
            wo_num = clean_doc_num(wo.get("number", ""))
            wo_type = (wo.get("id_type") or "").strip().upper()
            if co_num == wo_num and co_type == wo_type:
                return True, f"Hard Match Priorité 6 : Autre Enregistrement identique ({co_num} - Type: {co_type})"

    return False, ""


# ------------------ CONTEXTUAL ADJUSTMENTS ------------------

def parse_dob(dob_str: str) -> datetime:
    """Parses date string YYYY-MM-DD to datetime."""
    try:
        return datetime.strptime(dob_str.strip(), "%Y-%m-%d")
    except ValueError:
        try:
            year = int(dob_str.strip()[:4])
            return datetime(year, 1, 1)
        except Exception:
            return None


def calculate_dob_adjustment(client_dobs: List[str], watchlist_dobs: List[str], config: dict) -> Tuple[float, str]:
    """
    DOB adjustment logic: Match exact (+15), Gap <= 2 years (+5), Gap > 2 years (-15).
    """
    rules = config.get("scoring", {}).get("contextual_rules", {})
    tolerance = rules.get("dob_tolerance_window", 2)
    exact_bonus = rules.get("dob_exact_bonus", 15)
    tolerance_bonus = rules.get("dob_tolerance_bonus", 5)
    out_malus = rules.get("dob_out_of_window_malus", -15)
    
    if not client_dobs or not watchlist_dobs:
        return 0.0, "Pas de comparaison DOB (donnée manquante)"
        
    c_dates = [parse_dob(d) for d in client_dobs if d and parse_dob(d)]
    w_dates = [parse_dob(d) for d in watchlist_dobs if d and parse_dob(d)]
    
    if not c_dates or not w_dates:
        return 0.0, "Pas de comparaison DOB (format invalide)"
        
    best_adj = -999.0
    best_desc = ""
    
    for c_date in c_dates:
        for w_date in w_dates:
            diff_days = abs((c_date - w_date).days)
            diff_years = diff_days / 365.25
            
            if diff_days == 0:
                adj = exact_bonus
                desc = f"Match exact DOB ({c_date.strftime('%Y-%m-%d')})"
            elif diff_years <= tolerance:
                adj = tolerance_bonus
                desc = f"DOB dans la fenêtre de tolérance (écart de {diff_years:.2f} ans)"
            else:
                adj = out_malus
                desc = f"DOB hors fenêtre de tolérance (écart de {diff_years:.2f} ans)"
                
            if adj > best_adj:
                best_adj = adj
                best_desc = desc
                
    return best_adj, best_desc


def calculate_gender_adjustment(client_gender: str, watchlist_genders: List[str], config: dict) -> Tuple[float, str]:
    rules = config.get("scoring", {}).get("contextual_rules", {})
    conflict_malus = rules.get("gender_conflict_malus", -20)
    
    cg = (client_gender or "").upper().strip()
    if cg not in ["M", "F"]:
        return 0.0, "Genre client non spécifié ou neutre"
        
    wgs = [g.upper().strip() for g in (watchlist_genders or []) if g and g.upper().strip() in ["M", "F"]]
    if not wgs:
        return 0.0, "Genre fiche non spécifié ou neutre"
        
    if cg in wgs:
        return 0.0, "Genres compatibles"
    else:
        return float(conflict_malus), f"Genre contradictoire (Client: {cg} vs Fiche: {wgs})"


def calculate_geography_adjustment(client_countries: List[str], watchlist_countries: List[str], config: dict) -> Tuple[float, str]:
    rules = config.get("scoring", {}).get("contextual_rules", {})
    match_bonus = rules.get("geography_match_bonus", 10)
    no_match_malus = rules.get("geography_no_match_malus", -10)
    
    cc = set(c.upper().strip() for c in (client_countries or []) if c and c.strip())
    wc = set(c.upper().strip() for c in (watchlist_countries or []) if c and c.strip())
    
    if not cc or not wc:
        return float(no_match_malus), "Aucun point de contact géographique (pays manquant)"
        
    intersection = cc.intersection(wc)
    if intersection:
        return float(match_bonus), f"Correspondance géographique trouvée ({', '.join(intersection)})"
    else:
        return float(no_match_malus), "Aucun point de contact géographique"


# ------------------ FULL MATCHING ENGINE ------------------

def match_entities(client: dict, watchlist_entry: dict, config: dict) -> Dict[str, Any]:
    """
    Matches client profile and watchlist entry.
    First checks the exact Hard Match sequence.
    If no hard match, runs the Fuzzy Scoring logic with context adjustments.
    """
    # 1. Sequential Hard Match Check
    is_hard_matched, hard_match_reason = check_hard_matches(client, watchlist_entry)
    
    if is_hard_matched:
        return {
            "status": "ALERT",
            "base_score": 100.0,
            "final_score": 100.0,
            "best_client_name": client.get("client_company_name") or client.get("client_last_name") or client.get("primary_name", ""),
            "best_watchlist_name": watchlist_entry.get("primary_name", ""),
            "adjustments": {
                "dob": {"score": 0.0, "description": "N/A (Hard Match)"},
                "gender": {"score": 0.0, "description": "N/A (Hard Match)"},
                "geography": {"score": 0.0, "description": "N/A (Hard Match)"}
            },
            "hard_match_triggered": True,
            "hard_match_details": hard_match_reason,
            "cut_off_applied": config.get("scoring", {}).get("cut_off_threshold", 75.0)
        }

    # 2. Gather names for Fuzzy Scoring
    c_names = []
    w_names = []
    
    is_client_pp = client.get("client_type") == "PP" or not client.get("client_company_name")
    
    # Client Names
    if is_client_pp:
        fname = client.get("client_first_name") or ""
        lname = client.get("client_last_name") or ""
        fullname = f"{fname} {lname}".strip()
        if fullname:
            c_names.append(fullname)
        maiden = client.get("client_maiden_name") or ""
        if maiden:
            c_names.append(maiden)
    else:
        comp = client.get("client_company_name") or ""
        if comp:
            c_names.append(comp)
            
    # Include fallback primary name and aliases
    if client.get("primary_name"):
        c_names.append(client.get("primary_name"))
    for a in (client.get("aliases") or []):
        if a:
            c_names.append(a)
            
    c_names = list(set([n.strip() for n in c_names if n and str(n).strip()]))
    
    # Watchlist Names
    if watchlist_entry.get("primary_name"):
        w_names.append(watchlist_entry.get("primary_name"))
        
    parsed = watchlist_entry.get("individual_name_parsed") or {}
    if isinstance(parsed, dict) and parsed.get("maiden_name"):
        w_names.append(parsed.get("maiden_name"))
        
    # High Priority Aliases ONLY
    wl_aliases = watchlist_entry.get("aliases", []) or []
    if isinstance(wl_aliases, dict):
        wl_high_aliases = wl_aliases.get("high_priority", []) or []
    else:
        # Fallback Dynamic qualification
        wl_high_aliases = []
        for a in wl_aliases:
            if not a:
                continue
            clean_a = re.sub(r"[\._\-]", " ", a).strip()
            words = clean_a.split()
            if len(words) <= 1 or len(clean_a) <= 4:
                continue
            wl_high_aliases.append(a)
            
    w_names.extend(wl_high_aliases)
    w_names = list(set([n.strip() for n in w_names if n and str(n).strip()]))
    
    if not c_names or not w_names:
        return {
            "status": "NO_MATCH",
            "base_score": 0.0,
            "final_score": 0.0,
            "best_client_name": "",
            "best_watchlist_name": "",
            "adjustments": {
                "dob": {"score": 0.0, "description": "Noms invalides ou absents"},
                "gender": {"score": 0.0, "description": "Noms invalides ou absents"},
                "geography": {"score": 0.0, "description": "Noms invalides ou absents"}
            },
            "hard_match_triggered": False,
            "cut_off_applied": config.get("scoring", {}).get("cut_off_threshold", 75.0)
        }
        
    # Best Match fuzzy scoring
    best_base_score = -1.0
    best_c_name = ""
    best_w_name = ""
    
    for cn in c_names:
        for wn in w_names:
            score = compute_base_score(cn, wn, config)
            if score > best_base_score:
                best_base_score = score
                best_c_name = cn
                best_w_name = wn
                
    # 3. Contextual Rules
    # DOBs
    client_dobs = [client.get("client_dob")] if client.get("client_dob") else []
    if client.get("dates_of_birth"):
        client_dobs.extend(client.get("dates_of_birth"))
    wl_dobs = watchlist_entry.get("dates_of_birth") or []
    dob_adj, dob_desc = calculate_dob_adjustment(client_dobs, wl_dobs, config)
    
    # Genders
    client_gender = client.get("client_gender") or (client.get("genders", ["U"])[0] if client.get("genders") else "U")
    wl_genders = [watchlist_entry.get("gender")] if watchlist_entry.get("gender") else []
    if watchlist_entry.get("genders"):
        wl_genders.extend(watchlist_entry.get("genders"))
    gender_adj, gender_desc = calculate_gender_adjustment(client_gender, wl_genders, config)
    
    # Geography (Countries)
    cc_dict = client.get("client_countries") or {}
    c_countries = list(set(
        (cc_dict.get("nationality") or []) +
        (cc_dict.get("residence") or []) +
        (cc_dict.get("birth_country") or []) +
        (cc_dict.get("registration_country") or [])
    ))
    if client.get("countries"):
        c_countries.extend(client.get("countries").get("citizenship", []) + client.get("countries").get("residence", []))
        
    wc_dict = watchlist_entry.get("countries") or {}
    w_countries = list(set(
        (wc_dict.get("citizenship") or []) +
        (wc_dict.get("residence") or []) +
        (wc_dict.get("birth_country") or []) +
        (wc_dict.get("jurisdiction_country") or [])
    ))
    
    geo_adj, geo_desc = calculate_geography_adjustment(c_countries, w_countries, config)
    
    # 4. Final aggregation
    total_adjustments = dob_adj + gender_adj + geo_adj
    final_score = best_base_score + total_adjustments
    final_score = max(0.0, min(100.0, final_score))
    
    cut_off = config.get("scoring", {}).get("cut_off_threshold", 75.0)
    status = "ALERT" if final_score >= cut_off else "NO_MATCH"
    
    return {
        "status": status,
        "base_score": round(best_base_score, 2),
        "final_score": round(final_score, 2),
        "best_client_name": best_c_name,
        "best_watchlist_name": best_w_name,
        "adjustments": {
            "dob": {
                "score": dob_adj,
                "description": dob_desc
            },
            "gender": {
                "score": gender_adj,
                "description": gender_desc
            },
            "geography": {
                "score": geo_adj,
                "description": geo_desc
            }
        },
        "hard_match_triggered": False,
        "cut_off_applied": cut_off
    }

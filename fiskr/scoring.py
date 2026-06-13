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


# ------------------ CONTEXTUAL ADJUSTMENTS ------------------

def parse_dob(dob_str: str) -> datetime:
    """Parses date string YYYY-MM-DD to datetime."""
    try:
        return datetime.strptime(dob_str.strip(), "%Y-%m-%d")
    except ValueError:
        # Try just parsing the year if standard format fails
        try:
            year = int(dob_str.strip()[:4])
            return datetime(year, 1, 1)
        except Exception:
            return None


def calculate_dob_adjustment(client_dobs: List[str], watchlist_dobs: List[str], config: dict) -> Tuple[float, str]:
    """
    Calculates DOB adjustment based on:
    - Match exact -> +15
    - Diff <= tolerance -> +5
    - Diff > tolerance -> -15
    Returns the adjustment score and a description of the result.
    """
    rules = config.get("scoring", {}).get("contextual_rules", {})
    tolerance = rules.get("dob_tolerance_window", 2)
    exact_bonus = rules.get("dob_exact_bonus", 15)
    tolerance_bonus = rules.get("dob_tolerance_bonus", 5)
    out_malus = rules.get("dob_out_of_window_malus", -15)
    
    if not client_dobs or not watchlist_dobs:
        return 0.0, "Pas de comparaison DOB (donnée manquante)"
        
    c_dates = [parse_dob(d) for d in client_dobs if parse_dob(d)]
    w_dates = [parse_dob(d) for d in watchlist_dobs if parse_dob(d)]
    
    if not c_dates or not w_dates:
        return 0.0, "Pas de comparaison DOB (format invalide)"
        
    # We apply the Best-Match rule: return the highest adjustment outcome
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
    """
    If gender is specified and contradictory (M vs F) -> -20.
    Otherwise -> 0.
    """
    rules = config.get("scoring", {}).get("contextual_rules", {})
    conflict_malus = rules.get("gender_conflict_malus", -20)
    
    cg = (client_gender or "").upper().strip()
    if cg not in ["M", "F"]:
        return 0.0, "Genre client non spécifié ou neutre"
        
    wgs = [g.upper().strip() for g in (watchlist_genders or []) if g and g.upper().strip() in ["M", "F"]]
    if not wgs:
        return 0.0, "Genre fiche non spécifié ou neutre"
        
    # If client gender is present, and watchlist genders are present, check if all of them are different
    # (Since watchlist can have multiple genders if it's a weird record, we check if there's any matching gender)
    if cg in wgs:
        return 0.0, "Genres compatibles"
    else:
        return float(conflict_malus), f"Genre contradictoire (Client: {cg} vs Fiche: {wgs})"


def calculate_geography_adjustment(client_countries: List[str], watchlist_countries: List[str], config: dict) -> Tuple[float, str]:
    """
    - Match found -> +10
    - No match found -> -10
    """
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
    Performs full comparison between a client profile and a watchlist entry.
    Considers aliases (Best-Match rule).
    Applies bonus/malus contextually.
    Clamps final score between 0 and 100.
    
    Returns a dictionary detailing the scoring decision tree.
    """
    # 1. Resolve names to check
    client_names = [client.get("primary_name", "")] + (client.get("aliases", []) or [])
    client_names = [n.strip() for n in client_names if n and n.strip()]
    
    wl_names = [watchlist_entry.get("primary_name", "")] + (watchlist_entry.get("aliases", []) or [])
    wl_names = [n.strip() for n in wl_names if n and n.strip()]
    
    if not client_names or not wl_names:
        return {
            "base_score": 0.0,
            "final_score": 0.0,
            "dob_adjustment": 0.0,
            "gender_adjustment": 0.0,
            "geo_adjustment": 0.0,
            "best_client_name": "",
            "best_watchlist_name": "",
            "details": "Noms invalides ou absents"
        }
        
    # Find Best-Match name combination
    best_base_score = -1.0
    best_c_name = ""
    best_w_name = ""
    
    for cn in client_names:
        for wn in wl_names:
            score = compute_base_score(cn, wn, config)
            if score > best_base_score:
                best_base_score = score
                best_c_name = cn
                best_w_name = wn
                
    # 2. Contextual rules
    # DOBs
    client_dobs = client.get("dates_of_birth", []) or []
    wl_dobs = watchlist_entry.get("dates_of_birth", []) or []
    dob_adj, dob_desc = calculate_dob_adjustment(client_dobs, wl_dobs, config)
    
    # Genders
    client_gender = client.get("genders", ["U"])[0] if client.get("genders", []) else "U"
    wl_genders = watchlist_entry.get("genders", []) or []
    gender_adj, gender_desc = calculate_gender_adjustment(client_gender, wl_genders, config)
    
    # Geography (Countries)
    client_countries_dict = client.get("countries", {}) or {}
    c_countries = list(set(
        (client_countries_dict.get("citizenship", []) or []) +
        (client_countries_dict.get("residence", []) or []) +
        (client_countries_dict.get("birth_country", []) or [])
    ))
    
    wl_countries_dict = watchlist_entry.get("countries", {}) or {}
    w_countries = list(set(
        (wl_countries_dict.get("citizenship", []) or []) +
        (wl_countries_dict.get("residence", []) or []) +
        (wl_countries_dict.get("birth_country", []) or [])
    ))
    
    geo_adj, geo_desc = calculate_geography_adjustment(c_countries, w_countries, config)
    
    # 3. Sum up
    total_adjustments = dob_adj + gender_adj + geo_adj
    final_score = best_base_score + total_adjustments
    final_score = max(0.0, min(100.0, final_score))
    
    # Cut-off
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
        "cut_off_applied": cut_off
    }

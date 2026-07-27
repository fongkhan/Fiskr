import re
from datetime import datetime
from typing import List, Tuple, Dict, Any, Optional

from fiskr import capabilities as caps

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


def resolve_cut_off(config: dict, watchlist_entry: dict = None) -> float:
    """
    Seuil de cut-off applicable : surcharge par type de liste
    (scoring.cut_off_overrides, cle = file_type du snapshot, ex. WATCHLIST_PEP)
    sinon seuil global. Le type est porte par la cle _list_type annotee au
    chargement du cache et lors du re-criblage.
    """
    scoring_cfg = config.get("scoring", {}) or {}
    cut_off = scoring_cfg.get("cut_off_threshold", 75.0)
    overrides = scoring_cfg.get("cut_off_overrides") or {}
    list_type = (watchlist_entry or {}).get("_list_type")
    if list_type and list_type in overrides:
        try:
            return float(overrides[list_type])
        except (TypeError, ValueError):
            return float(cut_off)
    return float(cut_off)


def apply_name_equivalences(s1: str, s2: str) -> Tuple[str, str]:
    """
    Applique les equivalences linguistiques aux deux noms compares.

    Chaque token connu est remplace par sa classe canonique : « Harry Dupont »
    et « Henri Dupont » deviennent tous deux « HENRY DUPONT », et les metriques
    de chaine — inchangees — retournent alors 100. Sans cette table, aucune
    metrique ne peut rapprocher Harry de Henri : ce n'est pas une question de
    distance d'edition, c'est une connaissance.

    Un token n'est remplace que si la classe correspondante est presente DES
    DEUX COTES. Canonicaliser chaque nom independamment serait une faute :
    « Henri Dupont » deviendrait « HENRY DUPONT » meme face a « Sofia
    Marchetti », et la distance a ce nom sans rapport changerait — activer la
    table deplacerait alors des scores qu'elle n'a aucune raison de toucher.
    Avec la regle du croisement, toute paire sans classe commune ressort
    caractere pour caractere identique : le seul effet possible de la table est
    de rapprocher deux termes declares equivalents.

    Les types `given_name` et `surname` sont appliques successivement : un nom
    complet contient les deux. Sans ressource active, les chaines sortent
    inchangees.
    """
    from fiskr import resources

    ctx = resources.current_context()
    index = ctx["index"]
    if index is None or not ctx["fields"]:
        return s1, s2
    left = s1.split()
    right = s2.split()
    for field in (resources.FIELD_GIVEN_NAME, resources.FIELD_SURNAME):
        if field not in ctx["fields"]:
            continue
        # Segments, pas tokens : un terme declare peut compter plusieurs mots
        # (« Al Assad », « Saint Petersbourg ») et serait sinon introuvable.
        left_spans = index.match_spans(left, field)
        right_spans = index.match_spans(right, field)
        right_classes = {c for _, c in right_spans if c}
        shared = {c for _, c in left_spans if c and c in right_classes}
        if not shared:
            continue
        left = [cls if cls in shared else span for span, cls in left_spans]
        right = [cls if cls in shared else span for span, cls in right_spans]
    return " ".join(left), " ".join(right)


def compute_base_score(s1: str, s2: str, config: dict) -> float:
    """
    Computes S_base = (w_jw * JW) + (w_dl * DL) + (w_ts * TS)

    Les equivalences linguistiques (fiskr/resources.py) sont appliquees en
    amont quand elles sont activees : elles ne modifient pas les metriques,
    elles ramenent les variantes connues d'un meme nom a une forme commune.
    """
    from fiskr.quality import strip_accents_for_matching
    # Translitteration PUIS passage en majuscules — jamais l'inverse. `upper()`
    # est sans effet sur les ecritures non latines : « 习 近平 ».upper() reste
    # « 习 近平 », et la translitteration rendait ensuite « Xi JinPing » en
    # casse mixte, compare a « XI JINPING » cote liste. Les metriques de chaine
    # sont sensibles a la casse : deux graphies pourtant identiques apres
    # translitteration ne marquaient que 64,40.
    channel = config.get("engine_channel", caps.CHANNEL_SCREENING)
    s1_norm = strip_accents_for_matching(s1.strip(), channel).upper()
    s2_norm = strip_accents_for_matching(s2.strip(), channel).upper()
    s1_norm, s2_norm = apply_name_equivalences(s1_norm, s2_norm)

    weights = config.get("scoring", {}).get("weights", {})
    w_jw = weights.get("jaro_winkler", 0.4)
    w_dl = weights.get("damerau_levenshtein", 0.4)
    w_ts = weights.get("token_sort", 0.2)
    
    jw = jaro_wink_similarity(s1_norm, s2_norm)
    dl = damerau_levenshtein_similarity(s1_norm, s2_norm)
    ts = token_sort_similarity(s1_norm, s2_norm)
    
    return (w_jw * jw) + (w_dl * dl) + (w_ts * ts)


# ------------------ HARD MATCH SÉQUENCE ------------------

def check_hard_matches(client: dict, watchlist: dict,
                       channel: str = caps.CHANNEL_SCREENING) -> Tuple[bool, str]:
    """
    Checks the exact ID matching sequence (Section 5.5).
    Returns (True, reason) if any match is verified, else (False, "").

    Chaque famille d'identifiant est pilotable (cf. fiskr/capabilities.py) :
    un hit force ALERT a 100/100 et CONTOURNE le seuil de coupure, donc
    couper l'une d'elles fait retomber au scoring flou une identite pourtant
    certaine — c'est un faux negatif reglementaire assume, pas un reglage de
    confort. Le catalogue le dit dans le champ `loss` de chaque capacite.
    """
    def clean_doc_num(num: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "", str(num)).upper()

    def on(capability: str) -> bool:
        return caps.is_active(capability, channel)

    # Priority 1: LEI (Legal Entity Identifier - Corporates)
    clei = (client.get("client_lei_number") or "").strip().upper() if on(caps.CAP_HARD_LEI) else ""
    wlei = (watchlist.get("lei_number") or "").strip().upper()
    # Confirm structural validity of LEI (20 chars alphanumeric)
    if clei and wlei and len(clei) == 20 and clei.isalnum() and clei == wlei:
        return True, f"Hard Match Priorité 1 : Numéro LEI identique ({clei})"

    # Priority 1bis: BIC/SWIFT (institutions financieres, agents du filtrage)
    cbic = (client.get("client_bic") or "").strip().upper() if on(caps.CAP_HARD_BIC) else ""
    wbic = (watchlist.get("bic_swift") or "").strip().upper()
    if cbic and wbic and len(cbic) in (8, 11) and cbic.isalnum():
        # Un BIC 8 est le prefixe d'un BIC 11 (agence) : comparaison sur 8
        if cbic == wbic or cbic[:8] == wbic[:8]:
            return True, f"Hard Match Priorité 1 : BIC/SWIFT identique ({cbic})"

    # Priority 1ter: Numero fiscal (Tax ID / INN)
    ctax = clean_doc_num(client.get("client_tax_id") or "") if on(caps.CAP_HARD_TAX_ID) else ""
    wtax = clean_doc_num(watchlist.get("tax_id") or "")
    if ctax and wtax and ctax == wtax:
        return True, f"Hard Match Priorité 1 : Numéro fiscal identique ({ctax})"

    # Priority 1quater: Adresse de monnaie numerique (exacte, sensible a la casse)
    c_wallets = (client.get("client_crypto_wallets") or []) if on(caps.CAP_HARD_CRYPTO) else []
    if isinstance(c_wallets, str):
        c_wallets = [c_wallets]
    w_wallets = watchlist.get("crypto_wallets") or []
    if not isinstance(w_wallets, list):
        w_wallets = []
    w_addresses = {(w.get("address") or "").strip() for w in w_wallets if isinstance(w, dict)}
    w_addresses.discard("")
    for cw in c_wallets:
        cw_addr = str(cw).strip()
        if cw_addr and cw_addr in w_addresses:
            return True, f"Hard Match Priorité 1 : Adresse crypto identique ({cw_addr[:16]}…)"

    # Priority 2: Passport (Individuals)
    c_passports = (client.get("client_passport_documents") or []) if on(caps.CAP_HARD_PASSPORT) else []
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
    c_reg = (client.get("client_national_registry_ids") or []) if on(caps.CAP_HARD_NATIONAL_REGISTRY) else []
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
    c_nid = (client.get("client_national_id_documents") or []) if on(caps.CAP_HARD_NATIONAL_ID) else []
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
    c_imo = (client.get("transaction_vessel_imo") or "").strip() if on(caps.CAP_HARD_VESSEL) else ""
    w_imo = (watchlist.get("imo_number") or "").strip()
    if c_imo and w_imo and clean_doc_num(c_imo) == clean_doc_num(w_imo):
        return True, f"Hard Match Priorité 5 : IMO Navire identique ({c_imo})"

    c_tail = (client.get("transaction_aircraft_registration") or "").strip().upper() if on(caps.CAP_HARD_AIRCRAFT) else ""
    w_tail = (watchlist.get("aircraft_tail_number") or "").strip().upper()
    if c_tail and w_tail and clean_doc_num(c_tail) == clean_doc_num(w_tail):
        return True, f"Hard Match Priorité 5 : Immatriculation Aéronef identique ({c_tail})"

    c_mmsi = (client.get("transaction_vessel_mmsi") or "").strip() if on(caps.CAP_HARD_VESSEL) else ""
    w_mmsi = (watchlist.get("vessel_mmsi") or "").strip()
    if c_mmsi and w_mmsi and clean_doc_num(c_mmsi) == clean_doc_num(w_mmsi):
        return True, f"Hard Match Priorité 5 : MMSI Navire identique ({c_mmsi})"

    c_call = (client.get("transaction_vessel_call_sign") or "").strip().upper() if on(caps.CAP_HARD_VESSEL) else ""
    w_call = (watchlist.get("vessel_call_sign") or "").strip().upper()
    if c_call and w_call and clean_doc_num(c_call) == clean_doc_num(w_call):
        return True, f"Hard Match Priorité 5 : Indicatif radio Navire identique ({c_call})"

    # Priority 6: Other IDs / Other Registrations
    c_oid = (client.get("client_other_id_documents") or []) if on(caps.CAP_HARD_OTHER_DOCUMENTS) else []
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

    c_oreg = (client.get("client_other_registration_ids") or []) if on(caps.CAP_HARD_OTHER_DOCUMENTS) else []
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


def collect_name_equivalences(left: str, right: str) -> List[Dict[str, str]]:
    """
    Equivalences linguistiques ayant rapproche deux noms.

    Inscrite au decision_tree : un analyste doit pouvoir lire POURQUOI deux
    noms dissemblables ont matche. Sans cette trace, « Harry Dupont » alerte
    sur « Henri DUPONT » a 100 sans aucune explication lisible — inacceptable
    en revue comme en contrôle.
    """
    from fiskr import resources

    out: List[Dict[str, str]] = []
    seen = set()
    for field in (resources.FIELD_GIVEN_NAME, resources.FIELD_SURNAME):
        for eq in resources.equivalences_for(left, right, field):
            key = (eq["field"], eq["source"], eq["target"])
            if key in seen:
                continue
            seen.add(key)
            out.append({**eq, "field_label": resources.FIELD_LABELS.get(eq["field"], eq["field"])})
    return out


def _country_key(value: str) -> str:
    """
    Cle de comparaison d'un pays.

    Quand le type `country` est active dans les ressources, « Allemagne »,
    « Germany », « Deutschland » et « DE » se ramenent a une meme classe et se
    rejoignent donc. Sinon on retourne la valeur brute en majuscules :
    comportement d'origine, a la lettre pres.
    """
    from fiskr import resources

    raw = str(value).upper().strip()
    ctx = resources.current_context()
    if ctx["index"] is None or resources.FIELD_COUNTRY not in ctx["fields"]:
        return raw
    return ctx["index"].canonical(raw, resources.FIELD_COUNTRY) or raw


def calculate_geography_adjustment(client_countries: List[str], watchlist_countries: List[str],
                                   config: dict,
                                   channel: str = caps.CHANNEL_SCREENING) -> Tuple[float, str]:
    rules = config.get("scoring", {}).get("contextual_rules", {})
    match_bonus = rules.get("geography_match_bonus", 10)
    no_match_malus = rules.get("geography_no_match_malus", -10)

    # cle de comparaison -> libelle d'origine, pour que la description reste
    # lisible par un analyste (on lui montre ce qui est ecrit dans les fiches,
    # pas l'identifiant interne de la classe d'equivalence)
    cc: Dict[str, str] = {}
    for c in (client_countries or []):
        if c and str(c).strip():
            cc.setdefault(_country_key(c), str(c).upper().strip())
    wc: Dict[str, str] = {}
    for c in (watchlist_countries or []):
        if c and str(c).strip():
            wc.setdefault(_country_key(c), str(c).upper().strip())

    if not cc or not wc:
        # Un pays MANQUANT d'un cote vaut malus par defaut — comportement
        # historique. Ce n'est pas evident : l'absence d'information n'est pas
        # une information contraire, et un referentiel client mal renseigne
        # voit ainsi ses scores baisser sans qu'aucune donnee ne le justifie.
        # La capacite permet de le rendre NEUTRE ; elle est inactive par
        # defaut, parce qu'elle ELARGIT le perimetre d'alertes et doit donc se
        # mesurer avant de s'appliquer.
        if caps.is_active(caps.CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL, channel):
            return 0.0, "Pays manquant d'un côté : ajustement neutre"
        return float(no_match_malus), "Aucun point de contact géographique (pays manquant)"

    intersection = set(cc).intersection(set(wc))
    if intersection:
        labels = []
        for key in sorted(intersection):
            left, right = cc[key], wc[key]
            labels.append(left if left == right else f"{left} ≡ {right}")
        return float(match_bonus), f"Correspondance géographique trouvée ({', '.join(labels)})"
    else:
        return float(no_match_malus), "Aucun point de contact géographique"


# ------------------ FULL MATCHING ENGINE ------------------

def _traced(result: Dict[str, Any], capabilities_applied: Optional[dict]) -> Dict[str, Any]:
    """
    Pose la trace du parametrage moteur sur une issue de rapprochement.

    La cle n'apparait que si le moteur s'ecarte des defauts du catalogue :
    un parametrage standard produit exactement le meme arbre de decision
    qu'avant l'introduction des capacites.
    """
    if capabilities_applied:
        result["capabilities_applied"] = capabilities_applied
    return result


def match_entities(client: dict, watchlist_entry: dict, config: dict) -> Dict[str, Any]:
    """
    Matches client profile and watchlist entry.
    First checks the exact Hard Match sequence.
    If no hard match, runs the Fuzzy Scoring logic with context adjustments.
    """
    # Canal du moteur : les capacites se reglent par canal (criblage vs
    # filtrage transactionnel). Il voyage dans la config, cf.
    # settings.scoring_config_with_thresholds — absent = criblage.
    channel = config.get("engine_channel", caps.CHANNEL_SCREENING)
    # Lu UNE fois par rapprochement, et pose sur chaque issue : une alerte de
    # hard match doit rester aussi explicable qu'une alerte floue.
    capabilities_applied = caps.describe_context(channel)

    # 1. Sequential Hard Match Check
    is_hard_matched, hard_match_reason = check_hard_matches(client, watchlist_entry, channel)

    if is_hard_matched:
        return _traced({
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
            "cut_off_applied": resolve_cut_off(config, watchlist_entry)
        }, capabilities_applied)

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
        # ORDRE INVERSE « NOM PRENOM ». Ce n'est pas une variante exotique :
        # les listes officielles ecrivent les noms d'Asie de l'Est dans l'ordre
        # d'origine, nom de famille EN TETE (« Kim Jong Un », « Xi Jinping »,
        # « Chen Quanguo », « Park Geun-hye »), alors qu'une base clients tient
        # prenom et nom dans des champs separes et les concatene « prenom nom ».
        # Les deux chaines comparees sont alors systematiquement inversees.
        # Jaro-Winkler et Damerau-Levenshtein, qui portent 80 % du poids, s'y
        # effondrent — seul le token sort (20 %) resiste, ce qui ne suffit
        # jamais a franchir un seuil. Mesure sur le panel de reference :
        # « 全国 陈 » contre « Chen Quanguo » plafonnait a 42,22.
        # Le cas depasse l'Asie (saisie inversee au guichet, formats d'echange
        # « NOM Prenom ») et vaut donc pour toute personne physique.
        if fname and lname and caps.is_active(caps.CAP_NAMES_REVERSED, channel):
            c_names.append(f"{lname} {fname}".strip())
        maiden = client.get("client_maiden_name") or ""
        if maiden and caps.is_active(caps.CAP_NAMES_MAIDEN, channel):
            c_names.append(maiden)
    else:
        comp = client.get("client_company_name") or ""
        if comp:
            c_names.append(comp)
            
    # Include fallback primary name and aliases
    if client.get("primary_name"):
        c_names.append(client.get("primary_name"))
    if caps.is_active(caps.CAP_NAMES_ALIASES_CLIENT, channel):
        for a in (client.get("aliases") or []):
            if a:
                c_names.append(a)
            
    c_names = list(set([n.strip() for n in c_names if n and str(n).strip()]))
    
    # Watchlist Names
    if watchlist_entry.get("primary_name"):
        w_names.append(watchlist_entry.get("primary_name"))
        
    parsed = watchlist_entry.get("individual_name_parsed") or {}
    if (isinstance(parsed, dict) and parsed.get("maiden_name")
            and caps.is_active(caps.CAP_NAMES_MAIDEN, channel)):
        w_names.append(parsed.get("maiden_name"))
        
    # High Priority Aliases ONLY
    wl_aliases = (watchlist_entry.get("aliases", []) or []
                  if caps.is_active(caps.CAP_NAMES_ALIASES_LISTED, channel) else [])
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
        return _traced({
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
            "cut_off_applied": resolve_cut_off(config, watchlist_entry)
        }, capabilities_applied)

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
    if caps.is_active(caps.CAP_ADJUST_DOB, channel):
        dob_adj, dob_desc = calculate_dob_adjustment(client_dobs, wl_dobs, config)
    else:
        dob_adj, dob_desc = 0.0, "Ajustement par date de naissance désactivé"
    
    # Genders
    client_gender = client.get("client_gender") or (client.get("genders", ["U"])[0] if client.get("genders") else "U")
    wl_genders = [watchlist_entry.get("gender")] if watchlist_entry.get("gender") else []
    if watchlist_entry.get("genders"):
        wl_genders.extend(watchlist_entry.get("genders"))
    if caps.is_active(caps.CAP_ADJUST_GENDER, channel):
        gender_adj, gender_desc = calculate_gender_adjustment(client_gender, wl_genders, config)
    else:
        gender_adj, gender_desc = 0.0, "Ajustement par genre désactivé"
    
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
    if client.get("client_country"):
        c_countries.append(client.get("client_country"))
        
    wc_dict = watchlist_entry.get("countries") or {}
    w_countries = list(set(
        (wc_dict.get("citizenship") or []) +
        (wc_dict.get("residence") or []) +
        (wc_dict.get("birth_country") or []) +
        (wc_dict.get("jurisdiction_country") or [])
    ))
    if watchlist_entry.get("country"):
        w_countries.append(watchlist_entry.get("country"))
    if watchlist_entry.get("jurisdiction_country"):
        w_countries.append(watchlist_entry.get("jurisdiction_country"))
    
    if caps.is_active(caps.CAP_ADJUST_GEOGRAPHY, channel):
        geo_adj, geo_desc = calculate_geography_adjustment(c_countries, w_countries, config, channel)
    else:
        geo_adj, geo_desc = 0.0, "Ajustement géographique désactivé"
    
    # 4. Final aggregation
    total_adjustments = dob_adj + gender_adj + geo_adj
    final_score = best_base_score + total_adjustments
    final_score = max(0.0, min(100.0, final_score))
    
    cut_off = resolve_cut_off(config, watchlist_entry)
    status = "ALERT" if final_score >= cut_off else "NO_MATCH"

    equivalences = collect_name_equivalences(best_c_name, best_w_name)

    result = {
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
    # Cle absente quand aucune equivalence n'a joue : le decision_tree des
    # criblages existants garde exactement sa forme actuelle
    if equivalences:
        result["resource_equivalences"] = equivalences
    return _traced(result, capabilities_applied)

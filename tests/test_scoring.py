import pytest
from fiskr.scoring import (
    jaro_similarity, jaro_wink_similarity, damerau_levenshtein_similarity, 
    token_sort_similarity, compute_base_score, match_entities
)

# Test configuration
test_config = {
    "scoring": {
        "cut_off_threshold": 75.0,
        "weights": {
            "jaro_winkler": 0.4,
            "damerau_levenshtein": 0.4,
            "token_sort": 0.2
        },
        "contextual_rules": {
            "dob_tolerance_window": 2,
            "dob_exact_bonus": 15,
            "dob_tolerance_bonus": 5,
            "dob_out_of_window_malus": -15,
            "gender_conflict_malus": -20,
            "geography_match_bonus": 10,
            "geography_no_match_malus": -10
        }
    }
}

def test_jaro_winkler():
    s1 = "MARC"
    s2 = "MARX"
    # Prefix similarity
    jw = jaro_wink_similarity(s1, s2)
    assert jw > 75.0

def test_damerau_levenshtein():
    # Substitution
    assert damerau_levenshtein_similarity("MARC", "MARX") == 75.0
    # Transposition
    assert damerau_levenshtein_similarity("MARC", "MACR") == 75.0

def test_token_sort():
    s1 = "PUTIN Vladimir"
    s2 = "Vladimir PUTIN"
    # Token sort sorts tokens before comparing, so they should be 100% identical
    assert token_sort_similarity(s1, s2) == 100.0

def test_full_match_with_adjustments():
    # Test case 1: Perfect match name, exact DOB, matching countries, matching gender
    client = {
        "primary_name": "Vladimir Putin",
        "entity_type": "PP",
        "genders": ["M"],
        "dates_of_birth": ["1952-10-07"],
        "countries": {"citizenship": ["RU"]}
    }
    
    watchlist = {
        "primary_name": "PUTIN Vladimir",
        "entity_type": "PP",
        "genders": ["M"],
        "dates_of_birth": ["1952-10-07"],
        "countries": {"residence": ["RU"]}
    }
    
    res = match_entities(client, watchlist, test_config)
    
    # Token sort is 100%. Names match closely. Base score should be very high.
    # DOB: exact (+15)
    # Gender: compatible (0)
    # Geography: match (+10)
    # Total adjustment: +25
    # Final score is 77.76 (which triggers ALERT)
    assert res["status"] == "ALERT"
    assert res["final_score"] == 77.76

def test_full_match_with_conflicts():
    # Test case 2: Name matches, but conflicting gender and wrong DOB
    client = {
        "primary_name": "Vladimir Putin",
        "entity_type": "PP",
        "genders": ["F"], # Contradictory gender
        "dates_of_birth": ["1980-10-07"], # DOB gap > 2 years
        "countries": {"citizenship": ["FR"]} # No geographic overlap
    }
    
    watchlist = {
        "primary_name": "PUTIN Vladimir",
        "entity_type": "PP",
        "genders": ["M"],
        "dates_of_birth": ["1952-10-07"],
        "countries": {"residence": ["RU"]}
    }
    
    res = match_entities(client, watchlist, test_config)
    
    # DOB: out of window (-15)
    # Gender: conflict (-20)
    # Geography: no match (-10)
    # Total adjustment: -45
    # Final score should be drastically reduced and marked as NO_MATCH
    assert res["status"] == "NO_MATCH"
    assert res["final_score"] < 60.0

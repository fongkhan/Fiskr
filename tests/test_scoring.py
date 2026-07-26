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


# ------------------ ORDRE NOM / PRENOM ------------------

def _individual(first, last, country="CN"):
    return {"client_id": "C", "client_type": "PP",
            "client_first_name": first, "client_last_name": last,
            "client_countries": {"nationality": [country], "residence": [country],
                                 "birth_country": [], "registration_country": []}}


def _listed(name, country="CN"):
    return {"entity_id": "E", "entity_type": "I", "primary_name": name,
            "countries": {"citizenship": [country], "residence": [country],
                          "birth_country": [country], "jurisdiction_country": []}}


def test_reversed_name_order_is_compared():
    """
    Les listes officielles ecrivent les noms d'Asie de l'Est dans l'ordre
    d'origine, nom de famille EN TETE (« Kim Jong Un », « Chen Quanguo »),
    alors qu'une base clients concatene « prenom nom ». Les deux chaines
    comparees sont alors systematiquement inversees : Jaro-Winkler et
    Damerau-Levenshtein, qui portent 80 % du poids, s'y effondrent, et le
    token sort seul (20 %) ne franchit jamais un seuil.
    """
    res = match_entities(_individual("Quanguo", "Chen"), _listed("Chen Quanguo"), test_config)
    assert res["status"] == "ALERT"
    assert res["final_score"] == 100.0
    assert res["best_client_name"] == "Chen Quanguo"

    res = match_entities(_individual("Jong Un", "Kim", "KP"),
                         _listed("Kim Jong Un", "KP"), test_config)
    assert res["status"] == "ALERT"


def test_reversed_order_does_not_match_an_unrelated_name():
    """L'ordre inverse est une variante de plus, pas un assouplissement."""
    res = match_entities(_individual("Sofia", "Marchetti", "IT"),
                         _listed("Chen Quanguo"), test_config)
    assert res["status"] == "NO_MATCH"
    assert res["final_score"] < 50.0

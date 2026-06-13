import pytest
from fiskr.blocking import generate_blocking_keys
from fiskr.phonetics import double_metaphone

def test_double_metaphone():
    # Test phonetic matching basics
    p1, s1 = double_metaphone("Müller")
    p2, s2 = double_metaphone("Meller")
    assert p1 == p2 == "MLR"
    
    # Test secondary key
    p3, s3 = double_metaphone("Smith")
    assert p3 == "SM0" or s3 == "XMT"

def test_blocking_key_generation():
    # Test generating standard blocking key [COUNTRY_ISO, ENTITY_TYPE, PHONETIC_FIRST]
    config = {
        "blocking": {
            "custom_key_layout": ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
        }
    }
    
    entity = {
        "primary_name": "Jean-Marc Muller",
        "entity_type": "PP",
        "countries": {
            "citizenship": ["FR"],
            "residence": ["DE"]
        }
    }
    
    keys = generate_blocking_keys(entity, config)
    # Double metaphone for "Jean-Marc" (first word "Jean") -> "JN"
    # Entity type -> "PP"
    # Countries -> "FR", "DE"
    # Expected combinations: FR_PP_JN, DE_PP_JN
    assert "FR_PP_JN" in keys
    assert "DE_PP_JN" in keys
    assert len(keys) == 4

def test_blocking_key_fallback():
    config = {
        "blocking": {
            "custom_key_layout": ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
        }
    }
    
    # Missing countries -> should fallback to 'XX'
    entity = {
        "primary_name": "Muller",
        "entity_type": "PP",
        "countries": {}
    }
    
    keys = generate_blocking_keys(entity, config)
    assert "XX_PP_MLR" in keys
    assert len(keys) == 1

import pytest
from fiskr.blocking import generate_blocking_keys
from fiskr.phonetics import double_metaphone


def phonetic_keys(keys):
    """
    Cles issues des seules composantes du layout, hors equivalences.

    Les ressources linguistiques ajoutent des cles `EQ<classe>` en plus des
    cles phonetiques (elles n'en retirent jamais aucune). Ces tests portent sur
    le produit cartesien du layout : ils comptent donc les cles phonetiques,
    et restent vrais que les ressources soient actives ou non.
    """
    return {k for k in keys if "_EQ" not in f"_{k}"}

def test_double_metaphone():
    # Test phonetic matching basics
    p1, s1 = double_metaphone("Müller")
    p2, s2 = double_metaphone("Meller")
    assert p1 == p2 == "MLR"
    
    # Test secondary key
    p3, s3 = double_metaphone("Smith")
    assert p3 == "SM0" or s3 == "XMT"

def test_blocking_key_generation_watchlist():
    # Test generating standard blocking key for Watchlist entity
    config = {
        "blocking": {
            "custom_key_layout": ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
        }
    }
    
    entity = {
        "primary_name": "Jean-Marc Muller",
        "entity_type": "I", # Watchlist individual
        "countries": {
            "citizenship": ["FR"],
            "residence": ["DE"]
        }
    }
    
    keys = generate_blocking_keys(entity, config)
    # Double metaphone for "Jean" (first word) -> "JN", "AN"
    # Entity type "I" maps to "PP"
    # Countries -> FR, DE
    # Expected: FR_PP_JN, DE_PP_JN, FR_PP_AN, DE_PP_AN
    assert "FR_PP_JN" in keys
    assert "DE_PP_JN" in keys
    assert len(phonetic_keys(keys)) == 4

def test_blocking_key_generation_client():
    # Test generating standard blocking key for Client entity
    config = {
        "blocking": {
            "custom_key_layout": ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
        }
    }
    
    entity = {
        "client_id": "CUST-001",
        "client_type": "PP", # Client Individual
        "client_first_name": "Jean-Marc",
        "client_last_name": "Muller",
        "client_countries": {
            "nationality": ["FR"],
            "residence": ["DE"]
        }
    }
    
    keys = generate_blocking_keys(entity, config)
    # Expected combinations with PP, FR/DE and JN/AN/MLR phonetics
    assert "FR_PP_JN" in keys
    assert "DE_PP_JN" in keys
    assert len(phonetic_keys(keys)) == 6

def test_blocking_key_fallback():
    config = {
        "blocking": {
            "custom_key_layout": ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
        }
    }
    
    # Missing countries -> should fallback to 'XX'
    entity = {
        "primary_name": "Muller",
        "entity_type": "I",
        "countries": {}
    }
    
    keys = generate_blocking_keys(entity, config)
    assert "XX_PP_MLR" in keys
    assert len(phonetic_keys(keys)) == 1


# ------------------ ECRITURES NON LATINES ------------------

def test_phonetic_key_is_computed_on_the_transliterated_form():
    """
    Le double metaphone ne connait que l'alphabet latin : sur « 陈 », « 김 » ou
    « Владимир » il retournait une cle VIDE. Une fiche ecrite dans son
    ecriture d'origine ne produisait donc AUCUNE cle phonetique et n'etait
    candidate de rien — quel que soit le contenu des tables d'equivalences.
    Le scoring, lui, translitterait deja des deux cotes : les deux etages du
    criblage se contredisaient.
    """
    config = {"blocking": {"custom_key_layout": ["ENTITY_TYPE", "PHONETIC_FIRST"]}}
    for native, latin in (("陈", "Chen"), ("김", "Kim"), ("Владимир", "Vladimir"),
                          ("習", "Xi")):
        native_keys = phonetic_keys(generate_blocking_keys(
            {"entity_type": "I", "primary_name": native}, config))
        assert native_keys != {"XX"}, f"{native} ne produit aucune cle phonetique"
        latin_keys = phonetic_keys(generate_blocking_keys(
            {"entity_type": "I", "primary_name": latin}, config))
        assert native_keys & latin_keys, f"{native} et {latin} ne sont pas candidats"


def test_non_latin_client_and_listed_record_become_candidates():
    """Le cas reel : client saisi en hanzi, fiche listee en caracteres latins."""
    config = {"blocking": {"custom_key_layout": ["ENTITY_TYPE", "PHONETIC_FIRST"]}}
    listed = generate_blocking_keys(
        {"entity_type": "I", "primary_name": "Chen Quanguo"}, config)
    client = generate_blocking_keys(
        {"client_type": "PP", "client_first_name": "全国", "client_last_name": "陈"}, config)
    assert listed & client

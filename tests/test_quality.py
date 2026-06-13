import pytest
from fiskr.quality import evaluate_and_clean

def test_rule_b01_empty_name():
    # Empty name should be rejected
    entity = {
        "primary_name": "   ",
        "entity_type": "PP",
        "dates_of_birth": ["1990-01-01"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert not report["is_valid"]
    assert report["status"] == "REJECT"
    assert any("Rule_B01" in err for err in report["errors"])

def test_rule_b02_short_name():
    # Short name (less than 2 alphanumeric chars) should be rejected
    entity = {
        "primary_name": "A",
        "entity_type": "PP",
        "dates_of_birth": ["1990-01-01"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert not report["is_valid"]
    assert any("Rule_B02" in err for err in report["errors"])

def test_rule_b03_unknown_entity_type():
    # Invalid entity type should be rejected
    entity = {
        "primary_name": "John Doe",
        "entity_type": "UNKNOWN",
        "dates_of_birth": ["1990-01-01"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert not report["is_valid"]
    assert any("Rule_B03" in err for err in report["errors"])

def test_rule_m01_missing_country():
    # Missing country is degraded, not rejected
    entity = {
        "primary_name": "John Doe",
        "entity_type": "PP",
        "dates_of_birth": ["1990-01-01"],
        "countries": {} # Empty
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert report["status"] == "DEGRADED"
    assert any("Rule_M01" in warn for warn in report["warnings"])

def test_rule_m02_missing_dob_for_pp():
    # PP without DOB is degraded, not rejected
    entity = {
        "primary_name": "John Doe",
        "entity_type": "PP",
        "dates_of_birth": [], # Empty DOB
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert report["status"] == "DEGRADED"
    assert any("Rule_M02" in warn for warn in report["warnings"])

def test_rule_m03_non_latin():
    # Cyrillic character triggers Rule_M03 warning
    entity = {
        "primary_name": "Владимир Путин",
        "entity_type": "PP",
        "dates_of_birth": ["1952-10-07"],
        "countries": {"citizenship": ["RU"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert any("Rule_M03" in warn for warn in report["warnings"])

def test_cleansing_pipeline():
    # Spaces, accents, symbols, case, and noise word removal
    entity = {
        "primary_name": "   Müller  @   Solutions SA   ",
        "entity_type": "PM",
        "countries": {"residence": ["DE"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    # Müller -> MULLER
    # @ removed
    # SA removed (PM only)
    # spaces collapsed
    assert report["cleansed_name"] == "MULLER SOLUTIONS"

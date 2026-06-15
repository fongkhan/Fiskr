import pytest
from fiskr.quality import evaluate_and_clean

def test_rule_b01_empty_name():
    # Empty name should be rejected
    entity = {
        "primary_name": "   ",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "John",
            "last_name": ""
        },
        "dates_of_birth": ["1990-01-01"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert not report["is_valid"]
    assert report["status"] == "REJECT"
    assert any("Rule_B01" in err for err in report["errors"])

def test_rule_b05_short_name():
    # Short name (less than 2 alphanumeric chars) should be rejected under Rule_B05
    entity = {
        "primary_name": "A",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "A",
            "last_name": ""
        },
        "dates_of_birth": ["1990-01-01"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert not report["is_valid"]
    assert any("Rule_B05" in err for err in report["errors"])

def test_rule_b02_invalid_entity_type():
    # Invalid entity type should be rejected under Rule_B02
    entity = {
        "primary_name": "John Doe",
        "entity_type": "UNKNOWN",
        "dates_of_birth": ["1990-01-01"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert not report["is_valid"]
    assert any("Rule_B02" in err for err in report["errors"])

def test_rule_b04_missing_individual_names():
    # If entity type is I but first_name and last_name are empty after parsing, reject
    entity = {
        "primary_name": "John Doe",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "",
            "last_name": "",
            "maiden_name": ""
        },
        "dates_of_birth": ["1990-01-01"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert not report["is_valid"]
    assert any("Rule_B04" in err for err in report["errors"])

def test_rule_m01_missing_country():
    # Missing country is degraded, not rejected
    entity = {
        "primary_name": "John Doe",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "John",
            "last_name": "Doe"
        },
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
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "John",
            "last_name": "Doe"
        },
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
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "Владимир",
            "last_name": "Путин"
        },
        "dates_of_birth": ["1952-10-07"],
        "countries": {"citizenship": ["RU"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert any("Rule_M03" in warn for warn in report["warnings"])

def test_rule_m04_contradiction_vital():
    # date_of_death is set but is_deceased is False
    entity = {
        "primary_name": "John Doe",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "John",
            "last_name": "Doe"
        },
        "dates_of_birth": ["1950-01-01"],
        "date_of_death": "2020-01-01",
        "is_deceased": False,
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert any("Rule_M04" in warn for warn in report["warnings"])
    assert entity["is_deceased"] is True # Auto-forced to True

def test_rule_m05_invalid_date():
    # Format of date is not YYYY-MM-DD
    entity = {
        "primary_name": "John Doe",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "John",
            "last_name": "Doe"
        },
        "dates_of_birth": ["1980/12/02"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert any("Rule_M05" in warn for warn in report["warnings"])

def test_rule_m06_passport_suspect():
    entity = {
        "primary_name": "John Doe",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "John",
            "last_name": "Doe"
        },
        "dates_of_birth": ["1980-01-01"],
        "countries": {"citizenship": ["FR"]},
        "passport_documents": [{"number": "A_B_C", "issuing_country": "FR"}]
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert any("Rule_M06" in warn for warn in report["warnings"])

def test_rule_m07_invalid_lei():
    entity = {
        "primary_name": "Company Inc",
        "entity_type": "E",
        "lei_number": "SHORT123", # Less than 20 chars
        "countries": {"residence": ["US"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert any("Rule_M07" in warn for warn in report["warnings"])

def test_rule_m08_low_confidence_pdf():
    entity = {
        "primary_name": "Alert Entity",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "Alert",
            "last_name": "Entity"
        },
        "extraction_confidence": 75.0, # Below 85.0
        "countries": {"citizenship": ["US"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert any("Rule_M08" in warn for warn in report["warnings"])

def test_rule_i03_multi_gender():
    # Incoherence of gender multi-valued resolves to "U"
    entity = {
        "primary_name": "John Doe",
        "entity_type": "I",
        "individual_name_parsed": {
            "first_name": "John",
            "last_name": "Doe"
        },
        "genders": ["M", "F"],
        "countries": {"citizenship": ["FR"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    assert report["resolved_gender"] == "U"
    assert any("Rule_I03" in warn for warn in report["warnings"])

def test_cleansing_pipeline():
    # Spaces, accents, symbols, case, and noise word removal
    entity = {
        "primary_name": "   Müller  @   Solutions SA   ",
        "entity_type": "E",
        "countries": {"residence": ["DE"]}
    }
    report = evaluate_and_clean(entity)
    assert report["is_valid"]
    # Müller -> MULLER
    # @ removed
    # SA removed (E is a PM type)
    # spaces collapsed
    assert report["cleansed_name"] == "MULLER SOLUTIONS"

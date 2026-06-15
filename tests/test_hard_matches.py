import pytest
from fiskr.scoring import check_hard_matches

def test_hard_match_priority_1_lei():
    # Priority 1: LEI (20 chars alphanumeric matching)
    client = {
        "client_lei_number": "12345678901234567890"
    }
    watchlist = {
        "lei_number": "12345678901234567890"
    }
    matched, reason = check_hard_matches(client, watchlist)
    assert matched
    assert "Hard Match Priorité 1" in reason

    # Invalid LEI structure should not trigger hard match
    client_invalid = {
        "client_lei_number": "12345" # Too short
    }
    watchlist_invalid = {
        "lei_number": "12345"
    }
    matched, _ = check_hard_matches(client_invalid, watchlist_invalid)
    assert not matched

def test_hard_match_priority_2_passport():
    # Priority 2: Passport matching number and issuing country
    client = {
        "client_passport_documents": [{"number": "12-34-56", "issuing_country": "FR"}]
    }
    watchlist = {
        "passport_documents": [{"number": "123456", "issuing_country": "FR"}]
    }
    matched, reason = check_hard_matches(client, watchlist)
    assert matched
    assert "Hard Match Priorité 2" in reason

    # Mismatch in country should not match
    client_mismatch = {
        "client_passport_documents": [{"number": "12-34-56", "issuing_country": "US"}]
    }
    matched, _ = check_hard_matches(client_mismatch, watchlist)
    assert not matched

def test_hard_match_priority_3_national_registry():
    # Priority 3: National Registry IDs matching number and country
    client = {
        "client_national_registry_ids": [{"number": "VAT999", "country": "DE"}]
    }
    watchlist = {
        "national_registry_ids": [{"number": "VAT999", "country": "DE"}]
    }
    matched, reason = check_hard_matches(client, watchlist)
    assert matched
    assert "Hard Match Priorité 3" in reason

def test_hard_match_priority_4_national_id():
    # Priority 4: National ID matching number and issuing country
    client = {
        "client_national_id_documents": [{"number": "ID-888", "issuing_country": "IT"}]
    }
    watchlist = {
        "national_id_documents": [{"number": "ID888", "issuing_country": "IT"}]
    }
    matched, reason = check_hard_matches(client, watchlist)
    assert matched
    assert "Hard Match Priorité 4" in reason

def test_hard_match_priority_5_transport():
    # Priority 5: Vessel IMO or Aircraft tail registration matching
    # Case A: Vessel IMO
    client_vessel = {
        "transaction_vessel_imo": "99412"
    }
    watchlist_vessel = {
        "imo_number": "99412"
    }
    matched, reason = check_hard_matches(client_vessel, watchlist_vessel)
    assert matched
    assert "Hard Match Priorité 5 : IMO Navire" in reason

    # Case B: Aircraft tail
    client_aircraft = {
        "transaction_aircraft_registration": "N-12345"
    }
    watchlist_aircraft = {
        "aircraft_tail_number": "N12345"
    }
    matched, reason = check_hard_matches(client_aircraft, watchlist_aircraft)
    assert matched
    assert "Hard Match Priorité 5 : Immatriculation Aéronef" in reason

def test_hard_match_priority_6_other_ids():
    # Priority 6: Other ID matching number and doc/id type
    client = {
        "client_other_id_documents": [{"number": "LICENSE-77", "doc_type": "DriverLicense"}]
    }
    watchlist = {
        "other_id_documents": [{"number": "LICENSE77", "doc_type": "DriverLicense"}]
    }
    matched, reason = check_hard_matches(client, watchlist)
    assert matched
    assert "Hard Match Priorité 6" in reason

    # If types differ, it shouldn't match to prevent collisions
    client_type_diff = {
        "client_other_id_documents": [{"number": "LICENSE-77", "doc_type": "Visa"}]
    }
    matched, _ = check_hard_matches(client_type_diff, watchlist)
    assert not matched

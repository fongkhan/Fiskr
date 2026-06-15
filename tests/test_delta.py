import pytest
from fiskr.delta import calculate_delta

def test_calculate_delta_added_removed():
    old_ents = [
        {"entity_id": "WL-001", "primary_name": "JOHN DOE", "entity_type": "I", "entity_checksum": "hash1"},
        {"entity_id": "WL-002", "primary_name": "JANE SMITH", "entity_type": "I", "entity_checksum": "hash2"}
    ]
    
    new_ents = [
        {"entity_id": "WL-002", "primary_name": "JANE SMITH", "entity_type": "I", "entity_checksum": "hash2"}, # Unchanged
        {"entity_id": "WL-003", "primary_name": "AL-MANSOUR SHIPPING", "entity_type": "V", "entity_checksum": "hash3"} # Added
    ]
    
    report = calculate_delta(old_ents, new_ents, "entity_id")
    
    # Summary assertions
    assert report["summary"]["added_count"] == 1
    assert report["summary"]["removed_count"] == 1
    assert report["summary"]["modified_count"] == 0
    
    # Details assertions
    assert report["details"]["added"][0]["id"] == "WL-003"
    assert report["details"]["added"][0]["primary_name"] == "AL-MANSOUR SHIPPING"
    assert report["details"]["added"][0]["type"] == "V"
    
    assert report["details"]["removed"][0]["id"] == "WL-001"
    assert report["details"]["removed"][0]["primary_name"] == "JOHN DOE"
    assert report["details"]["removed"][0]["type"] == "I"

def test_calculate_delta_modified():
    old_ents = [
        {
            "entity_id": "WL-001", 
            "primary_name": "HANS MULLER", 
            "entity_type": "I", 
            "dates_of_birth": ["1975-12-15"], 
            "countries": {"residence": ["DE"]},
            "entity_checksum": "hash_old"
        }
    ]
    
    new_ents = [
        {
            "entity_id": "WL-001", 
            "primary_name": "HANS MULLER", 
            "entity_type": "I", 
            "dates_of_birth": ["1975-12-15", "1975-12-16"], # Modified DOB
            "countries": {"residence": ["FR"]}, # Modified country
            "entity_checksum": "hash_new"
        }
    ]
    
    report = calculate_delta(old_ents, new_ents, "entity_id")
    
    assert report["summary"]["added_count"] == 0
    assert report["summary"]["removed_count"] == 0
    assert report["summary"]["modified_count"] == 1
    
    mod = report["details"]["modified"][0]
    assert mod["id"] == "WL-001"
    assert mod["primary_name"] == "HANS MULLER"
    
    # Verify nested diff detection
    assert "dates_of_birth" in mod["changes_detected"]
    assert "countries.residence" in mod["changes_detected"]
    
    # Check values before and after
    assert "1975-12-16" in mod["after"]["dates_of_birth"]
    assert mod["before"]["countries"]["residence"] == ["DE"]
    assert mod["after"]["countries"]["residence"] == ["FR"]

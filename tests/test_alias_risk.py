import pytest
from fiskr.ingest import qualify_alias_priority, categorize_aliases

def test_qualify_alias_priority_strong_weak_attr():
    # Test native provider Strong / Weak attributes
    assert qualify_alias_priority("Alias Name", "Strong") == "high"
    assert qualify_alias_priority("Alias Name", "Weak") == "low"

def test_qualify_alias_priority_heuristics():
    # Case 1: Single-word alias -> LOW
    assert qualify_alias_priority("PILOTE") == "low"
    assert qualify_alias_priority("ALEX") == "low"

    # Case 2: Short name <= 4 characters -> LOW
    assert qualify_alias_priority("M_R") == "low"
    assert qualify_alias_priority("A.B.") == "low"

    # Case 3: Noise words only -> LOW
    assert qualify_alias_priority("SA LLC") == "low"
    assert qualify_alias_priority("SARL GMBH") == "low"

    # Case 4: Long multi-word alias -> HIGH
    assert qualify_alias_priority("JOHN SMITH") == "high"
    assert qualify_alias_priority("SOCIETE GENERALE TRADING") == "high"

def test_categorize_aliases():
    aliases = [
        {"name": "PILOTE", "type": "Strong"}, # Attribute says Strong, but heuristics fallback?
        # Wait, if type attr is provided, qualify_alias_priority uses it first:
        # if alias_type_attr is "Strong" -> "high".
        # Let's verify that.
        {"name": "ALEX", "type": "Weak"},
        {"name": "JOHN SMITH", "type": ""},
        {"name": "M_R", "type": ""}
    ]
    
    result = categorize_aliases(aliases)
    
    # "PILOTE" has type "Strong" -> high
    # "ALEX" has type "Weak" -> low
    # "JOHN SMITH" has no type but is > 4 chars and 2 words -> high
    # "M_R" has no type but <= 4 chars -> low
    
    assert "PILOTE" in result["high_priority"]
    assert "JOHN SMITH" in result["high_priority"]
    assert "ALEX" in result["low_priority"]
    assert "M_R" in result["low_priority"]
    
    assert len(result["high_priority"]) == 2
    assert len(result["low_priority"]) == 2

import os
from pathlib import Path
import yaml

# Resolve the project root (where config.yaml resides)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config.yaml"
    if not config_path.exists():
        # Default fallback configuration
        return {
            "blocking": {
                "strategy": "standard_performance",
                "custom_key_layout": ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
            },
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
            },
            "database": {
                "url": "postgresql://postgres:postgres@localhost:5432/fiskr",
                "fallback_to_sqlite": True,
                "sqlite_path": "fiskr.sqlite3"
            }
        }
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)
    except Exception:
        # Fallback in case of parse error
        return {}

config = load_config()

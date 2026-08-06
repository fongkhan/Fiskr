import os
import re
from pathlib import Path
import yaml
from dotenv import load_dotenv

# Resolve the project root (where config.yaml and .env reside)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load environment variables from .env if present
env_path = PROJECT_ROOT / ".env"
if env_path.exists():
    load_dotenv(env_path)

def resolve_env_vars(data):
    """Recursively resolves ${VAR_NAME} placeholders in strings using environment variables."""
    if isinstance(data, dict):
        return {k: resolve_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [resolve_env_vars(i) for i in data]
    elif isinstance(data, str):
        def replace_match(match):
            var_name = match.group(1)
            # Default fallbacks for common DB variables if not specified in env
            defaults = {
                "DB_USER": "postgres",
                "DB_PASSWORD": "postgrespassword",
                "DB_HOST": "localhost",
                "DB_PORT": "5438",
                "DB_NAME": "fiskr"
            }
            return os.getenv(var_name, defaults.get(var_name, ""))
        return re.sub(r"\$\{([A-Za-z0-9_]+)\}", replace_match, data)
    return data

def load_config() -> dict:
    config_path = PROJECT_ROOT / "config.yaml"
    raw_config = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                raw_config = yaml.safe_load(f) or {}
        except Exception:
            raw_config = {}
    
    if not raw_config:
        raw_config = {
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
                "url": "postgresql://${DB_USER}:${DB_PASSWORD}@${DB_HOST}:${DB_PORT}/${DB_NAME}",
                "fallback_to_sqlite": True,
                "sqlite_path": "fiskr.sqlite3"
            }
        }
    
    cfg = resolve_env_vars(raw_config)
    
    # Allow explicit DATABASE_URL env override if provided
    db_env_url = os.getenv("DATABASE_URL")
    if db_env_url:
        if "database" not in cfg:
            cfg["database"] = {}
        cfg["database"]["url"] = db_env_url
        
    return cfg

config = load_config()

# Security & Authentication Settings
_DEFAULT_SECRET_KEY = "fiskr_super_secret_jwt_key_change_in_production_2026"
_DEFAULT_ADMIN_PASSWORD = "adminpassword"
SECRET_KEY = os.getenv("SECRET_KEY", _DEFAULT_SECRET_KEY)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", _DEFAULT_ADMIN_PASSWORD)

# La cle par defaut signe les jetons de session : connue de tous (elle est dans
# le code source), elle permettrait a un tiers de forger un cookie admin. Le mot
# de passe admin par defaut est tout aussi public. On le signale FORT au
# demarrage — silencieux en test/CI, ou ces valeurs sont normales et voulues.
INSECURE_DEFAULT_SECRET_KEY = (SECRET_KEY == _DEFAULT_SECRET_KEY)
INSECURE_DEFAULT_ADMIN_PASSWORD = (ADMIN_PASSWORD == _DEFAULT_ADMIN_PASSWORD)


def warn_on_insecure_defaults() -> list:
    """
    Liste les secrets restes a leur valeur par defaut (non definis en
    variable d'environnement). Chaque entree est journalisee en WARNING.
    Appele au demarrage de l'API ; rend la liste pour le diagnostic.
    """
    import logging
    problems = []
    if INSECURE_DEFAULT_SECRET_KEY:
        problems.append("SECRET_KEY")
    if INSECURE_DEFAULT_ADMIN_PASSWORD:
        problems.append("ADMIN_PASSWORD")
    if problems and not os.getenv("PYTEST_CURRENT_TEST"):
        log = logging.getLogger("fiskr.config")
        log.warning(
            "SÉCURITÉ : %s utilise(nt) encore la valeur par défaut du code source. "
            "Définissez-la/les en variable d'environnement AVANT toute mise en "
            "production — une clé de signature publique permet de forger un jeton "
            "de session administrateur.", " et ".join(problems),
        )
    return problems

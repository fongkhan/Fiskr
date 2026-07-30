"""
Tests d'equivalence de la projection memoire (screenpool.ENTITY_PROJECTION).

La projection reduit une fiche listee de ~8,4 Ko a ~3,8 Ko en ne chargeant
que ce que le moteur lit — c'est elle qui permet a un univers de 750 000
fiches (cas PEP) de tenir en memoire. Ces tests garantissent qu'elle ne
change JAMAIS un resultat de criblage :
1. match_entities et generate_blocking_keys rendent la meme chose sur la
   fiche complete et sur la fiche projetee (fiche ou TOUTES les colonnes
   sont remplies, hard matches compris) ;
2. un criblage a blanc complet rend le meme rapport, full vs projete ;
3. une regle anti-FP qui lit une colonne hors projection force sa
   re-inclusion (scan lexical du code des regles) ;
4. sentinelle : tout champ de fiche lu par scoring.py ou blocking.py doit
   figurer dans la projection ou dans la liste d'exclusions ASSUMEES — un
   futur champ de matching ne peut pas etre oublie silencieusement.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from fiskr import screenpool
from fiskr.blocking import generate_blocking_keys
from fiskr.config import config
from fiskr.database import WatchlistEntity
from fiskr.scoring import match_entities

# Fiche synthetique : TOUTES les colonnes remplies avec des valeurs plausibles.
FULL_ENTITY = {
    "id": 1, "snapshot_id": "PROJ-SNAP", "entity_id": "PROJ-1", "entity_type": "I",
    "primary_name": "Igor PETROV",
    "individual_name_parsed": {"first_name": "Igor", "last_name": "PETROV", "maiden_name": ""},
    "aliases": {"high_priority": ["Igor PETROFF"], "low_priority": ["I. Petrov"]},
    "dates_of_birth": ["1965-03-12"], "date_of_death": None, "is_deceased": False,
    "gender": "M",
    "countries": {"citizenship": ["RU"], "residence": ["FR"], "birth_country": ["RU"],
                  "jurisdiction_country": []},
    "place_of_birth": "Moscou", "address": "1 rue X", "city": "Moscou", "state": "",
    "country": "RU", "origin": "TEST", "designation": "Oligarque",
    "designation_reasons": "Motifs longs...", "additional_informations": "Notes...",
    "alternative_addresses": [{"city": "Nice"}],
    "imo_number": "9876543", "aircraft_tail_number": "RA-12345", "lei_number": "LEI123456789",
    "national_registry_ids": [{"number": "NR-1", "country": "RU"}],
    "other_registration_ids": [{"id_type": "X", "number": "OR-1"}],
    "passport_documents": [{"number": "P-123", "issuing_country": "RU"}],
    "national_id_documents": [{"number": "NID-1", "issuing_country": "RU"}],
    "other_id_documents": [{"doc_type": "D", "number": "OID-1"}],
    "official_reference": "Reglement (UE) 2026/1", "crypto_wallets": [{"currency": "XBT", "address": "bc1qxyz"}],
    "bic_swift": "SABRRUMM", "tax_id": "7736050003", "duns_number": "123456789",
    "vessel_call_sign": "UBLZ", "vessel_mmsi": "273456789", "vessel_flag": "RU",
    "vessel_type": "Cargo", "vessel_tonnage": "5000", "vessel_owner": "X Corp",
    "aircraft_model": "A320", "aircraft_operator": "OpX", "aircraft_construction_number": "C-1",
    "sanction_programs": ["UKR"], "listed_on": "2022-02-24", "delisted_on": None,
    "name_original_script": "Игорь Петров", "title": "M.", "pep_role": "Ministre",
    "secondary_sanctions_risk": "CAATSA", "designating_state": "FR",
    "organization_established_date": None, "organization_type": None,
    "phone_numbers": ["+7 495 000"], "email_addresses": ["x@y.ru"], "websites": ["y.ru"],
    "entity_checksum": "chk-proj-1",
    "modified_by": None, "modified_at": None,
    "excluded": False, "exclusion_justification": None, "exclusion_file_name": None,
    "exclusion_file_path": None, "excluded_by": None, "excluded_at": None,
    "_list_type": "WATCHLIST_EU",
}

# Clients qui declenchent chaque chemin : flou (nom+dob+geo), hard matches.
CLIENTS = [
    {"client_id": "C-FUZZY", "client_type": "PP", "client_first_name": "Igor",
     "client_last_name": "Petrov", "client_dob": "1965-03-12", "client_gender": "M",
     "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [],
                          "registration_country": []}},
    {"client_id": "C-LEI", "client_type": "PM", "client_company_name": "X Corp",
     "client_lei_number": "LEI123456789", "client_countries": {}},
    {"client_id": "C-BIC", "client_type": "PM", "client_company_name": "Banque X",
     "client_bic": "SABRRUMM", "client_countries": {}},
    {"client_id": "C-TAX", "client_type": "PP", "client_first_name": "A",
     "client_last_name": "B", "client_tax_id": "7736050003", "client_countries": {}},
    {"client_id": "C-CRYPTO", "client_type": "PP", "client_first_name": "A",
     "client_last_name": "B", "client_crypto_wallets": [{"currency": "XBT", "address": "bc1qxyz"}],
     "client_countries": {}},
    {"client_id": "C-PASSPORT", "client_type": "PP", "client_first_name": "A",
     "client_last_name": "B", "client_passport_documents": [{"number": "P-123"}],
     "client_countries": {}},
]


def _projected(entity, projection):
    keep = set(projection) | {"_list_type"}
    return {k: v for k, v in entity.items() if k in keep}


def test_match_and_blocking_identical_full_vs_projected():
    from fiskr.settings import blocking_config_for

    projected = _projected(FULL_ENTITY, screenpool.ENTITY_PROJECTION)
    cfg = blocking_config_for(["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])
    assert generate_blocking_keys(FULL_ENTITY, cfg) == generate_blocking_keys(projected, cfg)

    for client in CLIENTS:
        full = match_entities(client, FULL_ENTITY, config)
        slim = match_entities(client, projected, config)
        assert full["final_score"] == slim["final_score"], client["client_id"]
        assert full["status"] == slim["status"], client["client_id"]
        assert full.get("hard_match_triggered") == slim.get("hard_match_triggered"), client["client_id"]


def test_dry_run_screen_identical_full_vs_projected():
    projected = _projected(FULL_ENTITY, screenpool.ENTITY_PROJECTION)
    from fiskr.settings import blocking_config_for
    cfg = blocking_config_for(["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])

    def run(entity):
        index = {}
        for key in generate_blocking_keys(entity, cfg):
            index.setdefault(key, []).append(entity)
        agg = screenpool.new_partial()
        for client in CLIENTS:
            screenpool.apply_outcome(
                agg, screenpool.screen_one(client, index, cfg, set(), []))
        return screenpool.merge_partials([agg])

    assert run(FULL_ENTITY) == run(projected)


def test_rule_reading_projected_out_column_is_reincluded():
    rule = SimpleNamespace(id=1, name="Programme UKR",
                           code='def rule(ctx):\n    return "UKR" in (ctx["entity"].get("sanction_programs") or [])')
    projection = screenpool.projection_for([rule])
    assert "sanction_programs" in projection
    # ... et sans la regle, la colonne reste hors projection (c'est le gain)
    assert "sanction_programs" not in screenpool.ENTITY_PROJECTION


def test_rule_verdict_identical_with_derived_projection():
    from fiskr.settings import blocking_config_for
    rule = SimpleNamespace(id=7, name="Programme UKR",
                           code='def rule(ctx):\n    return "UKR" in (ctx["entity"].get("sanction_programs") or [])')
    projection = screenpool.projection_for([rule])
    projected = _projected(FULL_ENTITY, projection)
    cfg = blocking_config_for(["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])

    def run(entity):
        index = {}
        for key in generate_blocking_keys(entity, cfg):
            index.setdefault(key, []).append(entity)
        agg = screenpool.new_partial()
        for client in CLIENTS:
            screenpool.apply_outcome(
                agg, screenpool.screen_one(client, index, cfg, set(), [rule]))
        return screenpool.merge_partials([agg])

    full, slim = run(FULL_ENTITY), run(projected)
    assert full == slim
    assert full["rule_suppressed"] >= 1  # la regle a bien un effet a comparer


# ------------------ SENTINELLE ------------------

# Lectures de champs de fiche CONNUES et volontairement hors projection :
# - genders / jurisdiction_country : jamais des colonnes (toujours None sur
#   les dicts issus de la base, verifie dans le schema) ;
# - excluded : filtre de REQUETE, pas une lecture sur le dict.
_ASSUMED_ABSENT = {"genders", "jurisdiction_country", "excluded"}

# Variables qui designent une fiche LISTEE dans les sources du moteur (les
# lectures sur le client, prefixees client_/transaction_, ne sont pas
# concernees par la projection des fiches listees).
_ENTITY_VARS = ("watchlist_entry", "watchlist", "entity", "ent")


def test_sentinel_every_engine_read_is_projected_or_assumed():
    """
    Re-derive par regex les champs de fiche listee lus par scoring.py et
    blocking.py. Chaque champ doit etre dans ENTITY_PROJECTION ou dans les
    exclusions assumees ci-dessus : un futur champ de matching oublie ferait
    ECHOUER ce test au lieu de fausser silencieusement le cahier de tests.
    """
    root = Path(__file__).resolve().parents[1] / "fiskr"
    columns = {c.name for c in WatchlistEntity.__table__.columns}
    read = set()
    pattern = re.compile(
        r"(?:%s)\.get\(\s*['\"]([a-z_]+)['\"]" % "|".join(_ENTITY_VARS))
    for source in ("scoring.py", "blocking.py"):
        for name in pattern.findall((root / source).read_text(encoding="utf-8")):
            # Ne garder que les lectures qui correspondent a une VRAIE colonne
            # de fiche listee (les variables 'ent' designent aussi des clients
            # dans certaines fonctions : leurs champs client_* sont ignores)
            if name in columns or name in _ASSUMED_ABSENT:
                read.add(name)

    allowed = set(screenpool.ENTITY_PROJECTION) | _ASSUMED_ABSENT
    missing = sorted(read - allowed)
    assert not missing, (
        f"Champ(s) de fiche lus par le moteur mais absents de la projection : {missing}. "
        f"Ajoutez-les a screenpool.ENTITY_PROJECTION (ou aux exclusions assumees "
        f"si la lecture ne porte jamais sur une colonne)."
    )

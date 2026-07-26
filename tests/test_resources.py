"""
Tests des ressources linguistiques : tables d'equivalences (homonymes,
translitterations concurrentes, equivalents entre langues, exonymes) et leur
application au blocking, au scoring, a la geographie et a la tracabilite.

Le point que ces tests protegent avant tout : une table branchee UNIQUEMENT
sur le scoring serait sans effet, parce que « Henri » et « Harry » ne tombent
jamais dans le meme seau de blocking et ne sont donc jamais compares. Le
blocking DOIT produire une cle commune, sans jamais perdre les siennes.
"""
import textwrap

import pytest
from fastapi.testclient import TestClient

from fiskr import resources
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.blocking import generate_blocking_keys
from fiskr.config import config
from fiskr.database import get_db, AdminAuditLog, AppSetting
from fiskr.scoring import (
    calculate_geography_adjustment, compute_base_score, match_entities,
)
from fiskr.settings import SETTING_RESOURCE_FIELDS


# Index de reference des tests : deliberement petit et explicite, pour que
# chaque assertion se lise sans ouvrir les fichiers livres.
SAMPLE = {
    resources.FIELD_GIVEN_NAME: {
        "HENRY": ["Henri", "Henry", "Harry", "Heinrich"],
        "MUHAMMAD": ["Mohammad", "Mohammed", "Muhammad", "Mohamed"],
        "VLADIMIR": ["Vladimir", "Wladimir", "Владимир"],
    },
    resources.FIELD_SURNAME: {
        "DUPONT": ["Dupont", "Dupond", "Du Pont"],
        # Termes dont la forme NORMALISEE compte plusieurs mots : le cas des
        # exonymes composes et des noms arabes a particule.
        "ALASSAD": ["Al-Assad", "El Assad", "Assad"],
    },
    resources.FIELD_COUNTRY: {
        "DE": ["DE", "DEU", "Allemagne", "Germany", "Deutschland"],
    },
    resources.FIELD_CITY: {
        "LONDON": ["Londres", "London", "Londra"],
        "THE_HAGUE": ["La Haye", "The Hague", "Den Haag"],
    },
}


def _activate(fields):
    """Force l'index de test et les types actifs, sans passer par la base."""
    index = resources.index_from_mapping(SAMPLE)
    resources.set_index(index)
    resources._context_cache = {"index": index if fields else None, "fields": set(fields)}
    return index


@pytest.fixture(autouse=True)
def reset_resources():
    """Aucun test ne doit laisser des equivalences actives derriere lui."""
    yield
    resources.set_index(None)
    resources.invalidate_context()


# ------------------ MOTEUR ------------------

def test_normalize_term_is_script_and_accent_insensitive():
    assert resources.normalize_term("Müller") == "MULLER"
    assert resources.normalize_term("MULLER") == "MULLER"
    assert resources.normalize_term("Владимир") == "VLADIMIR"
    assert resources.normalize_term("  Al-Assad  ") == "AL ASSAD"
    assert resources.normalize_term("") == ""


def test_canonical_and_variants():
    index = resources.index_from_mapping(SAMPLE)
    assert index.canonical("Harry", resources.FIELD_GIVEN_NAME) == "HENRY"
    assert index.canonical("HENRI", resources.FIELD_GIVEN_NAME) == "HENRY"
    # Un prenom n'est pas un pays : chaque type a son propre index
    assert index.canonical("Harry", resources.FIELD_COUNTRY) is None
    assert index.canonical("Inconnu", resources.FIELD_GIVEN_NAME) is None
    assert "Heinrich" in index.variants("Harry", resources.FIELD_GIVEN_NAME)
    assert index.variants("Inconnu", resources.FIELD_GIVEN_NAME) == []


def test_canonicalize_tokens_converges():
    index = resources.index_from_mapping(SAMPLE)
    left = index.canonicalize_tokens("Harry Dupond", resources.FIELD_GIVEN_NAME)
    right = index.canonicalize_tokens("Henri Dupont", resources.FIELD_GIVEN_NAME)
    # Seuls les prenoms convergent : le type surname n'est pas interroge ici
    assert left.split()[0] == right.split()[0] == "HENRY"


def test_applied_equivalences_reports_only_differing_tokens():
    index = resources.index_from_mapping(SAMPLE)
    applied = index.applied_equivalences("Harry", "Henri", resources.FIELD_GIVEN_NAME)
    assert applied == [{"source": "HARRY", "target": "HENRI",
                        "class": "HENRY", "field": resources.FIELD_GIVEN_NAME}]
    # Deux ecritures identiques n'ont besoin d'aucune equivalence
    assert index.applied_equivalences("Henri", "Henri", resources.FIELD_GIVEN_NAME) == []


def test_multi_word_terms_are_reachable():
    """
    Un terme dont la forme normalisee contient une espace doit rester
    trouvable. La recherche se faisait token par token alors que la table est
    indexee sur le terme entier : « La Haye » etait cherche comme « LA » puis
    « HAYE », donc jamais trouve. Un terme sur dix des fichiers livres est
    dans ce cas — tous les exonymes composes etaient inertes.
    """
    index = resources.index_from_mapping(SAMPLE)
    assert index.canonicalize_tokens("La Haye", resources.FIELD_CITY) == "THE_HAGUE"
    assert index.canonicalize_tokens("The Hague", resources.FIELD_CITY) == "THE_HAGUE"
    assert index.canonicalize_tokens("Den Haag", resources.FIELD_CITY) == "THE_HAGUE"
    # Le segment le plus long l'emporte : « EL ASSAD » n'est pas coupe en deux
    assert index.applied_equivalences(
        "Bachar El Assad", "Bashar Al-Assad", resources.FIELD_SURNAME) == [
        {"source": "EL ASSAD", "target": "AL ASSAD",
         "class": "ALASSAD", "field": resources.FIELD_SURNAME}]


def test_multi_word_lookup_leaves_unrelated_text_untouched():
    """Le decoupage glouton ne doit rien canonicaliser qui ne soit declare."""
    index = resources.index_from_mapping(SAMPLE)
    assert index.canonicalize_tokens("La Rochelle", resources.FIELD_CITY) == "LA ROCHELLE"
    assert index.canonicalize_tokens("Haye Fouassiere", resources.FIELD_CITY) == \
        "HAYE FOUASSIERE"


def test_collision_is_detected_and_named(tmp_path):
    """
    Un terme rattache a deux classes rendrait le criblage non deterministe :
    il doit etre signale avec le fichier fautif, jamais absorbe en silence.
    """
    (tmp_path / "noms.yaml").write_text(textwrap.dedent("""
        type: surname
        groups:
          - id: WANG
            terms: [Wang, Wong]
          - id: HUANG
            terms: [Huang, Wong]
    """), encoding="utf-8")
    index = resources.load_index(tmp_path)
    assert len(index.collisions) == 1
    collision = index.collisions[0]
    assert collision["term"] == "WONG"
    assert collision["file"] == "noms.yaml"
    assert "WANG" in collision["classes"] and "HUANG" in collision["classes"]
    # Le premier declarant l'emporte : le resultat reste deterministe
    assert index.canonical("Wong", resources.FIELD_SURNAME) == "WANG"


def test_invalid_resource_file_is_refused(tmp_path):
    (tmp_path / "bad.yaml").write_text("type: inconnu\ngroups: []\n", encoding="utf-8")
    with pytest.raises(resources.ResourceError) as excinfo:
        resources.load_index(tmp_path)
    assert "bad.yaml" in str(excinfo.value)


def test_group_with_single_term_is_refused(tmp_path):
    (tmp_path / "solo.yaml").write_text(textwrap.dedent("""
        type: given_name
        groups:
          - id: SEUL
            terms: [Unique]
    """), encoding="utf-8")
    with pytest.raises(resources.ResourceError) as excinfo:
        resources.load_index(tmp_path)
    assert "SEUL" in str(excinfo.value)


def test_missing_directory_yields_empty_index(tmp_path):
    index = resources.load_index(tmp_path / "absent")
    assert index.stats()["total_terms"] == 0
    assert index.canonical("Harry", resources.FIELD_GIVEN_NAME) is None


def test_shipped_resources_load_without_collision():
    """Les fichiers livres doivent etre sains : un defaut ici part en production."""
    index = resources.load_index(resources.default_directory())
    stats = index.stats()
    assert stats["total_terms"] > 500
    assert index.collisions == [], f"collisions dans les ressources livrées : {index.collisions}"
    assert index.canonical("Harry", resources.FIELD_GIVEN_NAME) == \
        index.canonical("Henri", resources.FIELD_GIVEN_NAME)
    assert index.canonical("Mohammed", resources.FIELD_GIVEN_NAME) == \
        index.canonical("Muhammad", resources.FIELD_GIVEN_NAME)
    assert index.canonical("Londres", resources.FIELD_CITY) == \
        index.canonical("London", resources.FIELD_CITY)


def test_shipped_resources_bridge_east_asian_romanisations():
    """
    Coree : la translitteration automatique produit la romanisation revisee
    officielle (박 -> Bag, 이 -> I, 최 -> Choe) alors que les listes emploient
    la graphie consacree (Park, Lee, Choi). Aucune metrique de chaine ne
    franchit « Bag » ≡ « Park ».

    Japon : les kanji sont lus EN CHINOIS par la translittteration (田中 donne
    « Tianzhong », pas « Tanaka ») — la lecture japonaise doit etre declaree.

    Chine : rien a declarer, le pinyin de la translittteration tombe
    exactement sur le terme romanise deja present (陈 -> CHEN).
    """
    index = resources.load_index(resources.default_directory())
    S = resources.FIELD_SURNAME
    for native, listed in (("박", "Park"), ("이", "Lee"), ("최", "Choi"),
                           ("김", "Kim"), ("정", "Jeong"), ("윤", "Yoon"),
                           ("문", "Moon"), ("田中", "Tanaka"), ("安倍", "Abe"),
                           ("佐々木", "Sasaki"), ("陈", "Chen"), ("张", "Zhang")):
        assert index.canonical(native, S) is not None, f"{native} non déclaré"
        assert index.canonical(native, S) == index.canonical(listed, S), \
            f"{native} et {listed} ne partagent pas de classe"


# ------------------ BLOCKING ------------------

def _keys(entity):
    layout = {"blocking": {"custom_key_layout": ["ENTITY_TYPE", "PHONETIC_FIRST"]}}
    return generate_blocking_keys(entity, layout)


HENRI = {"entity_type": "I", "primary_name": "Henri DUPONT"}
HARRY = {"client_type": "PP", "client_first_name": "Harry", "client_last_name": "Dupont"}


def test_blocking_without_resources_never_pairs_henri_and_harry():
    """Le probleme que la table doit resoudre, constate avant tout branchement."""
    _activate(set())
    assert not (_keys(HENRI) & _keys(HARRY))


def test_blocking_with_resources_creates_a_shared_key():
    _activate({resources.FIELD_GIVEN_NAME})
    shared = _keys(HENRI) & _keys(HARRY)
    assert shared, "Henri et Harry doivent devenir candidats"
    assert all(k.endswith("_EQHENRY") for k in shared)


def test_blocking_bridges_on_the_surname_of_a_listed_record():
    """
    Une fiche listee porte son nom complet dans UNE chaine (« Muammar
    Gaddafi ») la ou un client a des champs separes. Tant que le blocking ne
    regardait que le PREMIER mot, une equivalence de NOM DE FAMILLE ne pouvait
    jamais creer de pont : le client emettait la cle du nom, la fiche listee
    n'emettait que celles du prenom. La table des noms de famille etait donc
    inerte sur le cas ordinaire.
    """
    listed = {"entity_type": "I", "primary_name": "Bashar Al-Assad"}
    client = {"client_type": "PP", "client_first_name": "Bachar",
              "client_last_name": "El Assad"}
    _activate(set())
    assert not (_keys(listed) & _keys(client))
    _activate({resources.FIELD_SURNAME})
    shared = _keys(listed) & _keys(client)
    assert shared, "le nom de famille doit rendre les deux fiches candidates"
    assert all(k.endswith("_EQALASSAD") for k in shared)


def test_blocking_keys_are_additive():
    """
    Aucune paire aujourd'hui candidate ne doit cesser de l'etre : les cles
    phonetiques d'origine restent toutes produites.
    """
    _activate(set())
    before_henri, before_harry = _keys(HENRI), _keys(HARRY)
    _activate({resources.FIELD_GIVEN_NAME, resources.FIELD_SURNAME})
    assert before_henri <= _keys(HENRI)
    assert before_harry <= _keys(HARRY)


def test_blocking_ignores_disabled_field_types():
    _activate({resources.FIELD_COUNTRY})  # actif, mais pas sur les prenoms
    assert not (_keys(HENRI) & _keys(HARRY))


# La composante pays du layout compare des chaines brutes : sans classe
# d'equivalence, un client dont la nationalite est saisie « Allemagne » et une
# fiche portant « DE » ne partagent aucune cle. Ils ne sont donc jamais
# candidats — et la canonicalisation des pays au scoring reste sans effet.
GEO_LAYOUT = {"blocking": {"custom_key_layout": ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]}}
GEO_CLIENT = {"client_type": "PP", "client_first_name": "Harry", "client_last_name": "Dupont",
              "client_countries": {"nationality": ["Allemagne"]}}
GEO_LISTED = {"entity_type": "I", "primary_name": "Henri DUPOND",
              "countries": {"citizenship": ["DE"]}}


def test_country_component_without_resources_never_pairs_synonyms():
    _activate(set())
    assert not (generate_blocking_keys(GEO_CLIENT, GEO_LAYOUT)
                & generate_blocking_keys(GEO_LISTED, GEO_LAYOUT))


def test_country_component_pairs_synonyms_when_active():
    _activate({resources.FIELD_GIVEN_NAME, resources.FIELD_COUNTRY})
    shared = (generate_blocking_keys(GEO_CLIENT, GEO_LAYOUT)
              & generate_blocking_keys(GEO_LISTED, GEO_LAYOUT))
    assert shared, "« Allemagne » et « DE » doivent se rejoindre au blocking"
    assert all(k.startswith("DE_") for k in shared)


def test_country_component_keys_are_additive():
    _activate(set())
    before = generate_blocking_keys(GEO_CLIENT, GEO_LAYOUT)
    _activate({resources.FIELD_COUNTRY})
    assert before <= generate_blocking_keys(GEO_CLIENT, GEO_LAYOUT)


# ------------------ SCORING ------------------

def test_score_without_resources_stays_low():
    _activate(set())
    assert compute_base_score("Henri Dupont", "Harry Dupont", config) < 90


def test_score_with_resources_reaches_identity():
    _activate({resources.FIELD_GIVEN_NAME})
    assert compute_base_score("Henri Dupont", "Harry Dupont", config) == 100.0


def test_unrelated_names_are_unchanged_by_resources():
    """
    Le garde-fou : activer la table ne doit RIEN faire aux paires sans classe
    commune, meme quand un seul des deux noms figure dans la table (« Henri »
    et « Dupont » y sont, « Sofia Marchetti » non).
    """
    _activate(set())
    before = compute_base_score("Henri Dupont", "Sofia Marchetti", config)
    _activate(set(resources.FIELD_TYPES))
    assert compute_base_score("Henri Dupont", "Sofia Marchetti", config) == before


def test_equivalence_applies_only_when_it_bridges_both_sides():
    """
    Regle du croisement : un token n'est canonicalise que si sa classe est
    presente des deux cotes. Sinon les chaines ressortent intactes.
    """
    from fiskr.scoring import apply_name_equivalences

    _activate({resources.FIELD_GIVEN_NAME})
    # Classe HENRY des deux cotes -> remplacement
    assert apply_name_equivalences("HARRY SMITH", "HENRI SMITH") == \
        ("HENRY SMITH", "HENRY SMITH")
    # Classe HENRY d'un seul cote -> aucune reecriture
    assert apply_name_equivalences("HARRY SMITH", "SOFIA MARCHETTI") == \
        ("HARRY SMITH", "SOFIA MARCHETTI")


def test_deactivating_restores_previous_behaviour_exactly():
    _activate(set())
    before = compute_base_score("Henri Dupont", "Harry Dupont", config)
    _activate({resources.FIELD_GIVEN_NAME})
    assert compute_base_score("Henri Dupont", "Harry Dupont", config) > before
    _activate(set())
    assert compute_base_score("Henri Dupont", "Harry Dupont", config) == before


# ------------------ GEOGRAPHIE ------------------

def test_geography_without_resources_misses_country_synonyms():
    _activate(set())
    score, _ = calculate_geography_adjustment(["Allemagne"], ["DE"], config)
    assert score < 0


def test_geography_with_resources_matches_country_synonyms():
    _activate({resources.FIELD_COUNTRY})
    score, desc = calculate_geography_adjustment(["Allemagne"], ["DE"], config)
    assert score > 0
    # La description montre les libelles des fiches, pas la classe interne
    assert "ALLEMAGNE" in desc and "DE" in desc


def test_geography_exact_match_description_unchanged():
    """Un pays ecrit pareil des deux cotes ne doit pas afficher d'equivalence."""
    _activate({resources.FIELD_COUNTRY})
    _, desc = calculate_geography_adjustment(["FR"], ["FR"], config)
    assert "≡" not in desc and "FR" in desc


# ------------------ TRACABILITE (decision tree) ------------------

CLIENT = {"client_type": "PP", "client_first_name": "Harry", "client_last_name": "Dupont",
          "client_countries": {"nationality": ["Allemagne"]}}
LISTED = {"entity_type": "I", "primary_name": "Henri DUPOND",
          "countries": {"citizenship": ["DE"]}}


def test_decision_tree_has_no_equivalence_key_when_inactive():
    _activate(set())
    result = match_entities(CLIENT, LISTED, config)
    assert "resource_equivalences" not in result
    assert result["status"] == "NO_MATCH"


def test_decision_tree_records_applied_equivalences():
    _activate({resources.FIELD_GIVEN_NAME, resources.FIELD_SURNAME,
               resources.FIELD_COUNTRY})
    result = match_entities(CLIENT, LISTED, config)
    assert result["status"] == "ALERT"
    applied = result["resource_equivalences"]
    classes = {e["class"] for e in applied}
    assert {"HENRY", "DUPONT"} <= classes
    # Un analyste doit lire le libelle du champ, pas seulement sa cle technique
    assert all(e["field_label"] for e in applied)


# ------------------ API ------------------

def _override_user(username, role):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_setting():
    """
    Ramene le reglage d'activation a son defaut livre.

    Appele AVANT et APRES : ces tests verifient le comportement par defaut du
    produit, pas celui de l'installation qui les execute. Sur une installation
    ou un responsable a active des types, le seul fait de lire l'etat ambiant
    les ferait echouer — ce qui n'apprendrait rien sur le code.
    """
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key == SETTING_RESOURCE_FIELDS).delete(
            synchronize_session=False)
        # Le journal d'administration est append-only en production ; les
        # traces laissees par les tests n'ont rien a y faire
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "res_admin").delete(
            synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def admin_client():
    _cleanup_setting()
    resources.invalidate_context()
    _override_user("res_admin", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_setting()
    resources.set_index(None)
    resources.invalidate_context()


@pytest.fixture
def user_client():
    _override_user("res_user", "user")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_get_resources_reports_state(admin_client):
    data = admin_client.get("/api/resources").json()
    assert data["total_terms"] > 0
    assert data["content_hash"]
    assert set(data["field_types"]) == set(resources.FIELD_TYPES)
    # Prenoms et noms actifs a la livraison (impact mesure, cf.
    # Documentation/MESURE_RESSOURCES.md) ; les trois autres types non.
    assert data["active"] is True
    assert data["enabled_fields"]["given_name"] is True
    assert data["enabled_fields"]["surname"] is True
    assert data["enabled_fields"]["city"] is False
    assert data["enabled_fields"]["country"] is False
    assert data["enabled_fields"]["state"] is False


def test_lookup_returns_class_and_variants(admin_client):
    data = admin_client.get("/api/resources/lookup", params={"term": "Harry"}).json()
    assert data["found"] is True
    assert data["normalized"] == "HARRY"
    given = [r for r in data["results"] if r["field"] == resources.FIELD_GIVEN_NAME]
    assert given and given[0]["enabled"] is True
    assert any("Henri" in v for v in given[0]["variants"])


def test_lookup_unknown_term(admin_client):
    data = admin_client.get("/api/resources/lookup", params={"term": "Zzzqqq"}).json()
    assert data["found"] is False and data["results"] == []


def test_lookup_rejects_unknown_field(admin_client):
    response = admin_client.get("/api/resources/lookup",
                                params={"term": "Harry", "field": "planete"})
    assert response.status_code == 400


def test_reload_requires_admin(user_client):
    assert user_client.post("/api/resources/reload").status_code == 403


def test_reload_is_traced(admin_client):
    before = admin_client.get("/api/resources").json()["content_hash"]
    response = admin_client.post("/api/resources/reload")
    assert response.status_code == 200
    assert response.json()["content_hash"] == before
    log = admin_client.get("/api/admin-log", params={"action": "RESOURCES_RELOADED"}).json()
    assert log["total"] >= 1
    assert log["items"][0]["username"] == "res_admin"


def test_enabling_a_field_takes_effect_immediately(admin_client):
    """
    Le criblage lit les types actifs depuis un cache : sans invalidation, le
    reglage resterait sans effet jusqu'au redemarrage.
    """
    # On part du reglage livre : prenoms et noms actifs
    assert resources.FIELD_GIVEN_NAME in resources.current_context()["fields"]
    assert compute_base_score("Henri Dupont", "Harry Dupont", config) == 100.0

    response = admin_client.put("/api/settings/ingestion",
                                json={"resource_fields": {"given_name": False}})
    assert response.status_code == 200
    assert response.json()["resource_fields"]["given_name"] is False
    assert resources.FIELD_GIVEN_NAME not in resources.current_context()["fields"]
    assert compute_base_score("Henri Dupont", "Harry Dupont", config) < 90

    admin_client.put("/api/settings/ingestion", json={"resource_fields": {"given_name": True}})
    assert resources.FIELD_GIVEN_NAME in resources.current_context()["fields"]
    assert compute_base_score("Henri Dupont", "Harry Dupont", config) == 100.0


def test_changing_fields_reloads_the_screening_cache(admin_client, monkeypatch):
    """
    L'index des fiches listees fige ses cles de blocking au chargement. Si le
    reglage change sans rechargement, seule la sonde du client gagne ses cles
    d'equivalence : les deux cotes ne se rencontrent jamais et le reglage est
    sans aucun effet (constate en E2E avant ce correctif).
    """
    from fiskr import api

    calls = []
    monkeypatch.setattr(api, "load_watchlist_cache", lambda db: calls.append(1))

    # given_name est actif a la livraison : c'est sa DESACTIVATION qui change
    # le reglage, donc qui doit declencher le rechargement.
    admin_client.put("/api/settings/ingestion", json={"resource_fields": {"given_name": False}})
    assert len(calls) == 1

    # Reglage identique : rien ne change, aucun rechargement inutile
    admin_client.put("/api/settings/ingestion", json={"resource_fields": {"given_name": False}})
    assert len(calls) == 1

    admin_client.put("/api/settings/ingestion", json={"resource_fields": {"given_name": True}})
    assert len(calls) == 2


def test_unknown_field_type_is_rejected(admin_client):
    response = admin_client.put("/api/settings/ingestion",
                                json={"resource_fields": {"planete": True}})
    assert response.status_code == 400

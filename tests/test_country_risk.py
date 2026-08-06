"""
Risque géographique GAFI (juridictions à haut risque) :
- classification d'un pays (appel à l'action / surveillance renforcée / non listé) ;
- priorité du niveau le plus sévère (noir > gris) sur un ensemble de pays ;
- collecte des pays d'un profil client quel que soit le schéma d'entrée ;
- surcharge à chaud du référentiel par config.yaml (mise à jour post-plénière
  sans redéploiement), avec restauration ;
- endpoints /api/country-risk (référentiel) et /assess ;
- enrichissement `country_risk` de la réponse de criblage SANS toucher au
  score ni au verdict (lentille complémentaire).
"""
import pytest
from fastapi.testclient import TestClient

from fiskr import country_risk as cr
from fiskr.api import app
from fiskr.auth import get_current_user


def _override_admin():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "cr_admin", "full_name": "CR Admin",
        "role": "admin", "roles": ["admin"],
    }


@pytest.fixture
def client():
    _override_admin()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ------------------ CLASSIFICATION ------------------

def test_classify_black_grey_and_unlisted():
    assert cr.classify("IR")["tier"] == cr.TIER_BLACKLIST
    assert cr.classify("kp")["tier"] == cr.TIER_BLACKLIST      # insensible à la casse
    assert cr.classify("VG")["tier"] == cr.TIER_GREYLIST
    assert cr.classify("FR") is None                            # non listé
    assert cr.classify("") is None and cr.classify(None) is None
    assert cr.classify("FRANCE") is None                        # pas un ISO2


def test_assess_takes_the_most_severe_tier():
    # Un pays gris (VE) et un pays noir (IR) : le noir l'emporte
    res = cr.assess(["FR", "ve", "IR", "us"])
    assert res["tier"] == cr.TIER_BLACKLIST
    codes = [m["country"] for m in res["matches"]]
    assert codes[0] == "IR" and set(codes) == {"IR", "VE"}      # noir listé en premier
    assert cr.assess(["FR", "US", "GB"]) is None                # aucun listé
    assert cr.assess([]) is None


def test_client_countries_collects_every_schema():
    nested = cr.client_countries({"client_countries": {
        "nationality": ["IR"], "residence": ["fr"], "birth_country": ["VE"]}})
    assert set(nested) == {"IR", "FR", "VE"}
    flat = cr.client_countries({"nationality": "IR", "client_country": "FR"})
    assert set(flat) == {"IR", "FR"}
    assert cr.assess_client({"client_country": "MM"})["tier"] == cr.TIER_BLACKLIST
    assert cr.assess_client({"client_country": "FR"}) is None


# ------------------ SURCHARGE A CHAUD (config.yaml) ------------------

def test_reference_can_be_overridden_by_config():
    from fiskr.config import config
    original = config.get("country_risk")
    try:
        # Une plénière fictive retire l'Iran du blacklist et ajoute la France au grey
        config["country_risk"] = {"as_of": "2099-01-01",
                                  "blacklist": ["KP", "MM"],
                                  "greylist": ["FR"]}
        assert cr.classify("IR") is None                        # plus sur la liste surchargée
        assert cr.classify("FR")["tier"] == cr.TIER_GREYLIST
        ref = cr.reference()
        assert ref["as_of"] == "2099-01-01" and ref["overridden"] is True
        assert len(ref["blacklist"]) == 2
    finally:
        if original is None:
            config.pop("country_risk", None)
        else:
            config["country_risk"] = original
    # Restauré : le référentiel intégré reprend
    assert cr.classify("IR")["tier"] == cr.TIER_BLACKLIST
    assert cr.reference()["as_of"] == cr.BUILTIN_AS_OF


# ------------------ ENDPOINTS ------------------

def test_reference_endpoint(client):
    ref = client.get("/api/country-risk").json()
    assert ref["as_of"] == cr.BUILTIN_AS_OF
    assert len(ref["blacklist"]) == 3 and len(ref["greylist"]) == 22
    black = {r["country"] for r in ref["blacklist"]}
    assert black == {"IR", "KP", "MM"}
    # Chaque ligne porte un nom lisible et un niveau
    assert all(r.get("fr") and r["tier"] == "BLACKLIST" for r in ref["blacklist"])


def test_assess_endpoint(client):
    res = client.get("/api/country-risk/assess", params={"countries": "FR,IR,VE"}).json()
    assert res["country_risk"]["tier"] == "BLACKLIST"
    none = client.get("/api/country-risk/assess", params={"countries": "FR,US"}).json()
    assert none["country_risk"] is None


# ------------------ ENRICHISSEMENT DU CRIBLAGE (sans impact moteur) ------------------

def test_screen_result_carries_country_risk_without_touching_score(client):
    # Un profil neutre rattaché à l'Iran : aucun nom listé, mais risque
    # géographique signalé — le score reste NO_MATCH (le moteur n'est pas touché).
    payload = {"client_type": "PP", "client_first_name": "Paulette",
               "client_last_name": "Tranquillova",
               "client_countries": {"nationality": ["IR"]}}
    res = client.post("/api/screen", json=payload).json()
    assert res["country_risk"] is not None
    assert res["country_risk"]["tier"] == "BLACKLIST"
    assert [m["country"] for m in res["country_risk"]["matches"]] == ["IR"]
    # Le criblage par nom n'a pas trouvé de correspondance : verdict inchangé
    assert res["best_match"] is None or res["best_match"]["status"] != "ALERT"

    # Un profil sans pays listé : pas d'enrichissement de risque géographique
    neutre = client.post("/api/screen", json={
        "client_type": "PP", "client_first_name": "Jean", "client_last_name": "Dupont",
        "nationality": "FR"}).json()
    assert neutre["country_risk"] is None

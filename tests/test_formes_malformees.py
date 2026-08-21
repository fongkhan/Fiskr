"""
Le moteur ne doit pas mourir sur une fiche mal formée.

Les champs multivalués — `countries.citizenship`, `aliases.high_priority`,
`dates_of_birth`, `genders` — sont des colonnes JSON. Leur forme n'est garantie
par aucun schéma, et deux portes d'entrée documentées laissent passer une
chaîne là où le moteur attend une liste :

  * `PATCH /api/entities/{id}` — `countries: Optional[Dict[str, Any]]`, donc
    `{"citizenship": "FR"}` est **valide** pour Pydantic ;
  * le webhook d'upsert client — `client_countries: Optional[Dict[str, Any]]`,
    alimenté système-à-système par un amont qu'on ne contrôle pas.

`[] + "FR"` lève. Côté client, la requête rendait 500. Côté **fiche listée**,
la conséquence était bien pire : une seule fiche malformée faisait échouer
**tous** les criblages dont le blocking la sélectionne — et un criblage qui
n'aboutit pas ne laisse **aucune ligne d'audit**. Un faux négatif invisible.

Perdre un champ de contexte vaut mieux que perdre le criblage.
"""
import pytest

from fiskr.scoring import _mapping, _valeurs, match_entities
from fiskr.settings import DEFAULT_SCORING_WEIGHTS

CLIENT = {"client_type": "PP", "client_first_name": "IVAN", "client_last_name": "IVANOV"}
FICHE = {"entity_id": "E1", "primary_name": "IVAN IVANOV", "entity_type": "I"}


def _cfg():
    return {"scoring": {"weights": dict(DEFAULT_SCORING_WEIGHTS),
                        "contextual_rules": {}},
            "cut_off_threshold": 75}


# ------------------ NORMALISATION ------------------

@pytest.mark.parametrize("brut,attendu", [
    (None, []),
    ("", []),
    ("   ", []),
    ("FR", ["FR"]),
    (["FR", "IT"], ["FR", "IT"]),
    (("FR",), ["FR"]),
    (["FR", None, "", "IT"], ["FR", "IT"]),
    ([["FR"], "IT"], ["FR", "IT"]),       # imbrication d'un connecteur
    (42, ["42"]),
    (3.5, ["3.5"]),
    ({}, []),                              # un dictionnaire ne dit rien ici
    ({"a": "FR"}, []),
    (True, []),                            # un booleen n'est pas une valeur
])
def test_normalisation_des_champs_multivalues(brut, attendu):
    assert _valeurs(brut) == attendu


@pytest.mark.parametrize("brut", [None, "FR", 42, ["FR"], True])
def test_un_conteneur_qui_n_en_est_pas_un_vaut_vide(brut):
    assert _mapping(brut) == {}


def test_un_conteneur_valide_est_rendu_tel_quel():
    conteneur = {"citizenship": ["FR"]}
    assert _mapping(conteneur) is conteneur


# ------------------ LE MOTEUR SURVIT ------------------

@pytest.mark.parametrize("fiche", [
    {"countries": {"citizenship": "FR"}},          # chaine au lieu de liste
    {"countries": {"citizenship": 42}},
    {"countries": {"residence": {"a": 1}}},
    {"countries": "FR"},                            # conteneur qui n'en est pas un
    {"countries": ["FR"]},
    {"dates_of_birth": "1970-01-01"},
    {"genders": "M"},
    {"aliases": "IVAN"},
    {"aliases": {"high_priority": "IVAN"}},
    {"individual_name_parsed": "IVAN"},
])
def test_une_fiche_listee_malformee_ne_fait_pas_echouer_le_criblage(fiche):
    resultat = match_entities(CLIENT, {**FICHE, **fiche}, _cfg())
    assert resultat["final_score"] >= 0.0
    assert resultat["status"] in ("ALERT", "NO_MATCH")


@pytest.mark.parametrize("profil", [
    {"client_countries": {"nationality": "FR"}},
    {"client_countries": "FR"},
    {"client_countries": ["FR"]},
    {"countries": {"citizenship": "FR"}},           # forme historique
    {"client_dob": "1970-01-01", "dates_of_birth": "1971-01-01"},
    {"client_aliases": "IVAN IVANOV"},
    {"genders": "M"},
])
def test_un_profil_client_malforme_ne_fait_pas_echouer_le_criblage(profil):
    resultat = match_entities({**CLIENT, **profil}, FICHE, _cfg())
    assert resultat["final_score"] >= 0.0


# ------------------ ET LA VALEUR EST RÉCUPÉRÉE, PAS JETÉE ------------------

def test_un_pays_ecrit_en_chaine_corrobore_quand_meme():
    """Tolérer ne veut pas dire ignorer : « FR » écrit sans crochets reste un
    pays, et il doit produire le même bonus géographique qu'une liste."""
    cfg = _cfg()
    cfg["scoring"]["contextual_rules"] = {"geography_match_bonus": 10,
                                          "geography_no_match_malus": -10}
    chaine = match_entities({**CLIENT, "client_countries": {"nationality": "FR"}},
                            {**FICHE, "countries": {"citizenship": "FR"}}, cfg)
    liste = match_entities({**CLIENT, "client_countries": {"nationality": ["FR"]}},
                           {**FICHE, "countries": {"citizenship": ["FR"]}}, cfg)
    assert chaine["adjustments"]["geography"] == liste["adjustments"]["geography"]
    assert chaine["adjustments"]["geography"]["score"] == 10


def test_un_alias_ecrit_en_chaine_est_crible_comme_une_liste():
    """
    Le cas le plus vicieux : `{"high_priority": "IVAN IVANOV"}` était étendu
    CARACTÈRE PAR CARACTÈRE — « I », « V », « A », « N »… — donc l'alias
    n'était jamais comparé. Une fiche listée sous son alias passait à travers
    sans que rien ne le signale.
    """
    cfg = _cfg()
    fiche = {"entity_id": "E1", "primary_name": "PERSONNE SANS RAPPORT",
             "entity_type": "I"}
    chaine = match_entities(CLIENT, {**fiche, "aliases": {"high_priority": "IVAN IVANOV"}}, cfg)
    liste = match_entities(CLIENT, {**fiche, "aliases": {"high_priority": ["IVAN IVANOV"]}}, cfg)
    assert chaine["best_watchlist_name"] == "IVAN IVANOV"
    assert chaine["final_score"] == liste["final_score"]
    assert chaine["final_score"] > 75


def test_une_date_de_naissance_en_chaine_departage_quand_meme():
    cfg = _cfg()
    cfg["scoring"]["contextual_rules"] = {"dob_exact_match_bonus": 15,
                                          "dob_mismatch_malus": -20}
    chaine = match_entities({**CLIENT, "client_dob": "1970-01-01"},
                            {**FICHE, "dates_of_birth": "1970-01-01"}, cfg)
    liste = match_entities({**CLIENT, "client_dob": "1970-01-01"},
                           {**FICHE, "dates_of_birth": ["1970-01-01"]}, cfg)
    assert chaine["adjustments"]["dob"] == liste["adjustments"]["dob"]
    assert chaine["adjustments"]["dob"]["score"] > 0


def test_les_formes_correctes_ne_bougent_pas():
    """La normalisation ne doit RIEN changer au cas nominal — sinon elle
    déplacerait des scores calibrés."""
    cfg = _cfg()
    cfg["scoring"]["contextual_rules"] = {"geography_match_bonus": 10,
                                          "geography_no_match_malus": -10}
    resultat = match_entities(
        {**CLIENT, "client_countries": {"nationality": ["RU"], "residence": ["FR"]},
         "client_dob": "1952-10-07"},
        {**FICHE, "countries": {"citizenship": ["RU"]},
         "dates_of_birth": ["1952-10-07"],
         "aliases": {"high_priority": ["IVAN IVANOVITCH"]}},
        cfg)
    assert resultat["status"] == "ALERT"
    assert resultat["adjustments"]["geography"]["score"] == 10
    assert resultat["best_watchlist_name"] in ("IVAN IVANOV", "IVAN IVANOVITCH")

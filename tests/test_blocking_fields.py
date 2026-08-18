"""
Tous les champs disponibles en composante de clé de criblage.

Le blocking ne proposait que trois composantes (pays, type d'entité,
phonétique du nom). Cette suite couvre l'ouverture à l'ensemble des champs —
année de naissance, genre, lieu de naissance, ville, identifiants fiscaux,
LEI, BIC, IBAN, IMO, registre national.

Deux propriétés font toute la sûreté de cette ouverture, et sont ici
verrouillées :

1. **Symétrie.** Une composante doit se calculer des DEUX côtés — profil client
   et fiche listée. Une composante calculable d'un seul côté produirait une clé
   qui ne se rencontre jamais : le candidat serait perdu, sans erreur.

2. **Joker.** Une fiche listée qui ne renseigne pas le champ porte un joker, et
   la sonde interroge AUSSI les variantes jokerisées. Sans cela, ajouter
   « Année de naissance » ferait disparaître toutes les fiches sans date —
   c'est-à-dire l'essentiel des listes officielles.
"""
import pytest

from fiskr.blocking import (BLOCKING_FIELDS, FIELD_WILDCARD,
                            field_component_value, generate_blocking_keys,
                            lookup_blocking_keys)
from fiskr.settings import BLOCKING_COMPONENTS, MAX_BLOCKING_FIELDS, _valid_layout


def _cfg(layout):
    return {"blocking": {"custom_key_layout": layout, "channel": "SCREENING"}}


CLIENT = {"client_type": "PP", "client_id": "C1", "client_first_name": "Igor",
          "client_last_name": "Sechin", "client_dob": "1960-09-07",
          "client_gender": "M", "client_city": "Moscou",
          "client_tax_id": "FR 123 456 789",
          "client_countries": {"nationality": ["RU"], "residence": [],
                               "birth_country": [], "registration_country": []}}

LISTE = {"entity_type": "I", "entity_id": "E1", "primary_name": "SECHIN Igor",
         "dates_of_birth": ["1960-09-07"], "gender": "M", "city": "MOSCOU",
         "tax_id": "FR123456789",
         "countries": {"citizenship": ["RU"], "residence": [],
                       "birth_country": [], "jurisdiction_country": []}}


# ------------------ SYMÉTRIE ------------------

@pytest.mark.parametrize("champ", sorted(BLOCKING_FIELDS))
def test_every_field_extracts_on_both_sides(champ):
    """Une composante calculable d'un seul côté produirait une clé qui ne se
    rencontre jamais : le listé deviendrait invisible, sans la moindre erreur."""
    _libelle, cote_client, cote_liste = BLOCKING_FIELDS[champ]
    assert callable(cote_client) and callable(cote_liste)
    # Sur une entité vide, les deux côtés doivent rendre le joker — pas lever
    assert field_component_value(champ, {}, True) == FIELD_WILDCARD
    assert field_component_value(champ, {}, False) == FIELD_WILDCARD


def test_the_same_value_is_read_identically_on_both_sides():
    """Le test qui compte : la même personne, décrite des deux côtés, doit
    produire la MÊME valeur de composante."""
    for champ in ("DOB_YEAR", "GENDER", "CITY", "TAX_ID"):
        cote_client = field_component_value(champ, CLIENT, True)
        cote_liste = field_component_value(champ, LISTE, False)
        assert cote_client == cote_liste != FIELD_WILDCARD, (
            f"{champ} : « {cote_client} » côté client, « {cote_liste} » côté liste")


def test_identifiers_ignore_formatting():
    """« FR 123 456 789 » et « FR123456789 » sont le même identifiant fiscal :
    une différence de saisie ne doit pas séparer deux clés."""
    assert (field_component_value("TAX_ID", CLIENT, True)
            == field_component_value("TAX_ID", LISTE, False) == "FR123456789")


def test_partial_dates_still_yield_a_year():
    """Les listes publient des dates partielles ; l'année suffit à bloquer."""
    for brut in ("1960", "1960-00-00", "07/09/1960", "circa 1960"):
        assert field_component_value(
            "DOB_YEAR", {"dates_of_birth": [brut]}, False) == "1960"


# ------------------ JOKER : NE JAMAIS PERDRE UN CANDIDAT ------------------

def test_a_listed_record_without_the_field_stays_reachable():
    """LE point critique. Une fiche listée sans date de naissance porte le
    joker ; le client qui, lui, a une date doit tout de même la rencontrer."""
    layout = ["ENTITY_TYPE", "PHONETIC_FIRST", "DOB_YEAR"]
    cles_liste = generate_blocking_keys(
        {**LISTE, "dates_of_birth": []}, _cfg(layout))
    sondes = lookup_blocking_keys(CLIENT, _cfg(layout))
    assert cles_liste & sondes, (
        "fiche listée sans date devenue inatteignable : ajouter un champ au "
        "blocking ferait manquer des listés")


def test_several_missing_fields_at_once_stay_reachable():
    """Une fiche à qui il manque PLUSIEURS des champs bloquants reste
    atteignable : c'est le cas courant des listes officielles."""
    layout = ["ENTITY_TYPE", "PHONETIC_FIRST", "DOB_YEAR", "CITY"]
    cles_liste = generate_blocking_keys(
        {**LISTE, "dates_of_birth": [], "city": None}, _cfg(layout))
    sondes = lookup_blocking_keys(CLIENT, _cfg(layout))
    assert cles_liste & sondes


def test_matching_values_still_meet_without_any_wildcard():
    """Le cas nominal ne doit pas dépendre du joker."""
    layout = ["ENTITY_TYPE", "PHONETIC_FIRST", "DOB_YEAR"]
    assert generate_blocking_keys(LISTE, _cfg(layout)) & \
        generate_blocking_keys(CLIENT, _cfg(layout))


def test_a_different_value_does_narrow_the_candidates():
    """L'intérêt d'ajouter un champ : deux personnes dont l'année de naissance
    diffère ne sont plus candidates l'une de l'autre."""
    layout = ["ENTITY_TYPE", "PHONETIC_FIRST", "DOB_YEAR"]
    autre = {**LISTE, "dates_of_birth": ["1985-01-01"]}
    assert not (generate_blocking_keys(autre, _cfg(layout))
                & generate_blocking_keys(CLIENT, _cfg(layout)))


# ------------------ CATALOGUE ET GARDE-FOUS ------------------

def test_all_fields_are_offered_as_components():
    for champ in BLOCKING_FIELDS:
        assert champ in BLOCKING_COMPONENTS


def test_layout_refuses_more_fields_than_the_cap():
    """Chaque champ ajouté DOUBLE le nombre de sondes (il faut interroger la
    variante jokerisée). Le plafond est explicite plutôt que subi."""
    trop = ["PHONETIC_FIRST"] + list(BLOCKING_FIELDS)[:MAX_BLOCKING_FIELDS + 1]
    assert not _valid_layout(trop)
    juste = ["PHONETIC_FIRST"] + list(BLOCKING_FIELDS)[:MAX_BLOCKING_FIELDS]
    assert _valid_layout(juste)


def test_historic_layout_is_untouched():
    """Sans champ ajouté, le blocking se comporte exactement comme avant."""
    layout = ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]
    assert _valid_layout(layout)
    assert generate_blocking_keys(LISTE, _cfg(layout)) \
        & generate_blocking_keys(CLIENT, _cfg(layout))

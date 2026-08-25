"""
Câblage des capacités dans le moteur — aller-retour, capacité par capacité.

Le lot précédent posait le catalogue sans rien changer au criblage. Celui-ci
branche les gardes. La forme de chaque test est donc toujours la même, et
c'est le critère de recette du lot :

    ACTIVE  → le moteur rend exactement ce qu'il rendait avant ;
    COUPÉE  → l'effet documenté dans le champ `loss` se produit, et RIEN
              d'autre ne bouge.

La deuxième moitié est la moins évidente et la plus importante : une garde mal
posée coupe plus que sa capacité. C'est pourquoi presque chaque test coupé
vérifie aussi qu'un voisin est resté intact.
"""
import pytest

from fiskr import capabilities as caps
from fiskr import resources
from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys
from fiskr.config import config
from fiskr.scoring import calculate_geography_adjustment, match_entities
from fiskr.capabilities import (
    CHANNEL_SCREENING, CHANNEL_FILTERING, defaults_for_channel, use_context,
    invalidate_context,
)


LAYOUT = {"blocking": {"custom_key_layout":
                       ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]}}


@pytest.fixture(autouse=True)
def _clean():
    invalidate_context()
    yield
    caps._local.override = None
    resources.set_index(None)
    resources.invalidate_context()
    invalidate_context()


def actives(channel=CHANNEL_SCREENING, sans=(), avec=()):
    """Le parametrage par defaut du canal, moins / plus les capacites citees."""
    defauts = defaults_for_channel(channel)
    jeu = {cap_id for cap_id, on in defauts.items() if on}
    return (jeu - set(sans)) | set(avec)


def coupe(*capacites, channel=CHANNEL_SCREENING):
    return use_context(channel, actives(channel, sans=capacites))


def tout_actif(channel=CHANNEL_SCREENING):
    """Toutes capacités cochées, y compris celles inactives par défaut."""
    return use_context(channel, set(defaults_for_channel(channel)))


# ================== SÉLECTION DES CANDIDATS ==================

def test_phonetic_keys_gather_two_spellings_that_sound_alike():
    """Actives : « Shmit » et « Schmidt » tombent dans le même seau."""
    a = generate_blocking_keys({"entity_type": "I", "primary_name": "Shmit"}, LAYOUT)
    b = generate_blocking_keys({"entity_type": "I", "primary_name": "Schmidt"}, LAYOUT)
    assert set(a) & set(b)


def test_cutting_phonetics_makes_those_two_spellings_never_compared():
    """
    Coupée : elles ne se rencontrent plus. C'est la perte la plus brutale du
    catalogue — le scoring ne les voit MÊME PAS, aucun score n'est calculé.
    """
    with coupe(caps.CAP_BLOCKING_PHONETIC):
        a = generate_blocking_keys({"entity_type": "I", "primary_name": "Shmit"}, LAYOUT)
        b = generate_blocking_keys({"entity_type": "I", "primary_name": "Schmidt"}, LAYOUT)
        assert not (set(a) & set(b))
        # Mais le partitionnement pays/nature subsiste : la clé reste formée,
        # elle ne porte simplement plus de composante phonétique.
        assert a and all(k.count("_") == 2 for k in a)


def test_cutting_phonetics_leaves_two_identical_names_together():
    """La garde ne coupe QUE la phonétique : l'identité stricte survit."""
    with coupe(caps.CAP_BLOCKING_PHONETIC):
        a = generate_blocking_keys({"entity_type": "I", "primary_name": "Schmidt"}, LAYOUT)
        b = generate_blocking_keys({"entity_type": "I", "primary_name": "Schmidt"}, LAYOUT)
        assert set(a) & set(b)


def _index_henri():
    index = resources.index_from_mapping(
        {resources.FIELD_GIVEN_NAME: {"HENRY": ["Henri", "Harry"]}})
    resources.set_index(index)
    resources._context_cache = {"index": index, "fields": {resources.FIELD_GIVEN_NAME}}


def test_equivalence_keys_bring_henri_and_harry_together():
    _index_henri()
    a = generate_blocking_keys({"entity_type": "I", "primary_name": "Henri Dupont"}, LAYOUT)
    b = generate_blocking_keys({"entity_type": "I", "primary_name": "Harry Dupont"}, LAYOUT)
    assert set(a) & set(b)


def test_cutting_the_blocking_key_makes_the_resource_tables_inert():
    """
    Le piège que le plan demandait de verrouiller : `resources.enabled_fields`
    a beau être ACTIF, sans clé commune au blocking « Henri » et « Harry » ne
    sont jamais candidats l'un pour l'autre — la table n'a rien à rapprocher.
    Un écran qui laisserait croire l'inverse tromperait son lecteur.
    """
    _index_henri()
    assert resources.current_context()["fields"]   # la table est bien active
    # Noms de famille DIFFÉRENTS à dessein : depuis qu'une fiche listée émet
    # aussi une clé phonétique sur son dernier mot, un « Dupont » commun
    # suffirait à les rapprocher — et le test ne prouverait plus rien sur les
    # équivalences, qui sont son sujet.
    with coupe(caps.CAP_BLOCKING_EQUIVALENCES):
        a = generate_blocking_keys({"entity_type": "I", "primary_name": "Henri Dupont"}, LAYOUT)
        b = generate_blocking_keys({"entity_type": "I", "primary_name": "Harry Lefevre"}, LAYOUT)
        assert not (set(a) & set(b))


def test_cutting_equivalences_keeps_the_phonetic_keys():
    """
    Les deux mécanismes sortaient de la MÊME branche de code et étaient donc
    indissociables. Le lot les a séparés : ce test le prouve dans un sens…
    """
    with coupe(caps.CAP_BLOCKING_EQUIVALENCES):
        a = generate_blocking_keys({"entity_type": "I", "primary_name": "Shmit"}, LAYOUT)
        b = generate_blocking_keys({"entity_type": "I", "primary_name": "Schmidt"}, LAYOUT)
        assert set(a) & set(b)


def test_cutting_phonetics_keeps_the_equivalence_keys():
    """… et ce test dans l'autre."""
    _index_henri()
    with coupe(caps.CAP_BLOCKING_PHONETIC):
        a = generate_blocking_keys({"entity_type": "I", "primary_name": "Henri Dupont"}, LAYOUT)
        b = generate_blocking_keys({"entity_type": "I", "primary_name": "Harry Dupont"}, LAYOUT)
        assert set(a) & set(b)


def _sans_pays():
    return {"entity_id": "E1", "entity_type": "E",
            "primary_name": "GOLDEN DRAGON CAPITAL LTD",
            "countries": {"jurisdiction_country": []}}


def _client_hk():
    return {"client_id": "C1", "client_type": "PM",
            "client_company_name": "GOLDEN DRAGON CAPITAL LTD",
            "client_countries": {"registration_country": ["HK"]}}


def test_the_country_wildcard_reaches_a_listed_record_without_a_country():
    assert set(generate_blocking_keys(_sans_pays(), LAYOUT)) & \
        set(lookup_blocking_keys(_client_hk(), LAYOUT))


def test_cutting_the_country_wildcard_reopens_the_hole():
    """
    Effet documenté : la fiche listée sans pays redevient structurellement
    inatteignable. La capacité restitue le comportement d'avant la correction.
    """
    with coupe(caps.CAP_BLOCKING_COUNTRY_WILDCARD):
        assert not (set(generate_blocking_keys(_sans_pays(), LAYOUT)) &
                    set(lookup_blocking_keys(_client_hk(), LAYOUT)))


# ================== VARIANTES DE NOMS ==================

XI = {"entity_type": "I", "primary_name": "Xi Jinping"}
CLIENT_XI = {"client_type": "PP", "client_first_name": "Jinping", "client_last_name": "Xi"}


def test_reversed_name_order_matches_an_east_asian_listing():
    assert match_entities(CLIENT_XI, XI, config)["status"] == "ALERT"


def test_cutting_reversed_name_order_collapses_that_match():
    """
    Effet documenté : les deux chaînes restent systématiquement inversées et
    seul le token sort résiste — ce qui ne franchit aucun seuil.
    """
    with coupe(caps.CAP_NAMES_REVERSED):
        result = match_entities(CLIENT_XI, XI, config)
    assert result["status"] == "NO_MATCH"
    assert result["final_score"] < config["scoring"]["cut_off_threshold"]


def test_cutting_reversed_name_order_leaves_the_direct_order_intact():
    direct = {"client_type": "PP", "client_first_name": "Xi", "client_last_name": "Jinping"}
    with coupe(caps.CAP_NAMES_REVERSED):
        assert match_entities(direct, XI, config)["status"] == "ALERT"


MARIE = {"client_type": "PP", "client_first_name": "Marie", "client_last_name": "Durand",
         "client_maiden_name": "Marie Kovalenko"}
KOVALENKO = {"entity_type": "I", "primary_name": "Marie Kovalenko"}


def test_the_maiden_name_reaches_a_listing_under_the_birth_name():
    assert match_entities(MARIE, KOVALENKO, config)["status"] == "ALERT"


def test_cutting_maiden_names_loses_that_match():
    with coupe(caps.CAP_NAMES_MAIDEN):
        assert match_entities(MARIE, KOVALENKO, config)["status"] == "NO_MATCH"


def test_the_maiden_name_of_the_listing_is_compared_too():
    """La capacité couvre les DEUX côtés : client et fiche."""
    fiche = {"entity_type": "I", "primary_name": "Marie Petrova",
             "individual_name_parsed": {"maiden_name": "Marie Kovalenko"}}
    cliente = {"client_type": "PP", "client_first_name": "Marie",
               "client_last_name": "Kovalenko"}
    assert match_entities(cliente, fiche, config)["status"] == "ALERT"
    with coupe(caps.CAP_NAMES_MAIDEN):
        assert match_entities(cliente, fiche, config)["status"] == "NO_MATCH"


ALIAS_LISTE = {"entity_type": "E", "primary_name": "SOCIETE ANONYME DES MINES DU NORD",
               "aliases": {"high_priority": ["ACME HOLDING"], "low_priority": []}}
CLIENT_ACME = {"client_type": "PM", "client_company_name": "ACME HOLDING"}


def test_a_listed_alias_is_screened():
    assert match_entities(CLIENT_ACME, ALIAS_LISTE, config)["status"] == "ALERT"


def test_cutting_listed_aliases_screens_the_primary_name_only():
    with coupe(caps.CAP_NAMES_ALIASES_LISTED):
        result = match_entities(CLIENT_ACME, ALIAS_LISTE, config)
    assert result["status"] == "NO_MATCH"
    # et le nom principal, lui, est toujours comparé
    with coupe(caps.CAP_NAMES_ALIASES_LISTED):
        principal = {"client_type": "PM",
                     "client_company_name": "SOCIETE ANONYME DES MINES DU NORD"}
        assert match_entities(principal, ALIAS_LISTE, config)["status"] == "ALERT"


CLIENT_ALIAS = {"client_type": "PM", "client_company_name": "LES MINES DU NORD SA",
                "aliases": ["ACME HOLDING"]}
ACME_LISTE = {"entity_type": "E", "primary_name": "ACME HOLDING"}


def test_a_client_alias_is_screened():
    assert match_entities(CLIENT_ALIAS, ACME_LISTE, config)["status"] == "ALERT"


def test_cutting_client_aliases_loses_that_match():
    with coupe(caps.CAP_NAMES_ALIASES_CLIENT):
        assert match_entities(CLIENT_ALIAS, ACME_LISTE, config)["status"] == "NO_MATCH"


# ================== AJUSTEMENTS CONTEXTUELS ==================

HOMONYME = {"client_type": "PP", "client_first_name": "Mohamed", "client_last_name": "Ali",
            "client_dob": "1942-01-17", "client_gender": "M",
            "client_countries": {"nationality": ["US"]}}
LISTE_ALI = {"entity_type": "I", "primary_name": "Mohamed Ali",
             "dates_of_birth": ["1982-06-03"], "gender": "F",
             "countries": {"citizenship": ["RU"]}}


def test_the_three_adjustments_apply_by_default():
    adj = match_entities(HOMONYME, LISTE_ALI, config)["adjustments"]
    assert adj["dob"]["score"] < 0
    assert adj["gender"]["score"] < 0
    assert adj["geography"]["score"] < 0


@pytest.mark.parametrize("capacite,cle,voisines", [
    (caps.CAP_ADJUST_DOB, "dob", ("gender", "geography")),
    (caps.CAP_ADJUST_GENDER, "gender", ("dob", "geography")),
    (caps.CAP_ADJUST_GEOGRAPHY, "geography", ("dob", "gender")),
])
def test_cutting_one_adjustment_neutralises_it_and_only_it(capacite, cle, voisines):
    """
    Le score remonte de l'exacte valeur du malus perdu — donc un homonyme
    précédemment écarté peut repasser au-dessus du seuil. C'est ce que dit le
    champ `loss`, et c'est mesurable ici.
    """
    reference = match_entities(HOMONYME, LISTE_ALI, config)
    with coupe(capacite):
        result = match_entities(HOMONYME, LISTE_ALI, config)
    assert result["adjustments"][cle]["score"] == 0.0
    assert "désactivé" in result["adjustments"][cle]["description"]
    for autre in voisines:
        assert result["adjustments"][autre] == reference["adjustments"][autre]
    assert result["base_score"] == reference["base_score"]
    perdu = reference["adjustments"][cle]["score"]
    assert result["final_score"] == pytest.approx(reference["final_score"] - perdu)


def test_a_missing_country_is_a_malus_by_default():
    """Comportement historique, conservé tel quel."""
    score, desc = calculate_geography_adjustment(["FR"], [], config)
    assert score < 0
    assert "manquant" in desc


def test_the_missing_country_can_be_made_neutral():
    """
    Capacité inactive par défaut parce qu'elle ÉLARGIT le périmètre d'alertes.
    L'absence d'information n'est pas une information contraire : un
    référentiel client mal renseigné voyait ses scores baisser sans raison.
    """
    with tout_actif():
        score, desc = calculate_geography_adjustment(["FR"], [], config)
    assert score == 0.0
    assert "neutre" in desc


def test_the_missing_country_capability_is_inert_without_its_prerequisite():
    """Elle ne peut rien faire si l'ajustement géographique est coupé."""
    with use_context(CHANNEL_SCREENING,
                     actives(sans=(caps.CAP_ADJUST_GEOGRAPHY,),
                             avec=(caps.CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL,))):
        result = match_entities(HOMONYME, LISTE_ALI, config)
    assert result["adjustments"]["geography"]["score"] == 0.0
    assert "désactivé" in result["adjustments"]["geography"]["description"]


def test_a_geographic_match_still_earns_its_bonus_when_missing_is_neutral():
    """La bascule ne touche QUE le cas du pays absent."""
    with tout_actif():
        score, _ = calculate_geography_adjustment(["FR"], ["FR"], config)
    assert score > 0


# ================== RAPPROCHEMENT SUR IDENTIFIANTS ==================
# Un hit force ALERT a 100/100 et contourne le seuil : couper l'une de ces
# capacites fait retomber au scoring flou une identite pourtant certaine.
# Les noms sont volontairement DIFFERENTS, pour qu'aucun de ces cas ne puisse
# alerter autrement que par l'identifiant.

RAISON_A = {"client_type": "PM", "client_company_name": "ACME HOLDING SA"}
RAISON_B = {"entity_type": "E", "primary_name": "ZENITH TRADING LIMITED"}

CAS_IDENTIFIANTS = [
    (caps.CAP_HARD_LEI,
     {"client_lei_number": "969500HQ2P8HN1KQMY42"}, {"lei_number": "969500HQ2P8HN1KQMY42"}),
    (caps.CAP_HARD_BIC,
     {"client_bic": "BNPAFRPP"}, {"bic_swift": "BNPAFRPP"}),
    (caps.CAP_HARD_TAX_ID,
     {"client_tax_id": "7707083893"}, {"tax_id": "7707083893"}),
    (caps.CAP_HARD_CRYPTO,
     # Cote client l'adresse est une chaine, cote fiche un objet : structures
     # d'origine differentes, c'est bien ce que le moteur recoit en production.
     {"client_crypto_wallets": ["1KZbe5nEjCWJHhK6egNhSCk7BQXpXBcnr"]},
     {"crypto_wallets": [{"address": "1KZbe5nEjCWJHhK6egNhSCk7BQXpXBcnr"}]}),
    (caps.CAP_HARD_PASSPORT,
     {"client_passport_documents": [{"number": "12AB34567", "country": "FR"}]},
     {"passport_documents": [{"number": "12AB34567", "country": "FR"}]}),
    (caps.CAP_HARD_NATIONAL_REGISTRY,
     {"client_national_registry_ids": [{"number": "552081317", "country": "FR"}]},
     {"national_registry_ids": [{"number": "552081317", "country": "FR"}]}),
    (caps.CAP_HARD_NATIONAL_ID,
     {"client_national_id_documents": [{"number": "980456789", "country": "FR"}]},
     {"national_id_documents": [{"number": "980456789", "country": "FR"}]}),
    (caps.CAP_HARD_VESSEL,
     {"transaction_vessel_imo": "9074729"}, {"imo_number": "9074729"}),
    (caps.CAP_HARD_AIRCRAFT,
     {"transaction_aircraft_registration": "P-914"}, {"aircraft_tail_number": "P-914"}),
    (caps.CAP_HARD_OTHER_DOCUMENTS,
     {"client_other_id_documents": [{"number": "X99887766", "type": "Carte de séjour"}]},
     {"other_id_documents": [{"number": "X99887766", "type": "Carte de séjour"}]}),
]


@pytest.mark.parametrize("capacite,champs_client,champs_fiche",
                         CAS_IDENTIFIANTS,
                         ids=[c[0] for c in CAS_IDENTIFIANTS])
def test_each_identifier_family_triggers_a_hard_match_by_default(
        capacite, champs_client, champs_fiche):
    client = dict(RAISON_A, **champs_client)
    fiche = dict(RAISON_B, **champs_fiche)
    result = match_entities(client, fiche, config)
    assert result["hard_match_triggered"] is True, capacite
    assert result["final_score"] == 100.0


@pytest.mark.parametrize("capacite,champs_client,champs_fiche",
                         CAS_IDENTIFIANTS,
                         ids=[c[0] for c in CAS_IDENTIFIANTS])
def test_cutting_an_identifier_family_drops_its_certain_match(
        capacite, champs_client, champs_fiche):
    """
    Effet documenté : l'identité certaine retombe au scoring flou. Les raisons
    sociales étant différentes, il ne reste RIEN — faux négatif réglementaire,
    ce que le catalogue annonce.
    """
    client = dict(RAISON_A, **champs_client)
    fiche = dict(RAISON_B, **champs_fiche)
    with coupe(capacite):
        result = match_entities(client, fiche, config)
    assert result["hard_match_triggered"] is False, capacite
    assert result["status"] == "NO_MATCH"


def test_cutting_one_identifier_family_leaves_the_others_working():
    """La garde est posée famille par famille, pas sur la séquence entière."""
    client = dict(RAISON_A, client_lei_number="969500HQ2P8HN1KQMY42",
                  client_bic="BNPAFRPP")
    fiche = dict(RAISON_B, lei_number="969500HQ2P8HN1KQMY42", bic_swift="BNPAFRPP")
    with coupe(caps.CAP_HARD_LEI):
        result = match_entities(client, fiche, config)
    assert result["hard_match_triggered"] is True
    assert "BIC" in result["hard_match_details"]


def test_the_vessel_capability_covers_its_three_identifiers():
    """IMO, MMSI et indicatif radio désignent le même navire : une bascule."""
    for champ_client, champ_fiche, valeur in [
        ("transaction_vessel_imo", "imo_number", "9074729"),
        ("transaction_vessel_mmsi", "vessel_mmsi", "273345670"),
        ("transaction_vessel_call_sign", "vessel_call_sign", "UBQZ"),
    ]:
        client = dict(RAISON_A, **{champ_client: valeur})
        fiche = dict(RAISON_B, **{champ_fiche: valeur})
        assert match_entities(client, fiche, config)["hard_match_triggered"] is True
        with coupe(caps.CAP_HARD_VESSEL):
            assert match_entities(client, fiche, config)["hard_match_triggered"] is False


# ================== CANAUX ==================

def test_the_channel_travels_in_the_config_without_changing_any_signature():
    """
    Le canal voyage dans la config plutôt que dans la signature de
    `match_entities` : les six appelants existants n'ont pas bougé, et un
    appel sans canal crible comme avant.
    """
    coupe_au_filtrage = use_context(CHANNEL_FILTERING,
                                    actives(CHANNEL_FILTERING,
                                            sans=(caps.CAP_HARD_LEI,)))
    client = dict(RAISON_A, client_lei_number="969500HQ2P8HN1KQMY42")
    fiche = dict(RAISON_B, lei_number="969500HQ2P8HN1KQMY42")
    with coupe_au_filtrage:
        # Canal absent = criblage : le réglage du filtrage ne l'atteint pas
        assert match_entities(client, fiche, config)["hard_match_triggered"] is True
        filtrage = dict(config, engine_channel=CHANNEL_FILTERING)
        assert match_entities(client, fiche, filtrage)["hard_match_triggered"] is False


def test_the_blocking_channel_travels_the_same_way():
    layout_filtrage = {"blocking": {"custom_key_layout":
                                    ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"],
                                    "channel": CHANNEL_FILTERING}}
    with use_context(CHANNEL_FILTERING,
                     actives(CHANNEL_FILTERING, sans=(caps.CAP_BLOCKING_PHONETIC,))):
        a = generate_blocking_keys({"entity_type": "I", "primary_name": "Shmit"},
                                   layout_filtrage)
        b = generate_blocking_keys({"entity_type": "I", "primary_name": "Schmidt"},
                                   layout_filtrage)
        assert not (set(a) & set(b))
        # Le criblage, lui, n'a pas bougé
        assert (set(generate_blocking_keys({"entity_type": "I", "primary_name": "Shmit"}, LAYOUT))
                & set(generate_blocking_keys({"entity_type": "I", "primary_name": "Schmidt"}, LAYOUT)))


def test_the_settings_helpers_carry_the_channel():
    from fiskr.settings import blocking_config_for
    cfg = blocking_config_for(["COUNTRY_ISO", "PHONETIC_FIRST"], channel=CHANNEL_FILTERING)
    assert cfg["blocking"]["channel"] == CHANNEL_FILTERING
    assert cfg["engine_channel"] == CHANNEL_FILTERING
    # Par defaut : criblage, soit le comportement historique
    assert blocking_config_for(["COUNTRY_ISO"])["engine_channel"] == CHANNEL_SCREENING


# ================== TRAÇABILITÉ ==================

def test_a_standard_setup_leaves_the_decision_tree_unchanged():
    """
    Même doctrine que `resource_equivalences` : la clé n'apparaît QUE si le
    moteur s'écarte du catalogue. Une installation standard produit exactement
    l'arbre de décision qu'elle produisait avant ce lot.
    """
    result = match_entities(CLIENT_XI, XI, config)
    assert "capabilities_applied" not in result


def test_a_cut_capability_is_written_into_the_decision_tree():
    """
    Sans cette trace, un contrôleur relisant en 2029 une alerte de 2026 ne
    peut pas savoir quels mécanismes tournaient ce jour-là : le réglage vit en
    base, il n'est pas recopié dans le `config_state` figé au criblage.
    """
    with coupe(caps.CAP_NAMES_REVERSED, caps.CAP_ADJUST_DOB):
        trace = match_entities(CLIENT_XI, XI, config)["capabilities_applied"]
    assert trace["channel"] == CHANNEL_SCREENING
    assert trace["disabled"] == sorted([caps.CAP_NAMES_REVERSED, caps.CAP_ADJUST_DOB])
    assert "enabled" not in trace


def test_a_capability_switched_on_beyond_the_defaults_is_written_too():
    with tout_actif():
        trace = match_entities(CLIENT_XI, XI, config)["capabilities_applied"]
    assert trace["enabled"] == [caps.CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL]
    assert "disabled" not in trace


def test_an_inert_capability_is_reported_as_such():
    """Cochée mais sans son prérequis : elle n'a rien fait, l'arbre le dit."""
    with use_context(CHANNEL_SCREENING,
                     actives(sans=(caps.CAP_ADJUST_GEOGRAPHY,),
                             avec=(caps.CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL,))):
        trace = match_entities(CLIENT_XI, XI, config)["capabilities_applied"]
    assert caps.CAP_ADJUST_GEOGRAPHY in trace["disabled"]
    assert trace["inert"] == [caps.CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL]


def test_the_trace_is_carried_by_hard_matches_too():
    """Une alerte à 100/100 doit être aussi explicable qu'une alerte floue."""
    client = dict(RAISON_A, client_lei_number="969500HQ2P8HN1KQMY42")
    fiche = dict(RAISON_B, lei_number="969500HQ2P8HN1KQMY42")
    with coupe(caps.CAP_HARD_BIC):
        result = match_entities(client, fiche, config)
    assert result["hard_match_triggered"] is True
    assert result["capabilities_applied"]["disabled"] == [caps.CAP_HARD_BIC]


def test_the_trace_is_carried_when_no_name_can_be_compared():
    with coupe(caps.CAP_NAMES_REVERSED):
        result = match_entities({"client_type": "PP"}, XI, config)
    assert result["status"] == "NO_MATCH"
    assert result["capabilities_applied"]["disabled"] == [caps.CAP_NAMES_REVERSED]

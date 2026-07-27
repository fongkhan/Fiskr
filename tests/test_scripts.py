"""
Écritures : nommer ce que le moteur ne savait que compter.

`has_non_latin_chars` était BINAIRE — latin ou non latin, sur un seuil de
point de code. Aucun code du dépôt ne nommait une écriture, il était donc
impossible de traiter le cyrillique autrement que le chinois. C'est pourtant
deux décisions de conformité distinctes : un établissement exposé à la Russie
et pas à la Chine paie aujourd'hui le coût de l'une pour l'autre.

Ce que ces tests verrouillent :
- la détection nomme correctement les écritures, et RIEN n'échappe au réglage
  (ce qui n'entre dans aucune plage tombe dans « other », qui a sa bascule) ;
- le périmètre de translittération n'a pas bougé d'un caractère ;
- `strip_accents` reste INCONDITIONNEL — l'index des ressources est bâti avec
  lui, le rendre réglable ferait perdre à l'index ses propres entrées ;
- toutes capacités actives, la normalisation de comparaison rend exactement
  ce que rendait `strip_accents`.
"""
import pytest

from fiskr import capabilities as caps
from fiskr import resources
from fiskr.capabilities import (
    CHANNEL_SCREENING, SCRIPTS, defaults_for_channel, script_capability, use_context,
    invalidate_context,
)
from fiskr.config import config
from fiskr.quality import (
    clean_noise_words, detect_scripts, evaluate_and_clean, has_non_latin_chars,
    script_of, strip_accents, strip_accents_for_matching,
)
from fiskr.scoring import compute_base_score, match_entities


@pytest.fixture(autouse=True)
def _clean():
    invalidate_context()
    yield
    caps._local.override = None
    resources.set_index(None)
    resources.invalidate_context()
    invalidate_context()


def actives(sans=(), avec=()):
    defauts = defaults_for_channel(CHANNEL_SCREENING)
    jeu = {cap_id for cap_id, on in defauts.items() if on}
    return (jeu - set(sans)) | set(avec)


def coupe(*capacites):
    return use_context(CHANNEL_SCREENING, actives(sans=capacites))


def sans_ressources():
    """
    Neutralise les tables d'équivalences pour isoler l'effet mesuré.

    Ce n'est pas une commodité de test : les tables RATTRAPENT une écriture
    coupée quand le nom y figure (« Владимир » y est listé avec « Vladimir »),
    et un test qui les laisserait actives mesurerait deux mécanismes à la
    fois. La compensation elle-même est vérifiée plus bas, explicitement.
    """
    resources.set_index(None)
    resources._context_cache = {"index": None, "fields": set()}


# ------------------ DÉTECTION ------------------

@pytest.mark.parametrize("texte,attendu", [
    ("Владимир Путин", {"cyrillic"}),
    ("习近平", {"han"}),
    ("محمد بن سلمان", {"arabic"}),
    ("김정은", {"hangul"}),
    ("ヤマモト イソロク", {"kana"}),
    ("משה", {"hebrew"}),
    ("Ελληνικά", {"greek"}),
    ("ยิ่งลักษณ์ ชินวัตร", {"thai"}),
    ("नरेंद्र मोदी", {"devanagari"}),
])
def test_each_script_is_named(texte, attendu):
    assert detect_scripts(texte) == attendu


def test_a_latin_name_declares_no_script():
    """Accents et diacritiques restent latins : ce n'est pas de l'écriture."""
    assert detect_scripts("Müller") == frozenset()
    assert detect_scripts("IBAÑEZ-Dupont, S.A.") == frozenset()
    assert detect_scripts("") == frozenset()


def test_a_mixed_name_declares_all_its_scripts():
    """Le cas réel : une raison sociale mêlant l'écriture d'origine et le latin."""
    assert detect_scripts("ООО Ромашка Ltd") == {"cyrillic"}
    assert detect_scripts("陈 Quanguo / Владимир") == {"han", "cyrillic"}


def test_an_unlisted_script_falls_into_other_rather_than_escaping_the_setting():
    """
    Arménien, géorgien, éthiopien : aucune plage déclarée. Ils ne doivent pas
    pour autant échapper au réglage — sans quoi une écriture oubliée serait
    translittérée quoi qu'en décide l'établissement.
    """
    assert detect_scripts("Հայաստան") == {"other"}
    assert script_of("ა") == "other"


def test_every_detectable_script_has_its_toggle_in_the_catalog():
    """
    Verrou d'intégrité : ajouter une plage au détecteur sans ajouter sa
    capacité produirait une écriture non réglable, donc une promesse fausse.
    """
    echantillons = ["Владимир", "习近平", "محمد", "김정은", "ヤマモト", "משה",
                    "Ελληνικά", "ยิ่ง", "नरेंद्र", "Հայաստան"]
    vues = set()
    for texte in echantillons:
        vues |= detect_scripts(texte)
    assert vues == set(SCRIPTS), "détecteur et catalogue ont divergé"
    for script in SCRIPTS:
        assert script_capability(script) in caps.CAPABILITY_CATALOG


def test_the_transliteration_perimeter_has_not_moved():
    """
    `has_non_latin_chars` décide TOUJOURS s'il faut translittérer. Le nommage
    d'écriture vient après et ne peut donc ni élargir ni restreindre ce que le
    moteur translittérait hier.
    """
    for texte in ["Владимир Путин", "习近平", "Müller", "ACME S.A.", "",
                  "Jean-Pierre O'Neill", "— guillemets « » —"]:
        assert has_non_latin_chars(texte) is bool(detect_scripts(texte)), texte
    # Ponctuation et espaces ne déclenchent rien, comme avant
    assert has_non_latin_chars("— « » …") is False


# ------------------ NORMALISATION DE COMPARAISON ------------------

def test_with_every_capability_on_the_result_is_the_historical_one():
    """Le critère de non-régression de ce lot."""
    for texte in ["Владимир Путин", "习近平", "Müller", "김정은", "ACME S.A.",
                  "محمد بن سلمان", "陈 Quanguo"]:
        assert strip_accents_for_matching(texte) == strip_accents(texte), texte


def test_cutting_one_script_leaves_that_script_untransliterated():
    with coupe(script_capability("cyrillic")):
        assert strip_accents_for_matching("Владимир") == "Владимир"


def test_cutting_one_script_does_not_touch_the_others_in_the_same_name():
    """
    Le besoin d'origine, littéralement : traiter le cyrillique autrement que
    le chinois, y compris à l'intérieur d'une même chaîne.
    """
    with coupe(script_capability("cyrillic")):
        rendu = strip_accents_for_matching("陈 Владимир")
    assert "Владимир" in rendu
    assert "Chen" in rendu or "Chen" in rendu.title()
    assert "陈" not in rendu


def test_cutting_transliteration_altogether_cuts_every_script():
    with coupe(caps.CAP_TRANSLIT):
        assert strip_accents_for_matching("Владимир 习近平") == "Владимир 习近平"


def test_a_script_toggle_is_inert_without_its_prerequisite():
    """Cochée seule, sans la translittération, elle ne peut rien faire."""
    with use_context(CHANNEL_SCREENING, {script_capability("cyrillic")}):
        assert strip_accents_for_matching("Владимир") == "Владимир"


def test_cutting_diacritics_keeps_the_accents():
    assert strip_accents_for_matching("Müller") == "Muller"
    with coupe(caps.CAP_DIACRITICS):
        assert strip_accents_for_matching("Müller") == "Müller"


def test_cutting_diacritics_costs_a_measurable_score():
    """« Müller » cesse de rapprocher « MULLER » : les métriques de chaîne
    sont sensibles aux accents, ce que le catalogue annonce."""
    sans_ressources()
    avec = compute_base_score("Müller", "MULLER", config)
    with coupe(caps.CAP_DIACRITICS):
        sans = compute_base_score("Müller", "MULLER", config)
    assert avec == pytest.approx(100.0)
    assert sans < avec


# ------------------ CE QUI DOIT RESTER INCONDITIONNEL ------------------

def test_strip_accents_ignores_the_settings_entirely():
    """
    Il bâtit l'index des ressources et sert la recherche d'API. S'il devenait
    réglable, l'index cesserait de retrouver ses propres entrées dès qu'un
    réglage changerait — le piège que ce lot devait éviter.
    """
    with use_context(CHANNEL_SCREENING, set()):
        assert strip_accents("Владимир") == "Vladimir"
        assert strip_accents("Müller") == "Muller"


def test_the_resource_index_still_finds_its_entries_with_everything_cut():
    index = resources.index_from_mapping(
        {resources.FIELD_GIVEN_NAME: {"VLADIMIR": ["Vladimir", "Владимир"]}})
    resources.set_index(index)
    resources._context_cache = {"index": index, "fields": {resources.FIELD_GIVEN_NAME}}
    with use_context(CHANNEL_SCREENING, set()):
        assert resources.normalize_term("Владимир") == "VLADIMIR"
        assert index.canonical("Владимир", resources.FIELD_GIVEN_NAME) == "VLADIMIR"


def test_ingestion_normalisation_never_depends_on_a_hot_setting():
    """
    Ce qui est STOCKÉ est versé au dossier réglementaire avec son instantané
    de liste. Le faire dépendre d'un réglage à chaud normaliserait deux fiches
    de la même liste différemment selon l'heure de leur import.
    """
    fiche = {"entity_type": "I", "primary_name": "Владимир Путин",
             "individual_name_parsed": {"first_name": "Владимир", "last_name": "Путин"}}
    with use_context(CHANNEL_SCREENING, set()):
        assert evaluate_and_clean(fiche)["cleansed_name"] == "VLADIMIR PUTIN"


def test_the_screening_probe_does_obey_the_setting():
    """Le canal fourni = chemin de comparaison : là, le réglage s'applique."""
    client = {"client_id": "C1", "client_type": "PP",
              "client_first_name": "Владимир", "client_last_name": "Путин"}
    assert evaluate_and_clean(dict(client),
                              channel=CHANNEL_SCREENING)["cleansed_name"] == "VLADIMIR PUTIN"
    with coupe(script_capability("cyrillic")):
        rendu = evaluate_and_clean(dict(client), channel=CHANNEL_SCREENING)["cleansed_name"]
    assert rendu == "ВЛАДИМИР ПУТИН"


# ------------------ SUFFIXES JURIDIQUES ------------------

def test_legal_suffixes_are_stripped_unconditionally_at_ingestion():
    assert clean_noise_words("ACME SARL") == "ACME"
    with use_context(CHANNEL_SCREENING, set()):
        assert clean_noise_words("ACME SARL") == "ACME"


def test_cutting_legal_suffixes_keeps_them_on_the_comparison_path():
    assert clean_noise_words("ACME SARL", CHANNEL_SCREENING) == "ACME"
    with coupe(caps.CAP_NOISE_WORDS):
        assert clean_noise_words("ACME SARL", CHANNEL_SCREENING) == "ACME SARL"


# ------------------ EFFET SUR LE CRIBLAGE ------------------

CLIENT_RUSSE = {"client_type": "PP", "client_first_name": "Владимир",
                "client_last_name": "Путин"}
FICHE_LATINE = {"entity_type": "I", "primary_name": "VLADIMIR PUTIN"}


def test_a_cyrillic_client_reaches_a_latin_listing():
    sans_ressources()
    assert match_entities(CLIENT_RUSSE, FICHE_LATINE, config)["status"] == "ALERT"


def test_cutting_cyrillic_loses_that_client_entirely():
    """
    Effet documenté, et c'est le risque principal du dispositif : couper une
    écriture rend le moteur AVEUGLE sur elle. Aucune métrique de chaîne ne
    peut rien faire d'un nom resté en cyrillique face à un nom latin.
    """
    sans_ressources()
    with coupe(script_capability("cyrillic")):
        result = match_entities(CLIENT_RUSSE, FICHE_LATINE, config)
    assert result["status"] == "NO_MATCH"
    # Il ne reste que le bruit de deux alphabets sans un caractère commun,
    # très loin du seuil : rien qu'un relèvement de seuil ne rattraperait.
    assert result["final_score"] < 30


def test_cutting_cyrillic_leaves_the_chinese_client_matching():
    """La bascule est bien par écriture, pas globale."""
    sans_ressources()
    client_chinois = {"client_type": "PP", "client_first_name": "近平",
                      "client_last_name": "习"}
    fiche = {"entity_type": "I", "primary_name": "XI JINPING"}
    with coupe(script_capability("cyrillic")):
        assert match_entities(client_chinois, fiche, config)["status"] == "ALERT"


def test_the_resource_tables_soften_a_cut_script_for_the_names_they_know():
    """
    Découverte de mise au point, et elle compte pour qui décide : couper une
    écriture ne rend PAS le moteur totalement aveugle quand les tables
    d'équivalences connaissent le nom. « Владимир » y figure avec
    « Vladimir » ; le rapprochement survit par la table, sans passer par la
    translittération.

    Deux conséquences : la perte réelle est moindre que le pire cas décrit —
    mais elle est INÉGALE, limitée aux noms recensés. Un nom absent des tables
    disparaît complètement. C'est pour cela que la mesure d'impact se fait sur
    un panel, et pas au raisonnement.
    """
    index = resources.index_from_mapping(
        {resources.FIELD_GIVEN_NAME: {"VLADIMIR": ["Vladimir", "Владимир"]},
         resources.FIELD_SURNAME: {"PUTIN": ["Putin", "Путин"]}})
    resources.set_index(index)
    resources._context_cache = {"index": index,
                                "fields": {resources.FIELD_GIVEN_NAME,
                                           resources.FIELD_SURNAME}}
    with coupe(script_capability("cyrillic")):
        assert match_entities(CLIENT_RUSSE, FICHE_LATINE, config)["status"] == "ALERT"
    # Un nom que les tables ne connaissent pas, lui, est bien perdu
    inconnu = {"client_type": "PP", "client_first_name": "Аркадий",
               "client_last_name": "Ротенберг"}
    fiche = {"entity_type": "I", "primary_name": "ARKADY ROTENBERG"}
    with coupe(script_capability("cyrillic")):
        assert match_entities(inconnu, fiche, config)["status"] == "NO_MATCH"


def test_the_loss_is_written_into_the_decision_tree():
    sans_ressources()
    with coupe(script_capability("cyrillic")):
        trace = match_entities(CLIENT_RUSSE, FICHE_LATINE, config)["capabilities_applied"]
    assert trace["disabled"] == [script_capability("cyrillic")]

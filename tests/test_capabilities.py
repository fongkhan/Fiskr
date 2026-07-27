"""
Catalogue des capacités du moteur — socle.

Ce lot ne change AUCUN comportement de criblage : il pose le catalogue, le
réglage et le contexte d'exécution. Le critère de recette est précisément
là — la suite complète doit rester verte sans qu'un seul test existant ne
bouge, ce qui prouve que le socle est neutre.

Les tests ci-dessous verrouillent les propriétés dont dépendra tout le reste :
le catalogue est bien formé, les défauts sont ceux du moteur actuel, la
fusion sur les défauts protège les installations existantes, et la surcharge
de contexte est isolée par thread.
"""
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import capabilities as caps
from fiskr.capabilities import (
    CAPABILITY_CATALOG, CHANNEL_SCREENING, CHANNEL_FILTERING, CHANNELS,
    FAMILY_LABELS, FAMILY_ORDER, SCRIPTS, SCRIPT_LABELS,
    CAP_TRANSLIT, CAP_ADJUST_GEOGRAPHY, CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL,
    CAP_BLOCKING_EQUIVALENCES, CAP_NAMES_REVERSED,
    capabilities_for_channel, defaults_for_channel, resolve_inactive_dependencies,
    script_capability, current_context, is_active, use_context, invalidate_context,
)
from fiskr.database import Base, AppSetting
from fiskr.settings import SETTING_ENGINE_CAPABILITIES, engine_capabilities, set_setting


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'caps.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clean_context():
    """Aucun test ne doit laisser de contexte derrière lui."""
    invalidate_context()
    yield
    caps._local.override = None
    invalidate_context()


# ------------------ FORME DU CATALOGUE ------------------

def test_every_capability_says_what_is_lost_by_switching_it_off():
    """
    `loss` est obligatoire par conception : on ne doit pas pouvoir ajouter une
    bascule en oubliant d'expliquer son risque. C'est le garde-fou qui rend
    l'avertissement de l'écran automatique plutôt que déclaratif.
    """
    for cap_id, cap in CAPABILITY_CATALOG.items():
        assert cap.loss.strip(), f"{cap_id} n'explique pas ce qu'on perd"
        assert len(cap.loss) > 40, f"{cap_id} : explication trop courte pour être utile"
        assert cap.label.strip(), cap_id
        assert cap.family in FAMILY_LABELS, f"{cap_id} : famille inconnue {cap.family}"
        assert set(cap.channels) <= set(CHANNELS), cap_id


def test_declared_dependencies_point_to_real_capabilities():
    for cap_id, cap in CAPABILITY_CATALOG.items():
        for dep in cap.depends_on:
            assert dep in CAPABILITY_CATALOG, f"{cap_id} dépend de {dep}, inconnue"
            assert dep != cap_id, f"{cap_id} dépend d'elle-même"


def test_every_family_is_represented_and_ordered():
    familles = {cap.family for cap in CAPABILITY_CATALOG.values()}
    assert familles == set(FAMILY_ORDER)
    assert len(FAMILY_ORDER) == len(set(FAMILY_ORDER))


def test_one_capability_per_script_with_transliteration_as_prerequisite():
    """Le besoin d'origine : traiter le cyrillique autrement que le chinois."""
    for script in SCRIPTS:
        cap_id = script_capability(script)
        assert cap_id in CAPABILITY_CATALOG, script
        assert CAPABILITY_CATALOG[cap_id].depends_on == (CAP_TRANSLIT,)
        assert SCRIPT_LABELS[script] in CAPABILITY_CATALOG[cap_id].label or script == "other"


# ------------------ DÉFAUTS : LE MOTEUR D'AUJOURD'HUI ------------------

def test_defaults_reproduce_todays_engine():
    """
    Tout est actif par défaut — sauf ce qui ÉLARGIRAIT le périmètre d'alertes.
    C'est la doctrine du dépôt : une installation existante ne change pas de
    comportement, et ce qui élargit se mesure avant de s'appliquer.
    """
    defaults = defaults_for_channel(CHANNEL_SCREENING)
    inactives = {cap_id for cap_id, on in defaults.items() if not on}
    assert inactives == {CAP_ADJUST_GEOGRAPHY_MISSING_NEUTRAL}


def test_the_filtering_channel_switches_off_what_payment_data_cannot_feed():
    """
    Un message de paiement ne porte ni date de naissance, ni genre, ni pays
    fiable : ces ajustements y sont inactifs par défaut, comme le layout de
    blocking du canal filtrage est déjà réduit à la phonétique seule.
    """
    screening = defaults_for_channel(CHANNEL_SCREENING)
    filtering = defaults_for_channel(CHANNEL_FILTERING)
    coupees = {c for c in filtering if screening.get(c) and not filtering[c]}
    assert "adjust.dob" in coupees
    assert "adjust.gender" in coupees
    assert CAP_ADJUST_GEOGRAPHY in coupees
    assert CAP_NAMES_REVERSED in coupees
    # Ce qui touche à l'identité même reste actif sur les deux canaux
    assert filtering[CAP_TRANSLIT] is True
    assert filtering["hard.bic"] is True


def test_channel_listing_is_stable_and_complete():
    for channel in CHANNELS:
        listed = capabilities_for_channel(channel)
        assert len(listed) == len(set(listed))
        assert set(listed) == set(defaults_for_channel(channel))


# ------------------ RÉGLAGE : FUSION SUR LES DÉFAUTS ------------------

def test_an_unknown_stored_key_is_ignored_and_a_new_capability_gets_its_default(db):
    """
    Protection des installations existantes : ajouter une capacité au catalogue
    ne doit pas invalider un réglage déjà stocké, et une clé devenue obsolète
    ne doit pas ressurgir.
    """
    set_setting(db, SETTING_ENGINE_CAPABILITIES, {
        CHANNEL_SCREENING: {CAP_TRANSLIT: False, "capacite.disparue": True}
    })
    effectif = engine_capabilities(db, CHANNEL_SCREENING)
    assert effectif[CAP_TRANSLIT] is False          # la valeur stockée gagne
    assert "capacite.disparue" not in effectif      # la clé inconnue est ignorée
    # Toutes les autres reprennent leur défaut
    assert effectif[CAP_NAMES_REVERSED] is True


def test_a_partial_setting_never_switches_off_the_rest(db):
    set_setting(db, SETTING_ENGINE_CAPABILITIES,
                {CHANNEL_SCREENING: {CAP_ADJUST_GEOGRAPHY: False}})
    effectif = engine_capabilities(db, CHANNEL_SCREENING)
    assert effectif[CAP_ADJUST_GEOGRAPHY] is False
    assert sum(1 for on in effectif.values() if on) == \
        sum(1 for on in defaults_for_channel(CHANNEL_SCREENING).values() if on) - 1


def test_the_two_channels_are_independent(db):
    set_setting(db, SETTING_ENGINE_CAPABILITIES,
                {CHANNEL_SCREENING: {CAP_TRANSLIT: False}})
    assert engine_capabilities(db, CHANNEL_SCREENING)[CAP_TRANSLIT] is False
    assert engine_capabilities(db, CHANNEL_FILTERING)[CAP_TRANSLIT] is True


def test_a_malformed_setting_degrades_to_the_full_engine(db):
    """Jamais bloquant : au pire on crible avec le moteur au complet."""
    db.add(AppSetting(key=SETTING_ENGINE_CAPABILITIES, value="pas un dict"))
    db.commit()
    assert engine_capabilities(db, CHANNEL_SCREENING) == defaults_for_channel(CHANNEL_SCREENING)


# ------------------ DÉPENDANCES ------------------

def test_a_capability_whose_prerequisite_is_off_is_inert():
    """
    Le piège déjà rencontré sur les ressources : une table branchée au seul
    scoring ne sert à rien sans la clé de blocking correspondante. L'écran
    doit pouvoir le dire plutôt que de laisser croire que la bascule agit.
    """
    actives = {script_capability("cyrillic"), CAP_BLOCKING_EQUIVALENCES}
    inertes = resolve_inactive_dependencies(actives)
    assert script_capability("cyrillic") in inertes
    assert inertes[script_capability("cyrillic")] == (CAP_TRANSLIT,)
    # Avec son prérequis, plus rien d'inerte
    assert not resolve_inactive_dependencies(actives | {CAP_TRANSLIT})


def test_is_active_checks_prerequisites_so_engine_guards_do_not_have_to():
    cyrillic = script_capability("cyrillic")
    with use_context(CHANNEL_SCREENING, {cyrillic}):        # sans CAP_TRANSLIT
        assert is_active(cyrillic, CHANNEL_SCREENING) is False
    with use_context(CHANNEL_SCREENING, {cyrillic, CAP_TRANSLIT}):
        assert is_active(cyrillic, CHANNEL_SCREENING) is True


def test_an_unknown_capability_is_never_active():
    with use_context(CHANNEL_SCREENING, {"capacite.inventee"}):
        assert is_active("capacite.inventee", CHANNEL_SCREENING) is False


# ------------------ CONTEXTE : ISOLATION PAR THREAD ------------------

def test_the_override_does_not_leak_to_other_threads():
    """
    La propriété qui rend la mesure d'impact possible : une simulation tourne
    dans un thread de fond pendant que l'API sert des criblages réels. Une
    surcharge globale les corromprait.
    """
    caps._context_cache[CHANNEL_SCREENING] = frozenset({CAP_TRANSLIT})
    vu_ailleurs = {}

    def observer():
        vu_ailleurs["actives"] = set(current_context(CHANNEL_SCREENING))

    with use_context(CHANNEL_SCREENING, set()):
        assert current_context(CHANNEL_SCREENING) == frozenset()
        autre = threading.Thread(target=observer)
        autre.start()
        autre.join()

    assert vu_ailleurs["actives"] == {CAP_TRANSLIT}, "la production a vu la simulation"


def test_the_override_is_restored_even_on_exception():
    caps._context_cache[CHANNEL_SCREENING] = frozenset({CAP_TRANSLIT})
    with pytest.raises(RuntimeError):
        with use_context(CHANNEL_SCREENING, set()):
            raise RuntimeError("échec au milieu d'une mesure")
    assert current_context(CHANNEL_SCREENING) == frozenset({CAP_TRANSLIT})


def test_overrides_nest_and_stay_per_channel():
    with use_context(CHANNEL_SCREENING, {CAP_TRANSLIT}):
        with use_context(CHANNEL_FILTERING, set()):
            assert current_context(CHANNEL_SCREENING) == frozenset({CAP_TRANSLIT})
            assert current_context(CHANNEL_FILTERING) == frozenset()
        assert current_context(CHANNEL_SCREENING) == frozenset({CAP_TRANSLIT})


def test_invalidating_the_context_forces_a_reread():
    caps._context_cache[CHANNEL_SCREENING] = frozenset({"marqueur"})
    assert current_context(CHANNEL_SCREENING) == frozenset({"marqueur"})
    invalidate_context()
    assert current_context(CHANNEL_SCREENING) != frozenset({"marqueur"})


def test_without_any_setting_the_context_is_the_full_engine():
    """Une installation qui n'a jamais touché au réglage crible comme avant."""
    invalidate_context()
    actives = current_context(CHANNEL_SCREENING)
    attendu = {c for c, on in defaults_for_channel(CHANNEL_SCREENING).items() if on}
    assert set(actives) == attendu

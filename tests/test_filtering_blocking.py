"""
Unification du blocking du filtrage transactionnel.

L'index du filtrage était bâti avec `blocking.generate_blocking_keys`, mais
les parties de paiement l'interrogeaient avec une implantation MAISON qui en
divergeait sur trois points, tous à perte : pas de translittération, pas de
clés d'équivalence, aucune capacité du moteur. Une bascule posée dans
`blocking.py` restait donc sans effet sur ce canal — ce qui aurait vidé de
son sens le réglage « par canal ».

Ces tests verrouillent la réparation et, surtout, la propriété qui la rend
acceptable : ce qui était déjà atteint le reste. L'unification n'enlève rien.
"""
import pytest

from fiskr import capabilities as caps
from fiskr import resources
from fiskr.capabilities import (
    CHANNEL_FILTERING, defaults_for_channel, script_capability, use_context,
    invalidate_context,
)
from fiskr.blocking import generate_blocking_keys
from fiskr.settings import blocking_config_for
from fiskr.transactions import _name_rotations, party_blocking_keys


# Layout par defaut du canal filtrage : phonetique seule. Les donnees de
# paiement sont trop pauvres pour partitionner sur le pays ou la nature.
CFG = blocking_config_for(["PHONETIC_FIRST"], channel=CHANNEL_FILTERING)
CFG_PAYS = blocking_config_for(["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"],
                               channel=CHANNEL_FILTERING)


@pytest.fixture(autouse=True)
def _clean():
    invalidate_context()
    yield
    caps._local.override = None
    resources.set_index(None)
    resources.invalidate_context()
    invalidate_context()


def sans_ressources():
    """
    Neutralise les tables d'équivalences pour isoler l'effet mesuré : elles
    rattrapent une écriture coupée quand le nom y figure.
    """
    resources.set_index(None)
    resources._context_cache = {"index": None, "fields": set()}


def _partie(nom, country="", birth_country="", bic="", birth_date=""):
    return {"name": nom, "country": country, "birth_country": birth_country,
            "bic": bic, "birth_date": birth_date, "role": "DBTR"}


def _fiche(nom, entity_type="I"):
    return {"entity_id": "E1", "entity_type": entity_type, "primary_name": nom,
            "countries": {}, "aliases": {"high_priority": [], "low_priority": []}}


def _atteint(partie, fiche, cfg=CFG):
    return bool(set(generate_blocking_keys(fiche, cfg)) & party_blocking_keys(partie, cfg))


# ------------------ LE TROU, ET SA FERMETURE ------------------

def test_a_cyrillic_payment_party_now_reaches_a_latin_listing():
    """
    Le défaut réparé. Le double métaphone ne connaît que l'alphabet latin :
    sur « ВЛАДИМИР ПУТИН » il rendait une clé vide, la partie tombait dans le
    seau « XX » et n'était candidate de RIEN, alors que l'index avait bien
    translittéré la fiche.
    """
    assert _atteint(_partie("ВЛАДИМИР ПУТИН"), _fiche("Vladimir Putin"))


def test_an_alphabetic_script_crosses_over():
    """Le grec aussi : une écriture alphabétique se translittère mot à mot."""
    assert _atteint(_partie("ΓΕΩΡΓΙΟΣ"), _fiche("Georgios"))


def test_what_still_does_not_cross_and_why():
    """
    Limite RÉELLE, mesurée, et qui n'est pas propre au filtrage — elle vaut
    aussi pour le blocking du criblage.

    La translittération d'une écriture syllabique rend UN SEUL mot :
    « 习近平 » → « XiJinPing », « 김정은 » → « GimJeongEun ». La clé phonétique
    étant bâtie sur le premier mot, elle ne peut pas rencontrer celle de
    « Xi Jinping », dont le premier mot est « Xi ». L'arabe échoue pour une
    autre raison : les voyelles brèves ne s'écrivent pas, « محمد » rend
    « mhmd » là où la liste porte « Mohammed ».

    Ce test existe pour que la limite soit CONNUE plutôt que découverte en
    production, et pour qu'elle échoue bruyamment si un futur lot la corrige
    — auquel cas c'est ce test qu'il faudra retourner.
    """
    sans_ressources()
    assert not _atteint(_partie("习近平"), _fiche("Xi Jinping"))
    assert not _atteint(_partie("김정은"), _fiche("Kim Jong Un"))
    assert not _atteint(_partie("محمد بن سلمان"), _fiche("Mohammed bin Salman"))


def test_the_engine_capabilities_now_reach_this_channel():
    """
    Sans l'unification, couper une capacité au filtrage n'aurait rien changé
    ici — le réglage « par canal » aurait été une promesse creuse.
    """
    sans_ressources()
    partie, fiche = _partie("ВЛАДИМИР ПУТИН"), _fiche("Vladimir Putin")
    assert _atteint(partie, fiche)
    actifs = {c for c, on in defaults_for_channel(CHANNEL_FILTERING).items() if on}
    with use_context(CHANNEL_FILTERING, actifs - {script_capability("cyrillic")}):
        assert not _atteint(partie, fiche)


def test_the_equivalence_tables_now_bite_on_this_channel_too():
    """
    Elles étaient INERTES au filtrage : la requête ne produisait aucune clé
    d'équivalence, donc la table n'avait rien à rapprocher — le piège déjà
    rencontré sur le criblage, resté ouvert sur ce canal.
    """
    index = resources.index_from_mapping(
        {resources.FIELD_GIVEN_NAME: {"HENRY": ["Henri", "Harry"]}})
    resources.set_index(index)
    resources._context_cache = {"index": index, "fields": {resources.FIELD_GIVEN_NAME}}
    assert _atteint(_partie("HARRY DUPONT"), _fiche("Henri Dupont"))
    actifs = {c for c, on in defaults_for_channel(CHANNEL_FILTERING).items() if on}
    with use_context(CHANNEL_FILTERING, actifs - {caps.CAP_BLOCKING_EQUIVALENCES}):
        assert not _atteint(_partie("HARRY DUPONT"), _fiche("Henri Dupont"))


# ------------------ CE QUI EXISTAIT DOIT SURVIVRE ------------------

def test_a_latin_party_still_reaches_its_listing():
    assert _atteint(_partie("VLADIMIR PUTIN"), _fiche("Vladimir Putin"))


def test_the_word_order_of_a_payment_field_is_still_not_trusted():
    """
    Propriété héritée de l'ancienne implantation : « PUTIN VLADIMIR » et
    « VLADIMIR PUTIN » désignent la même personne, un champ libre de paiement
    n'a pas d'ordre fiable. Les rotations la reproduisent sans dupliquer le
    code de génération des clés.
    """
    assert _atteint(_partie("PUTIN VLADIMIR"), _fiche("Vladimir Putin"))
    assert _atteint(_partie("SMITH JOHN ROBERT"), _fiche("Robert Smith"))


def test_both_natures_are_queried_because_a_party_declares_none():
    """Un message de paiement ne dit pas si la partie est une personne."""
    assert _atteint(_partie("ACME TRADING"), _fiche("Acme Trading", entity_type="E"),
                    CFG_PAYS)
    assert _atteint(_partie("VLADIMIR PUTIN"), _fiche("Vladimir Putin", entity_type="I"),
                    CFG_PAYS)


def test_the_country_of_the_party_still_partitions_when_the_layout_asks_for_it():
    fiche = dict(_fiche("Vladimir Putin"), countries={"citizenship": ["RU"]})
    assert _atteint(_partie("VLADIMIR PUTIN", country="RU"), fiche, CFG_PAYS)


def test_rotations_keep_a_single_word_name_intact():
    assert _name_rotations("PUTIN") == ["PUTIN"]
    assert _name_rotations("") == [""]
    assert set(_name_rotations("A B C")) == {"A B C", "B A C", "C A B"}


# ------------------ NON-RÉGRESSION : AUCUNE PERTE ------------------

def test_the_new_query_never_loses_a_pair_the_old_one_reached():
    """
    Preuve de la propriété qui rend l'unification acceptable : sur un panel de
    noms latins, tout ce que l'ancienne implantation atteignait reste atteint.
    Elle produisait la phonétique de TOUS les mots ; les rotations la
    reproduisent, et la translittération et les équivalences viennent EN PLUS.
    """
    import re
    from fiskr.phonetics import double_metaphone

    def ancienne(nom):
        cles = set()
        for mot in re.split(r"[\s\-]+", (nom or "").strip()):
            if not mot:
                continue
            p, s = double_metaphone(mot)
            cles |= {k for k in (p, s) if k}
        return cles or {"XX"}

    panel = ["VLADIMIR PUTIN", "JOHN ROBERT SMITH", "ACME TRADING LIMITED",
             "MULLER", "O'NEILL", "JEAN-PIERRE DUPONT", "SHMIT"]
    for nom in panel:
        nouvelles = party_blocking_keys(_partie(nom), CFG)
        assert ancienne(nom) <= nouvelles, nom

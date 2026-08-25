"""
Une fiche listée ne se laissait rejoindre que par son PRÉNOM.

Le blocking décide qui sera comparé à qui. Côté client, les champs sont
séparés : le criblage émet une clé phonétique pour le prénom ET une pour le nom
de famille. Côté liste, le nom complet tient dans UNE seule chaîne
(« JOSE GARCIA LOPEZ ») et la clé n'était bâtie que sur le **premier mot**.

Mesuré sur **393 fiches réelles** de la production (échantillon paginé du
référentiel en service), en fabriquant pour chacune le client correspondant :

| écriture du client                  | tables inactives | tables actives |
|-------------------------------------|------------------|----------------|
| prénom + nom, identiques            | 100 %            | 100 %          |
| prénom réduit à l'initiale (J.)     | **0,8 %**        | **12,7 %**     |
| prénom absent (nom de famille seul) | **0 %**          | **12,0 %**     |

Le dernier cas est le cas ORDINAIRE d'un message de paiement, et une base KYC
en contient sa part. Le criblage rendait « aucune correspondance » sans avoir
comparé quoi que ce soit : le pire état du produit, puisqu'il ne se signale pas.

Le raisonnement était déjà écrit dans le même fichier, à propos des clés
d'équivalence — « en ne regardant que le premier mot, une équivalence de NOM DE
FAMILLE ne pouvait jamais créer de pont vers une fiche listée ». Le correctif
avait été appliqué là, et pas à la clé phonétique voisine. Ce pont-là existait
donc déjà, mais il ne portait que 12 % des cas : les tables ne connaissent
qu'une part des noms de famille (« LOPEZ » oui, « GARCIA » non), là où la clé
phonétique ne demande rien à personne.

Coût, mesuré sur 2 200 fiches réelles : les clés émises passent de 1,19 à 2,30
par fiche, mais elles se répartissent sur des seaux PLUS NOMBREUX et non plus
gros — le plus gros seau ne bouge pas (60 fiches). Les candidats à comparer
par client passent de 2,5 à 4,3. C'est réglable (`blocking.phonetic_last`)
pour un référentiel aux noms de famille très concentrés.
"""
import pytest

from fiskr import capabilities as caps
from fiskr.blocking import generate_blocking_keys
from fiskr.settings import blocking_config_for

LAYOUT = blocking_config_for(["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"], "SCREENING")


def _listee(nom_complet, pays=("FR",)):
    mots = nom_complet.split()
    return {"entity_type": "I", "primary_name": nom_complet,
            "individual_name_parsed": {"first_name": mots[0], "last_name": mots[-1]},
            "countries": {"citizenship": list(pays)}, "aliases": {}}


def _client(prenom, nom, pays=("FR",)):
    return {"client_id": "C", "client_type": "PP",
            "client_first_name": prenom, "client_last_name": nom,
            "client_countries": {"nationality": list(pays)}}


def _se_rencontrent(fiche, cli):
    return bool(generate_blocking_keys(fiche, LAYOUT) & generate_blocking_keys(cli, LAYOUT))


# Formes réellement observées dans le référentiel de production : le prénom
# ouvre le nom, aucun nom décomposé n'est fourni par la source.
FICHES_REELLES = ["JOSE GARCIA LOPEZ", "MARIA FERNANDA SOTO",
                  "MOHAMMAD REZA TEHRANI", "VLADIMIR SOKOLOV"]


@pytest.mark.parametrize("nom_complet", FICHES_REELLES)
def test_un_client_sans_prenom_rencontre_sa_fiche(nom_complet):
    """
    Le cas ordinaire d'un message de paiement : le nom de famille seul. Il ne
    rencontrait RIEN — 0 % sur 393 fiches réelles.
    """
    mots = nom_complet.split()
    assert _se_rencontrent(_listee(nom_complet), _client("", mots[-1]))


@pytest.mark.parametrize("nom_complet", FICHES_REELLES)
def test_un_prenom_reduit_a_l_initiale_rencontre_sa_fiche(nom_complet):
    mots = nom_complet.split()
    assert _se_rencontrent(_listee(nom_complet), _client(mots[0][0] + ".", mots[-1]))


@pytest.mark.parametrize("nom_complet", FICHES_REELLES)
def test_ce_qui_marchait_deja_marche_toujours(nom_complet):
    """Les clés sont ADDITIVES : aucune paire aujourd'hui candidate ne cesse
    de l'être."""
    mots = nom_complet.split()
    assert _se_rencontrent(_listee(nom_complet), _client(mots[0], mots[-1]))
    # ordre inversé par la source : marchait déjà, doit continuer
    assert _se_rencontrent(_listee(nom_complet), _client(mots[-1], mots[0]))


def test_la_cle_du_dernier_mot_est_reglable():
    """
    Elle double le nombre de clés émises. Sur un référentiel aux noms de
    famille très concentrés, un exploitant doit pouvoir la couper — et savoir
    ce qu'il perd en la coupant.
    """
    # Nom de famille que les tables linguistiques ne connaissent PAS : sinon
    # la clé d'équivalence ouvrirait le pont de son côté et le test ne dirait
    # rien de la capacité qu'il prétend mesurer. C'est exactement l'écart que
    # la mesure a chiffré : les tables portent 12 % des cas, pas 100.
    fiche, cli = _listee("JOSE GARCIA SOTO"), _client("", "SOTO")
    assert _se_rencontrent(fiche, cli)
    actifs = {c for c, on in caps.defaults_for_channel(caps.CHANNEL_SCREENING).items() if on}
    with caps.use_context(caps.CHANNEL_SCREENING,
                          actifs - {caps.CAP_BLOCKING_PHONETIC_LAST}):
        assert not _se_rencontrent(fiche, cli)


def test_la_capacite_annonce_ce_qu_elle_coute_et_ce_qu_elle_rapporte():
    """
    Une capacité dont la perte n'est pas décrite est un interrupteur qu'on
    n'ose pas toucher. Celle-ci porte les deux chiffres mesurés.
    """
    perte = caps.CAPABILITY_CATALOG[caps.CAP_BLOCKING_PHONETIC_LAST].loss
    assert "0,8 %" in perte and "12,7 %" in perte, (
        "la perte doit citer les DEUX configurations mesurées : sans les "
        "tables linguistiques, et avec")
    assert caps.CAPABILITY_CATALOG[caps.CAP_BLOCKING_PHONETIC_LAST].depends_on == (
        caps.CAP_BLOCKING_PHONETIC,)


def test_elle_est_active_par_defaut():
    """
    Le défaut doit être le plus sûr : ne pas comparer une paire est un défaut
    de conformité, comparer trop est un coût.
    """
    assert caps.defaults_for_channel(caps.CHANNEL_SCREENING)[caps.CAP_BLOCKING_PHONETIC_LAST]
    assert caps.defaults_for_channel(caps.CHANNEL_FILTERING)[caps.CAP_BLOCKING_PHONETIC_LAST]

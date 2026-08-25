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

Coût, mesuré à l'échelle sur 300 000 fiches tirées de la distribution de noms
réellement observée en production : les candidats à comparer par client passent
de 135 à 178 (+32 %) et le scoring de 8,75 à 11,51 ms par client. Le plus gros
seau, lui, ne bouge presque pas (+8,7 %) : les clés supplémentaires se
répartissent sur deux fois plus de seaux au lieu de grossir les existants — les
noms de famille sont moins concentrés que les prénoms dans le référentiel réel
(6,0 % contre 13,4 % pour le 1 % le plus fréquent). Réglable
(`blocking.phonetic_last`) si le criblage devenait trop lourd.
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


def test_les_cles_ajoutees_se_repartissent_au_lieu_de_concentrer():
    """
    C'est la propriété qui rend le coût acceptable, et elle mérite d'être
    tenue : la clé du nom de famille DOUBLE le nombre de clés émises, mais
    elles se répartissent sur deux fois plus de seaux au lieu de grossir les
    existants. Le plus gros seau — celui qui dicte le pire cas d'un criblage —
    ne doit pas enfler.

    Mesuré à l'échelle (300 000 fiches, distribution de noms réelle) : seaux
    ×2,09, plus gros seau +8,7 %, candidats par client +32 %. Si une évolution
    future faisait CONCENTRER ces clés au lieu de les répartir, le pire cas
    exploserait sans que le nombre moyen de candidats ne bouge beaucoup — ce
    test est là pour que cela ne passe pas inaperçu.
    """
    import collections
    import random

    prenoms = ["JEAN", "MARIA", "JOSE", "VLADIMIR", "ANN", "UWE", "MOHAMMAD",
               "ELENA", "CARLOS", "PEDRO"]
    familles = ["PLANT", "BOUSQUET", "NILSSON", "GARCIA", "LOPEZ", "SOTO",
                "MARTIN", "DUPONT", "IVANOV", "SMITH"]

    def mesure(couper):
        alea = random.Random(3)
        seaux = collections.Counter()
        total = 0
        # Les clés d'ÉQUIVALENCE sont coupées dans les deux mesures : elles
        # portent déjà sur le dernier mot, et leur présence brouillerait ce que
        # ce test isole — la répartition des clés PHONÉTIQUES.
        actifs = {c for c, on in
                  caps.defaults_for_channel(caps.CHANNEL_SCREENING).items() if on}
        actifs = actifs - {caps.CAP_BLOCKING_EQUIVALENCES}
        if couper:
            actifs = actifs - {caps.CAP_BLOCKING_PHONETIC_LAST}
        contexte = caps.use_context(caps.CHANNEL_SCREENING, actifs)
        contexte.__enter__()
        try:
            for _ in range(4000):
                fiche = _listee(f"{alea.choice(prenoms)} {alea.choice(familles)}")
                cles = generate_blocking_keys(fiche, LAYOUT)
                total += len(cles)
                seaux.update(cles)
        finally:
            contexte.__exit__(None, None, None)
        return total / 4000, len(seaux), max(seaux.values())

    cles_sans, seaux_sans, gros_sans = mesure(couper=True)
    cles_avec, seaux_avec, gros_avec = mesure(couper=False)

    assert cles_avec > 1.8 * cles_sans, "la clé doit bien être émise"
    assert seaux_avec >= 1.8 * seaux_sans, (
        f"les clés doivent se RÉPARTIR : {seaux_sans} → {seaux_avec} seaux")
    assert gros_avec <= 1.25 * gros_sans, (
        f"le plus gros seau enfle trop : {gros_sans} → {gros_avec}")


def test_la_capacite_annonce_ce_qu_elle_coute_et_ce_qu_elle_rapporte():
    """
    Une capacité dont la perte n'est pas décrite est un interrupteur qu'on
    n'ose pas toucher. Celle-ci porte les deux chiffres mesurés.
    """
    perte = caps.CAPABILITY_CATALOG[caps.CAP_BLOCKING_PHONETIC_LAST].loss
    assert "0,8 %" in perte and "12,7 %" in perte, (
        "la perte doit citer les DEUX configurations mesurées : sans les "
        "tables linguistiques, et avec")
    assert "+32 %" in perte and "8,7 %" in perte, (
        "elle doit aussi citer ce qu'elle COÛTE, mesuré à l'échelle : sans "
        "cela l'exploitant n'a qu'un seul plateau de la balance")
    assert caps.CAPABILITY_CATALOG[caps.CAP_BLOCKING_PHONETIC_LAST].depends_on == (
        caps.CAP_BLOCKING_PHONETIC,)


def test_elle_est_active_par_defaut():
    """
    Le défaut doit être le plus sûr : ne pas comparer une paire est un défaut
    de conformité, comparer trop est un coût.
    """
    assert caps.defaults_for_channel(caps.CHANNEL_SCREENING)[caps.CAP_BLOCKING_PHONETIC_LAST]
    assert caps.defaults_for_channel(caps.CHANNEL_FILTERING)[caps.CAP_BLOCKING_PHONETIC_LAST]

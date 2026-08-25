"""
Un listé écrit en han ou en hangul doit pouvoir rencontrer son client latin.

anyascii rend un fragment **capitalisé par signe** : « 习近平 » donnait
`XiJinPing`, « 김정은 » `GimJeongEun`. Les frontières de mots, absentes de la
source, étaient donc bien présentes dans le résultat — mais sous forme de
majuscules, pas d'espaces. La clé phonétique étant bâtie sur le premier mot,
`XIJINPING` ne pouvait jamais rencontrer `XI`.

Ce que cela coûtait, mesuré avant correction : « 习近平 » face au client
« Xi Jinping » obtenait **89,5** de score — largement au-dessus du seuil de 75.
Mais la paire n'était jamais rapprochée, donc jamais scorée. Un listé déclaré
non listé sur un nom que le moteur aurait reconnu s'il avait pu le regarder.

Les espaces sont rétablis à la translittération, signe par signe, et seulement
pour les écritures qui n'en écrivent pas. La décision se prend sur la SOURCE :
un « McDonald » latin ou un « Vladimir » cyrillique, qui sortent aussi
capitalisés, ne passent jamais par cette branche.
"""
import pytest

from fiskr.capabilities import SCRIPT_HAN, SCRIPT_HANGUL
from fiskr.config import config
from fiskr.quality import _ECRITURES_SANS_ESPACE, strip_accents, strip_accents_for_matching
from fiskr.scoring import compute_base_score
from fiskr.settings import blocking_config_for
from fiskr.blocking import generate_blocking_keys

SEUIL = 75.0


def _cfg():
    cfg = dict(config)
    cfg["blocking"] = blocking_config_for(
        ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"], "SCREENING")
    return cfg


def _se_rencontrent(nom_liste, prenom, nom):
    cfg = _cfg()
    parts = nom_liste.split() or [nom_liste]
    listee = {"primary_name": nom_liste, "entity_type": "I",
              "individual_name_parsed": {"first_name": parts[0], "last_name": parts[-1]},
              "countries": {"citizenship": ["CN"]}, "aliases": {}}
    client = {"client_id": "C", "client_type": "PP", "primary_name": f"{prenom} {nom}",
              "client_first_name": prenom, "client_last_name": nom,
              "client_countries": {"nationality": ["CN"]}}
    return bool(generate_blocking_keys(listee, cfg) & generate_blocking_keys(client, cfg))


def _score(nom_liste, client):
    return compute_base_score(strip_accents_for_matching(nom_liste).upper(),
                              strip_accents_for_matching(client).upper(), _cfg())


# --------------------------------------------------- ce que la règle rétablit

@pytest.mark.parametrize("source, attendu", [
    ("习近平", "Xi Jin Ping"),
    ("毛泽东", "Mao Ze Dong"),
    ("김정은", "Gim Jeong Eun"),
    ("東京", "Dong Jing"),
])
def test_une_ecriture_sans_espace_rend_ses_mots(source, attendu):
    assert strip_accents_for_matching(source) == attendu


@pytest.mark.parametrize("source, attendu", [
    ("Владимир", "Vladimir"),        # cyrillique : un mot, capitalisé
    ("Γεώργιος", "Georgios"),        # grec : idem
    ("McDonald", "McDonald"),        # latin : capitale au milieu, légitime
    ("MacArthur", "MacArthur"),
    ("ACME S.A.", "ACME S.A."),
    ("陈 Владимир", "Chen Vladimir"),  # un seul signe han : rien à délimiter
])
def test_rien_d_autre_n_est_coupe(source, attendu):
    """
    La coupure se décide sur la SOURCE, jamais sur la forme du résultat : un
    nom latin ou cyrillique capitalisé au milieu ne passe pas par cette
    branche. Couper « McDonald » en « Mc Donald » créerait un faux jeton et
    déplacerait la clé phonétique — le remède serait pire que le mal.
    """
    assert strip_accents_for_matching(source) == attendu


@pytest.mark.parametrize("nom_liste, prenom, nom", [
    ("习近平", "Xi", "Jinping"),
    ("毛泽东", "Mao", "Zedong"),
    ("李克强", "Li", "Keqiang"),
    ("김정은", "Kim", "Jong Un"),
])
def test_le_liste_et_son_client_latin_se_rencontrent(nom_liste, prenom, nom):
    """Le cœur du défaut : sans rencontre, aucun score n'est jamais calculé."""
    assert _se_rencontrent(nom_liste, prenom, nom), (
        f"« {nom_liste} » et « {prenom} {nom} » ne sont jamais rapprochés")


@pytest.mark.parametrize("nom_liste, client", [
    ("习近平", "Xi Jinping"),
    ("毛泽东", "Mao Zedong"),
    ("李克强", "Li Keqiang"),
])
def test_et_le_score_franchit_le_seuil(nom_liste, client):
    assert _score(nom_liste, client) >= SEUIL


# ------------------------------------------------- une seule règle, deux voies

@pytest.mark.parametrize("texte", [
    "Владимир Путин", "习近平", "Müller", "김정은", "ACME S.A.",
    "محمد بن سلمان", "陈 Quanguo", "陈 Владимир",
])
def test_les_deux_voies_de_normalisation_s_accordent(texte):
    """
    `strip_accents` bat l'index des équivalences, `strip_accents_for_matching`
    compare. Toutes capacités actives, les deux doivent rendre la même chose —
    sinon une équivalence déclarée en han cesserait d'être trouvée par le
    criblage qui, lui, délimite.
    """
    assert strip_accents_for_matching(texte) == strip_accents(texte)


def test_l_ensemble_des_ecritures_concernees_est_explicite():
    """
    Seules les écritures qui n'écrivent PAS les espaces sont concernées. Y
    ajouter le cyrillique ou le grec, alphabétiques, couperait des mots
    légitimes.
    """
    assert _ECRITURES_SANS_ESPACE == frozenset({SCRIPT_HAN, SCRIPT_HANGUL})


# --------------------------------------------- la voie rapide reste intacte

def test_l_ascii_traverse_toujours_inchange():
    """
    Le chemin le plus chaud du moteur : deux appels par comparaison, sur un
    univers entier de candidats. 98,3 % des noms listés sont ASCII purs.
    """
    for texte in ("JEAN DUPONT", "ACME LTD", "O'BRIEN", "MC DONALD"):
        # `is` et non `==` : la voie rapide rend l'objet REÇU, sans copie ni
        # détour par le cache. C'est ce qui la rend rapide.
        assert strip_accents_for_matching(texte) is texte

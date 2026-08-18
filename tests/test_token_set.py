"""
Similarité par ensembles de tokens : le cas que le moteur ne voyait pas.

`token_sort` corrige l'ORDRE des tokens, pas leur INCLUSION. Or une liste de
sanctions porte des noms longs — patronyme russe, double nom espagnol,
filiation arabe — là où le référentiel client n'en garde qu'une partie.

Mesuré sur le moteur, SANS donnée contextuelle (ni date de naissance, ni genre,
ni pays — cas fréquent des fiches listées, où la décision repose alors sur le
seul nom) :

    Vladimir Putin      vs Vladimir Vladimirovich Putin      60,6
    Igor Sechin         vs Sechin Igor Ivanovich             63,2
    Maria Carmen Lopez  vs Maria del Carmen Lopez Hernandez  63,9

Tous sous le seuil de 75 : la même personne n'était pas alertée. Avec date de
naissance et nationalité concordantes, les ajustements les rattrapent — c'est
bien l'absence de contexte qui isole le nom.

Balayage du poids sur 15 paires (8 mêmes personnes, 7 personnes différentes) :

    poids 0,0 -> 1/8 détectées, 4/7 autres alertées
    poids 0,4 -> 7/8 détectées, 4/7 autres alertées

Le poids par défaut reste NUL : ajouter une métrique déplacerait tous les
scores, donc les seuils calibrés et les cahiers de tests déjà homologués.
"""
import pytest

from fiskr.scoring import compute_base_score, token_set_similarity

POIDS_ACTUELS = {"jaro_winkler": 0.4, "damerau_levenshtein": 0.4, "token_sort": 0.2}
POIDS_AVEC_TSET = {"jaro_winkler": 0.2, "damerau_levenshtein": 0.2,
                   "token_sort": 0.2, "token_set": 0.4}


def _score(a, b, poids):
    return compute_base_score(a, b, {"scoring": {"weights": poids}})


# ------------------ SÉMANTIQUE DE LA MÉTRIQUE ------------------

def test_a_name_fully_contained_in_the_other_scores_100():
    """Le cœur : un nom entièrement contenu dans l'autre. C'est la relation
    qu'une liste de sanctions entretient avec un référentiel client."""
    assert token_set_similarity("VLADIMIR PUTIN",
                                "VLADIMIR VLADIMIROVICH PUTIN") == 100.0
    assert token_set_similarity("MARIA CARMEN LOPEZ",
                                "MARIA DEL CARMEN LOPEZ HERNANDEZ") == 100.0


def test_token_order_does_not_matter():
    """L'inversion nom/prénom, courante entre une liste et un référentiel."""
    assert token_set_similarity("IGOR SECHIN", "SECHIN IGOR") == 100.0


def test_disjoint_names_do_not_score_high():
    """Aucun token commun : la métrique ne doit rien inventer."""
    assert token_set_similarity("PIERRE DURAND", "SOCIETE GENERALE") < 60.0


def test_shared_token_alone_is_not_a_match():
    """Un prénom commun ne fait pas une personne. Sinon tous les « Ali » de la
    liste seraient candidats à 100."""
    score = token_set_similarity("ALI HASSAN", "ALI HUSSEIN")
    assert score < 100.0, "un seul token commun ne peut pas valoir identité"


def test_empty_input_is_not_an_error():
    assert token_set_similarity("", "VLADIMIR PUTIN") == 0.0
    assert token_set_similarity("VLADIMIR PUTIN", "") == 0.0


# ------------------ NEUTRALITÉ PAR DÉFAUT ------------------

def test_default_weight_is_zero_and_scores_are_unchanged():
    """LA garantie : sans réglage explicite, aucun score ne bouge. Un moteur de
    conformité ne change pas de comportement à l'occasion d'une mise à jour —
    les seuils, les règles anti-faux positifs et les cahiers de tests déjà
    homologués reposent sur les scores actuels."""
    paires = [("Vladimir Putin", "Vladimir Vladimirovich Putin"),
              ("Jean Martin", "Jean Martinez"),
              ("Kim Jong Un", "Kim Jong-un"),
              ("Pierre Durand", "Societe Generale")]
    for a, b in paires:
        avec_defaut = compute_base_score(a, b, {"scoring": {"weights": {}}})
        explicite = _score(a, b, POIDS_ACTUELS)
        assert avec_defaut == pytest.approx(explicite, abs=1e-9), (
            f"le score de « {a} » vs « {b} » a bougé sans réglage explicite")


def test_identical_strings_still_saturate_with_the_new_weight():
    """Le raccourci d'égalité doit compter la nouvelle métrique, sinon deux
    chaînes identiques marqueraient MOINS que deux chaînes proches."""
    identique = _score("VLADIMIR PUTIN", "VLADIMIR PUTIN", POIDS_AVEC_TSET)
    proche = _score("VLADIMIR PUTIN", "VLADIMIR PUTIM", POIDS_AVEC_TSET)
    assert identique == pytest.approx(100.0)
    assert identique > proche


# ------------------ EFFET MESURÉ ------------------

@pytest.mark.parametrize("client,liste", [
    ("Maria Carmen Lopez", "Maria del Carmen Lopez Hernandez"),
    ("Mohammed Salman", "Mohammed Bin Salman Al Saud"),
    ("Vladimir Putin", "Vladimir Vladimirovich Putin"),
    ("Igor Sechin", "Sechin Igor Ivanovich"),
    ("Ramzan Kadyrov", "Kadyrov Ramzan Akhmadovich"),
])
def test_activating_the_metric_recovers_the_missed_pairs(client, liste):
    """Ces paires — la même personne — passaient sous le seuil de 75."""
    avant = _score(client, liste, POIDS_ACTUELS)
    apres = _score(client, liste, POIDS_AVEC_TSET)
    assert avant < 75.0, f"la paire n'était pas manquée : {avant}"
    assert apres >= 75.0, f"toujours manquée après activation : {apres}"


@pytest.mark.parametrize("a,b", [
    ("Pierre Durand", "Societe Generale"),
    ("Marc Dubois", "Luc Bernard"),
])
def test_activating_the_metric_does_not_invent_matches(a, b):
    """…et n'invente pas de rapprochement entre noms sans rapport."""
    assert _score(a, b, POIDS_AVEC_TSET) < 75.0

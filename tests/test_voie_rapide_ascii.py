"""
Voie rapide ASCII de la normalisation de comparaison.

`strip_accents_for_matching` est le chemin le plus chaud du moteur : il est
emprunté deux fois par comparaison, sur un univers entier de candidats. Or un
texte purement ASCII le traverse INCHANGÉ — `detect_scripts` n'y trouve aucune
écriture non latine, et la décomposition NFKD d'ASCII est ASCII sans caractère
combinant. Mesure sur un échantillon réel de la production : 98,3 % des noms
listés sont ASCII purs.

Ces tests démontrent l'équivalence plutôt que de la supposer : le raccourci
d'un chemin de conformité doit être prouvé, pas constaté sur trois exemples.
"""
import unicodedata

import pytest

from fiskr import capabilities as caps
from fiskr.quality import (strip_accents, strip_accents_for_matching,
                           _strip_accents_for_matching_cached,
                           _strip_combining, has_non_latin_chars, detect_scripts)


def test_aucun_caractere_ascii_n_est_modifie_par_la_normalisation_complete():
    """La démonstration, exhaustive sur les 128 points de code ASCII."""
    fautifs = [
        (cp, repr(chr(cp))) for cp in range(128)
        if _strip_combining(chr(cp)) != chr(cp)
        or has_non_latin_chars(chr(cp))
        or detect_scripts(chr(cp))
    ]
    assert not fautifs, f"la voie rapide changerait le résultat sur {fautifs}"


@pytest.mark.parametrize("canal", list(caps.CHANNELS))
def test_la_voie_rapide_rend_exactement_le_chemin_complet(canal):
    """Sur des noms ASCII réels, les deux chemins doivent coïncider."""
    noms = [
        "MOHAMMED ALI", "Vladimir Putin", "JSC ROSNEFT OIL COMPANY",
        "O'BRIEN-SMITH", "MARIA DEL CARMEN LOPEZ", "AL-QADI, Yassin Abdullah",
        "Kim Jong Un", "ACME TRADING LLC", "x", "", "123 456",
        "DOE, JOHN (a.k.a. JOHNNY)", "SAINT-EXUPERY",
    ]
    contexte = caps.current_context(canal)
    for nom in noms:
        assert nom.isascii(), nom
        attendu = _strip_accents_for_matching_cached(nom, canal, contexte)
        assert strip_accents_for_matching(nom, canal) == attendu, nom


def test_les_textes_non_ascii_passent_toujours_par_le_chemin_complet():
    """La voie rapide ne doit rien court-circuiter de ce qui compte : accents,
    cyrillique, han, arabe traversent la normalisation entière."""
    assert strip_accents_for_matching("Müller", "SCREENING") == "Muller"
    for texte in ("Владимир", "习近平", "محمد", "Ibáñez"):
        assert not texte.isascii()
        rendu = strip_accents_for_matching(texte, "SCREENING")
        assert rendu != texte, f"{texte} n'a pas été normalisé"


def test_strip_accents_inconditionnel_a_la_meme_voie_rapide():
    for nom in ("MOHAMMED ALI", "JSC ROSNEFT", "O'BRIEN", ""):
        assert strip_accents(nom) == nom
    assert strip_accents("Müller") == "Muller"


def test_la_voie_rapide_ne_depend_d_aucune_capacite():
    """Le raccourci vaut TOUTES capacités coupées comme toutes actives : c'est
    ce qui autorise à le poser avant la lecture du contexte."""
    nom = "MOHAMMED ALI"
    for actives in ([], list(caps.capabilities_for_channel("SCREENING"))):
        with caps.use_context("SCREENING", actives):
            assert strip_accents_for_matching(nom, "SCREENING") == nom

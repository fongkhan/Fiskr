"""
Les dictionnaires de traduction, verifies EN ENTIER.

`test_intl.py` couvre l'i18n par sondage — « une entree cle », « un sondage sur
une entree ». Un sondage ne peut par construction pas voir un trou ailleurs, ni
une cle ecrite deux fois. Or en JavaScript, une cle repetee dans un litteral
d'objet ne provoque aucune erreur : **la seconde ecrase silencieusement la
premiere**. La traduction soignee que quelqu'un a ecrite n'est jamais affichee,
et personne ne l'apprend — c'est la meme classe de defaut que le reglage qu'on
peut enregistrer et qui n'agit pas.

Ces tests derivent la verification du fichier lui-meme, entree par entree.
"""

import json
import os
import re

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")
LANGUES_CIBLES = ("en", "de", "es", "zh", "ar")


def _source():
    with open(os.path.join(STATIC, "i18n.js"), encoding="utf-8") as f:
        return f.read()


def _bloc(src, nom, ouvre="{", ferme="}"):
    """Extrait le litteral `const <nom> = <ouvre> ... <ferme>` par comptage."""
    debut = src.index(f"const {nom} = {ouvre}")
    j = src.index(ouvre, debut)
    profondeur = 0
    for k in range(j, len(src)):
        if src[k] == ouvre:
            profondeur += 1
        elif src[k] == ferme:
            profondeur -= 1
            if profondeur == 0:
                return src[j:k + 1]
    raise AssertionError(f"litteral `{nom}` non termine")


def _en_json(txt):
    """Le litteral est du JSON a trois details pres : cles de langue non
    guillemetees, virgules finales tolerees, commentaires de ligne."""
    txt = re.sub(r"^\s*//.*$", "", txt, flags=re.M)
    txt = re.sub(r'([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)\s*:', r'\1"\2":', txt)
    txt = re.sub(r",(\s*[}\]])", r"\1", txt)
    return json.loads(txt)


def _dictionnaire(nom):
    bloc = _bloc(_source(), nom)
    return bloc, _en_json(bloc)


def _cles_ecrites(bloc):
    """Les cles telles qu'ECRITES, doublons compris — `json.loads` les fusionne
    exactement comme le fait JavaScript, donc les compter apres coup ne
    montrerait jamais le probleme."""
    return re.findall(r'^\s*"((?:[^"\\]|\\.)*)":\s*\{', bloc, re.M)


# --------------------------------------------------------- T (etiquettes) et P

def test_aucune_cle_de_traduction_ecrite_deux_fois():
    for nom in ("T", "P"):
        bloc, dico = _dictionnaire(nom)
        ecrites = _cles_ecrites(bloc)
        doublons = sorted({k for k in ecrites if ecrites.count(k) > 1})
        assert not doublons, (
            f"Dictionnaire {nom} : cle(s) ecrite(s) deux fois. En JavaScript la "
            f"seconde ecrase la premiere sans un mot, donc l'une des deux "
            f"traductions n'est jamais affichee :\n  " + "\n  ".join(doublons))
        assert len(ecrites) == len(dico), (
            f"{nom} : {len(ecrites)} cles ecrites pour {len(dico)} retenues")


def test_chaque_entree_porte_les_cinq_langues():
    for nom in ("T", "P"):
        _, dico = _dictionnaire(nom)
        trous = []
        for cle, valeurs in dico.items():
            manque = [l for l in LANGUES_CIBLES
                      if not isinstance(valeurs.get(l), str) or not valeurs[l].strip()]
            if manque:
                trous.append(f"{nom} [{','.join(manque)}] {cle[:70]}")
        assert not trous, (
            "Entrees sans traduction — l'utilisateur qui a choisi cette langue "
            f"voit du francais au milieu de sa page :\n  " + "\n  ".join(trous[:20]))


def test_le_corpus_verifie_est_reel():
    """Si l'extraction se cassait, les tests ci-dessus passeraient sur un
    dictionnaire vide sans rien verifier."""
    _, t = _dictionnaire("T")
    _, p = _dictionnaire("P")
    assert len(t) >= 500, f"dictionnaire T suspect : {len(t)} entrees"
    assert len(p) >= 80, f"dictionnaire P suspect : {len(p)} entrees"


# ------------------------------------------------------ R (chaines composees)

def _regles():
    bloc = _bloc(_source(), "R", "[", "]")
    return re.findall(r"\[\s*/(.+?)/,\s*\{(.*?)\}\s*\]", bloc, re.S)


def test_chaque_regle_composee_porte_les_cinq_langues():
    regles = _regles()
    assert len(regles) >= 8, f"regles R suspectes : {len(regles)}"
    trous = []
    for motif, trads in regles:
        presents = set(re.findall(r'\b(en|de|es|zh|ar):', trads))
        manque = [l for l in LANGUES_CIBLES if l not in presents]
        if manque:
            trous.append(f"[{','.join(manque)}] /{motif[:60]}/")
    assert not trous, "Regles composees incompletes :\n  " + "\n  ".join(trous)


def test_aucune_reference_de_groupe_hors_du_motif():
    """
    Une traduction composee reinjecte les groupes captures (`$1`, `$2`). Un `$3`
    dans une traduction dont le motif ne capture que deux groupes n'est pas une
    erreur JavaScript : c'est un « $3 » litteral affiche a l'utilisateur, dans
    cette langue-la seulement.
    """
    fautes = []
    for motif, trads in _regles():
        groupes = len(re.findall(r"\((?!\?)", motif))
        for lang, texte in re.findall(r'\b(en|de|es|zh|ar):\s*"((?:[^"\\]|\\.)*)"', trads):
            for ref in (int(n) for n in re.findall(r"\$(\d+)", texte)):
                if ref > groupes:
                    fautes.append(f"[{lang}] ${ref} pour {groupes} groupe(s) : /{motif[:50]}/")
    assert not fautes, "References de groupe hors motif :\n  " + "\n  ".join(fautes)

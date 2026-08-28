"""
Compte les fonctions de test du depot, et remet la phrase du README d'aplomb.

Pourquoi cet outil existe
-------------------------
Le README annonce la taille de la suite (« **N fonctions de test** reparties
sur M fichiers »). Une garde verifie que ce nombre est juste — elle a fait son
travail : elle a echoue a chacun des lots livres, parce qu'un lot ajoute des
tests. Mais elle se contentait de signaler l'ecart. Le nombre exact, il fallait
aller le reecrire a la main, a chaque fois. Un geste dont la seule issue
possible est la faute de frappe : recopier un nombre a quatre chiffres.

Le comptage vit donc ICI, et la garde de `tests/test_documentation_exacte.py`
l'importe. Une seule source de verite : l'outil qui corrige et la garde qui
verifie ne peuvent pas diverger, puisqu'ils comptent avec le meme code.

    python tools/compte_de_tests.py             # affiche le compte et l'ecart
    python tools/compte_de_tests.py --corriger  # reecrit la phrase du README

Ce que l'outil NE fait PAS
--------------------------
Il ne touche a rien d'autre que cette phrase, et il refuse de recrire un
compte manifestement faux (moins de 500 fonctions : l'analyse a rate quelque
chose). Mieux vaut un README perime qu'un README qui affirme un chiffre absurde
avec l'autorite d'un outil.
"""
import argparse
import ast
import os
import re
import sys

DEPOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# En dessous, le comptage est suspect : la suite n'a jamais ete aussi petite
# depuis longtemps, et un chiffre absurde ecrit par un outil se croit.
PLANCHER_VRAISEMBLABLE = 500

# La phrase du README, telle qu'elle est ecrite. Les blancs internes sont
# CAPTURES et rendus tels quels : reecrire la phrase corrige deux nombres,
# elle ne doit ni reflower le paragraphe ni imposer une coupure de ligne.
# Le separateur de milliers est laisse ouvert a la lecture (espace fine,
# insecable ou rien) : le README existant reste valide tel qu'il est ecrit.
MOTIF_PHRASE = re.compile(
    "\\*\\*(?P<n>[\\d\u202f\u00a0 ]+) fonctions de test\\*\\*"
    "(?P<b1>\\s+)r\u00e9parties(?P<b2>\\s+)sur(?P<b3>\\s+)(?P<m>\\d+)(?P<b4>\\s+)fichiers")

SEPARATEUR = "\u202f"  # espace fine insecable : la typographie deja en place


def compter(dossier_tests=None):
    """
    (fonctions, fichiers) : les `def test_*` de `tests/test_*.py`.

    Compte les definitions, pas les cas executes — un test parametre compte
    pour une fonction. C'est ce que la phrase du README annonce, et les deux
    doivent parler de la meme chose.
    """
    dossier = dossier_tests or os.path.join(DEPOT, "tests")
    fonctions, fichiers = 0, 0
    for nom in sorted(os.listdir(dossier)):
        if not (nom.startswith("test_") and nom.endswith(".py")):
            continue
        fichiers += 1
        with open(os.path.join(dossier, nom), encoding="utf-8") as f:
            arbre = ast.parse(f.read())
        fonctions += sum(
            1 for n in ast.walk(arbre)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name.startswith("test_"))
    return fonctions, fichiers


def _entier(ecrit):
    """Le nombre derriere son ecriture : « 1 699 », « 1 699 », « 1699 » -> 1699."""
    return int(re.sub(r"\D", "", ecrit) or 0)


def phrase(fonctions, fichiers):
    """Les deux nombres, ecrits comme le README les ecrit."""
    return (f"**{fonctions:,} fonctions de test**".replace(",", SEPARATEUR),
            f"{fichiers} fichiers")


def texte_corrige(texte, fonctions, fichiers):
    """
    Le README avec la phrase remise d'aplomb, et (True/False) s'il a change.

    Les blancs d'origine sont rendus intacts : la correction porte sur deux
    nombres, pas sur la mise en page du paragraphe.
    """
    nombre, _ = phrase(fonctions, fichiers)

    deja = MOTIF_PHRASE.search(texte)
    if deja and _entier(deja.group("n")) == fonctions and int(deja.group("m")) == fichiers:
        # Les deux nombres sont deja justes : on ne touche a rien. Normaliser
        # le separateur de milliers au passage ferait de cet outil une source
        # de diff gratuits, et surtout le ferait diverger de la garde, qui
        # accepte les trois typographies. Corriger, pas uniformiser.
        return texte, False

    def _reecrire(m):
        return (nombre + m.group("b1") + "réparties" + m.group("b2") + "sur"
                + m.group("b3") + str(fichiers) + m.group("b4") + "fichiers")

    nouveau, remplacements = MOTIF_PHRASE.subn(_reecrire, texte)
    if not remplacements:
        raise SystemExit(
            "Phrase introuvable dans le README : elle a change de forme. "
            "Corrigez a la main, puis ajustez MOTIF_PHRASE ici.")
    return nouveau, nouveau != texte


def corriger_readme(chemin, fonctions, fichiers):
    """Reecrit la phrase. Retourne True si le fichier a change."""
    with open(chemin, encoding="utf-8") as f:
        texte = f.read()
    nouveau, change = texte_corrige(texte, fonctions, fichiers)
    if change:
        with open(chemin, "w", encoding="utf-8") as f:
            f.write(nouveau)
    return change


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("--corriger", action="store_true",
                        help="reecrit la phrase du README au lieu de la verifier")
    args = parser.parse_args(argv)

    fonctions, fichiers = compter()
    if fonctions < PLANCHER_VRAISEMBLABLE:
        raise SystemExit(
            f"Comptage suspect : {fonctions} fonction(s) trouvee(s). "
            f"Rien n'est ecrit.")

    readme = os.path.join(DEPOT, "README.md")
    if not args.corriger:
        with open(readme, encoding="utf-8") as f:
            _, ecart = texte_corrige(f.read(), fonctions, fichiers)
        deja_juste = not ecart
        print(f"{fonctions} fonctions de test reparties sur {fichiers} fichiers.")
        print("README a jour." if deja_juste
              else "README perime — relancez avec --corriger.")
        return 0 if deja_juste else 1

    change = corriger_readme(readme, fonctions, fichiers)
    print(f"README {'mis a jour' if change else 'deja juste'} : "
          f"{fonctions} fonctions, {fichiers} fichiers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

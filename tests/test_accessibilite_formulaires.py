"""
Chaque champ de formulaire doit porter un nom accessible.

Un `<select>` ou un `<input>` sans nom est annoncé « liste déroulante » ou
« champ de saisie », sans plus, par un lecteur d'écran : l'utilisateur entend
qu'il y a un contrôle, pas ce qu'il fait. Sur un produit de conformité manipulé
toute la journée par des analystes, c'est un obstacle réel — et une exigence
d'accessibilité (WCAG 2.1, critère 4.1.2 « Nom, rôle, valeur »).

Trois formes valent nom accessible, et l'écran les emploie toutes les trois :

* un `<label for="…">` qui pointe le champ ;
* un `<label>` **englobant** (le motif le plus fréquent ici, pour les cases à
  cocher et les listes précédées de leur intitulé) ;
* un `aria-label` ou un `placeholder` explicite.

Dix champs n'en avaient aucune — surtout des listes de filtre posées seules dans
une barre d'outils, et les sélecteurs de fichier. Ce test empêche le prochain
d'arriver muet.
"""
import re
from pathlib import Path

import pytest

INDEX = (Path(__file__).resolve().parent.parent / "fiskr" / "static"
         / "index.html").read_text(encoding="utf-8")

# Un champ sans nom propre mais dont le rôle est porté par le contexte
_SANS_NOM_ADMIS = {"type": ("hidden", "submit", "button")}


def _champs_sans_nom():
    cibles_de_label = set(re.findall(r'<label[^>]*\bfor="([^"]+)"', INDEX))
    englobants = [(m.start(), m.end())
                  for m in re.finditer(r"<label\b.*?</label>", INDEX, re.S)]

    sans_nom = []
    for champ in re.finditer(r"<(input|select|textarea)\b([^>]*)>", INDEX):
        balise, attributs = champ.group(1), champ.group(2)
        if re.search(r'type="(%s)"' % "|".join(_SANS_NOM_ADMIS["type"]), attributs):
            continue
        if any(a in attributs for a in ("aria-label", "placeholder", "title=")):
            continue
        identifiant = re.search(r'id="([^"]+)"', attributs)
        if identifiant and identifiant.group(1) in cibles_de_label:
            continue
        if any(debut <= champ.start() <= fin for debut, fin in englobants):
            continue
        sans_nom.append(identifiant.group(1) if identifiant else f"<{balise}> sans id")
    return sans_nom


def test_la_detection_voit_bien_les_champs():
    """Sans cette garde, une regex cassée rendrait le test vert à vide."""
    champs = re.findall(r"<(input|select|textarea)\b", INDEX)
    assert len(champs) >= 100, f"seulement {len(champs)} champs détectés"


def test_aucun_champ_n_est_muet_pour_un_lecteur_d_ecran():
    muets = _champs_sans_nom()
    assert not muets, (
        "champs sans nom accessible — un lecteur d'écran n'annonce que leur "
        f"rôle : {sorted(muets)}")


@pytest.mark.parametrize("identifiant", [
    "history-decision-filter", "history-type-filter", "batch-file-input",
    "screening-bulk-priority", "filtering-bulk-priority", "workload-channel",
    "checklist-items", "apikey-role", "config-import-file",
    "profile-avatar-input",
])
def test_les_champs_corriges_gardent_leur_nom(identifiant):
    """Les dix qui étaient muets : chacun nommé, et nommé utilement."""
    champ = re.search(r'<(?:input|select|textarea)\b[^>]*id="'
                      + re.escape(identifiant) + r'"[^>]*>', INDEX)
    assert champ, f"{identifiant} a disparu de l'écran"
    libelle = re.search(r'aria-label="([^"]+)"', champ.group(0))
    assert libelle, f"{identifiant} n'a plus de nom accessible"
    assert len(libelle.group(1)) > 8, (
        f"{identifiant} : « {libelle.group(1)} » n'apprend rien de plus que le rôle")


def test_les_modales_annoncent_leur_role():
    """Complément du même critère : une modale doit se déclarer comme telle,
    sinon le lecteur d'écran la lit comme un bloc de page ordinaire."""
    app_js = (Path(__file__).resolve().parent.parent / "fiskr" / "static"
              / "app.js").read_text(encoding="utf-8")
    assert 'setAttribute("role", "dialog")' in app_js
    assert 'setAttribute("aria-modal", "true")' in app_js


def test_les_noms_accessibles_sont_traduits():
    """
    Le moteur d'internationalisation traduit explicitement les attributs
    `aria-label` : un nom accessible laissé en français est lu tel quel par un
    lecteur d'écran configuré en anglais, en allemand ou en arabe. Quatorze
    l'étaient — quatre d'origine, dix ajoutés en même temps que ce test.

    Seul « Langue / Language » reste hors dictionnaire : il est bilingue par
    construction, c'est le sélecteur de langue lui-même.
    """
    import json

    i18n = (Path(__file__).resolve().parent.parent / "fiskr" / "static"
            / "i18n.js").read_text(encoding="utf-8")
    labels = set(re.findall(r'aria-label="([^"]+)"', INDEX))
    assert len(labels) >= 20, "détection des aria-label cassée"
    non_traduits = [l for l in labels
                    if json.dumps(l, ensure_ascii=False) not in i18n
                    and l != "Langue / Language"]
    assert not non_traduits, f"noms accessibles non traduits : {sorted(non_traduits)}"

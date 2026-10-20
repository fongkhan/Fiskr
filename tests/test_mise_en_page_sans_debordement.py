"""
Aucun débordement horizontal de page — et des calques qu'on peut lire.

Mesuré dans Chromium (scrollWidth contre clientWidth, sur tous les écrans
principaux, à 390, 1 024 et 1 440 px). Avant : à 390 px, la file d'alertes
mesurait 1 187 px de large et la modale d'alerte était tronquée ; à 1 440 px,
l'écran Homologation débordait et « Examiner » faisait glisser le contenu sous
la barre latérale ; la colonne Actions de la liste des comptes était coupée.
Après : zéro débordement de page aux trois largeurs.

La cause tenait en une ligne répétée : vingt-deux blocs `.grid-layout`
portaient `style="grid-template-columns: 1fr"`. Une piste `1fr` nue ne descend
jamais sous la largeur minimale de son contenu — un tableau large gonflait la
colonne — et un style en ligne l'emporte sur toute media query : le correctif
`minmax(0, …)` de la feuille était annulé là où il comptait. La media query à
1 024 px refaisait d'ailleurs la même chose. Onze `.form-row` à `repeat(4, 1fr)`
en ligne empêchaient de même le repli à 768 px.

Ces tests sont statiques : la mesure demande un navigateur, la règle qui la
garantit se lit dans les fichiers.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "fiskr" / "static"
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")
CSS_SANS_COMMENTAIRES = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)


def _blocs():
    """(sélecteur, déclarations) de chaque règle, media queries comprises."""
    return [(m.group(1).strip(), m.group(2))
            for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", CSS_SANS_COMMENTAIRES)]


def test_aucune_grille_ne_fixe_ses_colonnes_en_style_en_ligne():
    """Un style en ligne gagne contre la media query : la grille ne se replie plus."""
    fautes = []
    for nom in ("index.html", "app.js", "login.html"):
        texte = (STATIC / nom).read_text(encoding="utf-8")
        for m in re.finditer(r'style="[^"]*grid-template-columns[^"]*"', texte):
            ligne = texte.count("\n", 0, m.start()) + 1
            fautes.append(f"{nom}:{ligne} {m.group(0)[:70]}")
    assert not fautes, ("grid-template-columns en ligne — passez par une classe "
                        "(.une-colonne, .colonnes-4…) :\n  " + "\n  ".join(fautes))


def test_aucune_piste_fr_nue_dans_les_grilles_de_mise_en_page():
    """Chaque piste fractionnaire passe par minmax(0, …) ou minmax(min(…, 100%), …)."""
    fautes = []
    for selecteur, corps in _blocs():
        if not re.search(r"\.(grid-layout|form-row|details-grid)\b", selecteur):
            continue
        for valeur in re.findall(r"grid-template-columns\s*:\s*([^;]+)", corps):
            reste = valeur
            # Retire les minmax(...) (imbrications min(...) comprises)
            while True:
                nouveau = re.sub(r"minmax\((?:[^()]|\([^()]*\))*\)", "", reste)
                if nouveau == reste:
                    break
                reste = nouveau
            if re.search(r"\d*\.?\d+fr\b", reste):
                fautes.append(f"{selecteur} → {valeur.strip()}")
    assert not fautes, "pistes `fr` nues, qui gonflent avec leur contenu :\n  " + "\n  ".join(fautes)


def test_les_variantes_de_grille_employees_sont_definies():
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for classe in ("une-colonne", "deux-colonnes", "grille-cartes",
                   "colonnes-3", "colonnes-4", "colonnes-1-2"):
        assert f" {classe}" in html, f"{classe} n'est plus employée"
        assert re.search(rf"\.{classe}\s*\{{", CSS), f".{classe} non définie"


def test_les_variantes_se_replient_aussi_en_petit_ecran():
    """Le sélecteur de la media query doit couvrir chaque variante : sans cela
    `.grid-layout.deux-colonnes` (deux classes) l'emporte sur `.grid-layout`."""
    a_1024 = CSS_SANS_COMMENTAIRES[CSS_SANS_COMMENTAIRES.index("@media (max-width: 1024px)"):]
    a_1024 = a_1024[:a_1024.index("@media", 10)]
    for variante in (".grid-layout.deux-colonnes", ".grid-layout.grille-cartes"):
        assert variante in a_1024
    a_768 = CSS_SANS_COMMENTAIRES[CSS_SANS_COMMENTAIRES.index("@media (max-width: 768px)"):]
    for variante in (".form-row.colonnes-3", ".form-row.colonnes-4", ".form-row.colonnes-1-2"):
        assert variante in a_768[:a_768.index("@media", 10)]


def test_les_petits_boutons_sont_petits():
    """`.btn-sm` était nommé par 147 boutons et défini nulle part : les deux
    boutons de la colonne Actions, à la taille des grands, la poussaient hors
    du cadre de la liste des comptes à 1 440 px."""
    assert re.search(r"\.btn\.btn-sm,\s*\.btn-sm\s*\{[^}]*padding", CSS_SANS_COMMENTAIRES)
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'class="data-table table-compact" id="users-table"' in html


# ------------------------------------------------------------- les calques

_FONDS_TRANSLUCIDES = re.compile(r"var\(--surface-(?:\d|hover)\)")


def test_un_calque_pose_sur_le_contenu_a_un_fond_opaque():
    """La bulle de visite guidée utilisait --surface-1 (2 % de blanc) : le texte
    de la barre latérale se lisait au travers. Même défaut sur le panneau des
    colonnes, la liste des combobox et les en-têtes collants (que la feuille
    disait opaques). Un élément positionné par-dessus le contenu prend un fond
    opaque ; un en-tête collant en déclare un."""
    fautes = []
    for selecteur, corps in _blocs():
        position = re.search(r"position\s*:\s*(absolute|fixed|sticky)", corps)
        if not position:
            continue
        fond = re.search(r"background(?:-color)?\s*:\s*([^;]+)", corps)
        if fond and _FONDS_TRANSLUCIDES.search(fond.group(1)):
            fautes.append(f"{selecteur} : {fond.group(1).strip()}")
        if position.group(1) == "sticky" and not fond:
            fautes.append(f"{selecteur} : collant sans fond (le contenu défile dessous)")
    assert not fautes, "calques translucides :\n  " + "\n  ".join(fautes)


def test_le_fond_des_calques_est_opaque_dans_les_deux_themes():
    for bloc in (r":root\s*\{", r':root\[data-theme="light"\]\s*\{'):
        debut = re.search(bloc, CSS).end()
        corps = CSS[debut: CSS.index("}", debut)]
        valeur = re.search(r"--modal-bg:\s*([^;]+);", corps).group(1).strip()
        assert re.fullmatch(r"#[0-9a-fA-F]{6}", valeur), f"--modal-bg doit être opaque : {valeur}"

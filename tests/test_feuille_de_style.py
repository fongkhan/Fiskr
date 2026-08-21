"""
La feuille de style, tenue contre le balisage qu'elle habille.

Deux defauts se cachent l'un derriere l'autre dans une feuille de style qui a
vecu.

**Une regle que plus personne ne nomme.** Le balisage evolue, la regle reste.
Elle ne fait aucun mal — sauf quand elle portait quelque chose que le nouveau
balisage n'a pas repris. C'est ce qui etait arrive au centre de notifications :
`.notif-item` tenait le padding, le curseur et le survol de lignes cliquables ;
le rendu est passe a des `<li>` nus, la regle est devenue orpheline, et les
lignes ont perdu tout signe qu'elles se cliquent — sans que rien ne casse.

**Une regle ecrasee par un style inline.** Elle est bien appliquee a un
element, mais chacune de ses proprietes est redeclaree dans l'attribut `style`
du meme element. On peut alors la modifier autant qu'on veut : il ne se passe
rien. C'est la version CSS du reglage qu'on enregistre et qui n'agit pas.
"""

import os
import re

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _css_sans_commentaires():
    return re.sub(r"/\*.*?\*/", "", _lire("styles.css"), flags=re.S)


def _balisage():
    """Tout ce qui peut nommer une classe : les pages et le JS qui les peuple."""
    return "".join(_lire(n) for n in ("index.html", "login.html", "app.js", "i18n.js"))


def test_aucune_classe_css_que_plus_rien_ne_nomme():
    css = _css_sans_commentaires()
    balisage = _balisage()
    orphelines = []
    for classe in sorted(set(re.findall(r"\.([a-zA-Z_][a-zA-Z0-9_-]*)", css))):
        if re.search(r"\b" + re.escape(classe) + r"\b", balisage):
            continue
        # Un nom peut etre COMPOSE a l'execution : `size-${w.size}`,
        # `toast toast-${type}`. On ne l'exige pas en toutes lettres, on exige
        # que le prefixe soit reellement fabrique quelque part.
        prefixe = classe.rsplit("-", 1)[0] if "-" in classe else None
        if prefixe and (f"{prefixe}-${{" in balisage or f'"{prefixe}-" +' in balisage):
            continue
        orphelines.append(classe)
    assert not orphelines, (
        "Classes CSS que ni le HTML ni le JS ne nomment. Verifiez d'abord ce "
        "que la regle portait : si c'etait un padding, un curseur ou un survol, "
        "le balisage actuel s'en est trouve appauvri sans que rien ne casse.\n  "
        + "\n  ".join("." + c for c in orphelines))


_DECLARATION = re.compile(r"([-a-zA-Z]+)\s*:\s*([^;]*)")


def _proprietes(corps, avec_important=True):
    trouvees = {}
    for prop, valeur in _DECLARATION.findall(corps):
        if not avec_important and "!important" in valeur:
            continue
        trouvees[prop.strip().lower()] = valeur
    return trouvees


def test_aucune_regle_d_identifiant_entierement_ecrasee_par_un_style_inline():
    """
    Portee aux selecteurs `#id` : un identifiant ne designe qu'un element, donc
    la conclusion est sans ambiguite. Les declarations `!important` sont
    exclues du calcul — elles, justement, gagnent contre l'inline (c'est ainsi
    que `.hidden { display: none !important }` continue de masquer un element
    qui porte un `display` inline).
    """
    css = _css_sans_commentaires()
    html = _lire("index.html")

    regles = {}
    for selecteur, corps in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        selecteur = selecteur.strip()
        if re.fullmatch(r"#[A-Za-z0-9_-]+", selecteur):
            props = _proprietes(corps, avec_important=False)
            if props:
                regles.setdefault(selecteur, set()).update(props)

    ecrasees = []
    for balise in re.findall(r"<[a-zA-Z][^>]*>", html):
        style = re.search(r'\sstyle="([^"]*)"', balise)
        ident = re.search(r'\sid="([^"]+)"', balise)
        if not style or not ident:
            continue
        inline = set(_proprietes(style.group(1)))
        declarees = regles.get("#" + ident.group(1))
        if declarees and declarees <= inline:
            ecrasees.append(
                f"#{ident.group(1)} : {len(declarees)} propriete(s) redeclarees "
                f"en inline ({', '.join(sorted(declarees)[:6])})")

    assert not ecrasees, (
        "Regles CSS entierement redeclarees dans l'attribut `style` du meme "
        "element : les modifier n'a aucun effet. Portez les valeurs dans la "
        "feuille et retirez le style inline.\n  " + "\n  ".join(sorted(set(ecrasees))))


def test_les_lignes_cliquables_du_centre_de_notifications_le_montrent():
    """
    Regression precise : les `<li>` de `#notif-list` portent un `onclick`. La
    preuve que le curseur etait attendu tient dans le rendu lui-meme — les
    lignes NON cliquables (« Rien a traiter. ») se donnent un
    `style="cursor: default;"`, ce qui n'a de sens que contre un
    `cursor: pointer` de base.
    """
    css = _css_sans_commentaires()
    corps = re.search(r"#notif-list li\s*\{([^{}]*)\}", css)
    assert corps, "regle `#notif-list li` introuvable"
    props = _proprietes(corps.group(1))
    for attendue in ("cursor", "padding"):
        assert attendue in props, f"`#notif-list li` sans {attendue}"
    assert "pointer" in props["cursor"], f"curseur inattendu : {props['cursor']}"
    assert re.search(r"#notif-list li:hover\s*\{[^{}]*background", css), \
        "aucun retour au survol sur une ligne cliquable"
    assert 'cursor: default' in _lire("app.js"), \
        "les lignes non cliquables ne neutralisent plus le curseur"


def test_la_feuille_est_syntaxiquement_equilibree():
    """Garde-fou des tests ci-dessus : ils analysent la feuille par expressions
    regulieres, donc une accolade perdue les rendrait aveugles."""
    css = _lire("styles.css")
    assert css.count("{") == css.count("}"), (
        f"{css.count('{')} accolades ouvrantes pour {css.count('}')} fermantes")
    assert len(re.findall(r"\{", css)) > 300, "feuille de style suspecte"

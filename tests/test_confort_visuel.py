"""
Lot « confort visuel » : thème système, nouveautés annoncées dans l'outil,
états vides qui indiquent le geste.

1. **Thème système** — deux préférences (sombre, clair) pour un poste dont le
   réglage change à la tombée du jour : l'analyste répétait le geste dans
   chaque application. Un troisième état suit `prefers-color-scheme`, en
   direct, y compris dans le script d'amorçage (pas de flash au chargement)
   et sur l'écran de connexion.
2. **Nouveautés** — le journal des modifications vit dans le dépôt ; personne
   en agence ne l'y lira. Un panneau dans l'outil, un point sur le bouton tant
   qu'un lot n'a pas été vu, et un contenu rédigé en français puis traduit
   comme n'importe quelle chaîne visible.
3. **États vides** — « Aucune alerte pour ce filtre » couvrait deux réalités
   opposées : la file à jour (bonne nouvelle) et le filtre trop étroit qui
   cache du travail (fausse bonne nouvelle). Le vide dit maintenant lequel,
   et les grands tableaux vides portent le geste qui les remplit.
"""
import json
import os
import re

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


# ------------------------------------------------------------- thème système

def test_le_script_d_amorcage_connait_la_preference_systeme():
    """Le thème s'applique avant le premier rendu par un script en tête de
    page. S'il ignore la préférence « system », l'utilisateur qui l'a choisie
    voit un flash sombre puis la bascule — sur chaque page, à chaque visite."""
    for page in ("index.html", "login.html"):
        src = _lire(page)
        amorce = src[:src.index("</head>")]
        assert 'localStorage.getItem("fiskr_theme")' in amorce, page
        assert '"system"' in amorce, f"{page} : l'amorçage ignore la préférence système"
        assert "prefers-color-scheme: light" in amorce, page


def test_le_cycle_du_bouton_couvre_les_trois_etats_sans_impasse():
    """Le clic parcourt sombre → clair → système → sombre. Une impasse dans la
    table (un état qui ne mène nulle part, ou qu'aucun autre n'atteint) rend
    un des trois états inaccessible au clic — pour toujours."""
    src = _lire("app.js")
    m = re.search(r"const suivant = \{([^}]*)\}", src)
    assert m, "table de succession du thème absente"
    table = dict(re.findall(r'(\w+):\s*"(\w+)"', m.group(1)))
    etats = {"dark", "light", "system"}
    assert set(table) == etats, f"états au départ : {set(table)}"
    assert set(table.values()) == etats, f"états atteints : {set(table.values())}"


def test_le_changement_de_reglage_de_l_os_s_applique_en_direct():
    """En préférence « système », l'OS qui bascule en sombre le soir doit
    emporter la page sans rechargement : c'est toute la promesse de l'état."""
    src = _lire("app.js")
    assert re.search(r'_mediaClair\.addEventListener\("change"', src), \
        "aucune écoute du changement de prefers-color-scheme"
    assert re.search(r'themePreference\(\) === "system"\) applyTheme\("system"\)', src), \
        "l'écoute n'applique pas le thème quand la préférence est « système »"


def test_chaque_etat_du_theme_a_son_icone_dans_le_sprite():
    """`uiIcon(name)` référence `#i-<name>` : une icône absente du sprite est
    un bouton vide, sans erreur nulle part."""
    app, page = _lire("app.js"), _lire("index.html")
    m = re.search(r"const icones = \{([^}]*)\}", app)
    assert m, "table des icônes du thème absente"
    for etat, icone in re.findall(r'(\w+):\s*"([\w-]+)"', m.group(1)):
        assert f'<symbol id="i-{icone}"' in page, f"icône i-{icone} ({etat}) absente du sprite"


def test_une_preference_inconnue_retombe_sur_le_sombre():
    """Un `fiskr_theme` corrompu (vieux format, faute de frappe d'une autre
    version) ne doit pas laisser la page dans un état indéfini."""
    src = _lire("app.js")
    assert re.search(r"if \(!_THEME_PREFERENCES\.includes\(pref\)\) pref = \"dark\";", src)
    assert re.search(r"_THEME_PREFERENCES\.includes\(memo\)\) return memo", src)


# ----------------------------------------------------------------- nouveautés

def _nouveautes():
    """Extrait le littéral FISKR_NOUVEAUTES et le lit comme du JSON (clés non
    guillemetées, virgules finales) — la même lecture indulgente que le
    moteur JavaScript, pour vérifier ce qui sera réellement affiché."""
    src = _lire("app.js")
    debut = src.index("const FISKR_NOUVEAUTES = [")
    j = src.index("[", debut)
    profondeur = 0
    for k in range(j, len(src)):
        if src[k] == "[":
            profondeur += 1
        elif src[k] == "]":
            profondeur -= 1
            if profondeur == 0:
                bloc = src[j:k + 1]
                break
    else:
        raise AssertionError("littéral FISKR_NOUVEAUTES non terminé")
    # Une clé JavaScript est suivie d'une VALEUR (guillemet, crochet, accolade).
    # Sans cette exigence, une phrase française comme « ..., vous : vos alertes »
    # se ferait prendre pour une clé et le littéral deviendrait illisible — le
    # test casserait sur du contenu parfaitement correct.
    bloc = re.sub(r'([{,]\s*)([a-z_][a-z0-9_]*)\s*:\s*(?=["\[{])', r'\1"\2": ', bloc)
    bloc = re.sub(r",(\s*[}\]])", r"\1", bloc)
    return json.loads(bloc)


def test_les_entrees_de_nouveautes_sont_bien_formees():
    entrees = _nouveautes()
    assert len(entrees) >= 4, "au moins un lot livré par entrée du programme"
    ids = [e["id"] for e in entrees]
    assert len(ids) == len(set(ids)), f"ids en double : {ids}"
    dates = [e["date"] for e in entrees]
    for d in dates:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d), f"date illisible : {d}"
    assert dates == sorted(dates, reverse=True), \
        "les entrées doivent aller du plus récent au plus ancien — c'est ce que la page promet"
    for e in entrees:
        assert e["titre"].strip(), e["id"]
        assert e["points"] and all(p.strip() for p in e["points"]), e["id"]


def test_le_point_ne_s_eteint_qu_a_l_ouverture_du_panneau():
    """« Vu » veut dire « le panneau a été ouvert », pas « la page a chargé » :
    si l'init marquait vu, le point ne s'afficherait jamais nulle part."""
    src = _lire("app.js")
    ouvrir = src[src.index("function ouvrirNouveautes"):]
    ouvrir = ouvrir[:ouvrir.index("\n}")]
    assert "localStorage.setItem(_NOUVEAUTES_CLE, FISKR_NOUVEAUTES[0].id)" in ouvrir
    init = src[src.index("function initNouveautes"):]
    init = init[:init.index("\n}")]
    assert "setItem" not in init, "l'init ne doit pas marquer les nouveautés comme vues"
    assert "_nouveautesNonVues()" in init


def test_le_bouton_la_modale_et_le_point_se_repondent():
    page, app = _lire("index.html"), _lire("app.js")
    assert re.search(r'id="nouveautes-btn"[^>]*onclick="ouvrirNouveautes\(\)"', page)
    assert re.search(r'id="nouveautes-btn"[^>]*aria-label="', page)
    for ident in ("nouveautes-modal", "nouveautes-liste", "nouveautes-point"):
        assert f'id="{ident}"' in page, ident
    assert "function ouvrirNouveautes" in app
    assert re.search(r"^ initNouveautes\(\);", app, re.M), \
        "initNouveautes doit être appelé au chargement, sinon le point ne s'affiche jamais"


def test_le_contenu_des_nouveautes_est_neutralise_au_rendu():
    src = _lire("app.js")
    ouvrir = src[src.index("function ouvrirNouveautes"):]
    ouvrir = ouvrir[:ouvrir.index("\n}")]
    assert "escapeHtml(n.titre)" in ouvrir
    assert "escapeHtml(p)" in ouvrir


def test_les_nouveautes_sont_traduites_comme_toute_chaine_visible():
    """Le contenu est rédigé en français et affiché dans une page qui se lit
    en six langues : chaque titre et chaque point doit être au dictionnaire.
    Une entrée ajoutée sans ses traductions casse ce test — c'est voulu."""
    dico = _lire("i18n.js")
    absents = []
    for e in _nouveautes():
        for texte in [e["titre"], *e["points"]]:
            if f'"{texte}"' not in dico:
                absents.append(texte[:70])
    assert not absents, "chaînes de nouveautés sans traduction :\n  " + "\n  ".join(absents)


# ---------------------------------------------------------------- états vides

def test_le_conseil_de_l_etat_vide_est_rendu_et_neutralise():
    src = _lire("app.js")
    fn = src[src.index("function tableEmpty"):]
    fn = fn[:fn.index("\n}")]
    assert 'class="empty-hint"' in fn
    assert "escapeHtml(conseil)" in fn
    assert re.search(r"conseil \? .* : \"\"", fn), \
        "sans conseil, aucun span vide ne doit être rendu"


def test_les_deux_vides_de_la_file_d_alertes_ne_se_ressemblent_plus():
    """« Aucune alerte pour ce filtre » quand aucun filtre n'est posé faisait
    lire une file à jour comme un écran mal réglé — et un filtre oublié comme
    une file à jour, la lecture la plus coûteuse d'un produit de conformité."""
    src = _lire("app.js")
    rendu = src[src.index("function renderAlertsTable"):]
    rendu = rendu[:rendu.index("\nfunction ", 10)]
    assert "Aucune alerte pour ce filtre." in rendu
    assert "File à jour : aucune alerte à instruire." in rendu
    assert "DEFAULT_ALERT_FILTER" in rendu, \
        "le choix de la phrase doit regarder les filtres réellement posés"
    for filtre in ("listFilter", "priorityFilter", "assigneeFilter"):
        assert filtre in rendu, f"le filtre {filtre} n'est pas consulté"


def test_les_conseils_des_etats_vides_sont_traduits():
    """Dérivé des appels eux-mêmes : tout littéral passé en cinquième argument
    de tableEmpty doit être au dictionnaire — pas de liste à maintenir ici."""
    src = _lire("app.js")
    dico = _lire("i18n.js")
    appels = re.findall(
        r'tableEmpty\([^;]*?uiIcon\("[\w-]+"\),\s*\n?\s*"((?:[^"\\]|\\.)+)"\);', src)
    assert len(appels) >= 6, f"appels avec conseil : {len(appels)} — l'extraction a-t-elle cassé ?"
    absents = [c[:70] for c in appels if f'"{c}"' not in dico]
    assert not absents, "conseils sans traduction :\n  " + "\n  ".join(absents)


def test_l_indice_et_le_point_sont_dans_la_feuille_de_style():
    css = _lire("styles.css")
    assert ".empty-state .empty-hint" in css
    assert ".nouveautes-point" in css
    assert "#nouveautes-btn { position: relative; }" in css, \
        "sans position:relative sur le bouton, le point se place contre la page entière"

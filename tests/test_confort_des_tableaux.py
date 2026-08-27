"""
Le confort des tableaux : choix des colonnes, densité, combobox cherchable.

1. **Colonnes** — le masquage passe par UNE feuille de style régénérée
   (`#table tr > *:nth-child(n)`) : les règles survivent à tous les re-rendus
   sans visiter une seule ligne, et l'export « tel qu'affiché » doit sortir
   les colonnes masquées du fichier comme elles sont sorties de l'écran —
   sinon son nom ment. Deux pièges gardés : tout masquer (tableau introuvable
   sans chemin de retour), et un identifiant forgé dans le stockage local qui
   entrerait dans la feuille de style.
2. **Densité** — un état global (confortable / compacte), persisté, appliqué
   par une classe sur body : les mêmes valeurs que la classe .table-compact
   existante, généralisées.
3. **Combobox** — le select d'origine reste la source de vérité : les options
   sont dérivées à CHAQUE ouverture (un annuaire repeuplé après coup est
   visible sans rien re-brancher), et la sélection repasse par select.value
   plus un évènement change — les onchange existants s'exécutent.
"""
import os
import re

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _bloc(src, marqueur):
    debut = src.index(marqueur)
    return src[debut:src.index("\n}", debut)]


# ----------------------------------------------------------------- colonnes

def test_le_masquage_survit_aux_re_rendus_par_la_feuille_de_style():
    src = _lire("app.js")
    fn = _bloc(src, "function _appliquerColonnes")
    assert "nth-child" in fn, "sans feuille de style, chaque re-rendu ferait réapparaître les colonnes"
    assert 'getElementById("regles-colonnes")' in fn
    assert "display: none" in fn


def test_un_identifiant_forge_n_entre_pas_dans_la_feuille_de_style():
    """localStorage est modifiable par n'importe quel script du poste : une
    clé « tableId » forgée deviendrait du CSS arbitraire dans la page."""
    fn = _bloc(_lire("app.js"), "function _appliquerColonnes")
    assert re.search(r"\^\[-\\w\]\+\$", fn), "aucun contrôle de forme sur l'identifiant de tableau"
    assert "Number.isInteger(idx)" in fn


def test_au_moins_une_colonne_reste_visible():
    fn = _bloc(_lire("app.js"), "function _basculerPanneauColonnes")
    assert re.search(r"liste\.size >= _entetesDeTable\(tableId\)\.length", fn), \
        "tout masquer rend le tableau introuvable, sans message ni retour évident"


def test_les_tables_filtrables_et_les_grandes_paginees_ont_le_choix():
    src = _lire("app.js")
    attach = _bloc(src, "function attachTableFilters")
    assert "attacherChoixDesColonnes(tableId)" in attach, \
        "toute table filtrable doit gagner le choix de ses colonnes au même endroit"
    for t in ("screening-alerts-table", "filtering-alerts-table", "watchlist-table", "audit-table"):
        assert f'"{t}"' in src, t


def test_l_export_tel_qu_affiche_respecte_les_colonnes_masquees():
    fn = _bloc(_lire("app.js"), "function exporterTableAffichee")
    assert "_colonnesMemorisees()" in fn and "cachees" in fn, \
        "un export « tel qu'affiché » qui sort les colonnes masquées ment sur son nom"


def test_le_libelle_de_repli_des_colonnes_sans_titre_est_traduit():
    """Les colonnes case-à-cocher et action n'ont pas d'en-tête texte : le
    panneau affiche « Colonne N », qui doit passer par une règle composée."""
    assert "`Colonne ${i + 1}`" in _lire("app.js")
    assert re.search(r"\[/\^Colonne \(\\d\+\)\$/", _lire("i18n.js")), \
        "règle composée « Colonne $1 » absente du dictionnaire"


# ------------------------------------------------------------------ densité

def test_la_densite_est_globale_persistee_et_annoncee():
    src = _lire("app.js")
    assert '"fiskr_densite"' in src
    assert 'classList.toggle("densite-compacte"' in src
    assert re.search(r"^ initDensite\(\);", src, re.M)
    page = _lire("index.html")
    assert re.search(r'id="densite-btn"[^>]*onclick="basculerDensite\(\)"', page)
    assert re.search(r'id="densite-btn"[^>]*aria-label="', page)
    css = _lire("styles.css")
    assert "body.densite-compacte th, body.densite-compacte td" in css
    dico = _lire("i18n.js")
    for cle in ("Densité confortable — cliquer pour l'affichage compact",
                "Densité compacte — cliquer pour l'affichage confortable"):
        assert f'"{cle}"' in dico, cle


# ----------------------------------------------------------------- combobox

def test_le_select_reste_la_source_de_verite():
    fn = _bloc(_lire("app.js"), "function activerCombobox")
    assert "Array.from(select.options)" in fn, "les options se dérivent du select, jamais recopiées"
    assert "select.value = li.dataset.valeur" in fn
    assert 'select.dispatchEvent(new Event("change"))' in fn, \
        "sans évènement change, les onchange existants (fetchAlerts…) ne partent plus"


def test_la_combobox_est_annoncee_aux_lecteurs_d_ecran():
    fn = _bloc(_lire("app.js"), "function activerCombobox")
    for aria in ('"role", "combobox"', '"aria-autocomplete", "list"', '"aria-expanded"',
                 '"aria-controls"', '"role", "listbox"', "aria-activedescendant"):
        assert aria in fn, f"attribut manquant : {aria}"


def test_le_filtre_ignore_les_accents():
    src = _lire("app.js")
    fn = _bloc(src, "function _texteSimplifie")
    assert 'normalize("NFD")' in fn and "u0300" in fn, \
        "« emond » doit trouver « Émond » — c'est la règle de la recherche globale"


def test_la_selection_passe_par_mousedown_pas_click():
    """click arrive APRÈS blur : le temps que le clic parte, la liste serait
    déjà fermée par la perte de focus et le clic tomberait dans le vide."""
    fn = _bloc(_lire("app.js"), "function activerCombobox")
    assert 'liste.addEventListener("mousedown"' in fn


def test_les_selects_longs_sont_marques():
    page = _lire("index.html")
    marques = re.findall(r'<select id="([^"]+)"[^>]*data-combobox', page)
    attendus = {"screening-assignee-filter", "filtering-assignee-filter",
                "whitelist-list-filter", "manual-list-type", "manual-batch-list-type"}
    assert attendus <= set(marques), f"selects marqués : {marques}"
    assert re.search(r"^ initComboboxes\(\);", _lire("app.js"), re.M)


def test_echap_referme_et_reaffiche_le_reel(ctx=None):
    fn = _bloc(_lire("app.js"), "function activerCombobox")
    assert '"Escape"' in fn and "libelleCourant()" in fn, \
        "un texte tapé qui n'est pas un choix ne vaut rien : Échap réaffiche la sélection réelle"

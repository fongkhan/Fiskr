"""
« Aucun résultat » et « le serveur n'a pas répondu » ne doivent pas se
ressembler.

Sur un produit de conformité, c'est une distinction de fond. Un analyste qui
voit un tableau vide en conclut qu'il n'y a rien à instruire ; si l'appel a en
réalité échoué, il vient de conclure faux — et rien ne le lui dit.

Trois formes existaient sur les écrans de liste, toutes mauvaises :

* **aucune vérification du code de retour** — la réponse d'erreur était lue
  comme une charge utile, `data.items` valait `undefined`, et le tableau
  s'affichait « aucun instantané » / « aucune décision » (instantanés, journal
  de criblage) ;
* **un `return` sec** — les lignes squelette du chargement restaient à
  l'écran, donc un tableau qui semble charger indéfiniment (file d'alertes,
  base des listés) ;
* **l'état VIDE réutilisé pour dire une erreur** — le bon texte, la mauvaise
  couleur, indistinguable au premier regard (charge de travail, rapport
  d'activité).

`tableError()` pose désormais un état d'erreur distinct — cadre rouge, icône
d'alerte, et ce qu'il faut faire — que `tableEmpty()` ne peut pas imiter.
"""
import re
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parent.parent
APP_JS = (RACINE / "fiskr" / "static" / "app.js").read_text(encoding="utf-8")
INDEX = (RACINE / "fiskr" / "static" / "index.html").read_text(encoding="utf-8")
CSS = (RACINE / "fiskr" / "static" / "styles.css").read_text(encoding="utf-8")


def test_l_aide_d_etat_d_erreur_existe_et_se_distingue():
    assert "function tableError(" in APP_JS
    # Une classe CSS a elle, pas celle de l'etat vide
    assert ".error-state {" in CSS
    bloc = re.search(r"function tableError\(.*?\n\}", APP_JS, re.S).group(0)
    assert 'class="error-state"' in bloc
    assert "empty-state" not in bloc


def test_l_etat_d_erreur_dit_quoi_faire():
    """Un message d'erreur qui n'indique pas la suite laisse l'utilisateur
    devant un mur."""
    bloc = re.search(r"function tableError\(.*?\n\}", APP_JS, re.S).group(0)
    assert "Réessayez" in bloc


def test_l_etat_d_erreur_echappe_son_message():
    """Le message peut venir d'un `detail` serveur : il est rendu en innerHTML."""
    bloc = re.search(r"function tableError\(.*?\n\}", APP_JS, re.S).group(0)
    assert "escapeHtml(message)" in bloc


@pytest.mark.parametrize("fonction,attendu", [
    ("fetchSnapshots", "#snapshots-table"),
    ("fetchAuditHistory", "#audit-table"),
    ("fetchWatchlist", "#watchlist-table"),
    ("fetchAlerts", "conf.table"),
])
def test_les_ecrans_de_liste_signalent_leurs_echecs(fonction, attendu):
    corps = re.search(r"async function " + fonction + r"\(.*?\n\}", APP_JS, re.S)
    assert corps, f"{fonction} introuvable"
    assert "tableError(" in corps.group(0), (
        f"{fonction} ne signale pas un échec de chargement")
    assert attendu in corps.group(0)


@pytest.mark.parametrize("fonction", ["fetchSnapshots", "fetchAuditHistory"])
def test_le_code_de_retour_est_verifie_avant_de_lire_la_charge_utile(fonction):
    """Le défaut d'origine : une réponse d'erreur lue comme une liste vide."""
    corps = re.search(r"async function " + fonction + r"\(.*?\n\}", APP_JS, re.S).group(0)
    position_ok = corps.find("response.ok")
    position_json = corps.find("await response.json()")
    assert position_ok != -1, f"{fonction} ne vérifie pas response.ok"
    assert position_ok < position_json, (
        f"{fonction} lit la charge utile avant d'avoir vérifié le code de retour")


def _colonnes_par_tableau():
    """Nombre de `<th>` de chaque tableau de l'écran.

    Le motif compte `<th ` et `<th>` — pas `<thead>`, qui commence par les
    mêmes trois lettres et gonflerait chaque tableau d'une colonne fantôme.
    """
    return {identifiant: len(re.findall(r"<th[ >]", entete))
            for identifiant, entete in
            re.findall(r'id="([a-z0-9-]+-table)"(.*?)</thead>', INDEX, re.S)}


def _colspans_annonces():
    """(ligne, tableau, colspan) de chaque état vide, d'erreur ou de
    chargement — que la cible soit un sélecteur ou la variable `tbody`."""
    lignes = APP_JS.split("\n")
    sortie = []
    for numero, ligne in enumerate(lignes, start=1):
        appel = re.search(
            r'table(?:Empty|Error|Loading)\(\s*(?:tbody|"#([a-z0-9-]+)(?: tbody)?")\s*,\s*(\d+)',
            ligne)
        if not appel:
            continue
        identifiant, colspan = appel.group(1), int(appel.group(2))
        if identifiant is None:      # cible passée par `tbody` : on remonte
            for k in range(numero - 1, max(0, numero - 60), -1):
                affectation = re.search(
                    r'tbody\s*=\s*document\.querySelector\("#([a-z0-9-]+)', lignes[k])
                if affectation:
                    identifiant = affectation.group(1)
                    break
        if identifiant:
            sortie.append((numero, identifiant, colspan))
    return sortie


def test_la_detection_des_colspans_fonctionne():
    assert len(_colonnes_par_tableau()) >= 20
    assert len(_colspans_annonces()) >= 20


def test_chaque_etat_couvre_toute_la_largeur_de_son_tableau():
    """
    Un `colspan` faux casse la mise en page au moment précis où l'écran doit
    rester lisible. La campagne batch en était là : son état vide annonçait
    neuf colonnes depuis qu'une dixième — « alertes ouvertes » — avait été
    ajoutée au tableau.
    """
    colonnes = _colonnes_par_tableau()
    ecarts = [(ligne, identifiant, colspan, colonnes[identifiant])
              for ligne, identifiant, colspan in _colspans_annonces()
              if identifiant in colonnes and colonnes[identifiant] != colspan]
    assert not ecarts, (
        "états dont le colspan ne couvre pas le tableau "
        "(ligne, tableau, annoncé, réel) : " + str(ecarts))


def test_les_deux_files_d_alertes_ont_le_meme_nombre_de_colonnes():
    """`fetchAlerts` sert les deux canaux avec un seul appel à tableError."""
    colonnes = _colonnes_par_tableau()
    tailles = {colonnes[i] for i in ("screening-alerts-table", "filtering-alerts-table")}
    assert len(tailles) == 1, f"les deux files diffèrent : {tailles}"
    annonce = re.search(r'tableError\(`#\$\{conf\.table\}`,\s*(\d+)', APP_JS)
    assert annonce and int(annonce.group(1)) == tailles.pop()

"""
Rafraîchissements en arrière-plan : ne recharger que ce qui est à l'écran.

Une synchronisation, un import, une homologation ou une purge rechargeaient
l'historique des lots (**282 Ko, 547 lignes** mesurés sur la production) *et*
la watchlist paginée — que l'utilisateur regarde ces tableaux ou pas.

C'était doublement inutile : `switchTab` et `switchSubTab` rechargent **déjà**
ces vues à chaque ouverture de leur onglet. La donnée fraîche est donc garantie
à l'arrivée, et le rafraîchissement en fond ne faisait que payer le poids sans
que personne ne le voie.

Ces tests lisent le frontal — il n'y a pas de moteur JS ici. Ils vérifient
deux choses opposées, parce que se tromper dans un sens casse la navigation et
dans l'autre annule le gain :

* les chemins de **navigation** rechargent toujours, sans condition ;
* les chemins d'**arrière-plan** passent tous par le garde de visibilité.
"""
import re
from pathlib import Path

APP_JS = Path("fiskr/static/app.js").read_text(encoding="utf-8")
LIGNES = APP_JS.split("\n")


def _lignes_appelant(nom: str):
    """(numéro, texte) des lignes qui appellent `nom(...)`, avec ou sans
    argument. Le motif accepte des arguments à dessein : quand la pagination a
    donné une page à `fetchSnapshots`, un motif limité à `nom()` ne trouvait
    plus rien et TOUS les tests de ce fichier passaient à vide."""
    motif = re.compile(rf"(?<![\w.]){re.escape(nom)}\(")
    return [(i + 1, l) for i, l in enumerate(LIGNES)
            if motif.search(l) and not l.strip().startswith("//")
            and "function " not in l]


def test_l_analyse_trouve_bien_les_appels():
    """Garde-fou : si le repérage ne trouve plus rien, tout ce fichier passe
    sans rien vérifier."""
    for nom in ("fetchSnapshots", "fetchWatchlist", "rafraichirHistoriqueDesLots"):
        assert _lignes_appelant(nom), f"aucun appel à {nom} repéré"


def _fonction_contenant(numero: int) -> str:
    """Nom de la fonction (ou du bloc) qui contient cette ligne."""
    for i in range(numero - 1, -1, -1):
        m = re.match(r"\s*(?:async\s+)?function\s+(\w+)", LIGNES[i])
        if m:
            return m.group(1)
    return "?"


# Recharger sans condition n'est légitime que si la vue est forcément à
# l'écran au moment de l'appel. Deux familles, et rien d'autre :
#
#   * la NAVIGATION — la vue vient précisément de devenir visible ;
#   * les COMMANDES DE L'ÉCRAN lui-même — tri, filtre, pagination, édition
#     d'une fiche : l'utilisateur les actionne depuis l'écran concerné.
#
# Cette liste est explicite à dessein : tout nouvel appelant échoue ici tant
# que quelqu'un n'a pas tranché de quel côté il tombe.
NAVIGATION = {"switchTab", "switchSubTab", "rafraichirHistoriqueDesLots"}
COMMANDES_DE_L_ECRAN = {
    # historique des lots
    "renderSnapshotsFiltered", "renderSnapshotsPagination",
    # watchlist active
    "sortWatchlistBy", "filterWatchlist", "changeWatchlistPage",
    "saveWatchlistEntityEdits",
}
SANS_GARDE_AUTORISE = NAVIGATION | COMMANDES_DE_L_ECRAN


def test_le_garde_de_visibilite_existe():
    assert "function vueAffichee(sectionId, subTabId)" in APP_JS
    assert "function rafraichirSiAffichee(sectionId, subTabId, fn)" in APP_JS
    assert "function rafraichirLotsEtWatchlist()" in APP_JS
    # Il regarde l'onglet ET le sous-onglet : un panneau peut porter `active`
    # alors que sa section est masquée.
    garde = APP_JS.split("function vueAffichee(sectionId, subTabId) {")[1].split("\n}")[0]
    assert 'sec-${sectionId}' in garde
    assert 'sub-sec-${subTabId}' in garde


def test_l_historique_des_lots_n_est_recharge_que_par_la_navigation():
    """282 Ko à chaque synchronisation, import, homologation ou purge."""
    fautifs = [(n, _fonction_contenant(n)) for n, _ in _lignes_appelant("fetchSnapshots")
               if _fonction_contenant(n) not in SANS_GARDE_AUTORISE]
    assert not fautifs, f"rechargement inconditionnel de l'historique des lots : {fautifs}"


def test_la_watchlist_paginee_n_est_rechargee_que_par_la_navigation():
    fautifs = [(n, _fonction_contenant(n)) for n, _ in _lignes_appelant("fetchWatchlist")
               if _fonction_contenant(n) not in SANS_GARDE_AUTORISE]
    assert not fautifs, f"rechargement inconditionnel de la watchlist : {fautifs}"


def test_la_navigation_recharge_toujours():
    """Le pendant : si la navigation passait aussi par le garde, ouvrir un
    onglet n'y afficherait plus rien de frais — ou rien du tout."""
    for chargeur in ("fetchSnapshots", "fetchWatchlist"):
        par_navigation = [n for n, _ in _lignes_appelant(chargeur)
                          if _fonction_contenant(n) in SANS_GARDE_AUTORISE]
        assert len(par_navigation) >= 1, f"{chargeur} : {par_navigation}"
    # L'ouverture de l'onglet passe par l'enrobage, appele sans condition
    ouvertures = [n for n, _ in _lignes_appelant("rafraichirHistoriqueDesLots")
                  if _fonction_contenant(n) in ("switchTab", "switchSubTab")]
    assert len(ouvertures) >= 3, ouvertures


def test_les_enrobages_sont_gardes():
    """L'enrobage qui recharge l'historique des lots ne doit être atteint,
    hors navigation, qu'à travers le garde de visibilité."""
    hors_navigation = [(n, _fonction_contenant(n))
                       for n, _ in _lignes_appelant("rafraichirHistoriqueDesLots")
                       if _fonction_contenant(n) not in ("switchTab", "switchSubTab")]
    for numero, porteuse in hors_navigation:
        assert "rafraichirSiAffichee(" in LIGNES[numero - 1], (
            f"ligne {numero} ({porteuse}) : rechargement inconditionnel")


def test_les_chemins_de_fond_passent_bien_par_le_garde():
    """Compte les appels gardés : les supprimer purement et simplement
    passerait les tests précédents tout en cassant l'écran."""
    assert APP_JS.count("rafraichirLotsEtWatchlist()") >= 3
    assert APP_JS.count("signalerChangementDeLots()") >= 4
    assert APP_JS.count(
        'rafraichirSiAffichee("watchlist-mgmt", "watchlist-snapshots", '
        'rafraichirHistoriqueDesLots)') >= 1
    assert APP_JS.count(
        'rafraichirSiAffichee("watchlist-mgmt", "watchlist-active", fetchWatchlist)') >= 1


def test_le_retour_sur_la_watchlist_ne_la_charge_plus_deux_fois():
    """Après un ajout manuel, le code appelait `fetchWatchlist()` puis
    `switchSubTab('watchlist-mgmt', 'watchlist-active')`, qui la recharge —
    la même requête, deux fois."""
    bloc = APP_JS.split("switchSubTab('watchlist-mgmt', 'watchlist-active');")[0][-400:]
    assert "fetchWatchlist();" not in bloc

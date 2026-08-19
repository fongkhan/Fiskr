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
    """(numéro, texte) des lignes qui appellent `nom()` directement."""
    motif = re.compile(rf"(?<![\w.]){re.escape(nom)}\(\)")
    return [(i + 1, l) for i, l in enumerate(LIGNES)
            if motif.search(l) and not l.strip().startswith("//")
            and "function " not in l]


def _fonction_contenant(numero: int) -> str:
    """Nom de la fonction (ou du bloc) qui contient cette ligne."""
    for i in range(numero - 1, -1, -1):
        m = re.match(r"\s*(?:async\s+)?function\s+(\w+)", LIGNES[i])
        if m:
            return m.group(1)
    return "?"


# Les seules fonctions autorisées à recharger sans condition : la navigation,
# où la vue vient précisément de devenir visible.
NAVIGATION = {"switchTab", "switchSubTab"}


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
               if _fonction_contenant(n) not in NAVIGATION]
    assert not fautifs, f"rechargement inconditionnel de l'historique des lots : {fautifs}"


def test_la_watchlist_paginee_n_est_rechargee_que_par_la_navigation():
    fautifs = [(n, _fonction_contenant(n)) for n, _ in _lignes_appelant("fetchWatchlist")
               if _fonction_contenant(n) not in NAVIGATION]
    assert not fautifs, f"rechargement inconditionnel de la watchlist : {fautifs}"


def test_la_navigation_recharge_toujours():
    """Le pendant : si la navigation passait aussi par le garde, ouvrir un
    onglet n'y afficherait plus rien de frais — ou rien du tout."""
    par_navigation = [n for n, _ in _lignes_appelant("fetchSnapshots")
                      if _fonction_contenant(n) in NAVIGATION]
    assert len(par_navigation) >= 3, par_navigation
    par_navigation = [n for n, _ in _lignes_appelant("fetchWatchlist")
                      if _fonction_contenant(n) in NAVIGATION]
    assert len(par_navigation) >= 3, par_navigation


def test_les_chemins_de_fond_passent_bien_par_le_garde():
    """Compte les appels gardés : les supprimer purement et simplement
    passerait les deux tests précédents tout en cassant l'écran."""
    assert APP_JS.count("rafraichirLotsEtWatchlist()") >= 4
    assert APP_JS.count('rafraichirSiAffichee("watchlist-mgmt", "watchlist-snapshots", fetchSnapshots)') >= 3
    assert APP_JS.count('rafraichirSiAffichee("watchlist-mgmt", "watchlist-active", fetchWatchlist)') >= 1


def test_le_retour_sur_la_watchlist_ne_la_charge_plus_deux_fois():
    """Après un ajout manuel, le code appelait `fetchWatchlist()` puis
    `switchSubTab('watchlist-mgmt', 'watchlist-active')`, qui la recharge —
    la même requête, deux fois."""
    bloc = APP_JS.split("switchSubTab('watchlist-mgmt', 'watchlist-active');")[0][-400:]
    assert "fetchWatchlist();" not in bloc

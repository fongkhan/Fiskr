"""
Chargement de page : ne télécharger que ce qui est affiché.

L'amorçage préchargeait six écrans que l'utilisateur ne regarde pas — et que
l'application RETÉLÉCHARGE à l'ouverture de leur onglet. Mesuré en production :

    base des listés      247 Ko
    snapshots            267 Ko
    journal d'audit      132 Ko
    homologation          11 Ko
    alertes / listes blanches  4 Ko
    -----------------------------
    661 Ko sur 670 Ko de chargement total, pour des écrans invisibles.

Ces tests verrouillent les deux moitiés de la garantie : l'amorçage ne
précharge plus ces écrans, ET chacun reste bien chargé à l'ouverture de son
onglet — sans quoi l'allègement laisserait des écrans vides.
"""
import re
from pathlib import Path

import pytest

APP = Path("fiskr/static/app.js").read_text(encoding="utf-8")

# Chargeurs d'écran : lourds, et propres à un onglet
CHARGEURS_D_ECRAN = [
    "fetchWatchlist", "fetchSnapshots", "fetchAuditHistory",
    "fetchPendingReviews", "fetchAlerts", "fetchWhitelist",
]


def _bloc_amorcage() -> str:
    """Le corps du DOMContentLoaded — ce qui part au chargement de la page."""
    debut = APP.index('document.addEventListener("DOMContentLoaded"')
    fin = APP.index("startOperationsPolling();", debut)
    return APP[debut:fin]


def _corps_fonction(nom: str) -> str:
    motif = re.compile(rf"^(?:async )?function {nom}\(", re.M)
    m = motif.search(APP)
    assert m, f"fonction {nom} introuvable"
    debut = m.start()
    fin = APP.index("\n}\n", debut)
    return APP[debut:fin]


@pytest.mark.parametrize("chargeur", CHARGEURS_D_ECRAN)
def test_screen_loaders_are_not_called_at_page_load(chargeur):
    """Aucun écran d'onglet n'est préchargé : c'est du poids pur, retéléchargé
    ensuite à l'ouverture de l'onglet."""
    amorcage = _bloc_amorcage()
    assert f"{chargeur}(" not in amorcage, (
        f"{chargeur} est appelé au chargement de la page alors que son onglet "
        f"le recharge : le téléchargement est payé deux fois, et la première "
        f"pour un écran invisible")


def _routage_deplie() -> str:
    """Le routage d'onglets, plus le corps des enrobages `rafraichir*` qu'il
    appelle. Un chargeur peut être atteint à travers un petit enrobage —
    l'historique des lots recharge aussi la source des listes déroulantes de
    comparaison, qui a la sienne depuis la pagination. Une seule profondeur :
    au-delà, « l'onglet charge bien l'écran » ne serait plus vérifiable de
    cette façon."""
    routage = _corps_fonction("switchSubTab") + _corps_fonction("switchTab")
    texte = routage
    for nom in sorted(set(re.findall(r"\b(rafraichir\w+)\s*\(", routage))):
        texte += _corps_fonction(nom)
    return texte


@pytest.mark.parametrize("chargeur", CHARGEURS_D_ECRAN)
def test_every_screen_still_loads_when_its_tab_opens(chargeur):
    """L'autre moitié de la garantie : retirer le préchargement n'est légitime
    que si l'ouverture de l'onglet charge bien l'écran. Sinon l'utilisateur
    trouve un tableau vide."""
    routage = _routage_deplie()
    assert f"{chargeur}(" in routage, (
        f"{chargeur} n'est appelé NI au chargement NI à l'ouverture d'un "
        f"onglet : son écran resterait vide")


def test_deep_links_go_through_the_same_routing():
    """Un lien profond (#watchlist-mgmt/watchlist-review) doit emprunter le
    même routage, sinon il afficherait un écran vide au chargement."""
    corps = _corps_fonction("applyHashRoute")
    assert "switchTab(" in corps and "switchSubTab(" in corps


def test_sidebar_state_is_still_loaded():
    """Ce qui est VISIBLE au chargement doit rester chargé : l'accueil, le
    badge de hash et les pastilles de la barre latérale."""
    amorcage = _bloc_amorcage()
    for indispensable in ("fetchHomeDashboard(", "fetchWatchlistHash(",
                          "refreshSidebarCounters("):
        assert indispensable in amorcage, f"{indispensable} manquant à l'amorçage"


def test_pending_badge_does_not_depend_on_the_removed_fetch():
    """La pastille d'homologation vient des compteurs légers, pas de l'écran
    d'homologation — c'est ce qui rend son retrait sûr."""
    compteurs = _corps_fonction("refreshSidebarCounters")
    assert "review-pending-badge" in compteurs
    assert "pending_reviews" in compteurs

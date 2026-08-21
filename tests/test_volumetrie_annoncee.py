"""
« Alerte » ne veut pas dire « client » : les trois écrans qui les confondaient.

Depuis que le criblage ouvre une alerte **par correspondance**, trois endroits
comptaient des clients tout en écrivant « alertes » :

| Écran | Ce qu'il affichait | Ce que c'était |
|---|---|---|
| Cahier de tests | « Alertes production / candidate » | clients interceptés |
| Impact moteur | « N → M alertes » | clients interceptés |
| Campagne batch | « N alerte(s) » | clients déclenchant au moins une correspondance |

Confondre les deux fait sous-estimer la charge du nombre d'homonymes — un
facteur de plusieurs centaines sur une liste comme les PEP. Les trois portent
désormais les deux chiffres, nommés distinctement : le taux d'interception d'un
côté (inchangé, c'est lui qui fait le verdict), le volume de travail de l'autre.
"""
import inspect
import re
from pathlib import Path

APP_JS = Path("fiskr/static/app.js").read_text(encoding="utf-8")
INDEX = Path("fiskr/static/index.html").read_text(encoding="utf-8")


def test_le_cahier_de_tests_porte_les_deux_chiffres():
    from fiskr import backtest
    code = inspect.getsource(backtest.run_backtest)
    assert '"hits": current.get("hits", 0)' in code
    assert '"hits": candidate.get("hits", 0)' in code
    assert "Clients interceptés — production" in APP_JS
    assert "Alertes ouvertes — production" in APP_JS


def test_l_impact_moteur_porte_les_deux_chiffres():
    from fiskr import engine_impact
    code = inspect.getsource(engine_impact)
    assert '"hits_before"' in code and '"hits_after"' in code
    assert "clients interceptés" in APP_JS
    assert "report.hits_before" in APP_JS


def test_la_campagne_batch_porte_les_deux_chiffres():
    from fiskr.database import BatchCampaign
    colonnes = {c.name for c in BatchCampaign.__table__.columns}
    assert "hits_count" in colonnes
    assert BatchCampaign.__table__.c.hits_count.nullable, (
        "colonne additive : les campagnes anciennes n'ont pas ce compte")
    assert "c.hits_count" in APP_JS
    assert "Clients en alerte" in INDEX and "Alertes ouvertes" in INDEX


# Les valeurs qui comptent des CLIENTS, sous tous leurs noms dans le frontal.
# `alerts` en est une : le rapport de cahier de tests l'affichait « N alerte(s) »
# alors qu'elle compte des clients interceptés, et le volume réel — `hits` — ne
# figurait nulle part sur cet écran.
_COMPTES_DE_CLIENTS = ("alert_count", ".alerts", "side.alerts")


def test_aucun_ecran_ne_dit_plus_seulement_alertes_pour_un_compte_de_clients():
    """Garde-fou de vocabulaire : les libellés qui comptent des clients le
    disent. Un « N alerte(s) » nu, sur une valeur qui compte des clients, est
    exactement le défaut corrigé ici."""
    formulations_justes = ("client(s) en alerte", "client(s) intercepté(s)",
                           "Clients interceptés", "clients interceptés")
    fautifs = []
    for ligne in APP_JS.split("\n"):
        if "alerte(s)" not in ligne:
            continue
        if not any(compte in ligne for compte in _COMPTES_DE_CLIENTS):
            continue
        if any(juste in ligne for juste in formulations_justes):
            continue
        fautifs.append(ligne.strip()[:160])
    assert not fautifs, (
        "Un compte de CLIENTS annoncé comme un compte d'alertes :\n  "
        + "\n  ".join(fautifs))


def test_le_rapport_de_cahier_montre_le_volume_et_pas_seulement_la_couverture():
    """
    L'écran principal du cahier de tests ne montrait que les clients
    interceptés. C'est la couverture, pas la charge : le réviseur approuvait
    une liste sans voir combien de dossiers elle allait ouvrir.
    """
    assert "side.hits" in APP_JS, (
        "la carte du rapport doit afficher les correspondances, pas seulement "
        "les clients interceptés")


def test_le_compte_de_correspondances_remonte_du_criblage():
    """Le chiffre affiché doit venir de ce que le criblage a réellement écrit,
    pas d'un recomptage approximatif."""
    from fiskr import api
    code = inspect.getsource(api._run_batch_campaign)
    assert 'result.get("hits")' in code
    assert "campaign.hits_count" in code

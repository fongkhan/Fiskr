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


def test_aucun_ecran_ne_dit_plus_seulement_alertes_pour_un_compte_de_clients():
    """Garde-fou de vocabulaire : les libellés qui comptent des clients le
    disent. Un « N alerte(s) » nu, sur une valeur qui compte des clients, est
    exactement le défaut corrigé ici."""
    fautifs = [l.strip() for l in APP_JS.split("\n")
               if "alert_count" in l and "alerte(s)" in l
               and "client(s) en alerte" not in l]
    assert not fautifs, f"libellé ambigu : {fautifs}"


def test_le_compte_de_correspondances_remonte_du_criblage():
    """Le chiffre affiché doit venir de ce que le criblage a réellement écrit,
    pas d'un recomptage approximatif."""
    from fiskr import api
    code = inspect.getsource(api._run_batch_campaign)
    assert 'result.get("hits")' in code
    assert "campaign.hits_count" in code

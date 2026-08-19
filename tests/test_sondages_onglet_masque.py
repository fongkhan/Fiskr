"""
Sondages de fond : un onglet masqué ne consomme plus rien.

Un onglet Fiskr laissé ouvert interrogeait le serveur sans fin :

| Sondage | Cadence | Requêtes/heure |
|---|---:|---:|
| `/api/progress/active` (opérations en cours) | 8 s | 450 |
| `/api/worker/status` | 30 s | 120 |
| `/api/counters` | 60 s | 60 |
| `/api/version` | 5 min | 12 |

Soit ~640 requêtes par heure et par onglet, pour un écran que personne ne
regarde — sur un hébergement mutualisé où la mesure donne ~0,15 s de travail
serveur par requête, c'est près de 100 s de serveur par heure et par onglet
oublié.

Masqué, l'onglet ne sonde plus ; il rattrape tout d'un coup au retour. C'est
même **plus frais** au moment où l'information est lue qu'un sondage périodique
tombé juste avant le retour.

Une exception, et elle compte : si une opération **tourne**, la cadence est
gardée même masqué. Sa fin déclenche des rappels d'écran et un toast, qui ne
doivent pas attendre que l'utilisateur revienne.
"""
import re
from pathlib import Path

APP_JS = Path("fiskr/static/app.js").read_text(encoding="utf-8")


def _corps(nom: str) -> str:
    m = re.search(rf"^(?:async )?function {re.escape(nom)}\(", APP_JS, re.M)
    assert m, f"fonction {nom} introuvable"
    return APP_JS[m.start():APP_JS.index("\n}\n", m.start())]


def _bloc_amorcage() -> str:
    debut = APP_JS.index('document.addEventListener("DOMContentLoaded"')
    return APP_JS[debut:APP_JS.index("startOperationsPolling();", debut)]


def test_le_test_de_visibilite_existe_et_est_prudent():
    """Si `visibilityState` n'existe pas, on doit sonder comme avant plutôt
    que de se taire pour toujours."""
    corps = _corps("ongletMasque")
    assert "document.hidden" in corps
    assert 'typeof document.visibilityState === "string"' in corps


def test_les_sondages_periodiques_sont_suspendus_quand_l_onglet_est_masque():
    amorcage = _bloc_amorcage()
    for sondage in ("refreshSidebarCounters", "refreshWorkerStatus",
                    "checkForNewVersion"):
        assert f"setInterval(siVisible({sondage})" in amorcage, sondage
        assert f"setInterval({sondage}" not in amorcage, (
            f"{sondage} sonde encore inconditionnellement")


def test_le_retour_sur_l_onglet_rattrape_tout():
    """L'autre moitié : suspendre n'est légitime que si le retour remet tout à
    jour d'un coup, sinon l'écran resterait figé sur des chiffres périmés."""
    assert 'document.addEventListener("visibilitychange", auRetourDeLOnglet)' in APP_JS
    corps = _corps("auRetourDeLOnglet")
    for sondage in ("refreshSidebarCounters", "refreshWorkerStatus",
                    "checkForNewVersion", "fetchActiveOperations"):
        assert f"{sondage}()" in corps, sondage
    # ... et il ne fait rien tant que l'onglet est encore masqué
    assert "if (ongletMasque()) return;" in corps


def test_le_suivi_des_operations_ralentit_mais_ne_s_arrete_jamais():
    """Il ne doit pas être coupé net : c'est lui qui rattrape la cadence au
    retour, et qui suit une opération lancée avant le masquage."""
    corps = _corps("fetchActiveOperations")
    assert "OPS_POLL_HIDDEN_MS" in corps
    assert "ongletMasque()" in corps
    assert re.search(r"OPS_POLL_HIDDEN_MS\s*=\s*\d+", APP_JS)


def test_une_operation_en_cours_garde_la_cadence_meme_masque():
    """Sa fin déclenche des rappels d'écran et un toast : ils ne doivent pas
    attendre que l'utilisateur revienne."""
    corps = _corps("fetchActiveOperations")
    bloc = corps[corps.index("scheduleOpsPoll(data.running > 0"):]
    bloc = bloc[:bloc.index(";")]
    # La branche « quelque chose tourne » est prise AVANT le test de visibilite
    assert bloc.index("OPS_POLL_BUSY_MS") < bloc.index("ongletMasque()")


def test_la_cadence_masquee_est_bien_plus_lente_que_le_repos():
    valeurs = {nom: int(v) for nom, v in
               re.findall(r"const (OPS_POLL_\w+_MS) = (\d+);", APP_JS)}
    assert valeurs["OPS_POLL_BUSY_MS"] < valeurs["OPS_POLL_IDLE_MS"]
    assert valeurs["OPS_POLL_IDLE_MS"] * 5 <= valeurs["OPS_POLL_HIDDEN_MS"]

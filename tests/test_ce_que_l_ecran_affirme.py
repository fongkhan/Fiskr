"""
Ce que l'écran affirme, il le tient du moteur — pas d'une relecture à part.

Défauts relevés en parcourant l'application dans un vrai navigateur :

- **« Homologation des lots — Vérifié »** sur une installation neuve où le
  premier import partait directement en production. La mise en service relisait
  le réglage avec son propre défaut (oui) ; le moteur appliquait le sien (non,
  celui de config.yaml).
- **« prochaine : 20/10/2026 06:00 »** sous des sources désactivées, la
  synchronisation automatique coupée : la date était calculée sur la seule
  expression cron, sans la condition que le planificateur teste.
- **Un 404 en console à chaque import** : le suivi interrogeait son jeton avant
  que le serveur le connaisse — un état normal rendu comme une erreur.
- **Des 403 à chaque page** pour qui n'est pas administrateur
  (`/api/setup/status`), et à chaque « Examiner » d'un réviseur
  (`/api/fprules`) : l'interface appelait ce que le rôle ne peut pas lire.
- **Aucune confirmation après la création d'un compte.**
"""
import ast
import re
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.cron import cron_matches
from fiskr.database import get_db, AppSetting
from fiskr import mise_en_service as mes
from fiskr.settings import (require_approval_enabled, SETTING_REQUIRE_APPROVAL,
                            SETTING_SYNC_AUTO_ENABLED, SETTING_SYNC_SOURCES_ENABLED)

RACINE = Path(__file__).resolve().parent.parent
APP_JS = (RACINE / "fiskr" / "static" / "app.js").read_text(encoding="utf-8")


def _connecte(role="admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "test_ecran", "full_name": "test_ecran",
        "role": role, "roles": [role],
    }


@pytest.fixture
def client():
    _connecte()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def reglages():
    """Retire puis restaure les réglages que ces tests posent."""
    cles = (SETTING_REQUIRE_APPROVAL, SETTING_SYNC_AUTO_ENABLED, SETTING_SYNC_SOURCES_ENABLED)
    db = next(get_db())
    avant = {r.key: r.value for r in db.query(AppSetting).filter(AppSetting.key.in_(cles)).all()}
    db.query(AppSetting).filter(AppSetting.key.in_(cles)).delete(synchronize_session=False)
    db.commit()
    yield db
    db.rollback()
    db.query(AppSetting).filter(AppSetting.key.in_(cles)).delete(synchronize_session=False)
    for cle, valeur in avant.items():
        db.add(AppSetting(key=cle, value=valeur))
    db.commit()
    db.close()


def _fonction(nom):
    """Corps d'une fonction de app.js (jusqu'à la fonction suivante)."""
    debut = re.search(rf"\n(async )?function {nom}\(", APP_JS)
    assert debut, f"{nom} introuvable dans app.js"
    suite = re.search(r"\n(async )?function \w+\(", APP_JS[debut.end():])
    return APP_JS[debut.start(): debut.end() + (suite.start() if suite else len(APP_JS))]


# ---------------------------------------------------- homologation des lots

@pytest.mark.parametrize("stocke", [None, True, False])
def test_le_controle_d_homologation_dit_ce_que_l_import_applique(reglages, stocke):
    """`None` : installation neuve, aucun réglage en base. C'est le cas où le
    contrôle affichait « Vérifié » pendant que l'import passait en production."""
    if stocke is not None:
        reglages.add(AppSetting(key=SETTING_REQUIRE_APPROVAL, value=stocke))
        reglages.commit()
    controle = mes._homologation(reglages)
    assert (controle["etat"] == mes.OK) == require_approval_enabled(reglages), (
        f"réglage {stocke!r} : le contrôle dit {controle['etat']}, le moteur "
        f"applique require_approval={require_approval_enabled(reglages)}")


def test_le_defaut_du_mode_homologation_n_est_ecrit_qu_une_fois():
    """Deux lectures du réglage, deux défauts (oui d'un côté, non de l'autre) :
    c'était l'origine du faux « Vérifié ». Hors de settings.py, on passe par
    require_approval_setting / require_approval_enabled."""
    fautes = []
    for fichier in sorted((RACINE / "fiskr").glob("*.py")):
        if fichier.name == "settings.py":
            continue
        for noeud in ast.walk(ast.parse(fichier.read_text(encoding="utf-8"))):
            if (isinstance(noeud, ast.Call)
                    and ast.unparse(noeud.func).split(".")[-1] in ("get_setting", "get_setting_with_source")
                    and any(ast.unparse(a) == "SETTING_REQUIRE_APPROVAL" for a in noeud.args)):
                fautes.append(f"{fichier.name}:{noeud.lineno}")
    assert not fautes, f"lecture du mode homologation avec un défaut à soi : {fautes}"


# ------------------------------------------------- prochaine récupération

def test_pas_de_date_pour_ce_que_le_planificateur_ne_lancera_pas(client, reglages):
    cfg = client.get("/api/sync/config").json()
    assert not cfg["auto_enabled"]
    assert all(v is None for v in cfg["next_runs"].values()), \
        "synchronisation automatique coupée : aucune source ne partira seule"
    assert set(cfg["next_run_missing_reason"].values()) == {"AUTO_COUPEE"}

    assert client.put("/api/settings/sync", json={
        "auto_enabled": True, "sources_enabled": {"ofac": True, "ofac_nonsdn": False},
    }).status_code == 200
    cfg = client.get("/api/sync/config").json()
    assert cfg["next_runs"]["ofac"] is not None
    assert cfg["next_runs"]["ofac_nonsdn"] is None
    assert cfg["next_run_missing_reason"]["ofac_nonsdn"] == "SOURCE_DESACTIVEE"


def test_l_ecran_et_le_planificateur_lisent_la_meme_condition(client, reglages, monkeypatch):
    """Au même instant, le planificateur soumet exactement les sources dont
    l'expression cron tombe ET auxquelles l'écran promet une date."""
    assert client.put("/api/settings/sync", json={
        "auto_enabled": True, "sources_enabled": {"ofac": True, "ofac_nonsdn": False, "un": True},
    }).status_code == 200
    cfg = client.get("/api/sync/config").json()
    instant = datetime(2026, 10, 21, 6, 0)
    promises = {s for s, quand in cfg["next_runs"].items()
                if quand and cron_matches(cfg["schedules"][s], instant)}
    soumises = []
    monkeypatch.setattr(api.job_queue, "submit",
                        lambda kind, **kw: soumises.append(kw["params"]["run_key"]))
    api._cron_sync_tick(instant)
    assert set(soumises) == promises
    assert "ofac_nonsdn" not in soumises and "ofac" in soumises


# ---------------------------------------------------- suivi de progression

def test_un_jeton_pas_encore_connu_n_est_pas_une_erreur_quand_on_l_attend(client):
    jeton = str(uuid.uuid4())
    r = client.get(f"/api/progress?id={jeton}&expected=1")
    assert r.status_code == 200 and r.json()["status"] == "PENDING"


def test_un_jeton_inconnu_reste_un_404_franc(client):
    """Le drapeau ne masque rien : sans lui, un jeton inconnu est une faute."""
    assert client.get(f"/api/progress?id={uuid.uuid4()}").status_code == 404


def test_le_suivi_d_un_import_annonce_qu_il_attend_son_jeton():
    corps = _fonction("startProgressPolling")
    assert "expected=1" in corps
    assert 'p.status === "PENDING"' in corps


# --------------------------------------------- n'appeler que ce qu'on lit

def test_la_mise_en_service_n_est_demandee_que_par_qui_peut_la_lire():
    corps = _fonction("rafraichirMiseEnService")
    assert corps.index('peutLire("auditor")') < corps.index('"/api/setup/status"')
    # ... et seulement une fois le rôle connu
    assert "checkAuthUser().then(() => setTimeout(verifierMiseEnService" in APP_JS


def test_les_regles_anti_faux_positifs_ne_sont_demandees_qu_avec_le_role():
    corps = _fonction("fetchBacktestCandidateRules")
    assert corps.index('peutLire("rules")') < corps.index('"/api/fprules')
    tuile = APP_JS[APP_JS.index('"tile-rules"'):]
    tuile = tuile[:tuile.index("go:")]
    assert tuile.index('peutLire("rules")') < tuile.index('"/api/fprules"')


def test_peut_lire_reprend_la_regle_du_serveur():
    """require_roles : l'administrateur passe toujours."""
    corps = _fonction("peutLire")
    assert 'r.includes("admin")' in corps


@pytest.mark.parametrize("role", ["user", "reviewer"])
def test_les_lectures_gardees_sont_bien_refusees_a_ces_roles(client, role):
    """Le garde du frontal n'invente rien : le serveur refuse vraiment."""
    _connecte(role)
    assert client.get("/api/setup/status").status_code == 403
    assert client.get("/api/fprules").status_code == 403


# ---------------------------------------------------- création de compte

def test_creer_modifier_supprimer_un_compte_se_confirme():
    assert 'showToast(data.message, "success")' in _fonction("handleSaveUser")
    assert 'showToast(data.message, "success")' in _fonction("deleteUserAccount")

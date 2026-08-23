"""
Mise en service : ce que CETTE installation a encore à régler.

Une installation neuve démarre muette. Rien ne dit qu'aucune liste n'est en
production — donc que le criblage répondra « aucune correspondance » sans
jamais se plaindre —, que le démon travailleur est absent, ou que les secrets
sont restés ceux du code source. Ces trois états se lisaient dans un WARNING du
démarrage, dans un journal que personne n'ouvre, ou nulle part.

Deux exigences tiennent tout le reste, et ces tests les verrouillent.

**Rien n'est mémorisé.** Le relevé interroge la même source que l'écran qu'il
décrit : la base pour les listes, la file pour le démon, `fiskr.config` pour les
secrets. Un point qui redeviendrait faux six mois après la mise en route le
redirait — c'est ce qui sépare un contrôle d'une case cochée une fois.

**Configuré n'est pas joignable.** Le relevé constate qu'un serveur SMTP est
déclaré, jamais qu'il répond : répondre demande d'ouvrir une connexion, ce
qu'une page de statut n'a pas le droit de faire à chaque affichage. D'où la
sonde explicite, déclenchée par un humain et bornée par un timeout court.
Constaté en production : un SMTP correctement déclaré dont chaque envoi tombait
en timeout, pendant que l'application se croyait capable de prévenir.
"""
import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db
from fiskr import mise_en_service as mes


def _connecte(role="admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "full_name": "Admin",
        "role": role, "roles": [role],
    }


@pytest.fixture
def client():
    _connecte()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def db():
    session = next(get_db())
    yield session
    session.close()


# ------------------------------------------------- le relevé dit quelque chose

def test_le_releve_couvre_les_quatre_familles(client):
    etat = client.get("/api/setup/status").json()
    assert etat["familles"] == ["Socle", "Listes", "Criblage", "Exploitation"]
    familles_vues = {c["famille"] for c in etat["controles"]}
    assert familles_vues == set(etat["familles"]), (
        "chaque famille doit porter au moins un contrôle", familles_vues)
    assert len(etat["controles"]) >= 10, etat["controles"]


def test_chaque_controle_dit_ce_qu_il_a_constate(client):
    """Un état sans constat n'aide personne : c'est un voyant, pas un contrôle."""
    manquants = [c["cle"] for c in client.get("/api/setup/status").json()["controles"]
                 if not (c.get("constat") or "").strip()]
    assert not manquants, f"contrôles sans constat : {manquants}"


def test_tout_point_a_regler_dit_quoi_faire(client):
    """Un défaut sans remède est une accusation. Les points VÉRIFIÉS n'ont rien
    à prescrire, eux."""
    muets = [c["cle"] for c in client.get("/api/setup/status").json()["controles"]
             if c["etat"] != mes.OK and not (c.get("remede") or "").strip()]
    assert not muets, f"points à régler sans remède : {muets}"


def test_les_etats_appartiennent_au_vocabulaire(client):
    connus = {mes.OK, mes.A_FAIRE, mes.ATTENTION, mes.BLOQUANT}
    etats = {c["etat"] for c in client.get("/api/setup/status").json()["controles"]}
    assert etats <= connus, etats - connus


def test_le_compte_de_bloquants_est_celui_des_controles(client):
    """Le bandeau se déclenche sur ce compte : il ne peut pas diverger de ce
    que la liste montre."""
    etat = client.get("/api/setup/status").json()
    reels = [c for c in etat["controles"] if c["etat"] == mes.BLOQUANT]
    assert etat["bloquants"] == len(reels)
    a_traiter = [c for c in etat["controles"] if c["etat"] != mes.OK]
    assert etat["a_traiter"] == len(a_traiter)


# ------------------------------------------------------- rien n'est mémorisé

def test_le_releve_suit_l_etat_reel_et_ne_le_memorise_pas(client, monkeypatch):
    """
    Le cœur du sujet. Une case cochée une fois à l'installation ne dit plus rien
    six mois plus tard ; un contrôle qui interroge, si.
    """
    from fiskr import config as cfg
    monkeypatch.setattr(cfg, "INSECURE_DEFAULT_SECRET_KEY", True, raising=False)
    monkeypatch.setattr(cfg, "INSECURE_DEFAULT_ADMIN_PASSWORD", True, raising=False)
    avant = next(c for c in client.get("/api/setup/status").json()["controles"]
                 if c["cle"] == "secrets")
    assert avant["etat"] == mes.BLOQUANT
    assert "SECRET_KEY" in avant["constat"]

    monkeypatch.setattr(cfg, "INSECURE_DEFAULT_SECRET_KEY", False, raising=False)
    monkeypatch.setattr(cfg, "INSECURE_DEFAULT_ADMIN_PASSWORD", False, raising=False)
    apres = next(c for c in client.get("/api/setup/status").json()["controles"]
                 if c["cle"] == "secrets")
    assert apres["etat"] == mes.OK, "le relevé doit suivre, sans mémoire"


def test_un_secret_par_defaut_ne_publie_jamais_sa_valeur(client, monkeypatch):
    """Le nom d'un secret se dit ; sa valeur, jamais — pas même dans un écran
    réservé à l'administrateur."""
    from fiskr import config as cfg
    monkeypatch.setattr(cfg, "INSECURE_DEFAULT_SECRET_KEY", True, raising=False)
    monkeypatch.setattr(cfg, "SECRET_KEY", "valeur-a-ne-jamais-publier", raising=False)
    corps = client.get("/api/setup/status").text
    assert "SECRET_KEY" in corps
    assert "valeur-a-ne-jamais-publier" not in corps


def test_aucune_liste_en_production_est_bloquant():
    """L'état le plus dangereux du produit : un criblage qui répond « aucune
    correspondance » parce qu'il n'a rien à comparer, et qui ne s'en plaint pas."""
    class _Vide:
        def query(self, *a, **k): return self
        def filter(self, *a, **k): return self
        def count(self): return 0
    controle = mes._listes_en_production(_Vide())
    assert controle["etat"] == mes.BLOQUANT
    assert controle["lien"], "un point bloquant doit mener à l'écran qui le règle"


def test_le_premier_demarrage_est_une_base_vide(client):
    """Aucune liste ET aucun client : l'état d'une installation qui vient de
    démarrer, pas un défaut."""
    etat = client.get("/api/setup/status").json()
    assert isinstance(etat["premier_demarrage"], bool)


# ------------------------------------------------ configuré n'est pas joignable

def test_un_smtp_declare_n_est_pas_annonce_comme_joignable(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.exemple.test")
    controle = mes._smtp()
    assert controle["etat"] == mes.OK
    assert "joignable" in controle["constat"], (
        "le relevé doit dire qu'il constate une CONFIGURATION, pas une capacité")


def test_la_sonde_smtp_est_bornee_et_dit_ce_qui_a_echoue(monkeypatch):
    """Une sonde qui pend serait une panne de plus."""
    import time
    monkeypatch.setenv("SMTP_HOST", "10.255.255.1")   # adresse qui absorbe
    monkeypatch.setenv("SMTP_PORT", "2525")
    debut = time.monotonic()
    resultat = mes.sonder_smtp(timeout=2.0)
    duree = time.monotonic() - debut
    assert resultat["ok"] is False
    assert duree < 15, f"sonde non bornée : {duree:.0f}s"
    assert "10.255.255.1" in resultat["detail"]


def test_la_sonde_sans_serveur_configure_le_dit_sans_reseau(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    resultat = mes.sonder_smtp()
    assert resultat["ok"] is False and "SMTP_HOST" in resultat["detail"]


# -------------------------------------------------------------- accès et écran

def test_le_releve_est_reserve_a_l_administrateur():
    """Chaque point renvoie vers un écran que seul un administrateur peut
    régler, et le relevé nomme des secrets."""
    _connecte(role="user")
    try:
        with TestClient(app) as c:
            assert c.get("/api/setup/status").status_code == 403
            assert c.post("/api/setup/probe-smtp").status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_l_ecran_existe_et_le_bandeau_avec():
    """Le relevé n'a d'intérêt que s'il atteint un lecteur."""
    import os
    racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(racine, "fiskr", "static", "index.html"), encoding="utf-8") as f:
        index = f.read()
    with open(os.path.join(racine, "fiskr", "static", "app.js"), encoding="utf-8") as f:
        app_js = f.read()
    assert 'id="sub-sec-guide-setup"' in index and 'id="sub-btn-guide-setup"' in index
    assert 'id="setup-banner"' in index, "le bandeau doit exister"
    assert "/api/setup/status" in app_js
    assert "premier_demarrage" in app_js, "l'ouverture au premier démarrage"
    assert "majBandeauMiseEnService" in app_js


def test_la_sonde_n_est_pas_appelee_a_chaque_affichage():
    """Garde de source : ouvrir une connexion réseau à chaque affichage d'une
    page de statut est exactement ce qu'on refuse ici."""
    import inspect
    code = inspect.getsource(mes.etat_de_mise_en_service)
    assert "sonder_smtp" not in code

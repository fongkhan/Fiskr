"""
Lot « gains rapides » : six frictions quotidiennes, mesurées avant d'être
corrigées.

1. **Session** — le jeton vit huit heures et le cookie est HttpOnly : le
   client ne pouvait pas connaître l'échéance, donc pas prévenir. Le premier
   appel après l'expiration recevait un 401 et redirigeait vers la connexion
   en emportant la saisie en cours. `/api/auth/me` donne maintenant
   l'échéance ; un bandeau prévient à T-10 min ; et si le 401 tombe malgré
   tout, les champs remplis sont photographiés avant la redirection et
   reposés après reconnexion.
2. **En-têtes collants** — zéro `position: sticky` dans la feuille de style :
   sur la base des listés, l'en-tête disparaissait à la trentième ligne.
3. **Copier en un clic** — identifiants, hash, lien d'un dossier d'alerte.
   Le lien n'est utile que si le destinataire arrive SUR le dossier : le
   routage accepte un troisième segment `alerte-<id>`.
4. **Filtres persistés** — les treize barres de filtres repartaient de zéro à
   chaque visite.
5. **Aide `?`** — Ctrl+K et les autres raccourcis existaient, rien ne les
   annonçait.
6. **Badge d'onglet** — un onglet en arrière-plan ne disait pas s'il y avait
   du travail.
"""
import os
import re

import pytest

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def app_js():
    return _lire("app.js")


@pytest.fixture(scope="module")
def index_html():
    return _lire("index.html")


@pytest.fixture(scope="module")
def styles():
    return _lire("styles.css")


# ------------------------------------------------------------------ session

class _Requete:
    def __init__(self, headers=None, cookies=None):
        self.headers = headers or {}
        self.cookies = cookies or {}


def test_l_echeance_de_session_se_lit_depuis_le_jeton():
    from fiskr.auth import create_access_token, session_expires_at

    jeton = create_access_token({"sub": "test", "role": "user"})
    echeance = session_expires_at(_Requete(headers={"Authorization": f"Bearer {jeton}"}))
    assert echeance and echeance.endswith("+00:00")

    echeance_cookie = session_expires_at(_Requete(cookies={"fiskr_access_token": jeton}))
    assert echeance_cookie == echeance


def test_une_cle_d_api_n_a_pas_d_echeance():
    """Une clé de service n'expire pas : None, jamais une date inventée."""
    from fiskr.auth import session_expires_at

    assert session_expires_at(_Requete(headers={"Authorization": "Bearer fsk_abc"})) is None
    assert session_expires_at(_Requete(cookies={"fiskr_access_token": "n-importe-quoi"})) is None
    assert session_expires_at(None) is None


def test_le_me_rend_l_echeance(client_connecte):
    corps = client_connecte.get("/api/auth/me").json()
    assert "session_expires_at" in corps


@pytest.fixture
def client_connecte():
    from fastapi.testclient import TestClient

    from fiskr.api import app
    from fiskr.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "u", "full_name": "U", "role": "user", "roles": ["user"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_le_401_photographie_la_saisie_avant_de_rediriger(app_js):
    """
    L'ordre est le défaut : sauver APRÈS `window.location.href` ne sauverait
    rien. On vérifie la séquence, pas seulement la présence.
    """
    debut = app_js.index("if (response.status === 401) {")
    bloc = app_js[debut:debut + 220]
    assert "sauverBrouillonDeSession()" in bloc
    assert bloc.index("sauverBrouillonDeSession()") < bloc.index("window.location.href")


def test_le_brouillon_ne_remplit_que_les_champs_vides(app_js):
    corps = app_js[app_js.index("function restaurerBrouillonDeSession()"):]
    corps = corps[:corps.index("\n}")]
    assert '!el.value' in corps, "écraser une saisie neuve avec un vieux brouillon serait pire que tout"


def test_la_veille_de_session_est_armee_a_la_connexion(app_js):
    debut = app_js.index("async function checkAuthUser()")
    assert "armerVeilleDeSession(data.session_expires_at)" in app_js[debut:debut + 800]


# ------------------------------------------------------------ en-têtes collants

def test_les_en_tetes_de_tableaux_sont_collants(styles):
    debut = styles.index(".table-container thead th")
    bloc = styles[debut:debut + 220]
    assert "position: sticky" in bloc
    assert "top: 0" in bloc
    assert "z-index" in bloc


# ------------------------------------------------------------------ copier

def test_la_copie_passe_par_une_delegation_unique(app_js):
    assert app_js.count('closest("[data-copier]")') == 1


def test_le_lien_d_une_alerte_se_copie_et_se_rouvre(app_js):
    """Copier un lien qui ramène à la FILE et non au dossier ne servirait à
    rien : le troisième segment du hash rouvre l'alerte elle-même."""
    assert "alerte-${a.id}" in app_js
    assert re.search(r"alerte-\(\\d\+\)", app_js), "le routage doit reconnaître alerte-<id>"
    assert "openAlertModal(parseInt(dossierAlerte[1], 10))" in app_js


def test_le_hash_de_snapshot_se_copie_en_entier(app_js):
    """L'écran tronque à huit caractères — c'est le hash COMPLET qui est la
    référence opposable, c'est donc lui que le bouton copie."""
    assert app_js.count('boutonCopier(snap.file_hash') == 2


# ------------------------------------------------------------ filtres persistés

def test_les_filtres_se_retiennent_et_se_restaurent(app_js):
    debut = app_js.index("function attachTableFilters(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "_filtresMemorises()[tableId]" in corps, "restauration absente"
    assert "_memoriserFiltres(tableId)" in corps, "mémorisation absente"


def test_un_filtre_restaure_attend_ses_options(app_js):
    """
    Les menus se remplissent avec les DONNÉES : au moment de la restauration,
    l'option voulue n'existe pas encore. Poser `select.value` directement
    rendrait "" en silence — le souhait est retenu et posé quand l'option
    apparaît.
    """
    assert "dataset.souhaite" in app_js
    debut = app_js.index("function refreshTableFilters(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "select.dataset.souhaite" in corps


def test_un_filtre_actif_se_voit(app_js, styles):
    """Le revers de la persistance : un filtre d'hier oublié ferait lire un
    tableau plein comme un tableau presque vide. Le repère est obligatoire."""
    assert 'classList.toggle("filtre-actif"' in app_js
    assert ".table-filter-bar.filtre-actif" in styles


# ------------------------------------------------------------------ aide ?

def test_l_aide_des_raccourcis_existe_et_s_ouvre_par_la_touche(index_html, app_js):
    assert 'id="raccourcis-modal"' in index_html
    assert '<kbd>Ctrl</kbd> + <kbd>K</kbd>' in index_html
    debut = app_js.index("function initAideRaccourcis()")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert 'e.key !== "?"' in corps
    assert 'tagName === "INPUT"' in corps, (
        "« ? » tapé dans un champ de recherche doit rester un point d'interrogation")


# ------------------------------------------------------------ badge d'onglet

def test_le_titre_de_l_onglet_porte_le_compte_d_alertes(app_js):
    debut = app_js.index("async function refreshSidebarCounters()")
    corps = app_js[debut:debut + 1200]
    assert "document.title" in corps
    assert "_titreDeBase" in corps, (
        "sans titre de base retenu, chaque rafraîchissement empilerait les (N)")

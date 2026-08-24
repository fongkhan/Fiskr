"""
Ce que Fiskr offre sans demander qui vous êtes.

Un point d'entrée oublié sans garde ne se voit pas : il répond, simplement.
Ce fichier fait donc l'inventaire — toutes les routes de l'application, la
chaîne de dépendances de chacune — et exige que chaque route demande une
authentification, sauf celles d'une liste explicite et justifiée. Une route
ajoutée sans garde fait tomber le test avec son nom.

La carte de l'API en faisait partie. `/docs`, `/openapi.json` et `/redoc`
répondaient **200 sans le moindre identifiant** — vérifié sur la production :
170 chemins dont 39 d'administration, 66 schémas, et les descriptions tirées
des docstrings, qui détaillent les défenses elles-mêmes (verrouillage
anti-force brute, enrôlement MFA, import de réglages). Les points d'entrée
restaient protégés ; ce qui était offert, c'est leur plan complet.
"""
import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user

# Les seules routes qui doivent répondre à un visiteur anonyme, et pourquoi.
SANS_AUTHENTIFICATION = {
    ("GET", "/"): "redirection vers la connexion",
    ("GET", "/login"): "page de connexion",
    ("GET", "/login.html"): "page de connexion",
    ("GET", "/favicon.ico"): "icône du navigateur",
    ("GET", "/api/health"): "sonde de supervision (liveness/readiness)",
    ("POST", "/api/auth/login"): "on ne peut pas exiger d'être connecté pour se connecter",
}

_MARQUEURS_D_AUTH = ("get_current_user", "require_admin", "require_role",
                     "require_roles", "require_capability", "get_current_admin")


def _noms_de_dependances(dependant, vus=None):
    """Tous les appelables de l'arbre de dépendances d'une route — la garde
    peut être posée par une fermeture (`require_capability(...)`), donc on
    descend jusqu'au bout au lieu de lire le premier niveau."""
    vus = set() if vus is None else vus
    noms = set()
    for sous in dependant.dependencies:
        if sous.call is not None:
            noms.add(getattr(sous.call, "__name__", ""))
            noms.add(getattr(sous.call, "__qualname__", ""))
        if id(sous) not in vus:
            vus.add(id(sous))
            noms |= _noms_de_dependances(sous, vus)
    return {n for n in noms if n}


def _routes():
    for route in app.routes:
        if not hasattr(route, "dependant"):
            continue  # montages de fichiers statiques : pas de dépendances
        for methode in sorted(getattr(route, "methods", []) or []):
            if methode in ("HEAD", "OPTIONS"):
                continue
            yield methode, route.path, _noms_de_dependances(route.dependant)


def test_toute_route_demande_qui_vous_etes():
    nues = []
    for methode, chemin, noms in _routes():
        if any(m in n for n in noms for m in _MARQUEURS_D_AUTH):
            continue
        if (methode, chemin) in SANS_AUTHENTIFICATION:
            continue
        nues.append(f"{methode} {chemin}")
    assert not nues, (
        "Routes atteignables sans authentification. Si c'est voulu, ajoutez-les "
        "à SANS_AUTHENTIFICATION avec la raison :\n  " + "\n  ".join(sorted(nues)))


def test_la_liste_des_exceptions_ne_contient_rien_de_perime():
    """Une exception qui ne correspond plus à aucune route est une permission
    qu'on croit avoir accordée à quelque chose qui n'existe plus."""
    existantes = {(m, c) for m, c, _ in _routes()}
    fantomes = [f"{m} {c}" for (m, c) in SANS_AUTHENTIFICATION if (m, c) not in existantes]
    assert not fantomes, "Exceptions sans route :\n  " + "\n  ".join(fantomes)


@pytest.mark.parametrize("chemin", ["/docs", "/openapi.json", "/redoc"])
def test_la_carte_de_l_api_n_est_pas_publique(chemin):
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(app) as client:
        assert client.get(chemin).status_code == 401, (
            f"{chemin} répond à un visiteur anonyme")


@pytest.mark.parametrize("chemin", ["/docs", "/openapi.json", "/redoc"])
def test_la_carte_reste_lisible_pour_qui_est_connecte(chemin):
    """Fermer la porte ne doit pas condamner la pièce : la documentation sert
    à qui écrit un connecteur, et le cookie de session la lui apporte."""
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "doc_lecteur", "role": "analyst", "roles": ["analyst"],
    }
    try:
        with TestClient(app) as client:
            reponse = client.get(chemin)
        assert reponse.status_code == 200
    finally:
        app.dependency_overrides.pop(get_current_user, None)


def test_le_schema_ne_se_documente_pas_lui_meme():
    """Les trois routes de documentation n'ont pas à figurer dans le schéma."""
    schema = app.openapi()
    for chemin in ("/docs", "/openapi.json", "/redoc"):
        assert chemin not in schema["paths"]

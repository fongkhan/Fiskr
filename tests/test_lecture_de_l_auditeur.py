"""
« Auditeur : lecture seule intégrale » — huit lectures lui étaient fermées.

Le rôle `auditor` est décrit dans `fiskr/auth.py` comme la lecture seule
intégrale d'un contrôleur externe : exclusif, jamais combiné à un autre rôle,
toute écriture refusée. Mais huit points d'entrée en **lecture** exigeaient
`require_admin`, dont le **journal d'administration** — c'est-à-dire la
première chose qu'un contrôleur demande : qui a changé quoi, et quand.

Le défaut s'est manifesté en conditions réelles : une clé d'API de rôle
`auditor`, créée pour diagnostiquer la production à distance, ne pouvait pas
lire le journal des notifications — le seul endroit où l'on voit que les
courriels d'alerte partent ou non.

Ce que ce fichier tient : les pièces d'audit se lisent, et rien ne s'écrit.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user

# Lectures qui CONSTITUENT le dossier d'un contrôle. Aucune ne rend de secret :
# les clés d'API ne sortent qu'en préfixe, l'export de réglages ne porte que des
# clés connues et jamais un secret.
LECTURES_D_AUDIT = [
    "/api/admin-log",
    "/api/admin/config/export",
    "/api/admin/retention",
    "/api/apikeys",
    "/api/hooks/stats",
    "/api/notifications/log",
    "/api/setup/status",
    "/api/users",
]


def _connecte(role):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": f"compte_{role}", "full_name": role,
        "role": role, "roles": [role],
    }


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.mark.parametrize("chemin", LECTURES_D_AUDIT)
def test_l_auditeur_lit_les_pieces_d_audit(client, chemin):
    _connecte("auditor")
    reponse = client.get(chemin)
    assert reponse.status_code == 200, (
        f"{chemin} refusé à un auditeur : {reponse.status_code} {reponse.text[:120]}")


@pytest.mark.parametrize("chemin", LECTURES_D_AUDIT)
def test_l_administrateur_les_lit_toujours(client, chemin):
    _connecte("admin")
    assert client.get(chemin).status_code == 200


@pytest.mark.parametrize("chemin", LECTURES_D_AUDIT)
def test_un_analyste_ne_les_lit_pas(client, chemin):
    """
    Ouvrir à l'auditeur ne doit ouvrir à personne d'autre : un analyste de
    conformité n'a pas à lire le journal d'administration ni la liste des
    comptes.
    """
    _connecte("user")
    assert client.get(chemin).status_code == 403


def test_aucune_lecture_ne_reste_reservee_au_seul_administrateur():
    """
    Garde d'inventaire, dérivée des routes : une lecture ajoutée derrière
    `require_admin` referme une porte au contrôleur sans que personne ne s'en
    aperçoive. Si c'est délibéré — une lecture qui exposerait un secret —
    ajoutez-la ici avec la raison.
    """
    DELIBEREMENT_ADMIN = {}   # aucune à ce jour

    def _dependances(dependant, vus=None):
        vus = set() if vus is None else vus
        noms = set()
        for sous in dependant.dependencies:
            if sous.call is not None:
                noms.add(getattr(sous.call, "__name__", ""))
            if id(sous) not in vus:
                vus.add(id(sous))
                noms |= _dependances(sous, vus)
        return {n for n in noms if n}

    fermees = []
    for route in app.routes:
        if not hasattr(route, "dependant"):
            continue
        methodes = set(getattr(route, "methods", []) or []) - {"HEAD", "OPTIONS"}
        if methodes != {"GET"}:
            continue
        if "require_admin" in _dependances(route.dependant) and route.path not in DELIBEREMENT_ADMIN:
            fermees.append(route.path)
    assert not fermees, (
        "Lectures fermées au contrôleur externe :\n  " + "\n  ".join(sorted(fermees)))


# ------------------------------------------- et rien ne s'écrit

ECRITURES = [
    ("POST", "/api/setup/probe-smtp", {}),
    ("PUT", "/api/settings/retention", {"audit_trail": 90}),
    ("POST", "/api/apikeys", {"name": f"t-{uuid.uuid4().hex[:6]}", "role": "auditor"}),
    ("POST", "/api/admin/config/import", {"settings": {}}),
    ("POST", "/api/admin/retention/run", {}),
]


@pytest.mark.parametrize("methode, chemin, corps", ECRITURES)
def test_l_auditeur_n_ecrit_jamais(client, methode, chemin, corps):
    """
    L'ouverture en lecture ne doit rien ouvrir en écriture.

    Ce que ce test vérifie exactement : ces écritures exigent toujours
    `require_admin`, et un auditeur n'est pas administrateur. Le second
    verrou — `enforce_auditor_readonly`, qui refuse toute méthode mutante dès
    `get_current_user` — n'est PAS exercé ici, puisque la fixture remplace
    justement `get_current_user`. Il a son propre test, juste en dessous.
    """
    _connecte("auditor")
    reponse = client.request(methode, chemin, json=corps)
    assert reponse.status_code == 403, (
        f"{methode} {chemin} accepté pour un auditeur : {reponse.status_code}")


def test_le_second_verrou_refuse_toute_methode_mutante():
    """
    Le premier verrou est la dépendance de chaque route ; le second est
    général et ne dépend d'aucune route : `enforce_auditor_readonly` refuse
    toute méthode mutante à un auditeur, quelle que soit la garde de
    l'endpoint. C'est lui qui protège une route qu'on ouvrirait par
    inadvertance — donc exactement le risque que ce lot introduit.
    """
    from fastapi import HTTPException

    from fiskr.auth import enforce_auditor_readonly

    class _Requete:
        def __init__(self, methode, chemin):
            self.method = methode
            self.url = type("U", (), {"path": chemin})()

    for methode in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(HTTPException) as exc:
            enforce_auditor_readonly(_Requete(methode, "/api/nimporte/quoi"), ["auditor"])
        assert exc.value.status_code == 403

    # La lecture passe, et un non-auditeur n'est pas concerné.
    enforce_auditor_readonly(_Requete("GET", "/api/admin-log"), ["auditor"])
    enforce_auditor_readonly(_Requete("POST", "/api/apikeys"), ["admin"])


def test_le_role_a_un_libelle_dans_l_interface():
    """Un rôle sans libellé s'affiche en brut dans la barre latérale — le
    contrôleur lit « auditor » au lieu de son rôle."""
    import os
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "fiskr", "static", "app.js")
    with open(chemin, encoding="utf-8") as f:
        app_js = f.read()
    assert 'auditor: "Auditeur (lecture seule)"' in app_js


def test_le_journal_d_administration_est_visible_pour_l_auditeur():
    """Lecture pure, aucune commande dans cet écran : il n'y a aucune raison
    de le masquer à qui a le droit de le lire."""
    import os
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "fiskr", "static", "app.js")
    with open(chemin, encoding="utf-8") as f:
        app_js = f.read()
    debut = app_js.index('const adminLogBtn = document.getElementById("sub-btn-audit-admin");')
    assert "isAdmin || isAuditor" in app_js[debut:debut + 200]

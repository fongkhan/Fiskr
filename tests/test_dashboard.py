"""
Tests de l'accueil personnalisable (disposition de panneaux par utilisateur) :
- GET /api/me/dashboard : null tant que rien n'est sauvegarde (= defaut livre) ;
- PUT : sauvegarde normalisee (taille md par defaut), validations (liste vide,
  taille inconnue, doublon, identifiant malsain, plafond de panneaux) ;
- DELETE : remise a zero idempotente ;
- isolation stricte entre utilisateurs.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, UserDashboard


def _override_user(username: str, role: str = "user"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


@pytest.fixture()
def client():
    _override_user("test_dash_admin", "admin")
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)
    db = next(get_db())
    try:
        db.query(UserDashboard).filter(
            UserDashboard.username.like("test_dash_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_dashboard_default_then_save_then_reset(client):
    me = f"test_dash_{uuid.uuid4().hex[:6]}"
    _override_user(me)
    assert client.get("/api/me/dashboard").json()["widgets"] is None

    layout = [{"id": "tile-screening", "size": "sm"}, {"id": "chart-alerts-30d", "size": "lg"}]
    saved = client.put("/api/me/dashboard", json={"widgets": layout})
    assert saved.status_code == 200, saved.text
    assert saved.json()["widgets"] == layout
    assert client.get("/api/me/dashboard").json()["widgets"] == layout

    # Taille absente : md par defaut (et la sauvegarde REMPLACE la precedente)
    saved = client.put("/api/me/dashboard", json={"widgets": [{"id": "table-todo"}]})
    assert saved.json()["widgets"] == [{"id": "table-todo", "size": "md"}]
    assert client.get("/api/me/dashboard").json()["widgets"] == [{"id": "table-todo", "size": "md"}]

    reset = client.delete("/api/me/dashboard")
    assert reset.status_code == 200 and reset.json()["reset"] is True
    assert client.get("/api/me/dashboard").json()["widgets"] is None
    # Remise a zero idempotente : pas d'erreur quand il n'y a rien a effacer
    assert client.delete("/api/me/dashboard").json()["reset"] is False


def test_dashboard_layout_validations(client):
    _override_user(f"test_dash_{uuid.uuid4().hex[:6]}")

    def put(widgets):
        return client.put("/api/me/dashboard", json={"widgets": widgets})

    assert put([]).status_code == 400                                    # vide : passer par DELETE
    assert put([{"id": "tile-a", "size": "xxl"}]).status_code == 400     # taille inconnue
    assert put([{"id": "tile-a"}, {"id": "tile-a"}]).status_code == 400  # doublon
    assert put([{"id": "Tile_A!"}]).status_code == 400                   # identifiant malsain
    assert put([{"id": ""}]).status_code == 400
    assert put([{"size": "md"}]).status_code == 400                      # identifiant absent
    assert put([{"id": f"w{i}"} for i in range(31)]).status_code == 400  # plafond depasse
    assert put("nope").status_code in (400, 422)                         # pas une liste
    # Un refus ne doit rien avoir stocke
    assert client.get("/api/me/dashboard").json()["widgets"] is None


def test_dashboard_isolation_between_users(client):
    user_a = f"test_dash_{uuid.uuid4().hex[:6]}"
    user_b = f"test_dash_{uuid.uuid4().hex[:6]}"
    _override_user(user_a)
    assert client.put("/api/me/dashboard",
                      json={"widgets": [{"id": "tile-screening", "size": "sm"}]}).status_code == 200
    _override_user(user_b)
    assert client.get("/api/me/dashboard").json()["widgets"] is None
    assert client.put("/api/me/dashboard",
                      json={"widgets": [{"id": "table-jobs", "size": "lg"}]}).status_code == 200
    _override_user(user_a)
    assert client.get("/api/me/dashboard").json()["widgets"] == [
        {"id": "tile-screening", "size": "sm"}]

"""
Espace « Mon compte » :
- GET /api/me/profile : champs du profil enrichi + catalogue des categories ;
- PUT /api/users/me/profile : telephone/bio/fonction valides, opt-out de
  notifications par categorie (categorie inconnue refusee) ;
- PUT /api/me/avatar : data-URI image accepte, mauvais type et surpoids
  refuses, null retire la photo ;
- notifier : un compte qui a coupe une categorie (ou tout) ne recoit plus
  les emails de cette categorie, sans toucher au routage des autres.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, User
from fiskr.events import EVENT_CATALOG


def _make_user(db, username, role="admin", email=None):
    user = User(username=username, hashed_password="x", salt="y",
                full_name=username, role=role, email=email)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _override_user(user_id, username, role="admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": user_id, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


@pytest.fixture()
def ctx():
    db = next(get_db())
    me = _make_user(db, f"test_acct_{uuid.uuid4().hex[:6]}", email="me@test-acct.example")
    _override_user(me.id, me.username)
    yield {"db": db, "me": me, "client": TestClient(app)}
    app.dependency_overrides.pop(get_current_user, None)
    try:
        db.query(User).filter(User.username.like("test_acct_%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_profile_roundtrip_and_validation(ctx):
    client = ctx["client"]
    profile = client.get("/api/me/profile").json()
    assert profile["username"] == ctx["me"].username
    assert profile["notification_opt_out"] == []
    assert profile["notification_categories"], "catalogue des catégories attendu"

    updated = client.put("/api/users/me/profile", json={
        "full_name": "Test Acct", "phone": "+33 6 12 34 56 78",
        "job_title": "Analyste conformité", "bio": "Périmètre sanctions UE.",
    })
    assert updated.status_code == 200, updated.text
    user = updated.json()["user"]
    assert user["phone"] == "+33 6 12 34 56 78"
    assert user["job_title"] == "Analyste conformité"
    assert user["bio"] == "Périmètre sanctions UE."

    # Validations
    assert client.put("/api/users/me/profile",
                      json={"phone": "pas-un-numéro!"}).status_code == 400
    assert client.put("/api/users/me/profile",
                      json={"bio": "x" * 2001}).status_code == 400
    assert client.put("/api/users/me/profile",
                      json={"notification_opt_out": ["CATEGORIE_INVENTEE"]}).status_code == 400

    # Opt-out valide : une vraie catégorie + ALL
    any_cat = next(iter({e.category for e in EVENT_CATALOG.values()}))
    ok = client.put("/api/users/me/profile", json={"notification_opt_out": [any_cat]})
    assert ok.status_code == 200 and ok.json()["user"]["notification_opt_out"] == [any_cat]
    ok = client.put("/api/users/me/profile", json={"notification_opt_out": ["ALL"]})
    assert ok.status_code == 200


def test_avatar_upload_rules(ctx):
    client = ctx["client"]
    good = "data:image/jpeg;base64," + ("QUJD" * 50)
    assert client.put("/api/me/avatar", json={"avatar": good}).status_code == 200
    assert client.get("/api/me/profile").json()["avatar"] == good

    assert client.put("/api/me/avatar",
                      json={"avatar": "data:image/svg+xml;base64,QUJD"}).status_code == 400
    assert client.put("/api/me/avatar",
                      json={"avatar": "<script>alert(1)</script>"}).status_code == 400
    oversized = "data:image/png;base64," + ("A" * 400_001)
    assert client.put("/api/me/avatar", json={"avatar": oversized}).status_code == 400

    cleared = client.put("/api/me/avatar", json={"avatar": None})
    assert cleared.status_code == 200
    assert client.get("/api/me/profile").json()["avatar"] is None


def test_notifier_respects_personal_opt_out(ctx):
    from fiskr.notifier import resolve_recipients

    db = ctx["db"]
    # Un événement du catalogue avec une audience par rôle
    event_key, event = next((k, e) for k, e in EVENT_CATALOG.items()
                            if any(a not in ("_assignee", "_actor") for a in e.audience))
    role = next(a for a in event.audience if a not in ("_assignee", "_actor"))

    listening = _make_user(db, f"test_acct_{uuid.uuid4().hex[:6]}", role=role,
                           email="listening@test-acct.example")
    muted_cat = _make_user(db, f"test_acct_{uuid.uuid4().hex[:6]}", role=role,
                           email="muted-cat@test-acct.example")
    muted_cat.notification_opt_out = [event.category]
    muted_all = _make_user(db, f"test_acct_{uuid.uuid4().hex[:6]}", role=role,
                           email="muted-all@test-acct.example")
    muted_all.notification_opt_out = ["ALL"]
    db.commit()

    recipients = resolve_recipients(db, event_key, {})
    assert "listening@test-acct.example" in recipients
    assert "muted-cat@test-acct.example" not in recipients
    assert "muted-all@test-acct.example" not in recipients

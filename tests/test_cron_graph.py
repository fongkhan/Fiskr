"""
Tests du planificateur cron par source et de la visualisation du graphe :
- moteur cron 5 champs (parse, correspondance, prochaine occurrence, erreurs) ;
- planification a chaud par source (PUT /api/settings/sync, validation stricte)
  et exposition schedules + next_runs dans GET /api/sync/config ;
- endpoint GET /api/relationships/graph/{id} (BFS deux sens, profondeur,
  drapeau de detention majoritaire, garde de volume).
"""
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.cron import CronError, cron_matches, next_run, parse_cron
from fiskr.database import get_db, AppSetting, EntityRelationship, Snapshot, WatchlistEntity, AdminAuditLog
from fiskr.settings import SETTING_REQUIRE_APPROVAL, SETTING_SYNC_SCHEDULES


# ====================================================================
# 1. MOTEUR CRON
# ====================================================================

def test_cron_parse_valid_and_invalid():
    parse_cron("0 6 * * *")
    parse_cron("*/15 * * * *")
    parse_cron("30 7 1,15 * 1-5")
    parse_cron("0 8-18/2 * * 0")
    for bad in ("", "0 6 * *", "61 * * * *", "* 25 * * *", "* * 0 * *",
                "a * * * *", "* * * * 9", "5-1 * * * *", "*/0 * * * *"):
        with pytest.raises(CronError):
            parse_cron(bad)


def test_cron_matches_basics():
    # Quotidien a 6h00
    assert cron_matches("0 6 * * *", datetime(2026, 7, 23, 6, 0))
    assert not cron_matches("0 6 * * *", datetime(2026, 7, 23, 6, 1))
    # Pas d'un quart d'heure
    assert cron_matches("*/15 * * * *", datetime(2026, 7, 23, 10, 45))
    assert not cron_matches("*/15 * * * *", datetime(2026, 7, 23, 10, 50))
    # Jours ouvres (23/07/2026 = jeudi)
    assert cron_matches("30 7 * * 1-5", datetime(2026, 7, 23, 7, 30))
    assert not cron_matches("30 7 * * 1-5", datetime(2026, 7, 26, 7, 30))  # dimanche
    # 7 = dimanche = 0
    assert cron_matches("0 9 * * 7", datetime(2026, 7, 26, 9, 0))


def test_cron_dom_dow_or_rule():
    # dom ET dow restreints : l'un OU l'autre suffit (convention cron)
    expr = "0 6 1 * 1"  # le 1er du mois OU le lundi
    assert cron_matches(expr, datetime(2026, 7, 1, 6, 0))    # mercredi 1er -> dom
    assert cron_matches(expr, datetime(2026, 7, 20, 6, 0))   # lundi 20 -> dow
    assert not cron_matches(expr, datetime(2026, 7, 23, 6, 0))  # jeudi 23


def test_cron_next_run():
    after = datetime(2026, 7, 23, 10, 5)
    assert next_run("0 6 * * *", after) == datetime(2026, 7, 24, 6, 0)
    assert next_run("*/15 * * * *", after) == datetime(2026, 7, 23, 10, 15)
    # Strictement apres l'instant fourni
    assert next_run("0 6 * * *", datetime(2026, 7, 23, 6, 0)) == datetime(2026, 7, 24, 6, 0)
    # Motif annuel : trouve dans l'horizon
    assert next_run("0 0 1 1 *", after) == datetime(2027, 1, 1, 0, 0)


# ====================================================================
# 2. PLANIFICATION PAR SOURCE (API)
# ====================================================================

def _override_user(username: str, role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key.in_([
            SETTING_SYNC_SCHEDULES, SETTING_REQUIRE_APPROVAL,
        ])).delete(synchronize_session=False)
        db.query(EntityRelationship).filter(
            EntityRelationship.from_entity_id.like("CG-%")
        ).delete(synchronize_session=False)
        snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_cg_%")).all()
        ids = [s.snapshot_id for s in snaps]
        if ids:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(ids)).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username == "admin_cg").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("admin_cg", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def test_sync_schedules_roundtrip_and_validation(client):
    # Surcharge valide pour une source
    response = client.put("/api/settings/sync", json={"schedules": {"ofac": "0 */4 * * *"}})
    assert response.status_code == 200, response.text
    assert response.json()["schedules"]["ofac"] == "0 */4 * * *"

    cfg = client.get("/api/sync/config").json()
    assert cfg["schedules"]["ofac"] == "0 */4 * * *"
    # Les autres sources retombent sur l'horaire global quotidien (M H * * *)
    assert cfg["schedules"]["un"].endswith("* * *")
    # next_runs expose une prochaine occurrence par source
    assert cfg["next_runs"]["ofac"] is not None

    # Chaine vide = retour au defaut
    client.put("/api/settings/sync", json={"schedules": {"ofac": ""}})
    assert client.get("/api/sync/config").json()["schedules"]["ofac"].endswith("* * *")

    # Cron invalide -> 400 avec le nom de la source ; source inconnue -> 400
    bad = client.put("/api/settings/sync", json={"schedules": {"ofac": "99 * * * *"}})
    assert bad.status_code == 400 and "ofac" in bad.json()["detail"]
    assert client.put("/api/settings/sync", json={"schedules": {"interpol": "0 6 * * *"}}).status_code == 400


def test_sync_schedules_requires_admin(client):
    _override_user("simple_user", "user")
    assert client.put("/api/settings/sync", json={"schedules": {"ofac": "0 6 * * *"}}).status_code == 403


# ====================================================================
# 3. GRAPHE DES RELATIONS
# ====================================================================

def _upload_entities(client, rows):
    assert client.put("/api/settings/ingestion", json={"require_approval": False}).status_code == 200
    body = "entity_id,entity_type,primary_name,nationality\n" + "\n".join(
        f"{eid},{etype},{name},RU" for eid, etype, name in rows
    ) + "\n"
    response = client.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_cg_{uuid.uuid4().hex[:8]}.csv", body, "text/csv")},
    )
    assert response.status_code == 200, response.text


def test_relationship_graph_bfs_depth_and_majority(client):
    tag = uuid.uuid4().hex[:6].upper()
    a, b, c = f"CG-A-{tag}", f"CG-B-{tag}", f"CG-C-{tag}"
    _upload_entities(client, [
        (a, "E", f"Fille {tag}"), (b, "E", f"Mere {tag}"), (c, "I", f"Patron {tag}"),
    ])
    # A --OWNED_BY 60%--> B ; C --LEADER_OF--> B
    assert client.post("/api/relationships", json={
        "from_entity_id": a, "to_entity_id": b, "relation_type": "OWNED_BY", "ownership_pct": 60,
    }).status_code == 200
    assert client.post("/api/relationships", json={
        "from_entity_id": c, "to_entity_id": b, "relation_type": "LEADER_OF",
    }).status_code == 200

    # Profondeur 1 depuis A : A + B seulement (C est a 2 sauts)
    graph1 = client.get(f"/api/relationships/graph/{a}", params={"depth": 1}).json()
    assert {n["id"] for n in graph1["nodes"]} == {a, b}
    assert len(graph1["edges"]) == 1
    assert graph1["truncated"] is False

    # Profondeur 2 : le dirigeant de la mere apparait, profondeurs correctes
    graph2 = client.get(f"/api/relationships/graph/{a}", params={"depth": 2}).json()
    depths = {n["id"]: n["depth"] for n in graph2["nodes"]}
    assert depths == {a: 0, b: 1, c: 2}
    edges = {(e["from"], e["to"]): e for e in graph2["edges"]}
    assert edges[(a, b)]["majority"] is True          # 60 % -> regle des 50 %
    assert edges[(c, b)]["majority"] is False
    assert edges[(a, b)]["ownership_pct"] == 60
    # Types d'entites restitues pour le rendu (I vs E)
    types = {n["id"]: n["entity_type"] for n in graph2["nodes"]}
    assert types[c] == "I" and types[a] == "E"

    # Garde de profondeur invalide -> 422 (validation FastAPI)
    assert client.get(f"/api/relationships/graph/{a}", params={"depth": 9}).status_code == 422

"""
Historique des homologations : rouvrir une décision, des mois après.

Le rapport du cahier de tests vit sur le snapshot, mais son CONTEXTE se perd :
le delta d'une liste candidate se lit « par rapport à la production », et
approuver la liste EN FAIT la production. Après coup, un recalcul comparerait
le snapshot à lui-même et rendrait un delta vide — précisément l'information
qu'on cherche à conserver.

Le dossier est donc constitué AU MOMENT DE LA DÉCISION et n'est plus jamais
réécrit. Ces tests verrouillent cette garantie, qui est tout l'objet de la
fonctionnalité : ce qu'on relit des mois après est ce que le réviseur avait
sous les yeux.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (get_db, AppSetting, ReviewRecord, Snapshot,
                            WatchlistEntity)
from fiskr.settings import SETTING_REQUIRE_APPROVAL, SETTING_BACKTEST_REQUIRED

TAG = uuid.uuid4().hex[:6].upper()


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "revi_test", "full_name": "Réviseur",
        "role": "admin", "roles": ["admin"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    session = next(get_db())
    try:
        session.query(ReviewRecord).filter(
            ReviewRecord.file_name.like(f"%{TAG}%")).delete(synchronize_session=False)
        ids = [s.snapshot_id for s in session.query(Snapshot).filter(
            Snapshot.file_name.like(f"%{TAG}%")).all()]
        if ids:
            session.query(WatchlistEntity).filter(
                WatchlistEntity.snapshot_id.in_(ids)).delete(synchronize_session=False)
            session.query(Snapshot).filter(
                Snapshot.snapshot_id.in_(ids)).delete(synchronize_session=False)
        session.query(AppSetting).filter(AppSetting.key.in_(
            [SETTING_REQUIRE_APPROVAL, SETTING_BACKTEST_REQUIRED])).delete(
                synchronize_session=False)
        session.commit()
    finally:
        session.close()


def _ingest(client, lignes, approbation):
    assert client.put("/api/settings/ingestion",
                      json={"require_approval": approbation}).status_code == 200
    corps = "entity_id,entity_type,primary_name,nationality,dob\n" + "".join(
        f"{eid},I,{nom},RU,1970-01-01\n" for eid, nom in lignes)
    r = client.post("/api/ingest", data={"file_type": "WATCHLIST_EU"},
                    files={"file": (f"hist_{TAG}_{uuid.uuid4().hex[:4]}.csv",
                                    corps, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _production_puis_candidat(client):
    """Une liste en production, puis une candidate qui ajoute un listé."""
    assert client.put("/api/settings/gouvernance",
                      json={"backtest_required": False}).status_code in (200, 404)
    _ingest(client, [(f"H-{TAG}-1", f"Boris Histo{TAG}")], approbation=False)
    candidat = _ingest(client, [(f"H-{TAG}-1", f"Boris Histo{TAG}"),
                                (f"H-{TAG}-2", f"Igor Nouveau{TAG}")],
                       approbation=True)
    assert candidat["status"] == "PENDING_REVIEW", candidat
    return candidat["snapshot_id"]


def test_approval_freezes_the_delta_it_was_decided_on(client):
    """LA garantie : le delta du dossier est celui d'AVANT la bascule. Calculé
    après, il comparerait le snapshot à lui-même et serait vide."""
    sid = _production_puis_candidat(client)

    avant = client.get(f"/api/review/snapshots/{sid}").json()
    assert avant["delta_summary"]["added_count"] == 1, avant["delta_summary"]

    r = client.post(f"/api/review/snapshots/{sid}/approve",
                    json={"comment": f"Homologué {TAG}"})
    assert r.status_code == 202, r.text

    histo = client.get("/api/review/history").json()["history"]
    dossier = next(h for h in histo if h["snapshot_id"] == sid)
    assert dossier["decision"] == "APPROVED"
    assert dossier["decided_by"] == "revi_test"
    assert dossier["comment"] == f"Homologué {TAG}"
    # Le delta figé est celui d'avant la mise en production
    assert dossier["delta_summary"]["added_count"] == 1, (
        "delta perdu : le dossier a été constitué après la bascule")


def test_reopened_record_still_holds_the_delta_details(client):
    """« Revoir le delta » : les détails doivent être relisibles, pas seulement
    les compteurs."""
    sid = _production_puis_candidat(client)
    client.post(f"/api/review/snapshots/{sid}/approve", json={"comment": "ok"})

    dossier = next(h for h in client.get("/api/review/history").json()["history"]
                   if h["snapshot_id"] == sid)
    detail = client.get(f"/api/review/history/{dossier['id']}").json()
    ajoutes = (detail["delta_details"] or {}).get("details", {}).get("added", [])
    assert ajoutes, detail["delta_details"]
    assert any(f"H-{TAG}-2" in str(a) for a in ajoutes), ajoutes
    assert detail["production_snapshot_id"], "la base de comparaison doit être tracée"


def test_record_survives_the_next_synchronisation(client):
    """Le dossier ne bouge pas quand la liste est remplacée ensuite : c'est la
    raison d'être de l'historisation."""
    sid = _production_puis_candidat(client)
    client.post(f"/api/review/snapshots/{sid}/approve", json={"comment": "ok"})
    dossier = next(h for h in client.get("/api/review/history").json()["history"]
                   if h["snapshot_id"] == sid)
    gele = client.get(f"/api/review/history/{dossier['id']}").json()

    # Une nouvelle liste arrive et remplace la précédente
    _ingest(client, [(f"H-{TAG}-1", f"Boris Histo{TAG}"),
                     (f"H-{TAG}-2", f"Igor Nouveau{TAG}"),
                     (f"H-{TAG}-3", f"Sergei Encore{TAG}")], approbation=False)

    relu = client.get(f"/api/review/history/{dossier['id']}").json()
    assert relu["delta_summary"] == gele["delta_summary"]
    assert relu["delta_details"] == gele["delta_details"]
    assert relu["snapshot_status"] in ("READY", "SUPERSEDED")


def test_rejection_is_historised_too(client):
    """Un rejet est une décision : la trace de ce qui a été écarté, et de ce
    qu'on avait sous les yeux pour l'écarter."""
    sid = _production_puis_candidat(client)
    r = client.post(f"/api/review/snapshots/{sid}/reject",
                    json={"comment": f"Écart trop élevé {TAG}"})
    assert r.status_code == 200, r.text

    dossier = next(h for h in client.get("/api/review/history").json()["history"]
                   if h["snapshot_id"] == sid)
    assert dossier["decision"] == "REJECTED"
    assert dossier["comment"] == f"Écart trop élevé {TAG}"
    assert dossier["delta_summary"]["added_count"] == 1


def test_history_filters_run_on_the_server(client):
    """Un filtre appliqué au navigateur ne verrait que la page affichée ; une
    recherche d'audit porte sur tout l'historique."""
    sid = _production_puis_candidat(client)
    client.post(f"/api/review/snapshots/{sid}/approve", json={"comment": "ok"})

    approuves = client.get("/api/review/history?decision=APPROVED").json()
    assert all(h["decision"] == "APPROVED" for h in approuves["history"])
    rejetes = client.get("/api/review/history?decision=REJECTED").json()
    assert all(h["decision"] == "REJECTED" for h in rejetes["history"])
    par_liste = client.get("/api/review/history?file_type=WATCHLIST_EU").json()
    assert all(h["file_type"] == "WATCHLIST_EU" for h in par_liste["history"])
    # Le total est celui du FILTRE, pas de la page
    assert par_liste["total"] >= len(par_liste["history"])


def test_history_is_paginated(client):
    """L'historique grossit sans fin : il se lit par pages."""
    page = client.get("/api/review/history?limit=1&offset=0").json()
    assert page["limit"] == 1 and page["offset"] == 0
    assert len(page["history"]) <= 1


def test_unknown_record_is_a_clean_404(client):
    assert client.get("/api/review/history/99999999").status_code == 404

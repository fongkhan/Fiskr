"""
Rapports de synchronisation et journal d'audit : la ligne de liste ne
transporte plus la pièce lourde.

Mesuré sur la production :

* `GET /api/sync/reports?limit=25` = 581 Ko, dont **98,9 %** de `delta_report`
  (des milliers d'ajouts/modifications par source) alors que le tableau n'en
  tirait qu'un compteur d'échecs partiels ;
* `GET /api/history?page_size=25` = 138 Ko, dont **97 %** de `config_state` et
  `decision_tree`, que le tableau n'affiche pas du tout — seule la modale
  d'inspection les lit, sur UNE décision à la fois.

Les deux listes acceptent désormais `include_details=false` et le détail
complet se lit fiche par fiche. Le défaut reste `true` : ces deux journaux
sont des pièces opposables et les intégrations qui les archivent doivent
continuer à tout recevoir — c'est ce que verrouille le premier test de chaque
paire.
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, SyncReport, AuditTrail

TAG = uuid.uuid4().hex[:6].upper()
APP_JS = Path("fiskr/static/app.js").read_text(encoding="utf-8")

# Delta volumineux, représentatif d'une synchronisation OFAC réelle
GROS_DELTA = {
    "summary": {"added_count": 400, "modified_count": 0, "removed_count": 0},
    "added": [{"entity_id": f"E{i}", "primary_name": f"Profil {i} " * 10}
              for i in range(400)],
    "fetch_failures": ["https://exemple/acte-1", "https://exemple/acte-2"],
    "pdf_failures": ["https://exemple/pdf-1"],
}
GROS_ARBRE = {"watchlist_entity": {"_list_type": "OFAC"},
              "adjustments": {"dob": {"score": 0, "description": "x" * 4000}}}
GROS_CONFIG = {"weights": {f"w{i}": i for i in range(500)}}


@pytest.fixture()
def contexte():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "pay", "full_name": "pay", "role": "admin",
        "roles": ["admin"]}
    db = next(get_db())
    rapport = SyncReport(
        source="OFAC", executed_at=datetime.utcnow(), trigger="MANUAL",
        status="SUCCESS", message=f"Rapport {TAG}",
        snapshot_id=f"snap-{TAG}", previous_snapshot_id=None,
        added_count=400, modified_count=0, removed_count=0,
        delta_report=GROS_DELTA, email_sent=False)
    decision = AuditTrail(
        timestamp=datetime.utcnow(), client_id=f"C-{TAG}",
        client_name=f"Client {TAG}", client_type="I",
        watchlist_id=f"WL-{TAG}", watchlist_name=f"Liste {TAG}",
        base_score=90.0, final_score=95.0, status="ALERT",
        list_type=None, decision_tree=GROS_ARBRE, config_state=GROS_CONFIG,
        watchlist_version="v1", watchlist_hash="abc123")
    db.add_all([rapport, decision])
    db.commit()
    ids = (rapport.id, decision.id)
    yield db, TestClient(app), ids
    db.query(SyncReport).filter(SyncReport.id == ids[0]).delete()
    db.query(AuditTrail).filter(AuditTrail.id == ids[1]).delete()
    db.commit()
    db.close()
    app.dependency_overrides.pop(get_current_user, None)


def _ligne(items, cle, valeur):
    return next(i for i in items if i[cle] == valeur)


# --------------------- rapports de synchronisation ---------------------

def test_sync_reports_sert_tout_par_defaut(contexte):
    """Contrat public inchangé : sans paramètre, le delta est toujours là."""
    _, client, (rid, _) = contexte
    items = client.get("/api/sync/reports?limit=50").json()
    ligne = _ligne(items, "id", rid)
    assert ligne["delta_report"]["summary"]["added_count"] == 400


def test_sync_reports_allege_omet_le_delta(contexte):
    _, client, (rid, _) = contexte
    items = client.get("/api/sync/reports?limit=50&include_details=false").json()
    ligne = _ligne(items, "id", rid)
    assert "delta_report" not in ligne
    # ... mais garde tout le reste
    for champ in ("id", "source", "executed_at", "trigger", "status", "message",
                  "snapshot_id", "previous_snapshot_id", "added_count",
                  "modified_count", "removed_count", "email_sent"):
        assert champ in ligne, champ


def test_le_compteur_d_echecs_partiels_survit_a_l_allegement(contexte):
    """Le badge du tableau comptait fetch_failures + pdf_failures dans le
    delta. Sans le delta, il lui faut le compteur dérivé — sinon un échec
    partiel devient invisible dans l'écran de suivi des sources."""
    _, client, (rid, _) = contexte
    items = client.get("/api/sync/reports?limit=50&include_details=false").json()
    assert _ligne(items, "id", rid)["partial_failures"] == 3


def test_detail_de_rapport_rend_le_delta_complet(contexte):
    _, client, (rid, _) = contexte
    detail = client.get(f"/api/sync/reports/{rid}").json()
    assert detail["delta_report"]["summary"]["added_count"] == 400
    assert len(detail["delta_report"]["added"]) == 400
    assert client.get("/api/sync/reports/99999999").status_code == 404


def test_la_route_detail_n_avale_pas_la_liste(contexte):
    """Piège de routage déjà rencontré : une route `/prefixe/{id}` déclarée
    au mauvais endroit capture la liste. Les deux doivent répondre."""
    _, client, _ = contexte
    assert client.get("/api/sync/reports?limit=1").status_code == 200
    assert client.get("/api/sync/config").status_code == 200


def test_poids_de_la_liste_de_rapports(contexte):
    _, client, _ = contexte
    lourd = len(client.get("/api/sync/reports?limit=50").content)
    leger = len(client.get("/api/sync/reports?limit=50&include_details=false").content)
    assert leger < lourd / 10, f"{lourd} -> {leger} octets"


# ----------------------------- journal d'audit -----------------------------

def test_history_sert_tout_par_defaut(contexte):
    """Le journal d'audit est opposable : le défaut ne retire rien."""
    _, client, (_, aid) = contexte
    items = client.get("/api/history?page_size=50").json()["items"]
    ligne = _ligne(items, "id", aid)
    assert ligne["decision_tree"] == GROS_ARBRE
    assert ligne["config_state"] == GROS_CONFIG


def test_history_allege_omet_arbre_et_configuration(contexte):
    _, client, (_, aid) = contexte
    items = client.get("/api/history?page_size=50&include_details=false").json()["items"]
    ligne = _ligne(items, "id", aid)
    assert "decision_tree" not in ligne
    assert "config_state" not in ligne


def test_le_repli_de_type_de_liste_survit_a_l_allegement(contexte):
    """`list_type` est NULL sur les décisions antérieures à la colonne et se
    relit dans le decision_tree. Ce repli est calculé côté serveur : il doit
    rester juste alors même que l'arbre n'est plus transporté."""
    _, client, (_, aid) = contexte
    items = client.get("/api/history?page_size=50&include_details=false").json()["items"]
    assert _ligne(items, "id", aid)["list_type"] == "OFAC"


def test_detail_de_decision_rend_arbre_et_configuration(contexte):
    _, client, (_, aid) = contexte
    detail = client.get(f"/api/history/{aid}").json()
    assert detail["decision_tree"] == GROS_ARBRE
    assert detail["config_state"] == GROS_CONFIG
    assert detail["list_type"] == "OFAC"
    assert client.get("/api/history/99999999").status_code == 404


def test_poids_du_journal_d_audit(contexte):
    _, client, _ = contexte
    lourd = len(client.get("/api/history?page_size=50").content)
    leger = len(client.get("/api/history?page_size=50&include_details=false").content)
    assert leger < lourd / 5, f"{lourd} -> {leger} octets"


# ------------------------- cohérence avec le frontal -------------------------

def test_le_frontal_demande_bien_les_listes_allegees():
    assert '"/api/sync/reports?include_details=false"' in APP_JS
    assert 'params.set("include_details", "false")' in APP_JS
    assert '"/api/history?page_size=8&include_details=false"' in APP_JS


def test_le_frontal_charge_les_details_a_l_ouverture():
    assert "/api/sync/reports/${encodeURIComponent(report.id)}" in APP_JS
    assert "/api/history/${encodeURIComponent(logId)}" in APP_JS
    # Les deux ouvertures de détail sont devenues asynchrones
    assert "async function showSyncReportDetail(report)" in APP_JS
    assert "async function viewAuditLogDetail(logId)" in APP_JS


def test_le_tableau_des_rapports_lit_le_compteur_derive():
    """Si quelqu'un remet un calcul depuis `delta_report` dans le tableau, il
    comptera zéro en silence sur la liste allégée."""
    assert "report.partial_failures" in APP_JS
    assert "delta.fetch_failures" not in APP_JS

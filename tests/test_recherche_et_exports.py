"""
Lot 3 du programme qualité de vie : recherche & navigation.

* **Les clients dans la palette** (n° 17) — Ctrl+K cherchait les listés et
  les alertes, jamais la base clients : « Dupont » ne rendait pas SES clients.
* **La palette trouve les réglages** (n° 18) — « SMTP », « seuil » mènent à la
  carte concernée. L'index est DÉRIVÉ du balisage (titres de cartes), jamais
  recopié : une carte ajoutée est trouvable sans toucher la palette.
* **Les exports manquants** (n° 20) — cinq tableaux d'exploitation n'avaient
  pas de CSV. Le journal d'administration et celui des envois passent par le
  serveur — le premier est paginé côté serveur, un export de « ce qui est
  affiché » n'en montrerait qu'une page, et c'est la pièce qu'un contrôle
  demande entière. Comptes, sources et équivalences s'exportent tels
  qu'affichés, filtres appliqués, avec la MÊME neutralisation d'injection de
  formules que le serveur.
"""
import csv as csv_module
import io
import os
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (AdminAuditLog, ClientEntity, NotificationDelivery,
                            Snapshot, get_db)

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


@pytest.fixture(scope="module")
def app_js():
    with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as f:
        return f.read()


def _connecte(role="admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": f"rx_{role}", "full_name": role, "role": role, "roles": [role],
    }


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# ------------------------------------------------- clients dans la palette

@pytest.fixture
def client_cherchable():
    db = next(get_db())
    snap_id = f"cb-{uuid.uuid4().hex[:8]}"
    ref = f"PAL-{uuid.uuid4().hex[:6]}"
    db.add(Snapshot(snapshot_id=snap_id, file_type="CLIENT_BASE", file_name="c.csv",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY",
                    uploaded_at=datetime.utcnow()))
    db.add(ClientEntity(snapshot_id=snap_id, client_id=ref, client_type="PP",
                        client_first_name="Palettine", client_last_name="Recherchable",
                        entity_checksum=f"ck-{ref}"))
    db.commit()
    yield ref
    db.query(ClientEntity).filter(ClientEntity.snapshot_id == snap_id).delete(synchronize_session=False)
    db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_la_recherche_globale_rend_aussi_les_clients(client, client_cherchable):
    _connecte("user")
    corps = client.get("/api/search/quick?q=Palettine").json()
    assert "clients" in corps, "la réponse doit porter la section clients"
    assert corps["clients"]["total"] >= 1
    trouve = corps["clients"]["items"][0]
    assert trouve["client_id"] == client_cherchable
    assert "Palettine" in trouve["name"]


def test_un_client_hors_production_n_apparait_pas(client):
    """La palette montre le référentiel EN PRODUCTION : une base en attente
    d'homologation n'a rien à y faire."""
    db = next(get_db())
    snap_id = f"cb-{uuid.uuid4().hex[:8]}"
    try:
        db.add(Snapshot(snapshot_id=snap_id, file_type="CLIENT_BASE", file_name="c.csv",
                        file_hash=uuid.uuid4().hex, record_count=1, status="PENDING_REVIEW",
                        uploaded_at=datetime.utcnow()))
        db.add(ClientEntity(snapshot_id=snap_id, client_id="HORSPROD-1", client_type="PP",
                            client_first_name="Fantomas", client_last_name="Horsprod",
                            entity_checksum=f"ck-{uuid.uuid4().hex[:8]}"))
        db.commit()
        _connecte("user")
        corps = client.get("/api/search/quick?q=Fantomas").json()
        assert corps["clients"]["total"] == 0
    finally:
        db.query(ClientEntity).filter(ClientEntity.snapshot_id == snap_id).delete(synchronize_session=False)
        db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_la_palette_affiche_le_groupe_clients(app_js):
    debut = app_js.index("async function runPaletteSearch(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "data.clients" in corps
    assert "openClient360(c2.client_id)" in corps


# ------------------------------------------------- réglages dans la palette

def test_l_index_des_reglages_est_derive_du_balisage(app_js):
    """Le vocabulaire est dérivé, jamais recopié : la palette lit les titres
    de cartes au moment de la frappe — pas de liste à maintenir."""
    debut = app_js.index("async function runPaletteSearch(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert '[id^="sub-sec-settings-"] .card h2' in corps
    assert '.card h3' in corps, (
        "les sous-cartes (SLA, notifications métier, webhooks) portent des h3 : "
        "un index limité aux h2 les manquerait")
    assert 'classList.contains("hidden")' in corps, (
        "une carte masquée (rôle insuffisant) ne doit pas être proposée : "
        "l'écran serait vide à l'arrivée")


# ------------------------------------------------- exports serveur

def test_le_journal_d_administration_s_exporte_en_entier(client):
    db = next(get_db())
    marqueur = f"EXPORT-{uuid.uuid4().hex[:6]}"
    try:
        db.add(AdminAuditLog(username="test-export", action="SETTINGS_UPDATED",
                             target=marqueur, detail="ligne de test d'export"))
        db.commit()
        _connecte("admin")
        reponse = client.get("/api/export/admin-log.csv")
        assert reponse.status_code == 200
        assert "text/csv" in reponse.headers["content-type"]
        # Le CSV maison est en point-virgule avec BOM UTF-8 (Excel français)
        lignes = list(csv_module.reader(io.StringIO(reponse.text.lstrip("\ufeff")), delimiter=";"))
        assert lignes[0][:4] == ["id", "horodatage", "utilisateur", "action"]
        assert any(marqueur in l for ligne in lignes for l in ligne)
    finally:
        db.query(AdminAuditLog).filter(AdminAuditLog.target == marqueur).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_l_auditeur_exporte_le_journal_l_analyste_non(client):
    """L'export suit la garde de la lecture : admin ou auditeur — c'est une
    pièce de contrôle, pas un écran d'analyste."""
    _connecte("auditor")
    assert client.get("/api/export/admin-log.csv").status_code == 200
    _connecte("user")
    assert client.get("/api/export/admin-log.csv").status_code == 403


def test_le_journal_des_envois_s_exporte_avec_son_filtre(client):
    db = next(get_db())
    try:
        db.add(NotificationDelivery(event_key="test_export_notif", urgency="immediate",
                                    status="FAILED", error="=1+1 tentative d'injection",
                                    recipients="a@b.c", created_at=datetime.utcnow()))
        db.commit()
        _connecte("admin")
        reponse = client.get("/api/export/notifications.csv?status=FAILED")
        assert reponse.status_code == 200
        assert "test_export_notif" in reponse.text
        # La neutralisation d'injection : une cellule commençant par = est préfixée
        assert "'=1+1" in reponse.text
        mauvais = client.get("/api/export/notifications.csv?status=NIMPORTE")
        assert mauvais.status_code == 400
    finally:
        db.query(NotificationDelivery).filter(
            NotificationDelivery.event_key == "test_export_notif").delete(synchronize_session=False)
        db.commit()
        db.close()


# ------------------------------------------------- export de l'affiché

def test_l_export_affiche_neutralise_les_formules_comme_le_serveur(app_js):
    """Même règle des deux côtés : une cellule commençant par = + - @ est
    préfixée d'une apostrophe — sinon un nom forgé s'exécute dans le tableur
    de l'analyste."""
    debut = app_js.index("function _csvNeutralise(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "^[=+\\-@]" in corps


def test_l_export_affiche_respecte_les_filtres(app_js):
    debut = app_js.index("function exporterTableAffichee(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert 'classList.contains("filtered-out")' in corps, (
        "exporter des lignes que le filtre cache trahirait « ce que je vois »")
    assert "table-group-row" in corps


def test_les_cinq_ecrans_ont_leur_bouton():
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert 'onclick="exportAdminLogCsv()"' in html
    assert 'onclick="exportNotificationsCsv()"' in html
    for table, nom in (("users-table", "comptes"), ("sources-table", "sources"),
                       ("mining-table", "equivalences")):
        assert f"exporterTableAffichee('{table}', 'fiskr_{nom}')" in html, table

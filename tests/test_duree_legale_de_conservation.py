"""
Trente jours ne sont pas cinq ans.

`RETENTION_MIN_DAYS` vaut 30 : c'est un garde-fou **technique**, il empêche de
vider la base par mégarde. L'article **L561-12 du Code monétaire et
financier** impose, lui, de conserver **cinq ans** les documents et
informations relatifs aux opérations et à la relation d'affaires — pour Fiskr,
la preuve que le criblage a eu lieu et ce qu'il a décidé (journal de criblage)
et le traitement des alertes avec sa justification (alertes clôturées).

Les deux ne se ressemblent pas, et seul le premier était écrit quelque part.
Un administrateur pouvait donc régler le journal de criblage sur 31 jours :
accepté sans un mot, et un mois plus tard la preuve n'existait plus. C'est le
genre de défaut qui ne se voit pas le jour où on le commet, mais le jour où un
contrôleur demande à voir — et il ne se répare pas, la preuve détruite ne se
reconstitue pas.

Ce qui est fait ici n'est **pas** un interdit : une installation hors de France
peut relever d'une autre règle, et c'est la décision de l'exploitant. Ce qui
est refusé, c'est que le choix se fasse sans le savoir. Le plancher est nommé,
l'API l'annonce, le journal d'administration le trace, l'écran de mise en
service le rappelle, et chaque purge concernée le redit.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import AdminAuditLog, AppSetting, AuditTrail, get_db
from fiskr.settings import (DUREE_LEGALE_JOURS, RETENTION_FAMILLES_PROBANTES,
                            RETENTION_MIN_DAYS, SETTING_RETENTION,
                            retention_sous_la_duree_legale)


@pytest.fixture
def client_admin():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin_conservation", "full_name": "Admin",
        "role": "admin", "roles": ["admin"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    db = next(get_db())
    try:
        db.query(AppSetting).filter(
            AppSetting.key == SETTING_RETENTION).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(
            AdminAuditLog.username == "admin_conservation").delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(
            AdminAuditLog.username == "test-retention").delete(synchronize_session=False)
        db.query(AuditTrail).filter(
            AuditTrail.client_name == "Ancien Dossier").delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_le_plancher_legal_vaut_cinq_ans():
    assert DUREE_LEGALE_JOURS == 5 * 365


def test_le_garde_fou_technique_ne_se_confond_pas_avec_l_obligation():
    """S'ils devenaient égaux, l'un des deux aurait été écrasé par l'autre —
    et le message dirait la mauvaise chose."""
    assert RETENTION_MIN_DAYS < DUREE_LEGALE_JOURS


@pytest.mark.parametrize("famille", RETENTION_FAMILLES_PROBANTES)
def test_une_purge_courte_sur_une_famille_probante_est_signalee(famille):
    ecarts = retention_sous_la_duree_legale({famille: 31})
    assert len(ecarts) == 1
    assert ecarts[0]["famille"] == famille
    assert "L561-12" in ecarts[0]["message"]
    assert str(DUREE_LEGALE_JOURS) in ecarts[0]["message"]


def test_la_conservation_illimitee_ne_declenche_rien():
    """0 = illimité, c'est le défaut du produit et c'est le plus sûr."""
    assert retention_sous_la_duree_legale(
        {f: 0 for f in RETENTION_FAMILLES_PROBANTES}) == []


def test_cinq_ans_pile_suffisent():
    assert retention_sous_la_duree_legale(
        {f: DUREE_LEGALE_JOURS for f in RETENTION_FAMILLES_PROBANTES}) == []


def test_les_familles_d_exploitation_ne_sont_pas_concernees():
    """Rapports de synchronisation et campagnes batch sont utiles, pas
    probants : leur imposer cinq ans serait une exigence inventée."""
    assert retention_sous_la_duree_legale(
        {"sync_reports": 31, "batch_campaigns": 31}) == []


def test_une_politique_illisible_ne_fait_pas_tomber_le_controle():
    """Un réglage corrompu ne doit pas rendre l'écran de réglages inutilisable."""
    assert retention_sous_la_duree_legale({"audit_trail": "trente"}) == []
    assert retention_sous_la_duree_legale({}) == []


# ------------------------------------------------- ce que l'API en dit

def test_l_api_annonce_le_plancher_et_l_ecart(client_admin):
    reponse = client_admin.put("/api/settings/retention", json={"audit_trail": 60})
    assert reponse.status_code == 200
    corps = reponse.json()
    assert corps["duree_legale_jours"] == DUREE_LEGALE_JOURS
    assert corps["avertissements"], "un réglage sous le plancher doit être annoncé"
    assert "L561-12" in corps["avertissements"][0]

    lecture = client_admin.get("/api/admin/retention").json()
    assert lecture["duree_legale_jours"] == DUREE_LEGALE_JOURS
    assert [e["famille"] for e in lecture["sous_la_duree_legale"]] == ["audit_trail"]
    # Le minimum technique reste rendu, distinctement
    assert lecture["min_days"] == RETENTION_MIN_DAYS


def test_le_reglage_reste_accepte(client_admin):
    """Le produit informe, il ne décide pas à la place de l'exploitant."""
    reponse = client_admin.put("/api/settings/retention", json={"closed_alerts": 90})
    assert reponse.status_code == 200
    assert reponse.json()["policy"]["closed_alerts"] == 90


def test_le_choix_est_trace_au_journal_d_administration(client_admin):
    """
    Un contrôle qui arrive deux ans plus tard doit pouvoir retrouver qui a
    raccourci la conservation, quand, et de combien.
    """
    client_admin.put("/api/settings/retention", json={"audit_trail": 45})
    db = next(get_db())
    try:
        trace = db.query(AdminAuditLog).filter(
            AdminAuditLog.action == "SETTINGS_UPDATED",
            AdminAuditLog.target == "retention",
        ).order_by(AdminAuditLog.id.desc()).first()
        assert trace is not None
        assert trace.detail and "L561-12" in trace.detail
        assert trace.after.get("audit_trail") == 45
    finally:
        db.close()


def test_un_reglage_conforme_ne_pollue_pas_le_journal(client_admin):
    client_admin.put("/api/settings/retention", json={"audit_trail": DUREE_LEGALE_JOURS})
    db = next(get_db())
    try:
        trace = db.query(AdminAuditLog).filter(
            AdminAuditLog.action == "SETTINGS_UPDATED",
            AdminAuditLog.target == "retention",
        ).order_by(AdminAuditLog.id.desc()).first()
        assert trace.detail is None
    finally:
        db.close()


# --------------------------------------- ce que l'écran de mise en service dit

def test_la_mise_en_service_rappelle_l_ecart(client_admin):
    """
    Une politique se change un jour et se vit des années : l'avertissement de
    l'instant du réglage ne suffit pas. Le point doit rester lisible longtemps
    après la personne qui l'a décidé.
    """
    from fiskr.mise_en_service import etat_de_mise_en_service, ATTENTION, OK

    client_admin.put("/api/settings/retention", json={"audit_trail": 60})
    db = next(get_db())
    try:
        controles = {c["cle"]: c for c in etat_de_mise_en_service(db)["controles"]}
        assert controles["conservation"]["etat"] == ATTENTION
        assert "L561-12" in controles["conservation"]["constat"]

        client_admin.put("/api/settings/retention", json={"audit_trail": 0})
        controles = {c["cle"]: c for c in etat_de_mise_en_service(db)["controles"]}
        assert controles["conservation"]["etat"] == OK
    finally:
        db.close()


# ------------------------------------------------ ce que la purge en dit

def test_la_purge_ecrit_ce_qu_elle_emporte(client_admin):
    """
    Dernière ligne : la purge ne s'interrompt pas — ce serait décider à la
    place de l'exploitant sans lui laisser de moyen d'agir — mais elle inscrit
    dans le journal que ce qu'elle a détruit l'était sous le plancher légal.
    """
    from fiskr.retention import run_retention

    db = next(get_db())
    try:
        client_admin.put("/api/settings/retention", json={"audit_trail": 30})
        vieux = AuditTrail(
            client_id=f"RET-{uuid.uuid4().hex[:8]}", client_name="Ancien Dossier",
            client_type="PP", watchlist_id="—", decision_tree={}, config_state={},
            watchlist_version="test", watchlist_hash="test",
            watchlist_name="—", status="NO_MATCH", base_score=0.0, final_score=0.0,
            timestamp=datetime.utcnow() - timedelta(days=400))
        db.add(vieux)
        db.commit()

        supprimes = run_retention(db, username="test-retention")
        assert supprimes["audit_trail"] >= 1

        trace = db.query(AdminAuditLog).filter(
            AdminAuditLog.action == "RETENTION_PURGE",
        ).order_by(AdminAuditLog.id.desc()).first()
        assert trace is not None
        assert "SOUS LA DURÉE LÉGALE" in trace.detail
        assert "audit_trail" in trace.after.get("sous_la_duree_legale", [])
    finally:
        client_admin.put("/api/settings/retention", json={"audit_trail": 0})
        db.close()

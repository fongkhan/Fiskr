"""
La revue de vigilance renforcée d'un dossier PPE échue prévient, une fois, et
bloque la mise en service si elle dure.

Constat (rapport du lot « dossier PPE ») : la prochaine revue, datée à la
décision d'entrer ou de rester en relation (CMF R.561-20-2 3°), était signalée
échue À L'ÉCRAN, et c'était tout — aucune notification, aucun contrôle. Et rien
ne permettait de la tenir : un dossier décidé ne se réécrit plus.

Ce que ces tests tiennent :
- à l'échéance, une notification immédiate `ppe_revue_echue` et une trace au
  journal de l'alerte — une fois par échéance, pas à chaque tic ;
- échue depuis plus que le délai de décision PPE, la revue rend le contrôle de
  mise en service BLOQUANT, rejoué par la sonde sans nom de personne ;
- la revue se tient (`POST /api/ppe/{id}/revue`) par une personne habilitée
  qui n'a pas instruit l'alerte, fixe la revue suivante, et lève le signal ;
  la revue suivante, échue à son tour, prévient de nouveau.
Une base isolée par test : le contrôle et la sonde portent sur toute
l'installation.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import api as api_mod
from fiskr import mise_en_service as mes
from fiskr import ppe
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import Alert, AlertEvent, AuditTrail, Base, DossierPPE, get_db
from fiskr.settings import SETTING_NOTIFICATION_BATCH, set_setting


def _connecter(username, roles=("reviewer",)):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": ",".join(roles), "roles": list(roles),
    }


@pytest.fixture
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'revue.sqlite3'}")
    Base.metadata.create_all(engine)
    fabrique = sessionmaker(bind=engine)
    session = fabrique()

    def _db():
        s = fabrique()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    emissions = []
    espion = lambda db, cle, charge=None, **k: emissions.append((cle, charge or {}))
    import fiskr.notifier as notifier_mod
    monkeypatch.setattr(api_mod, "emit", espion)
    monkeypatch.setattr(notifier_mod, "emit", espion)
    monkeypatch.setattr(api_mod, "get_db", lambda: iter([fabrique()]))
    set_setting(session, SETTING_NOTIFICATION_BATCH, {"enabled": False, "cron": "0 * * * *"})
    yield {"db": session, "client": TestClient(app), "emissions": emissions}
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    session.close()
    engine.dispose()


def _dossier_decide(db, revue_dans_jours):
    """Un dossier décidé — maintien autorisé, pièces au dossier — dont la revue
    tombe dans `revue_dans_jours` jours (négatif : échue depuis)."""
    audit = AuditTrail(client_id="CLI-PPE", client_name="Client Revue", client_type="PP",
                       watchlist_id="PEP-1", watchlist_name="Listé Revue", base_score=91.0,
                       final_score=91.0, status="ALERT", decision_tree={}, config_state={},
                       watchlist_version="t", watchlist_hash="t", list_type="WATCHLIST_PEP")
    db.add(audit)
    db.commit()
    alerte = Alert(audit_id=audit.id, client_id="CLI-PPE", client_name="Client Revue",
                   watchlist_entity_id="PEP-1", watchlist_name="Listé Revue", final_score=91.0,
                   list_type="WATCHLIST_PEP", status="CLOSED_CONFIRMED", channel="SCREENING",
                   priority="HIGH", proposed_by="analyste", decided_by="valideur",
                   decided_at=datetime.utcnow() - timedelta(days=400))
    db.add(alerte)
    db.commit()
    dossier = DossierPPE(
        alert_id=alerte.id, statut=ppe.STATUT_DECIDE, client_id="CLI-PPE",
        client_name="Client Revue", watchlist_entity_id="PEP-1", watchlist_name="Listé Revue",
        list_type="WATCHLIST_PEP", qualite_source=ppe.QUALITE_PPE, qualite_retenue=ppe.QUALITE_PPE,
        origine_fonds="Salaires.", origine_patrimoine="Résidence (acte).", pieces_origine=[1],
        ouvert_le=datetime.utcnow() - timedelta(days=400), ouvert_par="valideur",
        proposee_par="analyste", confirmee_par="valideur", decision="MAINTIEN",
        decision_motif="Documentée.", decidee_le=datetime.utcnow() - timedelta(days=395),
        decidee_par="direction",
        prochaine_revue_le=datetime.utcnow() + timedelta(days=revue_dans_jours))
    db.add(dossier)
    db.commit()
    return dossier


def _signalements(base):
    return [p for k, p in base["emissions"] if k == "ppe_revue_echue"]


def _actions(db, alert_id, action):
    db.expire_all()
    return db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id,
                                       AlertEvent.action == action).count()


def test_une_revue_echue_previent_une_fois_et_pas_a_chaque_tic(base):
    """Avant : aucune notification, à aucun tic — l'échéance ne se lisait qu'en
    ouvrant le registre filtré « revue échue »."""
    db = base["db"]
    dossier = _dossier_decide(db, revue_dans_jours=-1)
    api_mod._notification_batch_tick()
    signales = _signalements(base)
    assert len(signales) == 1, base["emissions"]
    assert signales[0]["Dossier"] == f"#{dossier.id}" and signales[0]["Client"] == "Client Revue"
    assert _actions(db, dossier.alert_id, "PPE_REVUE_ECHUE") == 1

    api_mod._notification_batch_tick()
    assert len(_signalements(base)) == 1, "un second tic ne prévient pas de nouveau"
    assert _actions(db, dossier.alert_id, "PPE_REVUE_ECHUE") == 1
    # Échue depuis un jour : dans le délai, le contrôle ne la compte pas encore
    assert mes._dossiers_ppe(db)["etat"] == mes.OK


def test_une_revue_a_venir_ne_sonne_pas(base):
    _dossier_decide(base["db"], revue_dans_jours=10)
    assert ppe.detecter_revues_echues(base["db"]) == 0
    assert not _signalements(base)


def test_une_revue_echue_depuis_plus_que_le_delai_bloque_la_mise_en_service_sans_nommer(base):
    db = base["db"]
    dossier = _dossier_decide(db, revue_dans_jours=-31)     # délai par défaut : 30 jours
    controle = mes._dossiers_ppe(db)
    assert controle["etat"] == mes.BLOQUANT, controle
    assert f"#{dossier.id}" in controle["constat"] and "revue" in controle["constat"]
    sonde = mes.etat_de_conformite(db)
    assert not sonde["conforme"] and "ppe" in sonde["portee"]
    constat = next(b["constat"] for b in sonde["bloquants"] if b["cle"] == "ppe")
    assert "Client Revue" not in constat and "Listé Revue" not in constat
    assert "revue de vigilance renforcée" in constat
    _connecter("auditeur", ("auditor",))
    vue = base["client"].get(f"/api/ppe/{dossier.id}").json()
    assert vue["revue_echue"] is True and vue["revue_en_retard"] is True
    synthese = base["client"].get("/api/ppe").json()["synthese"]
    assert synthese["reviews_due"] == 1 and synthese["reviews_overdue"] == 1


def test_tenir_la_revue_leve_le_signal_et_la_revue_suivante_previent_de_nouveau(base):
    db = base["db"]
    dossier = _dossier_decide(db, revue_dans_jours=-40)
    assert ppe.detecter_revues_echues(db) == 1
    c = base["client"]
    suivante = (datetime.utcnow() + timedelta(days=365)).isoformat()

    # Ceux qui ont instruit la confirmation ne tiennent pas la revue
    _connecter("valideur")
    assert c.post(f"/api/ppe/{dossier.id}/revue", json={
        "conclusion": "MAINTIEN", "motif": "Revu.", "prochaine_revue_le": suivante}).status_code == 403
    # Un analyste sans habilitation non plus
    _connecter("analyste_seul", ("user",))
    assert c.post(f"/api/ppe/{dossier.id}/revue", json={
        "conclusion": "MAINTIEN", "motif": "Revu.", "prochaine_revue_le": suivante}).status_code == 403
    _connecter("direction")
    sans_date = c.post(f"/api/ppe/{dossier.id}/revue",
                       json={"conclusion": "MAINTIEN", "motif": "Revu."})
    assert sans_date.status_code == 400 and "prochaine revue" in sans_date.json()["detail"]
    assert c.post(f"/api/ppe/{dossier.id}/revue", json={
        "conclusion": "MAINTIEN", "motif": " ", "prochaine_revue_le": suivante}).status_code == 400

    r = c.post(f"/api/ppe/{dossier.id}/revue", json={
        "conclusion": "MAINTIEN", "motif": "Avis d'imposition 2026 et relevés revus.",
        "reference": "Comité du 20/10/2026", "prochaine_revue_le": suivante})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["revue_echue"] is False and corps["revue_signalee_le"] is None
    assert corps["revues"][-1]["conclusion"] == "MAINTIEN" and corps["revues"][-1]["par"] == "direction"
    assert corps["revues"][-1]["conclusion_label"] == ppe.CONCLUSIONS_DE_REVUE["MAINTIEN"]
    assert corps["decision"] == "MAINTIEN", "la décision d'origine ne se réécrit pas"
    assert _actions(db, dossier.alert_id, "PPE_REVUE") == 1
    assert mes._dossiers_ppe(db)["etat"] == mes.OK

    # La revue suivante, échue à son tour, prévient de nouveau — une fois
    db.expire_all()
    d = db.get(DossierPPE, dossier.id)
    d.prochaine_revue_le = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    assert ppe.detecter_revues_echues(db) == 1
    assert ppe.detecter_revues_echues(db) == 0
    assert len(_signalements(base)) == 2


def test_une_fin_de_relation_a_la_revue_n_a_plus_de_revue_a_tenir(base):
    db = base["db"]
    dossier = _dossier_decide(db, revue_dans_jours=-5)
    _connecter("direction")
    r = base["client"].post(f"/api/ppe/{dossier.id}/revue", json={
        "conclusion": "FIN_DE_RELATION", "motif": "Origine des fonds non renouvelée."})
    assert r.status_code == 200, r.text
    assert r.json()["prochaine_revue_le"] is None and r.json()["revue_echue"] is False
    assert ppe.detecter_revues_echues(db) == 0
    again = base["client"].post(f"/api/ppe/{dossier.id}/revue", json={
        "conclusion": "MAINTIEN", "motif": "x",
        "prochaine_revue_le": (datetime.utcnow() + timedelta(days=30)).isoformat()})
    assert again.status_code == 409

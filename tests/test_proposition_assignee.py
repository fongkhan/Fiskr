"""
Une alerte assignée à quelqu'un d'autre ne se propose pas — à l'unité comme
en masse.

Constat (rapport du lot « file de l'analyste ») : la proposition EN MASSE
écartait une alerte assignée à un collègue (`skipped`, code
ASSIGNEE_A_UN_AUTRE), la proposition UNITAIRE la laissait passer. Le même
geste, deux règles : un analyste qui ne pouvait pas proposer l'alerte d'un
collègue dans une sélection le pouvait en l'ouvrant.

Décision : une seule règle, sans passe-droit pour le réviseur ni
l'administrateur. Reprendre une alerte est déjà un geste ouvert à tous,
« M'assigner » (`POST /api/alerts/{id}/assign`), tracé ASSIGNED et signalé
aux suiveurs ; proposer par-dessus l'assignation ferait la même reprise sans
en laisser la trace.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import Alert, AlertEvent, AppSetting, AuditTrail, get_db
from fiskr.settings import SETTING_ALERT_FOUR_EYES, set_setting

UID = uuid.uuid4().hex[:8].upper()
MOTIF = "Homonymie : date de naissance incompatible avec la fiche listée."


def _connecter(username, roles=("user",)):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": ",".join(roles), "roles": list(roles),
    }


@pytest.fixture()
def alerte_d_un_collegue():
    db = next(get_db())
    avant = db.query(AppSetting).filter(AppSetting.key == SETTING_ALERT_FOUR_EYES).first()
    valeur = avant.value if avant else None
    set_setting(db, SETTING_ALERT_FOUR_EYES, True)
    audit = AuditTrail(client_id=f"ASSIGN-{UID}", client_name=f"Client {UID}", client_type="PP",
                       watchlist_id=f"WL-ASSIGN-{UID}", watchlist_name=f"Listé {UID}",
                       base_score=88.0, final_score=88.0, status="ALERT",
                       decision_tree={"hard_match_triggered": False}, config_state={},
                       watchlist_version="test", watchlist_hash="test", list_type="WATCHLIST_PEP")
    db.add(audit)
    db.commit()
    alerte = Alert(audit_id=audit.id, client_id=audit.client_id, client_name=audit.client_name,
                   watchlist_entity_id=audit.watchlist_id, watchlist_name=audit.watchlist_name,
                   final_score=88.0, list_type="WATCHLIST_PEP", status="IN_PROGRESS",
                   channel="SCREENING", priority="MEDIUM", assigned_to=f"collegue_{UID}")
    db.add(alerte)
    db.commit()
    yield {"db": db, "id": alerte.id, "client": TestClient(app)}
    app.dependency_overrides.pop(get_current_user, None)
    db.rollback()
    db.query(AlertEvent).filter(AlertEvent.alert_id == alerte.id).delete(synchronize_session=False)
    db.query(Alert).filter(Alert.id == alerte.id).delete(synchronize_session=False)
    db.query(AuditTrail).filter(AuditTrail.id == audit.id).delete(synchronize_session=False)
    ligne = db.query(AppSetting).filter(AppSetting.key == SETTING_ALERT_FOUR_EYES).first()
    if avant is None and ligne is not None:
        db.delete(ligne)
    elif ligne is not None:
        ligne.value = valeur
    db.commit()
    db.close()


def _proposer(ctx):
    return ctx["client"].post(f"/api/alerts/{ctx['id']}/propose",
                              json={"decision": "FALSE_POSITIVE", "comment": MOTIF})


@pytest.mark.parametrize("roles", [("user",), ("user", "reviewer"), ("admin", "reviewer")],
                         ids=["analyste", "reviseur", "administrateur"])
def test_une_proposition_unitaire_sur_l_alerte_d_un_autre_est_refusee_quel_que_soit_le_role(
        alerte_d_un_collegue, roles):
    """Avant : 200, la proposition s'écrivait sur l'alerte du collègue."""
    ctx = alerte_d_un_collegue
    _connecter(f"moi_{UID}", roles)
    r = _proposer(ctx)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "Assignée à un autre analyste" in detail and f"@collegue_{UID}" in detail
    assert "M'assigner" in detail, "le refus dit comment reprendre l'alerte"
    from fiskr.apimessages import translate_message
    for langue in ("en", "de", "es", "zh", "ar"):
        assert translate_message(detail, langue), f"[{langue}] {detail}"
    db = ctx["db"]
    db.expire_all()
    alerte = db.get(Alert, ctx["id"])
    assert alerte.status == "IN_PROGRESS" and alerte.proposed_by is None
    assert not db.query(AlertEvent).filter(AlertEvent.alert_id == ctx["id"],
                                           AlertEvent.action == "PROPOSED").count()

    # Reprendre l'alerte, c'est « M'assigner » : la reprise est tracée, puis la
    # proposition passe
    assert ctx["client"].post(f"/api/alerts/{ctx['id']}/assign", json={}).status_code == 200
    r = _proposer(ctx)
    assert r.status_code == 200, r.text
    db.expire_all()
    actions = [e.action for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == ctx["id"]).order_by(AlertEvent.id)]
    assert actions == ["ASSIGNED", "PROPOSED"], "la reprise est tracée avant la proposition"


def test_l_unitaire_et_la_masse_lisent_la_meme_regle():
    """Une seule définition : la proposition unitaire et la proposition en
    masse appellent la même fonction, qui porte les deux écarts."""
    import inspect
    from fiskr import api
    regle = api._ecart_de_la_proposition
    assert "_ecart_de_la_proposition(" in inspect.getsource(api.propose_alert_decision)
    assert "_ecart_de_la_proposition(" in inspect.getsource(api.bulk_alert_action)
    autre = Alert(status="IN_PROGRESS", assigned_to="bob")
    assert regle(autre, "alice") == "ASSIGNEE_A_UN_AUTRE"
    assert regle(autre, "bob") is None
    assert regle(Alert(status="OPEN", assigned_to=None), "alice") is None
    assert regle(Alert(status="PENDING_VALIDATION", assigned_to="alice"), "alice") \
        == "EN_ATTENTE_DE_VALIDATION"

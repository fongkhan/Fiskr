"""
Suivre un dossier d'alerte sans se l'assigner (« s'abonner »).

Le besoin : le validateur attend l'issue d'un dossier qu'il n'instruit pas,
l'analyste veut savoir ce que devient l'alerte qu'il a escaladée. S'assigner
pour être tenu au courant fausserait la charge de travail affichée — suivre
est un geste PERSONNEL : réversible, hors du journal immuable de l'alerte
(qui ne trace que l'instruction), et notifié par les canaux existants.

Le piège central, testé ici : l'évènement « activité sur un dossier suivi »
a une audience vide — ses destinataires sont TOUJOURS les suiveurs, passés
en recipients_override. Or le routeur, devant une liste vide, replie sur les
destinataires globaux : émettre « pour personne » arroserait tout le monde.
L'émetteur doit donc se taire quand il n'y a aucun suiveur — et quand le
seul suiveur est l'auteur de l'action, qui la connaît déjà.
"""
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AlertFollower, AuditTrail, User, hash_password
from fiskr.events import AUDIENCE_FOLLOWERS, EVENT_CATALOG, CATEGORY_SCREENING

UID = uuid.uuid4().hex[:8].upper()
TAG = f"Suivi-{UID}"
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _spy_emit(monkeypatch):
    calls = []
    spy = lambda db, key, payload=None, **kw: calls.append((key, payload or {}, kw))
    import fiskr.api as api_mod
    import fiskr.notifier as notifier_mod
    monkeypatch.setattr(api_mod, "emit", spy)
    monkeypatch.setattr(notifier_mod, "emit", spy)
    return calls


def _connecter(username):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": "admin", "roles": ["admin", "reviewer"],
    }


@pytest.fixture()
def ctx():
    db = next(get_db())
    for nom in ("suiveur", "acteur"):
        username = f"test_sv_{UID}_{nom}"
        if not db.query(User).filter(User.username == username).first():
            hashed, salt = hash_password("MotDePasse#1234")
            db.add(User(username=username, hashed_password=hashed, salt=salt,
                        email=f"{username}@exemple.test", role="admin"))
    db.commit()
    _connecter(f"test_sv_{UID}_acteur")

    def creer(nom, **champs):
        audit = AuditTrail(client_id=f"test_sv_{UID}_{nom}", client_name=f"{nom} {TAG}",
                           client_type="PP", watchlist_id=f"SV-{UID}",
                           watchlist_name=f"Liste {TAG}", base_score=91.0,
                           final_score=91.0, status="ALERT", decision_tree={},
                           config_state={}, watchlist_version="test", watchlist_hash="test")
        db.add(audit)
        db.commit()
        champs = {"status": "OPEN", "channel": "SCREENING", **champs}
        alerte = Alert(audit_id=audit.id, client_id=audit.client_id, client_name=audit.client_name,
                       watchlist_entity_id=f"SV-{UID}", watchlist_name=audit.watchlist_name,
                       final_score=91.0, **champs)
        db.add(alerte)
        db.commit()
        return alerte

    yield {"db": db, "client": TestClient(app), "creer": creer}
    app.dependency_overrides.pop(get_current_user, None)
    try:
        ids = [a.id for a in db.query(Alert).filter(Alert.client_id.like(f"test_sv_{UID}%")).all()]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertFollower).filter(AlertFollower.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like(f"test_sv_{UID}%")).delete(synchronize_session=False)
        db.query(User).filter(User.username.like(f"test_sv_{UID}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------- le geste

def test_suivre_est_une_bascule(ctx):
    a = ctx["creer"]("Bascule")
    r1 = ctx["client"].post(f"/api/alerts/{a.id}/follow")
    assert r1.status_code == 200 and r1.json()["following"] is True
    assert f"test_sv_{UID}_acteur" in r1.json()["followers"]
    r2 = ctx["client"].post(f"/api/alerts/{a.id}/follow")
    assert r2.status_code == 200 and r2.json()["following"] is False
    assert r2.json()["followers"] == []


def test_suivre_ne_s_inscrit_pas_au_journal_immuable(ctx):
    """Le journal de l'alerte trace l'instruction ; suivre est personnel et
    réversible — l'y inscrire diluerait la valeur probante des vraies actions."""
    a = ctx["creer"]("HorsJournal")
    ctx["client"].post(f"/api/alerts/{a.id}/follow")
    detail = ctx["client"].get(f"/api/alerts/{a.id}").json()
    assert detail["events"] == [] or all("suiv" not in (e["detail"] or "").lower()
                                        and e["action"] not in ("FOLLOWED", "UNFOLLOWED")
                                        for e in detail["events"])
    assert detail["following_me"] is True
    assert detail["followers"] == [f"test_sv_{UID}_acteur"]


def test_la_file_marque_les_dossiers_suivis_du_lecteur(ctx):
    suivi = ctx["creer"]("EstSuivi")
    ctx["creer"]("PasSuivi")
    ctx["client"].post(f"/api/alerts/{suivi.id}/follow")
    items = ctx["client"].get(f"/api/alerts?search={TAG}&page_size=50").json()["items"]
    par_nom = {i["client_name"].split()[0]: i["following_me"] for i in items}
    assert par_nom == {"EstSuivi": True, "PasSuivi": False}


def test_suivre_une_alerte_close_est_refuse(ctx):
    a = ctx["creer"]("Close", status="CLOSED_FALSE_POSITIVE")
    assert ctx["client"].post(f"/api/alerts/{a.id}/follow").status_code == 409


# ------------------------------------------------------------ la notification

def test_le_suiveur_est_notifie_de_l_action_d_un_autre(ctx, monkeypatch):
    a = ctx["creer"]("Notifie")
    _connecter(f"test_sv_{UID}_suiveur")
    ctx["client"].post(f"/api/alerts/{a.id}/follow")
    _connecter(f"test_sv_{UID}_acteur")
    calls = _spy_emit(monkeypatch)
    ctx["client"].post(f"/api/alerts/{a.id}/comment", json={"comment": "Vu avec le client."})
    suivis = [(p, kw) for k, p, kw in calls if k == "alert_followed_activity"]
    assert len(suivis) == 1, f"une action, une notification de suivi : {calls}"
    payload, kw = suivis[0]
    assert kw.get("recipients_override") == [f"test_sv_{UID}_suiveur@exemple.test"], \
        "les destinataires sont les suiveurs, jamais une audience de rôle"
    assert payload["Action"] == "Commentaire ajouté"
    assert payload["Par"] == f"test_sv_{UID}_acteur"


def test_l_auteur_de_l_action_n_est_pas_notifie_de_sa_propre_action(ctx, monkeypatch):
    """Le seul suiveur est l'auteur : émettre serait pire qu'inutile — avec la
    liste vide, le routeur replierait sur les destinataires globaux et la
    notification « personnelle » arroserait tout le monde."""
    a = ctx["creer"]("AuteurSeul")
    ctx["client"].post(f"/api/alerts/{a.id}/follow")
    calls = _spy_emit(monkeypatch)
    ctx["client"].post(f"/api/alerts/{a.id}/comment", json={"comment": "Note à moi-même."})
    assert not [c for c in calls if c[0] == "alert_followed_activity"]


def test_sans_suiveur_aucune_emission(ctx, monkeypatch):
    a = ctx["creer"]("Personne")
    calls = _spy_emit(monkeypatch)
    ctx["client"].post(f"/api/alerts/{a.id}/comment", json={"comment": "Sans public."})
    assert not [c for c in calls if c[0] == "alert_followed_activity"]


def test_les_actions_majeures_notifient_les_suiveurs(ctx, monkeypatch):
    """Dérivé du parcours réel : chaque action du cycle de vie déclenche la
    notification de suivi — y compris la mise en attente du lot précédent."""
    a = ctx["creer"]("Cycle", priority="HIGH")
    _connecter(f"test_sv_{UID}_suiveur")
    ctx["client"].post(f"/api/alerts/{a.id}/follow")
    _connecter(f"test_sv_{UID}_acteur")
    calls = _spy_emit(monkeypatch)
    c = ctx["client"]
    from datetime import datetime, timedelta
    c.post(f"/api/alerts/{a.id}/assign", json={})
    c.post(f"/api/alerts/{a.id}/priority", json={"priority": "CRITICAL"})
    c.post(f"/api/alerts/{a.id}/snooze",
           json={"until": (datetime.utcnow() + timedelta(days=2)).isoformat(), "reason": "x"})
    c.post(f"/api/alerts/{a.id}/snooze", json={"until": None})
    c.post(f"/api/alerts/{a.id}/escalate", json={"comment": "Motif sérieux."})
    c.post(f"/api/alerts/{a.id}/propose", json={"decision": "FALSE_POSITIVE", "comment": "Homonyme."})
    actions = [p["Action"] for k, p, _ in calls if k == "alert_followed_activity"]
    assert len(actions) == 6, actions
    assert "Mise en attente" in actions and "Escaladée" in actions


def test_l_evenement_est_route_vers_les_suiveurs_sans_repli_global(ctx):
    from fiskr.notifier import resolve_recipients
    ev = EVENT_CATALOG["alert_followed_activity"]
    assert ev.category == CATEGORY_SCREENING
    assert ev.audience == (AUDIENCE_FOLLOWERS,), \
        "une audience de rôle doublerait les suiveurs avec un arrosage général"
    assert ev.default_enabled is True
    # Le piège du routeur : sans override, un évènement sans destinataire
    # replie sur les adresses globales. Pour les suiveurs, « personne » doit
    # rester « personne » — jamais « tout le monde ».
    assert resolve_recipients(ctx["db"], "alert_followed_activity", {}) == []


# ------------------------------------------------------- le frontal, en statique

def test_le_frontal_porte_le_suivi(ctx):
    src = _lire("app.js")
    assert "async function suivreAlerte" in src
    assert "a.following_me" in src
    assert "☆ Suivre ce dossier" in src and "★ Suivi — ne plus suivre" in src
    assert 'class="etoile-suivi"' in src, "l'étoile de la file"
    assert ".etoile-suivi" in _lire("styles.css")
    for cle in ('"☆ Suivre ce dossier"', '"★ Suivi — ne plus suivre"', '"Suivi par :"'):
        assert cle in _lire("i18n.js"), cle

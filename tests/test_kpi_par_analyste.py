"""
Tableau de bord conformité : le dernier N+1 de l'application.

`GET /api/kpi` calculait le délai moyen de décision **une requête par
analyste** — chacune ramenant ses 200 dernières décisions. Le coût croissait
donc avec l'équipe, sur un écran d'accueil. Mesuré sur la production : ~0,67 s
de travail serveur.

Une fonction de fenêtrage numérote les décisions de chaque analyste, de la plus
récente à la plus ancienne, et **une seule** requête rend les 200 premières de
chacun. La soustraction de dates reste en Python : elle n'est pas portable en
SQL (`interval` PostgreSQL contre dates texte SQLite) et le résultat doit
rester au chiffre près.

Deux choses que le regroupement ne doit pas avoir changées, et qui sont faciles
à casser :

* le **compte** de décisions filtre sur les statuts clos, le **délai** non — il
  prend toute alerte portant une date de décision. Une alerte décidée puis
  rouverte compte dans le délai et pas dans le compte ;
* la borne des 200 est **par analyste**, pas globale.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AuditTrail

TAG = uuid.uuid4().hex[:6].upper()
BASE = datetime(2026, 6, 1, 12, 0, 0)

# analyste -> [(heures de traitement, statut), ...]
EQUIPE = {
    f"alice-{TAG}": [(2.0, "CLOSED_CONFIRMED"), (4.0, "CLOSED_FALSE_POSITIVE"),
                     (6.0, "CLOSED_CONFIRMED")],
    f"bob-{TAG}": [(1.5, "CLOSED_FALSE_POSITIVE"),
                   # decidee puis rouverte : compte dans le delai, pas dans le compte
                   (10.0, "OPEN")],
    f"carol-{TAG}": [(24.0, "CLOSED_CONFIRMED")],
}


def _ajoute(db, analyste, heures, statut, rang):
    decision = AuditTrail(
        timestamp=BASE, client_id=f"KP-{TAG}-{analyste}-{rang}",
        client_name=f"Client {rang}", client_type="I",
        watchlist_id=f"WL-{TAG}-{rang}", watchlist_name=f"Liste {TAG}",
        base_score=95.0, final_score=95.0, status="ALERT",
        decision_tree={}, config_state={},
        watchlist_version="v-test", watchlist_hash="h-test")
    db.add(decision)
    db.flush()
    cree = BASE + timedelta(days=rang)
    db.add(Alert(audit_id=decision.id, client_id=f"KP-{TAG}-{analyste}-{rang}",
                 client_name=f"Client {rang}", watchlist_entity_id=f"WL-{TAG}-{rang}",
                 watchlist_name=f"Liste {TAG}", final_score=95.0,
                 status=statut, channel="SCREENING", decided_by=analyste,
                 created_at=cree, decided_at=cree + timedelta(hours=heures)))


@pytest.fixture()
def contexte():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "kp", "full_name": "kp", "role": "admin",
        "roles": ["admin"]}
    db = next(get_db())
    rang = 0
    for analyste, decisions in EQUIPE.items():
        for heures, statut in decisions:
            rang += 1
            _ajoute(db, analyste, heures, statut, rang)
    db.commit()
    yield db, TestClient(app)
    db.query(Alert).filter(Alert.client_id.like(f"KP-{TAG}-%")).delete(
        synchronize_session=False)
    db.query(AuditTrail).filter(AuditTrail.client_id.like(f"KP-{TAG}-%")).delete(
        synchronize_session=False)
    db.commit()
    db.close()
    app.dependency_overrides.pop(get_current_user, None)


def _par_analyste(client) -> dict:
    reponse = client.get("/api/kpi")
    assert reponse.status_code == 200, reponse.text
    return {l["analyst"]: l for l in reponse.json()["alerts"]["by_analyst"]
            if l["analyst"] in EQUIPE}


def test_le_delai_moyen_vaut_le_calcul_par_analyste(contexte):
    _, client = contexte
    servi = _par_analyste(client)
    for analyste, decisions in EQUIPE.items():
        attendu = round(sum(h for h, _ in decisions) / len(decisions), 1)
        assert servi[analyste]["avg_decision_hours"] == attendu, analyste


def test_le_compte_et_le_delai_ne_lisent_pas_le_meme_perimetre(contexte):
    """Bob a deux décisions datées mais une seule est close : son délai porte
    sur les deux (1,5 h et 10 h → 5,75 → 5,8 h) et son compte sur une."""
    _, client = contexte
    bob = _par_analyste(client)[f"bob-{TAG}"]
    assert bob["decided"] == 1
    assert bob["avg_decision_hours"] == 5.8


def test_chaque_analyste_a_son_propre_delai(contexte):
    """Garde-fou : si le regroupement mélangeait les analystes, ils
    partageraient tous la même moyenne."""
    _, client = contexte
    moyennes = {a: l["avg_decision_hours"] for a, l in _par_analyste(client).items()}
    assert len(moyennes) == 3
    assert len(set(moyennes.values())) == 3, moyennes


def test_un_analyste_sans_decision_datee_rend_None(contexte):
    db, client = contexte
    _ajoute(db, f"dave-{TAG}", 0, "CLOSED_CONFIRMED", 900)
    db.commit()
    ligne = db.query(Alert).filter(
        Alert.client_id == f"KP-{TAG}-dave-{TAG}-900").one()
    ligne.decided_at = None
    db.commit()
    try:
        servi = {l["analyst"]: l for l in client.get("/api/kpi").json()["alerts"]["by_analyst"]}
        assert servi[f"dave-{TAG}"]["decided"] == 1
        assert servi[f"dave-{TAG}"]["avg_decision_hours"] is None
    finally:
        db.query(Alert).filter(Alert.client_id == f"KP-{TAG}-dave-{TAG}-900").delete(
            synchronize_session=False)
        db.query(AuditTrail).filter(
            AuditTrail.client_id == f"KP-{TAG}-dave-{TAG}-900").delete(
            synchronize_session=False)
        db.commit()


def test_le_cout_ne_croit_plus_avec_l_equipe(contexte):
    """La mesure : trois analystes puis treize, le nombre de requêtes ne doit
    pas bouger."""
    db, client = contexte

    def _compte():
        vus = []

        def _ecoute(conn, cursor, statement, params, context, executemany):
            vus.append(1)

        event.listen(Engine, "before_cursor_execute", _ecoute)
        try:
            client.get("/api/kpi")
        finally:
            event.remove(Engine, "before_cursor_execute", _ecoute)
        return len(vus)

    avec_trois = _compte()
    for i in range(10):
        _ajoute(db, f"extra-{TAG}-{i}", 3.0, "CLOSED_CONFIRMED", 1000 + i)
    db.commit()
    try:
        avec_treize = _compte()
    finally:
        db.query(Alert).filter(Alert.client_id.like(f"KP-{TAG}-extra-%")).delete(
            synchronize_session=False)
        db.query(AuditTrail).filter(
            AuditTrail.client_id.like(f"KP-{TAG}-extra-%")).delete(
            synchronize_session=False)
        db.commit()

    assert avec_treize == avec_trois, (
        f"{avec_trois} requêtes pour 3 analystes, {avec_treize} pour 13 : "
        "le coût croît encore avec l'équipe")


def test_la_borne_des_200_est_par_analyste_et_prend_les_plus_recentes(contexte):
    """La borne était un `LIMIT 200` par requête ; elle est maintenant un rang
    de fenêtre. Deux façons de se tromper : l'appliquer globalement (les
    analystes se voleraient leurs lignes) ou trier à l'envers (la moyenne
    porterait sur les décisions les plus anciennes).

    Jeu construit pour distinguer les deux : 250 décisions à 1 h (les plus
    récentes) et 50 à 100 h (les plus anciennes). La borne correcte ne retient
    que des décisions à 1 h — moyenne 1,0. Sans borne : 17,5. À l'envers : 100.
    """
    db, client = contexte
    prolifique = f"erin-{TAG}"
    rang = 2000
    for i in range(50):          # les plus anciennes, longues
        rang += 1
        _ajoute(db, prolifique, 100.0, "CLOSED_CONFIRMED", rang)
    for i in range(250):         # les plus recentes, courtes
        rang += 1
        _ajoute(db, prolifique, 1.0, "CLOSED_CONFIRMED", rang)
    db.commit()
    try:
        servi = {l["analyst"]: l for l in client.get("/api/kpi").json()["alerts"]["by_analyst"]}
        assert servi[prolifique]["decided"] == 300
        assert servi[prolifique]["avg_decision_hours"] == 1.0
        # ... et les autres analystes gardent leur propre moyenne
        assert servi[f"carol-{TAG}"]["avg_decision_hours"] == 24.0
    finally:
        db.query(Alert).filter(Alert.client_id.like(f"KP-{TAG}-{prolifique}-%")).delete(
            synchronize_session=False)
        db.query(AuditTrail).filter(
            AuditTrail.client_id.like(f"KP-{TAG}-{prolifique}-%")).delete(
            synchronize_session=False)
        db.commit()

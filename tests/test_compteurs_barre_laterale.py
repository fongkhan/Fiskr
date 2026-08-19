"""
Badges de la barre latérale : une passe sur la table au lieu de cinq.

`GET /api/counters` émettait **six** requêtes — cinq `COUNT` sur `alerts`, tous
sur le même périmètre, plus un sur `snapshots`. Cet endpoint est interrogé en
boucle par la barre latérale : chaque aller-retour épargné l'est à chaque
rafraîchissement, de chaque onglet ouvert. Mesuré sur la production, il
répondait en ~0,45 s de travail serveur pour 132 octets.

Les cinq comptes d'alertes sont maintenant lus en une passe par agrégats
conditionnels. Un regroupement ne vaut que s'il rend exactement la même chose :
le test central recalcule chaque compteur ligne par ligne, en Python, sur un
jeu construit pour couvrir tous les cas où les deux implémentations pourraient
diverger — canal nul, canal filtrage, échéance dépassée, échéance absente,
alerte fermée, statut en attente de validation.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

from fiskr.api import app, ALERT_OPEN_STATUSES
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AuditTrail

TAG = uuid.uuid4().hex[:6].upper()
MAINTENANT = datetime.utcnow()

# (statut, canal, echeance) — un cas par branche des cinq compteurs
JEU = [
    ("OPEN", "SCREENING", MAINTENANT - timedelta(hours=1)),   # ouverte, criblage, en retard
    ("OPEN", None, None),                                     # canal nul = criblage
    ("OPEN", "FILTERING", MAINTENANT + timedelta(days=1)),    # ouverte, filtrage, dans les temps
    ("OPEN", "FILTERING", None),                              # sans echeance : jamais en retard
    ("IN_PROGRESS", "SCREENING", MAINTENANT - timedelta(days=2)),
    ("PENDING_VALIDATION", "SCREENING", None),
    ("CLOSED", "SCREENING", MAINTENANT - timedelta(days=3)),  # fermee : hors de tout
    ("CLOSED", "FILTERING", None),
]


@pytest.fixture()
def contexte():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "cb", "full_name": "cb", "role": "admin",
        "roles": ["admin"]}
    db = next(get_db())
    for i, (statut, canal, echeance) in enumerate(JEU):
        # Une alerte reference toujours la decision d'audit qui l'a ouverte
        decision = AuditTrail(
            timestamp=MAINTENANT, client_id=f"CB-{TAG}-{i}",
            client_name=f"Client {TAG} {i}", client_type="I",
            watchlist_id=f"WL-{TAG}-{i}", watchlist_name=f"Liste {TAG}",
            base_score=95.0, final_score=95.0, status="ALERT",
            decision_tree={}, config_state={},
            watchlist_version="v-test", watchlist_hash="h-test")
        db.add(decision)
        db.flush()
        db.add(Alert(audit_id=decision.id,
                     client_id=f"CB-{TAG}-{i}", client_name=f"Client {TAG} {i}",
                     watchlist_entity_id=f"WL-{TAG}-{i}",
                     watchlist_name=f"Liste {TAG}", final_score=95.0,
                     status=statut, channel=canal, due_at=echeance))
    db.commit()
    yield db, TestClient(app)
    db.query(Alert).filter(Alert.client_id.like(f"CB-{TAG}-%")).delete(
        synchronize_session=False)
    db.query(AuditTrail).filter(AuditTrail.client_id.like(f"CB-{TAG}-%")).delete(
        synchronize_session=False)
    db.commit()
    db.close()
    app.dependency_overrides.pop(get_current_user, None)


def _reference(db) -> dict:
    """Les cinq compteurs recalculés ligne par ligne, hors SQL agrégé."""
    lignes = db.query(Alert).all()
    maintenant = datetime.utcnow()
    ouvertes = [a for a in lignes if a.status in ALERT_OPEN_STATUSES]
    return {
        "open_alerts": len(ouvertes),
        "open_alerts_screening": len([a for a in ouvertes
                                      if a.channel == "SCREENING" or a.channel is None]),
        "open_alerts_filtering": len([a for a in ouvertes if a.channel == "FILTERING"]),
        "pending_validation": len([a for a in lignes if a.status == "PENDING_VALIDATION"]),
        "overdue_alerts": len([a for a in ouvertes
                               if a.due_at is not None and a.due_at < maintenant]),
    }


def test_les_compteurs_valent_le_calcul_ligne_par_ligne(contexte):
    db, client = contexte
    servis = client.get("/api/counters").json()
    for cle, attendu in _reference(db).items():
        assert servis[cle] == attendu, f"{cle} : {servis[cle]} au lieu de {attendu}"


def test_le_jeu_couvre_bien_les_cinq_compteurs(contexte):
    """Garde-fou du test précédent : sur un jeu où tous les compteurs valent
    zéro, il passerait sans rien comparer."""
    db, _ = contexte
    reference = _reference(db)
    assert all(v > 0 for v in reference.values()), reference
    # ... et ils ne valent pas tous la meme chose, sinon la comparaison
    # ne distinguerait pas un compteur d'un autre
    assert len(set(reference.values())) > 1, reference


def test_une_seule_passe_sur_la_table_des_alertes(contexte):
    """Le point de la mesure : cinq allers-retours pour cinq chiffres du même
    périmètre, sur un endpoint interrogé en boucle."""
    _, client = contexte
    vus = []

    def _ecoute(conn, cursor, statement, params, context, executemany):
        texte = " ".join(statement.split()).lower()
        if texte.startswith("select") and " from alerts" in texte:
            vus.append(texte)

    event.listen(Engine, "before_cursor_execute", _ecoute)
    try:
        assert client.get("/api/counters").status_code == 200
    finally:
        event.remove(Engine, "before_cursor_execute", _ecoute)
    assert len(vus) == 1, f"{len(vus)} requêtes sur `alerts` : " + "\n".join(vus)


def test_les_snapshots_en_attente_restent_comptes(contexte):
    _, client = contexte
    corps = client.get("/api/counters").json()
    assert "pending_reviews" in corps
    assert isinstance(corps["pending_reviews"], int)

"""
Rétention du journal de criblage : le piège du NOT IN sur une colonne nullable.

`_purgeable_audit_query` sélectionne les lignes d'audit expirées **qui ne sont
plus référencées par aucune alerte** :

    ~AuditTrail.id.in_(db.query(Alert.audit_id))

En SQL, `x NOT IN (…, NULL)` ne vaut JAMAIS vrai : il vaut NULL, donc faux pour
toutes les lignes. Si `alerts.audit_id` devenait un jour nullable et qu'une
seule alerte portait NULL, cette purge cesserait **silencieusement** de purger
quoi que ce soit — sans erreur, sans journal, avec un compteur à zéro qui a
l'air normal. La rétention RGPD ne s'appliquerait plus, et rien ne le dirait.

Ce test tient la propriété dont dépend la correction : la colonne est NOT NULL.
"""
import uuid
from datetime import datetime, timedelta

import pytest

from fiskr.database import (get_db, Alert, AuditTrail, Snapshot,
                            ALERT_CLOSED_STATUSES)
from fiskr.retention import _purgeable_audit_query

TAG = uuid.uuid4().hex[:6].upper()


def test_la_colonne_audit_id_est_non_nullable():
    """La condition qui rend le NOT IN correct. La changer casserait la purge
    en silence : ce test est là pour que la modification soit délibérée."""
    colonne = Alert.__table__.c.audit_id
    assert colonne.nullable is False, (
        "alerts.audit_id est devenue nullable : le NOT IN de la rétention ne "
        "purgera plus jamais rien. Réécrire _purgeable_audit_query en NOT "
        "EXISTS avant de changer le schéma."
    )


@pytest.fixture()
def journal():
    db = next(get_db())
    lignes = []
    ancien = datetime.utcnow() - timedelta(days=400)
    for i in range(3):
        ligne = AuditTrail(
            client_id=f"C{i}-{TAG}", client_name=f"CLIENT {i}", client_type="PP",
            watchlist_id=f"W{i}-{TAG}", watchlist_name=f"LISTE {i}",
            base_score=90.0, final_score=90.0, status="ALERT",
            decision_tree={}, config_state={}, watchlist_version="v",
            watchlist_hash=f"h-{TAG}", timestamp=ancien,
        )
        db.add(ligne)
        lignes.append(ligne)
    db.flush()
    # Une SEULE des trois reste référencée par une alerte
    db.add(Alert(
        audit_id=lignes[0].id, client_id=f"C0-{TAG}", client_name="CLIENT 0",
        watchlist_entity_id=f"W0-{TAG}", watchlist_name="LISTE 0",
        final_score=90.0, status="CLOSED_CONFIRMED", created_at=ancien,
        decided_at=ancien,
    ))
    db.commit()
    yield db, lignes
    db.query(Alert).filter(Alert.watchlist_entity_id.like(f"W%-{TAG}")).delete(
        synchronize_session=False)
    db.query(AuditTrail).filter(AuditTrail.watchlist_hash == f"h-{TAG}").delete(
        synchronize_session=False)
    db.commit()
    db.close()


def test_les_lignes_referencees_ne_sont_pas_purgeables(journal):
    """Une alerte conservée garde toujours son arbre de décision."""
    db, lignes = journal
    cutoff = datetime.utcnow() - timedelta(days=30)
    purgeables = {a.id for a in _purgeable_audit_query(db, cutoff).all()}
    assert lignes[0].id not in purgeables, "la ligne d'une alerte vivante est purgeable"


def test_les_lignes_orphelines_sont_purgeables(journal):
    db, lignes = journal
    cutoff = datetime.utcnow() - timedelta(days=30)
    purgeables = {a.id for a in _purgeable_audit_query(db, cutoff).all()}
    assert {lignes[1].id, lignes[2].id} <= purgeables


def test_une_ligne_recente_n_est_jamais_purgeable(journal):
    db, _ = journal
    recente = AuditTrail(
        client_id=f"CR-{TAG}", client_name="RECENTE", client_type="PP",
        watchlist_id=f"WR-{TAG}", watchlist_name="LISTE", base_score=10.0,
        final_score=10.0, status="NO_MATCH", decision_tree={}, config_state={},
        watchlist_version="v", watchlist_hash=f"h-{TAG}",
        timestamp=datetime.utcnow(),
    )
    db.add(recente)
    db.commit()
    cutoff = datetime.utcnow() - timedelta(days=30)
    assert recente.id not in {a.id for a in _purgeable_audit_query(db, cutoff).all()}

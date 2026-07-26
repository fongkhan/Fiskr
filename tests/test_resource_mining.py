"""
Tests du moteur de fouille d'homonymes.

Ce que ces tests protegent avant tout : le garde-fou d'alignement. Sans lui,
« Ali HASSAN » alias « Abu MUHAMMAD » — un nom de guerre — produirait les
paires absurdes Ali=Abu et Hassan=Muhammad, et le criblage se mettrait a
rapprocher des gens qui n'ont rien a voir. Une table d'equivalences fausse est
pire que pas de table du tout.
"""
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr import resource_mining, resources
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    AdminAuditLog, Alert, AppSetting, LearnedEquivalence, Snapshot, WatchlistEntity,
    compute_checksum, get_db,
)
from fiskr.settings import SETTING_MINING


SUFFIX = uuid.uuid4().hex[:8]
SNAPSHOT_ID = f"MINE-{SUFFIX}"

DEFAULTS = {
    "min_occurrences": 2, "min_similarity": 0.75,
    "auto_approve_confidence": 0.0, "sources": ["ALIAS", "ANALYST"],
}


def _settings(**overrides):
    cfg = dict(DEFAULTS)
    cfg.update(overrides)
    return cfg


def _seed_entities(db, fiches):
    db.add(Snapshot(snapshot_id=SNAPSHOT_ID, file_type="WATCHLIST_EU",
                    file_name=f"mining_{SUFFIX}.csv", file_hash=SNAPSHOT_ID,
                    record_count=len(fiches), status="READY",
                    uploaded_at=datetime.utcnow()))
    for i, (primary, aliases) in enumerate(fiches):
        entity_id = f"{SNAPSHOT_ID}-{i}"
        db.add(WatchlistEntity(
            snapshot_id=SNAPSHOT_ID, entity_id=entity_id, entity_type="I",
            primary_name=primary, aliases=aliases,
            entity_checksum=compute_checksum({"entity_id": entity_id,
                                              "primary_name": primary,
                                              "aliases": aliases})))
    db.commit()


def _cleanup(db):
    db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == SNAPSHOT_ID).delete(synchronize_session=False)
    db.query(Snapshot).filter(
        Snapshot.snapshot_id == SNAPSHOT_ID).delete(synchronize_session=False)
    db.query(LearnedEquivalence).filter(
        LearnedEquivalence.class_id.like("ZZTEST%")).delete(synchronize_session=False)
    db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a.in_(["ZZALPHA", "ZZALPHB", "ZZBETA", "ZZBETB"])
    ).delete(synchronize_session=False)
    db.commit()


@pytest.fixture
def db():
    session = next(get_db())
    _cleanup(session)
    yield session
    _cleanup(session)
    session.close()
    resources.set_index(None)
    resources.invalidate_context()


# ------------------ LE GARDE-FOU : ALIGNEMENT ------------------

def test_single_divergence_is_the_only_accepted_shape():
    """Une seule divergence : c'est necessairement une autre ecriture du meme mot."""
    aligned = resource_mining.align_single_divergence("Mohammad Al Assad",
                                                      "Mohammed Al Assad")
    assert aligned is not None
    position, term_a, term_b, total = aligned
    assert (position, term_a, term_b, total) == (0, "MOHAMMAD", "MOHAMMED", 3)


def test_nickname_with_two_divergences_is_rejected():
    """
    « Ali HASSAN » alias « Abu MUHAMMAD » est un nom de guerre. L'accepter
    produirait Ali=Abu et Hassan=Muhammad — exactement ce qu'il ne faut pas.
    """
    assert resource_mining.align_single_divergence("Ali Hassan", "Abu Muhammad") is None


def test_single_token_names_are_rejected():
    """Sans element commun, deux noms d'un mot ne prouvent aucune equivalence."""
    assert resource_mining.align_single_divergence("Youssef", "Yusuf") is None


def test_identical_names_yield_nothing():
    assert resource_mining.align_single_divergence("Ivan Petrov", "Ivan Petrov") is None


def test_different_token_counts_are_rejected():
    assert resource_mining.align_single_divergence("Ali Hassan", "Ali Hassan Al Sayed") is None


def test_field_is_inferred_from_position():
    assert resource_mining.field_for_position(0, 3) == resources.FIELD_GIVEN_NAME
    assert resource_mining.field_for_position(2, 3) == resources.FIELD_SURNAME
    # Position intermediaire : ambigue, on ne propose rien plutot que du faux
    assert resource_mining.field_for_position(1, 3) is None


def test_particles_are_never_mined():
    """AL, BIN, VAN... se repetent partout sans etre des noms."""
    for particle in ("AL", "BIN", "VAN", "DE", "ABU"):
        assert particle in resource_mining.PARTICLES
    assert not resource_mining._is_minable_token("AL")
    assert not resource_mining._is_minable_token("XY")     # trop court
    assert resource_mining._is_minable_token("MOHAMMED")


def test_proximity_separates_variants_from_unrelated_words():
    sim_variant, phon_variant = resource_mining.pair_proximity("MOHAMMAD", "MOHAMMED")
    sim_other, phon_other = resource_mining.pair_proximity("HASSAN", "MUHAMMAD")
    assert sim_variant > 0.9 and phon_variant
    assert sim_other < 0.75 and not phon_other


def test_confidence_grows_with_repetition_and_proximity():
    low = resource_mining.confidence_of(1, 0.80, False, resource_mining.SOURCE_ALIAS)
    high = resource_mining.confidence_of(5, 0.98, True, resource_mining.SOURCE_ANALYST)
    assert 0 < low < high <= 1.0
    # La validation humaine pese plus que l'alias officiel, a preuve egale
    assert (resource_mining.confidence_of(3, 0.9, True, resource_mining.SOURCE_ANALYST)
            > resource_mining.confidence_of(3, 0.9, True, resource_mining.SOURCE_ALIAS))


# ------------------ PASSE COMPLETE ------------------

NOVEL = [
    # Variantes absentes des tables livrees : vraies decouvertes attendues
    ("Zzalpha KADYROV", ["Zzalphb KADYROV"]),
    ("Zzalpha BAYSAROV", ["Zzalphb BAYSAROV"]),
    ("Zzalpha GELAYEV", ["Zzalphb GELAYEV"]),
    # Une seule occurrence : sous le seuil
    ("Zzbeta IVANOV", ["Zzbetb IVANOV"]),
    # Nom de guerre : ecarte par l'alignement
    ("Ali HASSAN", ["Abu MUHAMMAD"]),
]


def test_mining_discovers_a_real_variant_with_evidence(db):
    _seed_entities(db, NOVEL)
    report = resource_mining.run_mining(db, _settings())
    assert report["created"] == 1

    row = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZALPHA").first()
    assert row is not None
    assert row.term_b == "ZZALPHB"
    assert row.field == resources.FIELD_GIVEN_NAME
    assert row.occurrences == 3
    assert row.status == resource_mining.STATUS_PROPOSED
    # La preuve doit nommer les fiches : « le moteur l'a trouvee » ne suffit pas
    assert len(row.evidence) == 3
    assert "KADYROV" in row.evidence[0]["primary_name"]


def test_mining_ignores_pairs_below_occurrence_threshold(db):
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings())
    assert db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZBETA").count() == 0


def test_mining_never_creates_the_nickname_pair(db):
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings())
    bogus = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a.in_(["ABU", "ALI"])).count()
    assert bogus == 0


def test_mining_skips_pairs_already_declared_in_files(db):
    """Mohammad/Mohammed est deja dans la table livree : rien a apprendre."""
    _seed_entities(db, [
        ("Mohammad AL ASSAD", ["Mohammed AL ASSAD"]),
        ("Mohammad HADDAD", ["Mohammed HADDAD"]),
    ])
    report = resource_mining.run_mining(db, _settings())
    assert report["created"] == 0
    assert report["skipped_already_known"] >= 1


def test_mining_is_idempotent(db):
    _seed_entities(db, NOVEL)
    first = resource_mining.run_mining(db, _settings())
    second = resource_mining.run_mining(db, _settings())
    assert first["created"] == 1 and second["created"] == 0
    assert second["updated"] == 1
    assert db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZALPHA").count() == 1


def test_auto_approval_applies_high_confidence_only(db):
    _seed_entities(db, NOVEL)
    # Seuil inatteignable : la decouverte reste une proposition
    resource_mining.run_mining(db, _settings(auto_approve_confidence=0.99))
    row = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZALPHA").first()
    assert row.status == resource_mining.STATUS_PROPOSED

    resource_mining.run_mining(db, _settings(auto_approve_confidence=0.60))
    db.refresh(row)
    assert row.status == resource_mining.STATUS_APPROVED
    assert row.decided_by == "système"
    assert "confiance" in row.decision_comment


def test_a_human_rejection_survives_the_next_pass(db):
    """
    Une passe automatique ne doit jamais defaire une decision humaine : sinon
    l'analyste rejetterait la meme equivalence toutes les nuits.
    """
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings())
    row = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZALPHA").first()
    row.status = resource_mining.STATUS_REJECTED
    row.decided_by = "analyste"
    db.commit()

    resource_mining.run_mining(db, _settings(auto_approve_confidence=0.10))
    db.refresh(row)
    assert row.status == resource_mining.STATUS_REJECTED
    assert row.decided_by == "analyste"


def test_only_individuals_are_mined(db):
    """Une raison sociale n'a ni prenom ni nom : l'aligner produirait du bruit."""
    db.add(Snapshot(snapshot_id=SNAPSHOT_ID, file_type="WATCHLIST_EU",
                    file_name=f"mining_{SUFFIX}.csv", file_hash=SNAPSHOT_ID,
                    record_count=2, status="READY", uploaded_at=datetime.utcnow()))
    for i, name in enumerate(["Zzalpha TRADING LTD", "Zzalpha SHIPPING LTD"]):
        alias = name.replace("Zzalpha", "Zzalphb")
        db.add(WatchlistEntity(
            snapshot_id=SNAPSHOT_ID, entity_id=f"{SNAPSHOT_ID}-E{i}", entity_type="E",
            primary_name=name, aliases=[alias],
            entity_checksum=compute_checksum({"entity_id": f"{SNAPSHOT_ID}-E{i}"})))
    db.commit()
    report = resource_mining.run_mining(db, _settings())
    assert report["created"] == 0


# ------------------ CLASSEMENT ------------------

def test_resolve_class_joins_an_existing_group():
    index = resources.index_from_mapping(
        {resources.FIELD_GIVEN_NAME: {"HENRY": ["Henri", "Harry"]}})
    # Un terme connu : le nouveau rejoint sa classe
    assert resource_mining.resolve_class(
        index, resources.FIELD_GIVEN_NAME, "HARRY", "HARRIE") == "HENRY"
    # Deux termes inconnus : classe neuve, deterministe
    assert resource_mining.resolve_class(
        index, resources.FIELD_GIVEN_NAME, "ZZBETA", "ZZALPHA") == "ZZALPHA"
    # Deja equivalents : rien a faire
    assert resource_mining.resolve_class(
        index, resources.FIELD_GIVEN_NAME, "HENRI", "HARRY") is None


def test_resolve_class_refuses_to_merge_two_existing_classes():
    """
    Fusionner deux classes existantes sur la foi d'une decouverte automatique
    reunirait des univers que quelqu'un a deliberement separes.
    """
    index = resources.index_from_mapping({resources.FIELD_SURNAME: {
        "WANG": ["Wang", "Wong"], "HUANG": ["Huang", "Hwang"]}})
    assert resource_mining.resolve_class(
        index, resources.FIELD_SURNAME, "WONG", "HUANG") is None


def test_approved_groups_feed_the_index(db):
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings(auto_approve_confidence=0.60))
    groups = resource_mining.approved_groups(db)
    index = resources.load_index(resources.default_directory())
    assert index.canonical("Zzalphb", resources.FIELD_GIVEN_NAME) is None
    index.merge_learned(groups)
    assert index.canonical("Zzalphb", resources.FIELD_GIVEN_NAME) == \
        index.canonical("Zzalpha", resources.FIELD_GIVEN_NAME)
    assert index.is_learned("Zzalphb", resources.FIELD_GIVEN_NAME)


# ------------------ SOURCE ANALYSTE ------------------

def test_confirmed_alerts_are_mined(db):
    from fiskr.database import AuditTrail

    audit = AuditTrail(client_name="Zzalpha DUPONT", client_type="PP",
                       watchlist_id="X", watchlist_name="Zzalphb DUPONT",
                       base_score=90.0, final_score=90.0, status="ALERT",
                       decision_tree={}, config_state={},
                       watchlist_version="test", watchlist_hash="test")
    db.add(audit)
    db.commit()
    for i in range(2):
        db.add(Alert(audit_id=audit.id, client_id=f"ZZ-{SUFFIX}-{i}",
                     client_name="Zzalpha DUPONT", watchlist_entity_id=f"X{i}",
                     watchlist_name="Zzalphb DUPONT", final_score=90.0,
                     status="CLOSED_CONFIRMED"))
    db.commit()
    try:
        report = resource_mining.run_mining(db, _settings(sources=["ANALYST"]))
        assert report["created"] == 1
        row = db.query(LearnedEquivalence).filter(
            LearnedEquivalence.term_a == "ZZALPHA").first()
        assert row.source == resource_mining.SOURCE_ANALYST
        assert row.evidence[0]["client_name"] == "Zzalpha DUPONT"
    finally:
        db.query(Alert).filter(Alert.client_id.like(f"ZZ-{SUFFIX}-%")).delete(
            synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.id == audit.id).delete(
            synchronize_session=False)
        db.commit()


# ------------------ API ------------------

def _override_user(username, role):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


@pytest.fixture
def admin_client(db):
    _override_user("mine_admin", "admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    session = next(get_db())
    try:
        session.query(AppSetting).filter(
            AppSetting.key == SETTING_MINING).delete(synchronize_session=False)
        session.query(AdminAuditLog).filter(
            AdminAuditLog.username == "mine_admin").delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


@pytest.fixture
def user_client(db):
    _override_user("mine_user", "user")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_learned_endpoint_lists_with_counters(admin_client, db):
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings())
    data = admin_client.get("/api/resources/learned",
                            params={"status": "PROPOSED"}).json()
    assert data["counters"]["PROPOSED"] >= 1
    assert data["settings"]["cron"]
    found = [i for i in data["items"] if i["term_a"] == "ZZALPHA"]
    assert found and found[0]["evidence"]


def test_learned_endpoint_rejects_unknown_status(admin_client):
    assert admin_client.get("/api/resources/learned",
                            params={"status": "PEUT_ETRE"}).status_code == 400


def test_decide_requires_admin(user_client, db):
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings())
    row = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZALPHA").first()
    response = user_client.post(f"/api/resources/learned/{row.id}/decide",
                                json={"decision": "APPROVE"})
    assert response.status_code == 403


def test_decide_applies_and_revokes(admin_client, db):
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings())
    row = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZALPHA").first()

    approved = admin_client.post(f"/api/resources/learned/{row.id}/decide",
                                 json={"decision": "APPROVE"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert resources.get_index().canonical("Zzalphb", resources.FIELD_GIVEN_NAME)

    # Une approbation reste revocable : c'est la condition qui rend
    # l'auto-approbation acceptable
    rejected = admin_client.post(f"/api/resources/learned/{row.id}/decide",
                                 json={"decision": "REJECT", "comment": "faux positif"})
    assert rejected.json()["status"] == "REJECTED"
    assert resources.get_index().canonical("Zzalphb", resources.FIELD_GIVEN_NAME) is None


def test_decide_is_traced(admin_client, db):
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings())
    row = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZALPHA").first()
    admin_client.post(f"/api/resources/learned/{row.id}/decide",
                      json={"decision": "APPROVE"})
    log = admin_client.get("/api/admin-log",
                           params={"action": "LEARNED_EQUIVALENCE_DECIDED"}).json()
    assert log["total"] >= 1


def test_decide_rejects_unknown_decision(admin_client, db):
    _seed_entities(db, NOVEL)
    resource_mining.run_mining(db, _settings())
    row = db.query(LearnedEquivalence).filter(
        LearnedEquivalence.term_a == "ZZALPHA").first()
    assert admin_client.post(f"/api/resources/learned/{row.id}/decide",
                             json={"decision": "PEUT_ETRE"}).status_code == 400


def test_mining_settings_round_trip(admin_client):
    response = admin_client.put("/api/settings/ingestion", json={
        "resource_mining": {"enabled": False, "cron": "30 4 * * *",
                            "min_occurrences": 5, "auto_approve_confidence": 0.0}})
    assert response.status_code == 200
    cfg = response.json()["resource_mining"]
    assert cfg["enabled"] is False and cfg["cron"] == "30 4 * * *"
    assert cfg["min_occurrences"] == 5 and cfg["auto_approve_confidence"] == 0.0


def test_mining_settings_reject_invalid_cron(admin_client):
    response = admin_client.put("/api/settings/ingestion",
                                json={"resource_mining": {"cron": "pas un cron"}})
    assert response.status_code == 400


def test_mining_settings_reject_unknown_source(admin_client):
    response = admin_client.put("/api/settings/ingestion",
                                json={"resource_mining": {"sources": ["TWITTER"]}})
    assert response.status_code == 400


def test_manual_run_requires_admin(user_client):
    assert user_client.post("/api/resources/mine").status_code == 403

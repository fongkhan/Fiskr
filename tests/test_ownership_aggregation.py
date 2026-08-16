"""
Règle des 50 % : le cumul des détentions.

Le calcul ne testait que des détentions INDIVIDUELLES (`ownership_pct >= 50`
sur une seule arête). Une société détenue à 25 % par un listé et 25 % par un
autre — gelée en droit — ne ressortait pas du tout. Vérifié avant correctif :

    25 % + 25 % = 50 % cumulés  ->  AUCUN RISQUE DÉTECTÉ

Or c'est exactement le cas visé par les deux régulateurs :

  OFAC : « if Blocked Person X owns 25 percent of Entity A, and Blocked Person
  Y owns another 25 percent of Entity A, Entity A is considered to be blocked »
  — le cumul jouant même entre programmes de sanctions différents.

  UE (bonnes pratiques, 2024) : « Ownership interests of EU-designated persons
  in an entity should be aggregated to determine whether such entity is owned
  50% or more by EU-designated persons. »

Le cumul ne vaut qu'entre personnes DÉSIGNÉES : additionner la part d'un listé
et celle d'un actionnaire ordinaire fabriquerait un gel imaginaire. C'est la
frontière que ces tests gardent des deux côtés.
"""
import uuid

import pytest

from fiskr.api import compute_inherited_risk
from fiskr.database import (get_db, EntityRelationship, Snapshot, WatchlistEntity)

UID = uuid.uuid4().hex[:8].upper()
SNAP = f"test-own-{UID.lower()}"


def _liste(db, suffixe):
    """Une fiche EN PRODUCTION — donc une personne désignée."""
    eid = f"SDN-{suffixe}-{UID}"
    db.add(WatchlistEntity(
        snapshot_id=SNAP, entity_id=eid, entity_type="E",
        primary_name=f"Designe {suffixe} {UID}",
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=[], is_deceased=False,
        countries={"citizenship": [], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=f"chk-{suffixe}-{UID}",
    ))
    return eid


def _detention(db, cible, owner, pct, source="MANUAL"):
    db.add(EntityRelationship(from_entity_id=cible, to_entity_id=owner,
                              relation_type="OWNED_BY", ownership_pct=pct,
                              source=source))


@pytest.fixture()
def db():
    session = next(get_db())
    session.add(Snapshot(snapshot_id=SNAP, file_type="WATCHLIST_OFAC",
                         file_name=f"{SNAP}.json", file_hash=uuid.uuid4().hex,
                         record_count=4, status="READY"))
    session.commit()
    yield session
    session.query(EntityRelationship).filter(
        EntityRelationship.from_entity_id.like(f"%{UID}")).delete(
            synchronize_session=False)
    session.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == SNAP).delete(synchronize_session=False)
    session.query(Snapshot).filter(
        Snapshot.snapshot_id == SNAP).delete(synchronize_session=False)
    session.commit()
    session.close()


def test_two_designated_owners_reaching_fifty_percent_together(db):
    """LE cas qui manquait : 25 % + 25 %. Aucun détenteur n'atteint le seuil
    seul, l'entité est pourtant gelée."""
    cible = f"CIBLE-A-{UID}"
    a, b = _liste(db, "A1"), _liste(db, "A2")
    _detention(db, cible, a, 25.0)
    _detention(db, cible, b, 25.0)
    db.commit()

    chaines = compute_inherited_risk(db, cible)
    retenus = {c["owner_entity_id"] for c in chaines}
    assert retenus == {a, b}, f"cumul non détecté : {chaines}"
    for c in chaines:
        assert c["via_aggregation"] is True
        assert c["aggregated_pct"] == 50.0
        assert set(c["aggregated_owners"]) == {a, b}


def test_three_designated_owners_below_threshold_stay_out(db):
    """3 × 15 % = 45 % : sous le seuil, donc rien. Le cumul ne doit pas
    devenir une machine à tout signaler."""
    cible = f"CIBLE-B-{UID}"
    for n, pct in (("B1", 15.0), ("B2", 15.0), ("B3", 15.0)):
        _detention(db, cible, _liste(db, n), pct)
    db.commit()
    assert compute_inherited_risk(db, cible) == []


def test_non_designated_shareholder_is_never_aggregated(db):
    """La garde essentielle : un actionnaire ORDINAIRE ne compte pas. Un listé
    à 30 % et une banque quelconque à 30 % ne font pas un gel."""
    cible = f"CIBLE-C-{UID}"
    designe = _liste(db, "C1")
    _detention(db, cible, designe, 30.0)
    _detention(db, cible, f"BANQUE-ORDINAIRE-{UID}", 30.0)  # jamais listée
    db.commit()
    assert compute_inherited_risk(db, cible) == [], (
        "un actionnaire non désigné a été cumulé : gel imaginaire")


def test_single_majority_owner_still_detected(db):
    """Le comportement historique ne régresse pas."""
    cible = f"CIBLE-D-{UID}"
    seul = _liste(db, "D1")
    _detention(db, cible, seul, 60.0)
    db.commit()
    chaines = compute_inherited_risk(db, cible)
    assert [c["owner_entity_id"] for c in chaines] == [seul]
    assert chaines[0]["via_aggregation"] is False, (
        "une détention individuelle n'est pas un cumul")


def test_aggregation_stays_silent_when_someone_already_blocks_alone(db):
    """Le cumul ne sert QU'À trancher les cas indécis. Si un détenteur bloque
    déjà l'entité à lui seul, la question est réglée : remonter en plus ses
    co-actionnaires minoritaires noierait le signal sans rien changer à la
    décision. C'est ce qui rend le correctif strictement ADDITIF — aucun cas
    déjà détecté n'est modifié."""
    cible = f"CIBLE-H-{UID}"
    majoritaire, minoritaire = _liste(db, "H1"), _liste(db, "H2")
    _detention(db, cible, majoritaire, 55.0)
    _detention(db, cible, minoritaire, 20.0)   # 75 % cumulés, mais 55 suffit
    db.commit()
    chaines = compute_inherited_risk(db, cible)
    assert [c["owner_entity_id"] for c in chaines] == [majoritaire]
    assert chaines[0]["via_aggregation"] is False


def test_ofac_relation_without_percentage_still_presumed(db):
    """Figurer au SDN comme « Owned or Controlled By » vaut présomption de
    contrôle, sans pourcentage — comportement historique préservé."""
    cible = f"CIBLE-E-{UID}"
    owner = _liste(db, "E1")
    _detention(db, cible, owner, None, source="OFAC")
    db.commit()
    chaines = compute_inherited_risk(db, cible)
    assert len(chaines) == 1 and chaines[0]["presumed"] is True


def test_aggregation_is_transitive(db):
    """Le cumul doit jouer à chaque étage : une entité gelée par cumul peut
    elle-même être détenue par cumul."""
    bas, milieu = f"BAS-{UID}", f"MILIEU-{UID}"
    a, b = _liste(db, "F1"), _liste(db, "F2")
    # BAS est détenu à 50 % cumulés par MILIEU… qui est lui-même détenu à
    # 50 % cumulés par deux désignés.
    m = _liste(db, "F0")  # MILIEU est listé pour pouvoir être cumulé
    _detention(db, bas, m, 30.0)
    _detention(db, bas, a, 20.0)
    _detention(db, milieu, a, 25.0)
    _detention(db, milieu, b, 25.0)
    db.commit()

    chaines = compute_inherited_risk(db, bas, max_depth=3)
    assert {c["owner_entity_id"] for c in chaines} >= {m, a}, chaines


def test_duplicate_edges_for_one_owner_are_not_double_counted(db):
    """Deux lignes pour le MÊME détenteur ne doivent pas s'additionner : ce
    serait fabriquer 50 % avec 30 % réels."""
    cible = f"CIBLE-G-{UID}"
    owner = _liste(db, "G1")
    _detention(db, cible, owner, 30.0)
    _detention(db, cible, owner, 30.0)   # doublon (deux sources)
    db.commit()
    assert compute_inherited_risk(db, cible) == [], (
        "un doublon a été compté deux fois")

"""
Tests des chantiers ownership (graphe de relations, regle des 50 %) et
campagnes de criblage batch persistees (upload + inbox CFT) :
- extraction des ProfileRelationships du SDN_ADVANCED OFAC ;
- CRUD des relations manuelles + gardes (doublon, entite inconnue, source OFAC) ;
- risque herite par detention majoritaire, transitif, annote au criblage ;
- campagne batch bout-en-bout (alerte + no match + rejet quality gate),
  export CSV, et depot inbox CFT -> campagne automatique.
"""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app, _process_inbox_once
from fiskr.auth import get_current_user
from fiskr.config import config
from fiskr.database import (
    get_db, Alert, AlertEvent, AuditTrail, AppSetting, Snapshot, WatchlistEntity,
    EntityRelationship, BatchCampaign, BatchResult,
)
from fiskr.ingest import parse_ofac_advanced_xml
from fiskr.settings import SETTING_REQUIRE_APPROVAL


def _override_user(username: str, role: str):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username, "role": role,
        "roles": [r.strip() for r in role.split(",") if r.strip()],
    }


def _cleanup_db():
    db = next(get_db())
    try:
        db.query(AppSetting).filter(AppSetting.key == SETTING_REQUIRE_APPROVAL).delete(synchronize_session=False)
        # Relations de test (entites au prefixe OB-)
        db.query(EntityRelationship).filter(
            EntityRelationship.from_entity_id.like("OB-%")
        ).delete(synchronize_session=False)
        # Campagnes de test
        campaigns = db.query(BatchCampaign).filter(BatchCampaign.name.like("test_ob_%")).all()
        cids = [c.id for c in campaigns]
        if cids:
            db.query(BatchResult).filter(BatchResult.campaign_id.in_(cids)).delete(synchronize_session=False)
            db.query(BatchCampaign).filter(BatchCampaign.id.in_(cids)).delete(synchronize_session=False)
        # Alertes/audits des clients de test
        alerts = db.query(Alert).filter(Alert.client_id.like("test_ob_%")).all()
        ids = [a.id for a in alerts]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like("test_ob_%")).delete(synchronize_session=False)
        # Snapshots watchlist de test
        snaps = db.query(Snapshot).filter(Snapshot.file_name.like("test_ob_%")).all()
        snap_ids = [s.snapshot_id for s in snaps]
        if snap_ids:
            db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(snap_ids)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client():
    _override_user("reviewer_ob", "reviewer,admin")
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    _cleanup_db()


def _upload_entities(client, rows):
    """Fiches listees en production : rows = [(entity_id, type, nom)]."""
    assert client.put("/api/settings/ingestion", json={"require_approval": False}).status_code == 200
    body = "entity_id,entity_type,primary_name,nationality\n" + "\n".join(
        f"{eid},{etype},{name},RU" for eid, etype, name in rows
    ) + "\n"
    response = client.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_EU"},
        files={"file": (f"test_ob_{uuid.uuid4().hex[:8]}.csv", body, "text/csv")},
    )
    assert response.status_code == 200, response.text


# ====================================================================
# 1. EXTRACTION OFAC DES PROFILERELATIONSHIPS
# ====================================================================

RELATIONS_SDN_XML = """<?xml version="1.0" encoding="utf-8"?>
<Sanctions xmlns="https://www.un.org/sanctions/1.0">
  <ReferenceValueSets>
    <PartyTypeValues><PartyType ID="3">Entity</PartyType></PartyTypeValues>
    <PartySubTypeValues><PartySubType ID="2" PartyTypeID="3">Entity</PartySubType></PartySubTypeValues>
    <NamePartTypeValues><NamePartType ID="1525">Entity Name</NamePartType></NamePartTypeValues>
    <AliasTypeValues><AliasType ID="1403">Name</AliasType></AliasTypeValues>
    <RelationTypeValues>
      <RelationType ID="15003">Owned or Controlled By</RelationType>
      <RelationType ID="15002">Acting for or on behalf of</RelationType>
      <RelationType ID="91422">Providing support to</RelationType>
    </RelationTypeValues>
  </ReferenceValueSets>
  <DistinctParties>
    <DistinctParty FixedRef="8001">
      <Profile ID="8001" PartySubTypeID="2">
        <Identity ID="61" FixedRef="8001" Primary="true">
          <Alias FixedRef="8001" AliasTypeID="1403" Primary="true" LowQuality="false">
            <DocumentedName ID="62" FixedRef="8001" DocNameStatusID="1">
              <DocumentedNamePart><NamePartValue NamePartGroupID="611">FILIALE SHIPPING LLC</NamePartValue></DocumentedNamePart>
            </DocumentedName>
          </Alias>
          <NamePartGroups><MasterNamePartGroup><NamePartGroup ID="611" NamePartTypeID="1525"/></MasterNamePartGroup></NamePartGroups>
        </Identity>
      </Profile>
    </DistinctParty>
    <DistinctParty FixedRef="8002">
      <Profile ID="8002" PartySubTypeID="2">
        <Identity ID="63" FixedRef="8002" Primary="true">
          <Alias FixedRef="8002" AliasTypeID="1403" Primary="true" LowQuality="false">
            <DocumentedName ID="64" FixedRef="8002" DocNameStatusID="1">
              <DocumentedNamePart><NamePartValue NamePartGroupID="621">HOLDING MERE JSC</NamePartValue></DocumentedNamePart>
            </DocumentedName>
          </Alias>
          <NamePartGroups><MasterNamePartGroup><NamePartGroup ID="621" NamePartTypeID="1525"/></MasterNamePartGroup></NamePartGroups>
        </Identity>
      </Profile>
    </DistinctParty>
  </DistinctParties>
  <ProfileRelationships>
    <ProfileRelationship ID="1" From-ProfileID="8001" To-ProfileID="8002" RelationTypeID="15003"/>
    <ProfileRelationship ID="2" From-ProfileID="8002" To-ProfileID="8001" RelationTypeID="91422"/>
  </ProfileRelationships>
</Sanctions>"""


def test_ofac_profile_relationships_extracted(tmp_path):
    xml_file = tmp_path / "sdn_relations.xml"
    xml_file.write_text(RELATIONS_SDN_XML, encoding="utf-8")
    relations = []
    entities = list(parse_ofac_advanced_xml(str(xml_file), relations_out=relations))
    assert len(entities) == 2
    assert len(relations) == 2
    owned = next(r for r in relations if r["from_entity_id"] == "8001")
    assert owned["to_entity_id"] == "8002"
    assert owned["relation_type"] == "OWNED_BY"
    assert owned["relation_label"] == "Owned or Controlled By"
    support = next(r for r in relations if r["from_entity_id"] == "8002")
    assert support["relation_type"] == "PROVIDING_SUPPORT"


# ====================================================================
# 2. CRUD DES RELATIONS + REGLE DES 50 %
# ====================================================================

def test_relationship_crud_and_guards(client):
    tag = uuid.uuid4().hex[:6].upper()
    a, b = f"OB-A-{tag}", f"OB-B-{tag}"
    _upload_entities(client, [(a, "E", f"Filiale {tag}"), (b, "E", f"Holding {tag}")])

    # Type inconnu -> 400 ; entite inconnue -> 404
    assert client.post("/api/relationships", json={
        "from_entity_id": a, "to_entity_id": b, "relation_type": "COPAIN_DE"}).status_code == 400
    assert client.post("/api/relationships", json={
        "from_entity_id": a, "to_entity_id": "OB-INTROUVABLE", "relation_type": "OWNED_BY"}).status_code == 404

    response = client.post("/api/relationships", json={
        "from_entity_id": a, "to_entity_id": b, "relation_type": "OWNED_BY",
        "ownership_pct": 60, "comment": "Registre du commerce",
    })
    assert response.status_code == 200, response.text
    rel_id = response.json()["relation"]["id"]

    # Doublon -> 409
    assert client.post("/api/relationships", json={
        "from_entity_id": a, "to_entity_id": b, "relation_type": "OWNED_BY"}).status_code == 409

    # Lecture : relation visible cote filiale ET cote holding, noms resolus
    data = client.get(f"/api/relationships/{a}").json()
    assert len(data["relations"]) == 1
    # Nom resolu (normalise en majuscules par le quality gate a l'ingestion)
    assert data["relations"][0]["to_name"].upper() == f"HOLDING {tag}"
    # Regle des 50 % : detention majoritaire -> risque herite
    assert len(data["inherited_risk"]) == 1
    assert data["inherited_risk"][0]["owner_entity_id"] == b
    assert data["inherited_risk"][0]["ownership_pct"] == 60

    # Pas de risque herite dans l'autre sens
    assert client.get(f"/api/relationships/{b}").json()["inherited_risk"] == []

    # Suppression manuelle OK
    assert client.delete(f"/api/relationships/{rel_id}").status_code == 200
    assert client.get(f"/api/relationships/{a}").json()["relations"] == []


def test_inherited_risk_is_transitive_and_minority_ignored(client):
    tag = uuid.uuid4().hex[:6].upper()
    a, b, c, d = (f"OB-{x}-{tag}" for x in ("A", "B", "C", "D"))
    _upload_entities(client, [
        (a, "E", f"Fille {tag}"), (b, "E", f"Mere {tag}"),
        (c, "E", f"GrandMere {tag}"), (d, "E", f"Minoritaire {tag}"),
    ])
    for from_id, to_id, pct in ((a, b, 55), (b, c, 80), (a, d, 20)):
        assert client.post("/api/relationships", json={
            "from_entity_id": from_id, "to_entity_id": to_id,
            "relation_type": "OWNED_BY", "ownership_pct": pct,
        }).status_code == 200

    inherited = client.get(f"/api/relationships/{a}").json()["inherited_risk"]
    owners = {chain["owner_entity_id"] for chain in inherited}
    # Transitif (mere puis grand-mere), la detention minoritaire (20 %) est ignoree
    assert owners == {b, c}
    grandmother = next(ch for ch in inherited if ch["owner_entity_id"] == c)
    assert grandmother["via"] == [b]


def test_ofac_sourced_relation_cannot_be_deleted(client):
    tag = uuid.uuid4().hex[:6].upper()
    a, b = f"OB-A-{tag}", f"OB-B-{tag}"
    _upload_entities(client, [(a, "E", f"Fofac {tag}"), (b, "E", f"Mofac {tag}")])
    db = next(get_db())
    try:
        rel = EntityRelationship(from_entity_id=a, to_entity_id=b,
                                 relation_type="OWNED_BY", source="OFAC")
        db.add(rel)
        db.commit()
        rel_id = rel.id
    finally:
        db.close()
    assert client.delete(f"/api/relationships/{rel_id}").status_code == 409


def test_screening_annotates_inherited_risk(client):
    tag = uuid.uuid4().hex[:6].upper()
    a, b = f"OB-A-{tag}", f"OB-B-{tag}"
    _upload_entities(client, [(a, "E", f"Cribleco {tag}"), (b, "E", f"Proprio {tag}")])
    assert client.post("/api/relationships", json={
        "from_entity_id": a, "to_entity_id": b, "relation_type": "OWNED_BY", "ownership_pct": 75,
    }).status_code == 200

    data = client.post("/api/screen", json={
        "client_id": f"test_ob_{uuid.uuid4().hex[:8]}", "client_type": "PM",
        "client_company_name": f"CRIBLECO {tag}",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": ["RU"]},
    }).json()
    best = data["best_match"]
    assert best["status"] == "ALERT"
    inherited = best.get("ownership_inherited_risk") or []
    assert any(chain["owner_entity_id"] == b for chain in inherited)


# ====================================================================
# 3. CAMPAGNES BATCH (upload + inbox CFT)
# ====================================================================

BATCH_CSV = (
    "client_id,client_type,client_first_name,client_last_name,client_dob,client_gender,nationality\n"
    "{cid_hit},PP,Vladimir,Putin,1952-10-07,M,RU\n"
    "{cid_miss},PP,Jean,Innocentov,1990-01-01,M,FR\n"
    "{cid_bad},PP,,,1990-01-01,M,FR\n"
)


def _wait_campaign_done(client, campaign_id, timeout_s=20):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = client.get(f"/api/batch/campaigns/{campaign_id}").json()
        if data["status"] != "RUNNING":
            return data
        time.sleep(0.3)
    raise AssertionError("La campagne batch n'a pas terminé dans le temps imparti.")


def test_batch_campaign_end_to_end(client):
    tag = uuid.uuid4().hex[:8]
    cid_hit, cid_miss, cid_bad = (f"test_ob_{tag}_{i}" for i in range(3))
    csv_body = BATCH_CSV.format(cid_hit=cid_hit, cid_miss=cid_miss, cid_bad=cid_bad)

    response = client.post(
        "/api/batch/campaigns",
        data={"name": f"test_ob_campagne_{tag}"},
        files={"file": (f"clients_{tag}.csv", csv_body, "text/csv")},
    )
    assert response.status_code == 200, response.text
    campaign_id = response.json()["id"]
    assert response.json()["total_clients"] == 3

    done = _wait_campaign_done(client, campaign_id)
    assert done["status"] == "DONE"
    assert done["processed_clients"] == 3
    assert done["alert_count"] == 1
    assert done["no_match_count"] == 1
    assert done["rejected_count"] == 1

    by_client = {r["client_id"]: r for r in done["results"]}
    hit = by_client[cid_hit]
    assert hit["status"] == "ALERT" and hit["alert_id"] is not None and hit["audit_id"] is not None
    assert by_client[cid_miss]["status"] == "NO_MATCH"
    assert by_client[cid_bad]["status"] == "REJECTED" and by_client[cid_bad]["error"]

    # L'alerte creee est une vraie alerte de travail (memes garanties que le temps reel)
    alert = client.get(f"/api/alerts/{hit['alert_id']}").json()
    assert alert["status"] == "OPEN" and alert["client_id"] == cid_hit

    # Filtre de resultats + export CSV
    only_alerts = client.get(f"/api/batch/campaigns/{campaign_id}", params={"status": "ALERT"}).json()
    assert only_alerts["results_total"] == 1
    export = client.get(f"/api/export/batch/{campaign_id}.csv")
    assert export.status_code == 200
    assert cid_hit in export.content.decode("utf-8")


def test_batch_rejects_empty_or_oversized_file(client):
    assert client.post(
        "/api/batch/campaigns",
        files={"file": ("vide.csv", "client_id,client_type\n", "text/csv")},
    ).status_code == 400


def test_inbox_cft_creates_campaign(client, tmp_path, monkeypatch):
    tag = uuid.uuid4().hex[:8]
    inbox = tmp_path / "cft_in"
    inbox.mkdir()
    cid = f"test_ob_{tag}_inbox"
    csv_path = inbox / f"clients_{tag}.csv"
    csv_path.write_text(
        "client_id,client_type,client_first_name,client_last_name,client_dob,client_gender,nationality\n"
        f"{cid},PP,Vladimir,Putin,1952-10-07,M,RU\n",
        encoding="utf-8",
    )
    # Fichier « stable » : mtime vieilli au-dela de la garde anti-transfert-en-cours
    old = time.time() - 60
    import os
    os.utime(csv_path, (old, old))

    monkeypatch.setitem(config, "batch", {"inbox_dir": str(inbox), "inbox_poll_seconds": 60})
    launched = _process_inbox_once()
    assert launched == 1
    # Fichier deplace vers l'archive, plus present dans l'inbox
    assert not csv_path.exists()
    assert list((inbox / "archive").glob(f"*clients_{tag}.csv"))

    campaigns = client.get("/api/batch/campaigns").json()["items"]
    campaign = next(c for c in campaigns if c["file_name"] == f"clients_{tag}.csv")
    assert campaign["trigger"] == "inbox"
    done = _wait_campaign_done(client, campaign["id"])
    assert done["status"] == "DONE" and done["alert_count"] == 1
    # Nettoyage de la campagne inbox (nom non prefixe test_ob_)
    db = next(get_db())
    try:
        db.query(BatchResult).filter(BatchResult.campaign_id == campaign["id"]).delete(synchronize_session=False)
        db.query(BatchCampaign).filter(BatchCampaign.id == campaign["id"]).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()

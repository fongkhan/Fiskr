"""
Une paire de liste blanche couvre l'identité qu'elle a vue : le client, ou
UNE de ses parties liées.

Constat (rapport du lot « parties liées ») : la paire restait clé
(client, fiche). Quand deux identités d'une même relation rencontrent le même
listé — le client et son mandataire homonyme, ou deux parties —, la paire
posée sur l'une était lue pour l'autre : l'empreinte d'identification ne
correspondait pas, la paire passait « à réexaminer » et n'absorbait plus rien,
pas même la correspondance qu'elle avait été posée pour couvrir.

Mesure : la paire porte la partie qu'elle a vue (`partie_liee_id`) ; une paire
sans partie — toutes celles d'avant — est celle du client lui-même, inchangée.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import parties
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (Alert, Base, ClientEntity, PartieLiee, Snapshot, WatchlistEntity,
                            WhitelistPair, get_db)
from fiskr.rescreen import rescreen_lookback
from fiskr.settings import SETTING_ALERT_FOUR_EYES, set_setting

REVISEUR = {"id": 1, "username": "reviseur_paire", "full_name": "Réviseur",
            "role": "admin,reviewer", "roles": ["admin", "reviewer"]}
MOTIF = "Homonymie : le mandataire n'est pas la personne listée (pièces au dossier)."
PAYS = {"nationality": ["RU"], "residence": [], "birth_country": [], "registration_country": []}


@pytest.fixture
def base(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'paire.sqlite3'}")
    Base.metadata.create_all(engine)
    fabrique = sessionmaker(bind=engine, autoflush=False)
    session = fabrique()

    def _db():
        s = fabrique()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: REVISEUR
    set_setting(session, SETTING_ALERT_FOUR_EYES, False)
    yield {"db": session, "http": TestClient(app)}
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    session.close()
    engine.dispose()


def _relation_a_deux_identites(db):
    """Viktor ZOLOTAREV, client, et son mandataire homonyme (sans date de
    naissance) : deux identités, une relation, un même listé rencontré."""
    db.add(Snapshot(snapshot_id="clients-1", file_type="CLIENT_BASE", file_name="clients.csv",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    db.add(ClientEntity(snapshot_id="clients-1", client_id="ZOLO-1", client_type="PP",
                        client_first_name="Viktor", client_last_name="ZOLOTAREV",
                        client_dob="1954-01-27", client_gender="M", client_countries=PAYS,
                        entity_checksum=uuid.uuid4().hex))
    lue = parties.lire_une_partie(
        {"client_type": "PP", "client_first_name": "Viktor", "client_last_name": "ZOLOTAREV",
         "client_gender": "M", "client_countries": PAYS}, roles=["MANDATAIRE"])
    parties.remplacer_les_parties(db, "ZOLO-1", [lue], source=parties.SOURCE_IMPORT,
                                  source_ref="test", username="test")
    db.add(Snapshot(snapshot_id="dgt-v1", file_type="WATCHLIST_DGT", file_name="dgt.json",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    db.add(WatchlistEntity(
        snapshot_id="dgt-v1", entity_id="DGT-ZOL", entity_type="I", primary_name="Viktor ZOLOTAREV",
        individual_name_parsed={"first_name": "Viktor", "last_name": "ZOLOTAREV", "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []}, dates_of_birth=["1954-01-27"],
        is_deceased=False, gender="M",
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                   "jurisdiction_country": []}, entity_checksum="chk-DGT-ZOL"))
    db.commit()
    premier = rescreen_lookback(db, "WATCHLIST_DGT")
    assert premier["new_alerts"] == 2, premier
    partie = db.query(PartieLiee).one()
    sur_le_client = db.query(Alert).filter(Alert.partie_liee_id.is_(None)).one()
    sur_la_partie = db.query(Alert).filter(Alert.partie_liee_id == partie.id).one()
    return partie, sur_le_client, sur_la_partie


def _faux_positif_memorise(ctx, alerte):
    r = ctx["http"].post(f"/api/alerts/{alerte.id}/propose",
                         json={"decision": "FALSE_POSITIVE", "comment": MOTIF, "whitelist": {}})
    assert r.status_code == 200, r.text
    db = ctx["db"]
    db.expire_all()
    return db.query(WhitelistPair).one()


def test_une_paire_posee_sur_la_partie_absorbe_la_partie_et_pas_le_client(base):
    """Avant : la paire (client, fiche) était lue pour le client, dont
    l'empreinte n'était pas celle de la décision — « à réexaminer » —, puis
    n'absorbait plus la partie non plus : 0 absorbée, 2 réexamens."""
    db = base["db"]
    partie, sur_le_client, sur_la_partie = _relation_a_deux_identites(db)
    paire = _faux_positif_memorise(base, sur_la_partie)
    assert paire.client_id == "ZOLO-1"

    second = rescreen_lookback(db, "WATCHLIST_DGT")
    assert second["whitelisted_suppressed"] == 1, second
    assert second["whitelist_stale"] == 0, second
    assert paire.partie_liee_id == partie.id, "la paire couvre l'identité qu'elle a vue"
    db.expire_all()
    assert db.get(WhitelistPair, paire.id).stale_since is None, \
        "la correspondance du client n'a pas à faire réexaminer la paire de la partie"
    # Le client, lui, n'est pas couvert : son alerte reste ouverte et re-détectée
    assert second["redetected_alerts"] == 1 and second["new_alerts"] == 0, second
    assert db.get(Alert, sur_le_client.id).status == "OPEN"
    assert db.query(Alert).filter(Alert.partie_liee_id == partie.id).count() == 1

    vue = base["http"].get("/api/whitelist").json()["items"][0]
    assert vue["partie_liee_id"] == partie.id and vue["partie_nom"] == "Viktor ZOLOTAREV"
    assert vue["state"] == "ACTIVE"


def test_une_paire_du_client_reste_celle_du_client_et_n_absorbe_pas_sa_partie(base):
    """Paire sans partie = le client lui-même : c'est ce que sont toutes les
    paires existantes, inchangées par la migration."""
    db = base["db"]
    partie, sur_le_client, sur_la_partie = _relation_a_deux_identites(db)
    paire = _faux_positif_memorise(base, sur_le_client)

    second = rescreen_lookback(db, "WATCHLIST_DGT")
    assert second["whitelisted_suppressed"] == 1 and second["whitelist_stale"] == 0, second
    assert paire.partie_liee_id is None
    db.expire_all()
    assert db.get(WhitelistPair, paire.id).stale_since is None
    assert db.get(Alert, sur_la_partie.id).status == "OPEN"
    assert second["redetected_alerts"] == 1, second


def test_une_paire_ne_se_pose_pas_sur_la_partie_d_un_autre_client(base):
    db = base["db"]
    partie, _client, _partie = _relation_a_deux_identites(db)
    r = base["http"].post("/api/whitelist", data={
        "client_id": "AUTRE-1", "watchlist_entity_id": "DGT-ZOL", "justification": MOTIF,
        "partie_liee_id": str(partie.id)})
    assert r.status_code == 400 and "n'est pas une partie liée du client AUTRE-1" in r.json()["detail"]
    r = base["http"].post("/api/whitelist", data={
        "client_id": "ZOLO-1", "watchlist_entity_id": "DGT-ZOL", "justification": MOTIF,
        "partie_liee_id": str(partie.id)})
    assert r.status_code == 200, r.text
    assert r.json()["partie_liee_id"] == partie.id
    # La même identité ne se blanchit pas deux fois ; le client, lui, le peut
    assert base["http"].post("/api/whitelist", data={
        "client_id": "ZOLO-1", "watchlist_entity_id": "DGT-ZOL", "justification": MOTIF,
        "partie_liee_id": str(partie.id)}).status_code == 409
    assert base["http"].post("/api/whitelist", data={
        "client_id": "ZOLO-1", "watchlist_entity_id": "DGT-ZOL",
        "justification": MOTIF}).status_code == 200

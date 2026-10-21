"""
Les pièces imprimables et les projets de déclaration disent la partie liée.

Constat (rapport du lot « parties liées ») : une alerte née « via le
bénéficiaire effectif X (35 %) du client ACME » s'imprimait comme une
correspondance entre ACME et la personne listée. Le rapport d'alerte
(`GET /api/alerts/{id}/report`), le dossier d'investigation imprimable, le
narratif (fiskr/narrative.py) et les deux projets de déclaration — TRACFIN
(`str-draft`) et DG Trésor (`/api/gels/{id}/draft`) — ne nommaient ni la
partie, ni son rôle, ni sa détention : le résumé TRACFIN disait même
« Correspondance de criblage entre « ACME » et la personne listée », une
identité qui n'avait pas été comparée.

Ce que ces tests tiennent : chaque pièce nomme la partie, son rôle, sa
détention, son pouvoir d'engager les fonds et le client — une seule
définition (`parties.partie_de_l_alerte`, `parties.rubriques_de_la_partie`) —,
et une alerte sur le client lui-même n'en dit rien.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import gels, parties
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (Alert, Base, ClientEntity, MesureDeGel, Snapshot, WatchlistEntity,
                            get_db)
from fiskr.rescreen import rescreen_lookback

REVISEUR = {"id": 1, "username": "reviseur_pieces", "full_name": "Réviseur",
            "role": "admin,reviewer", "roles": ["admin", "reviewer"]}


@pytest.fixture
def base(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pieces.sqlite3'}")
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
    yield {"db": session, "http": TestClient(app)}
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    session.close()
    engine.dispose()


def _client(db, client_id, nom, type_="PM", **identite):
    snap = Snapshot(snapshot_id=f"clients-{uuid.uuid4().hex[:6]}", file_type="CLIENT_BASE",
                    file_name="clients.csv", file_hash=uuid.uuid4().hex, record_count=1,
                    status="READY")
    db.add(snap)
    pays = {"nationality": ["RU"] if type_ == "PP" else [], "residence": [], "birth_country": [],
            "registration_country": ["FR"] if type_ == "PM" else []}
    db.add(ClientEntity(snapshot_id=snap.snapshot_id, client_id=client_id, client_type=type_,
                        client_company_name=nom if type_ == "PM" else None,
                        entity_checksum=uuid.uuid4().hex, client_countries=pays, **identite))
    db.commit()


def _fiche(snap_id, entity_id, prenom, nom, dob):
    return WatchlistEntity(
        snapshot_id=snap_id, entity_id=entity_id, entity_type="I", primary_name=f"{prenom} {nom}",
        individual_name_parsed={"first_name": prenom, "last_name": nom, "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []}, dates_of_birth=[dob],
        is_deceased=False, gender="M",
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=f"chk-{entity_id}")


def _acme_et_son_beneficiaire(db, pct=60):
    _client(db, "ACME-1", "ACME HOLDING SAS")
    lue = parties.lire_une_partie(
        {"client_type": "PP", "client_first_name": "Viktor", "client_last_name": "ZOLOTAREV",
         "client_dob": "1954-01-27", "client_gender": "M",
         "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [],
                              "registration_country": []}},
        roles=["BENEFICIAIRE_EFFECTIF", "DIRIGEANT"], pourcentage=pct, peut_engager="non")
    parties.remplacer_les_parties(db, "ACME-1", [lue], source=parties.SOURCE_IMPORT,
                                  source_ref="test", username="test")
    db.add(Snapshot(snapshot_id="dgt-v1", file_type="WATCHLIST_DGT", file_name="dgt.json",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    db.add(_fiche("dgt-v1", "DGT-ZOL", "Viktor", "ZOLOTAREV", "1954-01-27"))
    db.commit()
    assert rescreen_lookback(db, "WATCHLIST_DGT")["new_alerts"] == 1
    alerte = db.query(Alert).one()
    assert alerte.partie_liee_id is not None
    return alerte


def _dit_la_partie(texte):
    """Ce qu'une pièce doit dire de la partie, partout le même."""
    for attendu in ("Viktor ZOLOTAREV", "Bénéficiaire effectif", "Dirigeant", "60 %",
                    "ACME HOLDING SAS", "ACME-1",
                    "via le bénéficiaire effectif et dirigeant Viktor ZOLOTAREV (60 %) "
                    "du client ACME HOLDING SAS"):
        assert attendu in texte, attendu


def test_le_rapport_d_alerte_et_le_dossier_imprimables_nomment_la_partie_et_son_role(base):
    """Avant : le rapport imprimait « ACME HOLDING SAS (ACME-1) » face à la
    fiche, sans un mot du bénéficiaire effectif comparé."""
    alerte = _acme_et_son_beneficiaire(base["db"])
    rapport = base["http"].get(f"/api/alerts/{alerte.id}/report")
    assert rapport.status_code == 200, rapport.text
    assert "Partie liée du client en cause" in rapport.text
    _dit_la_partie(rapport.text)
    assert "Peut engager les fonds du client</th><td>non" in rapport.text

    dossier = base["http"].get(f"/api/alerts/{alerte.id}/casefile")
    assert dossier.json()["partie_liee"]["roles"] == ["BENEFICIAIRE_EFFECTIF", "DIRIGEANT"]
    imprimable = base["http"].get(f"/api/alerts/{alerte.id}/casefile/print")
    assert imprimable.status_code == 200
    _dit_la_partie(imprimable.text)


def test_le_narratif_dit_que_c_est_la_partie_qui_a_ete_comparee(base):
    alerte = _acme_et_son_beneficiaire(base["db"])
    r = base["http"].post(f"/api/alerts/{alerte.id}/narrative")
    assert r.status_code == 200, r.text
    texte = r.json()["narrative"]
    assert ("« Viktor ZOLOTAREV », bénéficiaire effectif et dirigeant (60 %) du client "
            "« ACME HOLDING SAS »") in texte
    assert "qui ne peut pas engager les fonds du client" in texte
    assert "Cette partie a présenté une correspondance" in texte


def test_le_projet_tracfin_decrit_la_partie_et_non_une_correspondance_d_acme(base):
    alerte = _acme_et_son_beneficiaire(base["db"])
    projet = base["http"].get(f"/api/alerts/{alerte.id}/str-draft").json()
    partie = projet["partie_liee_en_cause"]
    assert partie["nom"] == "Viktor ZOLOTAREV"
    assert partie["roles_libelles"] == ["Bénéficiaire effectif", "Dirigeant"]
    assert partie["pourcentage_detention"] == 60.0 and partie["peut_engager_les_fonds"] is False
    assert partie["client_id"] == "ACME-1" and partie["date_naissance"] == "1954-01-27"
    assert projet["motifs"]["resume"].startswith(
        "Correspondance de criblage entre « Viktor ZOLOTAREV », bénéficiaire effectif et "
        "dirigeant (60 %) du client « ACME HOLDING SAS » et la personne/entité listée")
    imprimable = base["http"].get(f"/api/alerts/{alerte.id}/str-draft/print")
    assert "2 bis. Partie liée du client en cause" in imprimable.text
    _dit_la_partie(imprimable.text)


def test_le_projet_de_declaration_dg_tresor_nomme_la_partie_de_la_mesure(base):
    db = base["db"]
    alerte = _acme_et_son_beneficiaire(db)
    alerte.status, alerte.decided_by = "CLOSED_CONFIRMED", "reviseur"
    mesure = gels.ouvrir_si_due(db, alerte, "reviseur")
    db.commit()
    assert mesure is not None and mesure.fondement == parties.FONDEMENT_DETENTION
    projet = base["http"].get(f"/api/gels/{mesure.id}/draft")
    assert projet.status_code == 200, projet.text
    partie = projet.json()["partie_liee_en_cause"]
    assert partie["nom"] == "Viktor ZOLOTAREV" and partie["client_name"] == "ACME HOLDING SAS"
    imprimable = base["http"].get(f"/api/gels/{mesure.id}/draft/print")
    assert "Partie liée du client en cause" in imprimable.text
    _dit_la_partie(imprimable.text)


def test_une_alerte_sur_le_client_lui_meme_ne_parle_d_aucune_partie(base):
    db = base["db"]
    _client(db, "SOLO-1", None, type_="PP", client_first_name="Viktor",
            client_last_name="ZOLOTAREV", client_dob="1954-01-27", client_gender="M")
    db.add(Snapshot(snapshot_id="dgt-v1", file_type="WATCHLIST_DGT", file_name="dgt.json",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    db.add(_fiche("dgt-v1", "DGT-ZOL", "Viktor", "ZOLOTAREV", "1954-01-27"))
    db.commit()
    rescreen_lookback(db, "WATCHLIST_DGT")
    alerte = db.query(Alert).one()
    assert alerte.partie_liee_id is None
    assert "Partie liée" not in base["http"].get(f"/api/alerts/{alerte.id}/report").text
    projet = base["http"].get(f"/api/alerts/{alerte.id}/str-draft").json()
    assert projet["partie_liee_en_cause"] is None
    assert projet["motifs"]["resume"].startswith("Correspondance de criblage entre « ")
    assert "Le profil a présenté" in base["http"].post(
        f"/api/alerts/{alerte.id}/narrative").json()["narrative"]

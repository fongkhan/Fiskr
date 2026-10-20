"""
Une PPE confirmée a les suites que la loi impose, et le lecteur PEP n'affirme
pas une qualité qu'il n'a pas lue.

Enquête de conformité (lecture du code, octobre 2026) :
- `_close_alert` closait une alerte PEP confirmée comme les autres : aucune
  décision de la direction, aucune origine des fonds, rien ne suivait ;
- `client_pep_flag` n'était comparé à rien : une PPE confirmée chez un client
  que le SI amont déclare non-PPE ne déclenchait rien ;
- le lecteur PEP écrivait « Personne Politiquement Exposée (PEP) » sur chaque
  fiche, proche compris, alors que le format plat d'OpenSanctions ne porte ni
  topics `role.pep` / `role.rca`, ni fonction, ni dates d'occupation.

Ce que ces tests tiennent (Code monétaire et financier, texte consolidé de
codes.droit.org du 2 octobre 2026) :
- L.561-10 1° : PPE, membres directs de la famille, personnes étroitement
  associées ; R.561-18 I : « a cessé d'exercer depuis moins d'un an » ;
- R.561-20-2 : décision de nouer ou maintenir la relation par l'organe
  exécutif ou une personne qu'il habilite ; origine du patrimoine et des
  fonds ; vigilance renforcée.

Une base isolée par test : le contrôle de mise en service et la sonde portent
sur toute l'installation, la base partagée de la suite déciderait du verdict.
"""
import csv
import io
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import api as api_mod
from fiskr import ppe
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (Alert, AlertAttachment, AlertEvent, AuditTrail, Base,
                            ClientEntity, DossierPPE, MesureDeGel, Snapshot,
                            WatchlistEntity, get_db)
from fiskr.settings import (SETTING_ALERT_FOUR_EYES, SETTING_NOTIFICATION_BATCH,
                            SETTING_PERIMETERS, set_setting)


def _connecter(username, roles=("user",)):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": ",".join(roles), "roles": list(roles),
    }


@pytest.fixture
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ppe.sqlite3'}")
    Base.metadata.create_all(engine)
    fabrique = sessionmaker(bind=engine)
    session = fabrique()

    def _db():
        s = fabrique()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    emissions = []
    espion = lambda db, cle, charge=None, **k: emissions.append((cle, charge or {}))
    import fiskr.notifier as notifier_mod
    monkeypatch.setattr(api_mod, "emit", espion)
    monkeypatch.setattr(notifier_mod, "emit", espion)
    fichiers = []
    yield {"db": session, "fabrique": fabrique, "client": TestClient(app),
           "emissions": emissions, "fichiers": fichiers}
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    for chemin in fichiers:
        try:
            os.remove(chemin)
        except OSError:
            pass
    session.close()
    engine.dispose()


def _fiche_pep(db, entity_id, qualite="PPE", role="Ministre des Finances (2019-01-01 – en cours)",
               fin=None, list_type="WATCHLIST_PEP"):
    """La fiche telle que la production la porte : c'est elle que le dossier lit."""
    sid = f"pep-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=sid, file_type=list_type, file_name=f"{sid}.json",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    db.add(WatchlistEntity(snapshot_id=sid, entity_id=entity_id, entity_type="I",
                           primary_name="Listé", entity_checksum="c" * 64,
                           pep_qualite=qualite, pep_role=role, pep_fin_de_fonction=fin))
    db.commit()


def _referentiel(db, client_id, pep_flag):
    sid = f"cli-{uuid.uuid4().hex[:8]}"
    db.add(Snapshot(snapshot_id=sid, file_type="CLIENT_BASE", file_name="clients.csv",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    db.add(ClientEntity(snapshot_id=sid, client_id=client_id, client_type="PP",
                        client_first_name="Jean", client_last_name="Exposé",
                        client_pep_flag=pep_flag, entity_checksum="y" * 64))
    db.commit()


def _alerte(db, nom, list_type="WATCHLIST_PEP", statut="OPEN", client_id=None,
            avec_fiche=True, **fiche):
    client_id = client_id or f"CLI-{nom}"
    audit = AuditTrail(client_id=client_id, client_name=f"Client {nom}", client_type="PP",
                       watchlist_id=f"PEP-{nom}", watchlist_name=f"Listé {nom}",
                       base_score=91.0, final_score=91.0, status="ALERT",
                       decision_tree={"hard_match_triggered": False}, config_state={},
                       watchlist_version="test", watchlist_hash="test", list_type=list_type)
    db.add(audit)
    db.commit()
    a = Alert(audit_id=audit.id, client_id=client_id, client_name=audit.client_name,
              watchlist_entity_id=audit.watchlist_id, watchlist_name=audit.watchlist_name,
              final_score=91.0, list_type=list_type, status=statut, channel="SCREENING",
              priority="HIGH")
    db.add(a)
    db.commit()
    if avec_fiche:
        _fiche_pep(db, audit.watchlist_id, list_type=list_type, **fiche)
    return a


def _confirmer(ctx, a, analyste="analyste", valideur="reviseur"):
    """Proposition par l'analyste, validation 4-yeux par un réviseur."""
    c = ctx["client"]
    _connecter(analyste)
    r = c.post(f"/api/alerts/{a.id}/propose",
               json={"decision": "CONFIRMED", "comment": "Identité alignée, pièces au dossier."})
    assert r.status_code == 200, r.text
    _connecter(valideur, ("reviewer",))
    r = c.post(f"/api/alerts/{a.id}/validate", json={"approve": True, "comment": "Validé."})
    assert r.status_code == 200, r.text
    return r.json()


def _dossiers(ctx, alert_id):
    r = ctx["client"].get(f"/api/ppe?alert_id={alert_id}")
    assert r.status_code == 200, r.text
    return r.json()["items"]


def _actions(db, alert_id):
    db.expire_all()
    return [e.action for e in db.query(AlertEvent).filter(AlertEvent.alert_id == alert_id)]


# ------------------------------------------------- la confirmation ouvre un dossier

def test_confirmer_une_alerte_pep_ouvre_un_dossier_ppe_dans_la_cloture(base):
    """Avant : la clôture d'une alerte PEP confirmée n'ouvrait rien."""
    db = base["db"]
    _referentiel(db, "CLI-Pep", True)
    a = _alerte(db, "Pep")
    reponse = _confirmer(base, a)
    assert "Dossier PPE" in reponse["message"], "la clôture doit dire qu'une suite s'ouvre"

    dossiers = _dossiers(base, a.id)
    assert len(dossiers) == 1
    d = dossiers[0]
    assert d["statut"] == ppe.STATUT_A_DECIDER
    # La source a dit PPE : l'instruction part de là, avec la fonction lue
    assert d["qualite_source"] == ppe.QUALITE_PPE and d["qualite_retenue"] == ppe.QUALITE_PPE
    assert d["fonction"].startswith("Ministre des Finances")
    # Le délai court depuis la confirmation (validation 4-yeux)
    detail = base["client"].get(f"/api/alerts/{a.id}").json()
    assert d["ouvert_le"] == detail["decided_at"]
    assert [x["id"] for x in detail["ppe_dossiers"]] == [d["id"]]
    assert sorted(d["instruit_par"]) == ["analyste", "reviseur"]
    assert "PPE_DOSSIER_OUVERT" in _actions(db, a.id)
    # Client déclaré PPE par le SI amont : rien à signaler
    assert d["ppe_non_declaree"] is False
    assert "PPE_NON_DECLAREE" not in _actions(db, a.id)


def test_la_cloture_directe_sans_quatre_yeux_ouvre_aussi_le_dossier(base):
    """Le dossier vit dans `_close_alert` : aucun des deux chemins ne l'oublie."""
    db = base["db"]
    set_setting(db, SETTING_ALERT_FOUR_EYES, False)
    a = _alerte(db, "Direct")
    _connecter("analyste")
    r = base["client"].post(f"/api/alerts/{a.id}/propose",
                            json={"decision": "CONFIRMED", "comment": "Concordance établie."})
    assert r.status_code == 200, r.text
    assert len(_dossiers(base, a.id)) == 1


def test_une_alerte_de_liste_sanctions_n_ouvre_pas_de_dossier_ppe(base):
    db = base["db"]
    a = _alerte(db, "Ofac", list_type="WATCHLIST_OFAC")
    reponse = _confirmer(base, a)
    assert "Dossier PPE" not in reponse["message"]
    assert _dossiers(base, a.id) == []
    detail = base["client"].get(f"/api/alerts/{a.id}").json()
    assert detail["ppe_dossiers"] == [] and detail["ppe_du"] is False
    # Et on ne peut pas en forcer un
    assert base["client"].post(f"/api/alerts/{a.id}/ppe").status_code == 409


def test_un_faux_positif_pep_n_ouvre_aucun_dossier(base):
    db = base["db"]
    a = _alerte(db, "Fp")
    _connecter("analyste")
    base["client"].post(f"/api/alerts/{a.id}/propose",
                        json={"decision": "FALSE_POSITIVE", "comment": "Date de naissance incompatible."})
    _connecter("reviseur", ("reviewer",))
    assert base["client"].post(f"/api/alerts/{a.id}/validate", json={"approve": True}).status_code == 200
    assert _dossiers(base, a.id) == []


def test_le_classement_sanction_d_une_liste_pep_ouvre_le_gel_et_le_dossier(base):
    """Les deux obligations sont indépendantes : la liste PEP est dérivée de
    fiskr/perimeters.py, pas du périmètre SANCTION / HORS_SANCTION."""
    db = base["db"]
    set_setting(db, SETTING_PERIMETERS, {"WATCHLIST_PEP": "SANCTION"})
    a = _alerte(db, "Double")
    _confirmer(base, a)
    db.expire_all()
    assert db.query(MesureDeGel).filter(MesureDeGel.alert_id == a.id).count() == 1
    assert len(_dossiers(base, a.id)) == 1


def test_une_partie_de_paiement_n_a_pas_de_dossier_et_l_ecran_dit_pourquoi(base):
    db = base["db"]
    a = _alerte(db, "Txn", client_id="TXN:MSG-1:0")
    _confirmer(base, a)
    assert _dossiers(base, a.id) == []
    detail = base["client"].get(f"/api/alerts/{a.id}").json()
    assert detail["ppe_du"] is False
    assert "Partie d'un paiement" in detail["ppe_sans_dossier"]


# ------------------------------------------------- la décision de la direction

def _decider(ctx, dossier_id, qui, roles=("reviewer",), **champs):
    _connecter(qui, roles)
    corps = {"decision": "REFUS", "motif": "Origine des fonds non établie."}
    corps.update(champs)
    return ctx["client"].post(f"/api/ppe/{dossier_id}/decision", json=corps)


def test_la_decision_par_l_analyste_qui_a_confirme_est_refusee(base):
    db = base["db"]
    a = _alerte(db, "Decision")
    _confirmer(base, a, analyste="analyste_rev", valideur="valideur")
    d = _dossiers(base, a.id)[0]
    # Ni celui qui a validé la confirmation, ni celui qui l'a proposée — même réviseur
    assert _decider(base, d["id"], "valideur").status_code == 403
    assert _decider(base, d["id"], "analyste_rev").status_code == 403
    # Un analyste sans habilitation : refusé par le rôle
    assert _decider(base, d["id"], "autre_analyste", roles=("user",)).status_code == 403
    assert _dossiers(base, a.id)[0]["statut"] == ppe.STATUT_A_DECIDER
    # Une personne habilitée qui n'a pas instruit : la décision passe
    r = _decider(base, d["id"], "direction")
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == ppe.STATUT_DECIDE and r.json()["decidee_par"] == "direction"
    assert "PPE_DECISION" in _actions(db, a.id)
    # Un dossier décidé ne se réécrit plus
    _connecter("analyste_rev")
    assert base["client"].put(f"/api/ppe/{d['id']}", json={"origine_fonds": "x"}).status_code == 409


def test_accepter_la_relation_exige_l_origine_des_fonds_une_piece_et_une_revue(base):
    db = base["db"]
    a = _alerte(db, "Maintien")
    _confirmer(base, a)
    d = _dossiers(base, a.id)[0]
    revue = (datetime.utcnow() + timedelta(days=365)).isoformat()
    sans_rien = _decider(base, d["id"], "direction", decision="MAINTIEN",
                         motif="Relation ancienne et documentée.", prochaine_revue_le=revue)
    assert sans_rien.status_code == 409
    assert "origine des fonds" in sans_rien.json()["detail"]

    _connecter("analyste")
    r = base["client"].put(f"/api/ppe/{d['id']}", json={
        "origine_fonds": "Salaires de la fonction publique (bulletins 2025-2026).",
        "origine_patrimoine": "Résidence principale acquise en 2012 (acte notarié)."})
    assert r.status_code == 200, r.text
    assert _decider(base, d["id"], "direction", decision="MAINTIEN", motif="Documentée.",
                    prochaine_revue_le=revue).status_code == 409, "sans pièce, pas de maintien"
    _connecter("analyste")
    piece = base["client"].post(f"/api/ppe/{d['id']}/pieces", files={
        "file": ("avis_imposition.pdf", io.BytesIO(b"%PDF-1.4 avis"), "application/pdf")})
    assert piece.status_code == 200, piece.text
    db.expire_all()
    pj = db.query(AlertAttachment).filter(AlertAttachment.alert_id == a.id).first()
    base["fichiers"].append(pj.file_path)
    # La pièce est une pièce jointe de l'alerte : l'inventaire probant la voit
    assert piece.json()["pieces_origine"] == [{"id": pj.id, "file_name": pj.file_name,
                                               "file_present": True}]
    sans_revue = _decider(base, d["id"], "direction", decision="MAINTIEN", motif="Documentée.")
    assert sans_revue.status_code == 400 and "revue" in sans_revue.json()["detail"]
    ok = _decider(base, d["id"], "direction", decision="MAINTIEN", motif="Documentée.",
                  prochaine_revue_le=revue, reference="Comité des risques du 20/10/2026")
    assert ok.status_code == 200, ok.text
    assert ok.json()["prochaine_revue_le"] is not None and ok.json()["revue_echue"] is False


# ------------------------------------------------- la PPE que le SI amont ne déclare pas

@pytest.mark.parametrize("drapeau", [False, None])
def test_une_ppe_confirmee_chez_un_client_declare_non_ppe_leve_l_evenement(base, drapeau):
    """`client_pep_flag` faux ou vide : le dossier le dit, un événement d'alerte
    le nomme, et Fiskr ne corrige pas lui-même la classification amont."""
    db = base["db"]
    _referentiel(db, "CLI-NonDeclare", drapeau)
    a = _alerte(db, "NonDeclare", client_id="CLI-NonDeclare")
    reponse = _confirmer(base, a)
    assert "PPE non déclarée par le SI amont" in reponse["message"]
    d = _dossiers(base, a.id)[0]
    assert d["ppe_non_declaree"] is True
    assert d["client_pep_declare"] is drapeau
    assert "PPE_NON_DECLAREE" in _actions(db, a.id)
    db.expire_all()
    ligne = db.query(ClientEntity).filter(ClientEntity.client_id == "CLI-NonDeclare").first()
    assert ligne.client_pep_flag is drapeau, "le produit ne corrige pas le SI amont"
    filtre = base["client"].get("/api/ppe?statut=NON_DECLAREE").json()
    assert d["id"] in [x["id"] for x in filtre["items"]]


def test_un_client_absent_du_referentiel_n_est_pas_tenu_pour_declare(base):
    db = base["db"]
    a = _alerte(db, "Absent", client_id="CLI-INCONNU")
    _confirmer(base, a)
    assert _dossiers(base, a.id)[0]["ppe_non_declaree"] is True


# ------------------------------------------------- la qualité lue, pas supposée

def _nested(*cibles):
    return "\n".join(json.dumps(c, ensure_ascii=False) for c in cibles) + "\n"


PPE_EN_FONCTION = {
    "id": "NK-ppe", "caption": "Ramesh Kumar", "schema": "Person", "target": True,
    "first_seen": "2024-09-04T09:26:39",
    "properties": {"name": ["Ramesh Kumar"], "topics": ["role.pep"], "citizenship": ["in"],
                   "birthDate": ["1955-04-20"],
                   "positionOccupancies": [{"schema": "Occupancy", "properties": {
                       "status": ["current"], "startDate": ["2024-06-04"],
                       "post": [{"caption": "Member of the Lok Sabha", "schema": "Position",
                                 "properties": {"name": ["Member of the Lok Sabha"]}}]}}]},
}
PROCHE = {
    # Topic `role.rca` seul, lien Family vers une PPE : la mère d'un député
    "id": "NK-proche", "caption": "Mee Kaur", "schema": "Person", "target": True,
    "first_seen": "2026-07-29T11:20:56",
    "properties": {"name": ["Smt. Mee Kaur", "Mee Kaur"], "topics": ["role.rca"], "country": ["in"],
                   "familyRelative": [{"schema": "Family", "properties": {
                       "relationship": ["mother"], "relative": ["NK-proche"],
                       "person": [{"id": "Q7289411", "caption": "Ramesh Kumar", "schema": "Person",
                                   "properties": {"topics": ["role.pep"]}}]}}]},
}
ASSOCIE = {
    "id": "NK-associe", "caption": "Partenaire d'affaires", "schema": "Person", "target": True,
    "properties": {"name": ["Partenaire d'affaires"], "topics": ["role.rca"],
                   "associations": [{"schema": "Associate", "properties": {
                       "relationship": ["business partner"],
                       "person": [{"id": "Q1", "caption": "Ministre X", "schema": "Person",
                                   "properties": {"topics": ["role.pep"]}}]}}]},
}
PROCHE_SANS_LIEN = {"id": "NK-rca", "caption": "Proche sans lien", "schema": "Person", "target": True,
                    "properties": {"name": ["Proche sans lien"], "topics": ["role.rca"]}}
SANS_TOPIC = {"id": "NK-poi", "caption": "Agent public", "schema": "Person", "target": True,
              "properties": {"name": ["Agent public"], "topics": ["poi"]}}


def test_une_fiche_de_proche_n_est_plus_marquee_ppe(tmp_path):
    """Le format imbriqué dit la qualité : une proche n'est pas une PPE. Le
    format plat ne la dit pas : la fiche le dit, au lieu d'affirmer PPE."""
    from fiskr.ingest import parse_pep_targets
    imbrique = tmp_path / "targets.nested.json"
    imbrique.write_text(_nested(PPE_EN_FONCTION, PROCHE, ASSOCIE, PROCHE_SANS_LIEN, SANS_TOPIC),
                        encoding="utf-8")
    fiches = {f["entity_id"]: f for f in parse_pep_targets(str(imbrique))}
    assert {k: f["pep_qualite"] for k, f in fiches.items()} == {
        "PEP-NK-ppe": ppe.QUALITE_PPE, "PEP-NK-proche": ppe.QUALITE_FAMILLE,
        "PEP-NK-associe": ppe.QUALITE_ASSOCIE, "PEP-NK-rca": ppe.QUALITE_PROCHE,
        "PEP-NK-poi": ppe.QUALITE_NON_PRECISEE}
    proche = fiches["PEP-NK-proche"]
    assert proche["designation_reasons"] == ppe.motif_de_fiche(ppe.QUALITE_FAMILLE)
    assert proche["designation_reasons"] != ppe.ANCIEN_MOTIF_PEP
    assert not proche["designation_reasons"].startswith("Personne politiquement exposée")
    assert proche["pep_role"] is None, "la fonction de la PPE n'est pas celle de sa mère"
    assert proche["pep_liens"] == [{"nature": "FAMILLE", "relation": "mother", "personne": "Ramesh Kumar",
                                    "personne_id": "Q7289411", "personne_ppe": True}]
    assert fiches["PEP-NK-ppe"]["pep_role"] == "Member of the Lok Sabha (2024-06-04 – en cours)"
    assert fiches["PEP-NK-ppe"]["pep_fin_de_fonction"] is None, "fonction en cours : pas de fin"

    # Repli explicite : le format plat ne distingue rien, et le dit
    plat = tmp_path / "targets.simple.csv"
    plat.write_text("id,schema,name,aliases,birth_date,countries,addresses,identifiers,sanctions,"
                    "phones,emails,program_ids,dataset,first_seen,last_seen,last_change\n"
                    "NK-proche,Person,Mee Kaur,Smt. Mee Kaur,,in,,,,,,,India Lok and Rajya Sabha Members,"
                    "2026-07-29T11:20:56,2026-10-06T11:14:32,2026-07-29T11:20:56\n", encoding="utf-8")
    [lue] = list(parse_pep_targets(str(plat)))
    assert lue["pep_qualite"] == ppe.QUALITE_NON_PRECISEE
    assert lue["designation_reasons"] != ppe.ANCIEN_MOTIF_PEP
    assert "non précisée par la source" in lue["designation_reasons"]
    # Les deux formats donnent la même fiche d'identité : changer de format
    # n'est pas changer de personne
    from fiskr.database import normaliser_fiche
    from fiskr.screenpool import ENTITY_PROJECTION
    a, b = normaliser_fiche(lue), normaliser_fiche(proche)
    assert {k: a.get(k) for k in ENTITY_PROJECTION} == {k: b.get(k) for k in ENTITY_PROJECTION}


def test_la_fin_de_fonction_est_conservee_et_une_date_partielle_lue_par_sa_borne_haute(tmp_path):
    from fiskr.ingest import parse_pep_targets_nested_json
    ancienne = {"id": "NK-anc", "caption": "Ancien maire", "schema": "Person", "target": True,
                "properties": {"name": ["Ancien maire"], "topics": ["role.pep"],
                               "positionOccupancies": [
                                   {"properties": {"status": ["ended"], "startDate": ["2014"],
                                                   "endDate": ["2019"], "post": [{"caption": "Maire"}]}},
                                   {"properties": {"status": ["ended"], "startDate": ["2008-03"],
                                                   "endDate": ["2014-03"], "post": [{"caption": "Adjoint"}]}}]}}
    non_datee = {"id": "NK-nd", "caption": "Fin inconnue", "schema": "Person", "target": True,
                 "properties": {"name": ["Fin inconnue"], "topics": ["role.pep"],
                                "positionOccupancies": [{"properties": {"status": ["ended"],
                                                                         "post": [{"caption": "Député"}]}}]}}
    chemin = tmp_path / "t.json"
    chemin.write_text(_nested(ancienne, non_datee), encoding="utf-8")
    fiches = {f["entity_id"]: f for f in parse_pep_targets_nested_json(str(chemin))}
    # « 2019 » se lit 31 décembre : lue 1er janvier, la règle des douze mois
    # dirait l'exposition échue jusqu'à un an trop tôt
    assert fiches["PEP-NK-anc"]["pep_fin_de_fonction"] == "2019-12-31"
    assert fiches["PEP-NK-anc"]["pep_role"].startswith("Maire (2014 – 2019-12-31)")
    # Terminée sans date : la fin n'est pas inventée
    assert fiches["PEP-NK-nd"]["pep_fin_de_fonction"] is None
    assert "fin non datée" in fiches["PEP-NK-nd"]["pep_role"]


def test_une_fonction_terminee_depuis_treize_mois_est_signalee_sans_rien_fermer(base):
    db = base["db"]
    maintenant = datetime.utcnow()
    treize_mois = (maintenant - timedelta(days=396)).date().isoformat()
    onze_mois = (maintenant - timedelta(days=335)).date().isoformat()
    assert ppe.exposition_echue(treize_mois, maintenant) is True
    assert ppe.exposition_echue(onze_mois, maintenant) is False
    assert ppe.exposition_echue(None, maintenant) is False, "une fin inconnue n'est pas une fin"

    a = _alerte(db, "Ancien", fin=treize_mois, role="Ministre (2015 – " + treize_mois + ")")
    _confirmer(base, a)
    d = _dossiers(base, a.id)[0]
    assert d["fin_de_fonction"] == treize_mois and d["exposition_echue"] is True
    assert d["statut"] == ppe.STATUT_A_DECIDER, "rien n'est fermé automatiquement"
    export = list(csv.DictReader(io.StringIO(
        base["client"].get("/api/export/ppe.csv").text.lstrip("﻿")), delimiter=";"))
    assert [l["fonction_terminee_depuis_plus_d_un_an"] for l in export] == ["oui"]
    # L'instruction peut rectifier la fin de fonction ; le constat suit
    _connecter("analyste")
    r = base["client"].put(f"/api/ppe/{d['id']}", json={"fin_de_fonction": onze_mois})
    assert r.status_code == 200 and r.json()["exposition_echue"] is False


# ------------------------------------------------- le délai de décision

def _vieillir(db, dossier_id, jours):
    db.expire_all()
    d = db.query(DossierPPE).filter(DossierPPE.id == dossier_id).first()
    d.ouvert_le = datetime.utcnow() - timedelta(days=jours)
    db.commit()


def test_un_dossier_sans_decision_au_dela_du_delai_leve_evenement_et_controle(base, monkeypatch):
    from fiskr import mise_en_service as mes
    db = base["db"]
    _referentiel(db, "CLI-Retard", False)
    a = _alerte(db, "Retard", client_id="CLI-Retard")
    _confirmer(base, a)
    d = _dossiers(base, a.id)[0]
    _vieillir(db, d["id"], 31)          # seuil par défaut : 30 jours

    # Détecté par le tic de chaque minute, même récapitulatif coupé
    set_setting(db, SETTING_NOTIFICATION_BATCH, {"enabled": False, "cron": "0 * * * *"})
    monkeypatch.setattr(api_mod, "get_db", lambda: iter([base["fabrique"]()]))
    api_mod._notification_batch_tick()
    retards = [p for k, p in base["emissions"] if k == "ppe_decision_en_retard"]
    assert len(retards) == 1 and retards[0]["Dossier"] == f"#{d['id']}"
    # Une seule fois par dossier
    ppe.detecter_retards(db)
    assert len([1 for k, _ in base["emissions"] if k == "ppe_decision_en_retard"]) == 1
    assert "PPE_DECISION_EN_RETARD" in _actions(db, a.id)

    controle = mes._dossiers_ppe(db)
    assert controle["etat"] == mes.BLOQUANT and f"#{d['id']}" in controle["constat"]
    # La sonde le rejoue, sans nom de personne
    sonde = mes.etat_de_conformite(db)
    assert not sonde["conforme"] and "ppe" in sonde["portee"]
    constat = next(b["constat"] for b in sonde["bloquants"] if b["cle"] == "ppe")
    assert "Client Retard" not in constat and "Listé Retard" not in constat
    assert _dossiers(base, a.id)[0]["en_retard"] is True
    filtre = base["client"].get("/api/ppe?statut=EN_RETARD").json()
    assert [x["id"] for x in filtre["items"]] == [d["id"]]
    assert filtre["synthese"]["overdue"] == 1


def test_dans_le_delai_rien_ne_sonne_et_rien_ne_bloque(base):
    from fiskr import mise_en_service as mes
    db = base["db"]
    a = _alerte(db, "Delai")
    _confirmer(base, a)
    _vieillir(db, _dossiers(base, a.id)[0]["id"], 29)
    assert ppe.detecter_retards(db) == 0
    assert not [k for k, _ in base["emissions"] if k == "ppe_decision_en_retard"]
    assert mes._dossiers_ppe(db)["etat"] == mes.OK


def test_le_delai_est_un_reglage_borne(base):
    _connecter("admin", ("admin",))
    c = base["client"]
    assert c.put("/api/settings/ingestion", json={"ppe_decision_delay_days": 0}).status_code == 400
    assert c.put("/api/settings/ingestion", json={"ppe_decision_delay_days": 400}).status_code == 400
    r = c.put("/api/settings/ingestion", json={"ppe_decision_delay_days": 10})
    assert r.status_code == 200, r.text
    assert r.json()["ppe_decision_delay_days"] == 10
    db = base["db"]
    a = _alerte(db, "Court")
    _confirmer(base, a)
    _vieillir(db, _dossiers(base, a.id)[0]["id"], 11)
    assert ppe.detecter_retards(db) == 1


def test_une_ppe_confirmee_avant_le_dossier_est_nommee_et_rattrapable(base):
    """Rien ne fera entrer seules les PPE confirmées avant le dossier PPE : le
    contrôle les nomme, et le dossier ouvert après coup court depuis la
    confirmation — il ne rajeunit pas une décision qui manque."""
    from fiskr import mise_en_service as mes
    db = base["db"]
    a = _alerte(db, "Historique", statut="CLOSED_CONFIRMED")
    a.decided_at = datetime.utcnow() - timedelta(days=45)
    a.decided_by = "ancien_analyste"
    db.commit()
    controle = mes._dossiers_ppe(db)
    assert controle["etat"] == mes.BLOQUANT and f"alerte #{a.id}" in controle["constat"]
    assert "Client Historique" not in controle["constat_public"]
    _connecter("analyste")
    assert base["client"].get(f"/api/alerts/{a.id}").json()["ppe_du"] is True

    r = base["client"].post(f"/api/alerts/{a.id}/ppe")
    assert r.status_code == 200, r.text
    assert r.json()["ouvert_le"] == a.decided_at.isoformat()
    assert r.json()["en_retard"] is True
    assert base["client"].post(f"/api/alerts/{a.id}/ppe").status_code == 409
    assert f"alerte #{a.id}" not in mes._dossiers_ppe(db)["constat"]


# ------------------------------------------------- l'indicateur

def test_le_delai_confirmation_decision_n_a_qu_une_definition(base):
    from fiskr.kpi import decision_ppe_en_retard, delai_de_decision_ppe_secondes
    t0 = datetime(2026, 10, 1, 8, 0)
    assert delai_de_decision_ppe_secondes(t0, t0 + timedelta(days=3)) == 3 * 86400
    # Décision antérieure à la confirmation (client déjà connu PPE) : 0
    assert delai_de_decision_ppe_secondes(t0, t0 - timedelta(days=200)) == 0
    assert delai_de_decision_ppe_secondes(t0, None, t0 + timedelta(days=5)) == 5 * 86400
    assert decision_ppe_en_retard(ppe.STATUT_A_DECIDER, t0, None, 30, t0 + timedelta(days=31))
    assert not decision_ppe_en_retard(ppe.STATUT_DECIDE, t0, t0 + timedelta(days=40), 30,
                                      t0 + timedelta(days=41))

    db = base["db"]
    a = _alerte(db, "Kpi")
    _confirmer(base, a)
    d = _dossiers(base, a.id)[0]
    _vieillir(db, d["id"], 2)
    assert _decider(base, d["id"], "direction").status_code == 200
    _connecter("admin", ("admin",))
    kpi = base["client"].get("/api/kpi").json()["pep_dossiers"]
    assert kpi["decided"] == 1 and kpi["to_decide"] == 0
    assert 1.9 * 86400 <= kpi["avg_decision_delay_seconds"] <= 2.1 * 86400


def test_l_auditeur_lit_les_dossiers_et_ne_decide_pas(base):
    """L'écriture d'un auditeur est refusée par `get_current_user` lui-même
    (fiskr/auth.py) ; ici, la lecture lui est ouverte et la décision fermée."""
    db = base["db"]
    a = _alerte(db, "Audit")
    _confirmer(base, a)
    d = _dossiers(base, a.id)[0]
    _connecter("controleur", ("auditor",))
    c = base["client"]
    assert c.get("/api/ppe").status_code == 200
    detail = c.get(f"/api/ppe/{d['id']}")
    assert detail.status_code == 200
    assert "PPE_DOSSIER_OUVERT" in [e["action"] for e in detail.json()["evenements"]]
    assert c.get("/api/export/ppe.csv").status_code == 200
    assert _decider(base, d["id"], "controleur", roles=("auditor",)).status_code == 403


def test_l_ecran_des_dossiers_existe_et_appelle_les_dossiers():
    racine = Path(__file__).resolve().parent.parent / "fiskr" / "static"
    index = (racine / "index.html").read_text(encoding="utf-8")
    app_js = (racine / "app.js").read_text(encoding="utf-8")
    assert 'id="sub-sec-alerts-ppe"' in index and 'id="sub-btn-alerts-ppe"' in index
    assert 'id="setting-ppe-delay"' in index
    for appel in ("/api/ppe?", "/api/export/ppe.csv", "/pieces", "/decision", "/ppe`",
                  "blocDossiersPPE(a, isReviewer)", "ppe_decision_delay_days"):
        assert appel in app_js, appel


# ------------------------------------------------- le premier lot PEP après le déploiement

def _csv_pep(lignes):
    tampon = io.StringIO()
    colonnes = ["id", "schema", "name", "aliases", "birth_date", "countries", "addresses",
                "identifiers", "sanctions", "phones", "emails", "first_seen"]
    ecrivain = csv.DictWriter(tampon, fieldnames=colonnes, quoting=csv.QUOTE_ALL)
    ecrivain.writeheader()
    for os_id, nom in lignes:
        ecrivain.writerow({"id": os_id, "schema": "Person", "name": nom, "aliases": "",
                           "birth_date": "1960-01-01", "countries": "fr", "addresses": "",
                           "identifiers": "", "sanctions": "", "phones": "", "emails": "",
                           "first_seen": "2024-01-01"})
    return tampon.getvalue()


def test_le_premier_lot_pep_apres_le_deploiement_ne_fait_pas_de_vague(base, tmp_path):
    """La production porte « Personne Politiquement Exposée (PEP) » sur chaque
    fiche (version précédente). Le lot relu par cette version dit « qualité
    non précisée » : ce n'est pas une modification de la source, c'est le
    lecteur qui cesse d'affirmer. Compté à part, pas en 709 511 modifications ;
    une qualité LUE (format imbriqué), elle, reste une modification."""
    from fiskr.ingest import parse_pep_targets_csv
    from fiskr.names import ensure_parsed_name
    from fiskr.quality import evaluate_and_clean
    from fiskr.sync import build_watchlist_entity, run_pep_sync
    db = base["db"]
    lignes = [("NK-a", "Alice Exposée"), ("NK-b", "Bruno Exposé")]
    chemin = tmp_path / "prod.csv"
    chemin.write_text(_csv_pep(lignes), encoding="utf-8")
    sid = "pep-prod"
    db.add(Snapshot(snapshot_id=sid, file_type="WATCHLIST_PEP", file_name="prod.csv",
                    file_hash=uuid.uuid4().hex, record_count=2, status="READY",
                    uploaded_at=datetime(2026, 9, 1)))
    for item in parse_pep_targets_csv(str(chemin)):
        # Ce que la version précédente écrivait
        item.update(designation_reasons=ppe.ANCIEN_MOTIF_PEP, pep_qualite=None)
        item = ensure_parsed_name(item)
        db.add(build_watchlist_entity(sid, item, evaluate_and_clean(item)))
    db.commit()

    contenu = _csv_pep(lignes)
    rapport = run_pep_sync(db, fetcher=lambda url, dest: Path(dest).write_text(contenu, encoding="utf-8"))
    assert rapport.status == "SUCCESS", rapport.message
    resume, details = rapport.delta_report["summary"], rapport.delta_report["details"]
    assert (resume["added_count"], resume["removed_count"], resume["modified_count"]) == (0, 0, 0)
    assert details["reader_quality_form_count"] == 2
    assert details["reader_form_count"] == 2, "à homologuer une fois : la production change de forme"

    # Une qualité lue n'est pas une forme : le réviseur doit la voir
    from fiskr.delta import FORME_QUALITE, MODIFIEE, _comparer
    avant = {"entity_id": "PEP-NK-a", "designation_reasons": ppe.ANCIEN_MOTIF_PEP}
    apres = {"entity_id": "PEP-NK-a", "pep_qualite": ppe.QUALITE_FAMILLE,
             "designation_reasons": ppe.motif_de_fiche(ppe.QUALITE_FAMILLE)}
    assert _comparer("PEP-NK-a", "x", avant, apres, None)[0] == MODIFIEE
    non_precisee = dict(apres, pep_qualite=ppe.QUALITE_NON_PRECISEE,
                        designation_reasons=ppe.motif_de_fiche(ppe.QUALITE_NON_PRECISEE))
    assert _comparer("PEP-NK-a", "x", avant, non_precisee, None)[0] == FORME_QUALITE

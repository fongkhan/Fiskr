"""
L'analyste décide avec les faits sous les yeux, et ne peut pas se tromper de motif.

Mesuré dans un vrai navigateur, sur les données de l'exploration :

* l'alerte n'affichait que deux noms et l'explication du score ; la date de
  naissance du client se lisait dans « Client 360° » (troisième modale
  empilée), celle de la personne listée par Ctrl+K → la référence → Entrée —
  environ neuf gestes par alerte pour cocher « identité vérifiée » ;
* le dialogue « Proposer faux positif » offrait « Correspondance confirmée :
  identité alignée… », et le serveur acceptait un faux positif ainsi motivé ;
* l'alerte n° 20, close en faux positif après 4-yeux, s'est rouverte dix
  minutes plus tard en n° 28 sur la même paire (« Recribler ce client ») : la
  liste blanche ne se proposait qu'après coup ;
* 24 décisions sur 28 portaient un identifiant `SCREEN-<aléa>` tiré au hasard
  par le formulaire, et deux alertes sur la même fiche, au même score, ne
  se rapprochaient jamais.

Ces tests passent par les vraies portes de l'API, sur une base isolée, avec un
index de criblage simulé qui sert les fiches de cette base.
"""
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fiskr.api as api_module
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (Alert, AlertEvent, AuditTrail, Base, ClientEntity, Snapshot,
                            WatchlistEntity, WhitelistPair, compute_checksum, get_db)
from fiskr.settings import (DEFAULT_ALERT_CLOSE_REASONS, SETTING_ALERT_CLOSE_REASONS,
                            SETTING_ALERT_FOUR_EYES, SETTING_WHITELIST_FILE_REQUIRED,
                            alert_close_reasons, set_setting)

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")

ANALYSTE = {"id": 1, "username": "analyste-marie", "full_name": "Marie",
            "role": "user", "roles": ["user"]}
REVISEUR = {"id": 2, "username": "reviseur-paul", "full_name": "Paul",
            "role": "reviewer", "roles": ["reviewer"]}
MOTIF_VRAI_POSITIF = ("Correspondance confirmée : identité alignée sur la fiche listée "
                      "(pièces au dossier).")
MOTIF_HOMONYMIE = "Homonymie : date de naissance incompatible avec la fiche listée."


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'decider.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def qui():
    """L'utilisateur courant, modifiable en cours de test (4-yeux)."""
    return {"courant": ANALYSTE}


@pytest.fixture
def api(db, qui):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: qui["courant"]
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


def _referentiel(db, **champs):
    snap = Snapshot(snapshot_id=f"clients-{uuid.uuid4().hex[:6]}", file_type="CLIENT_BASE",
                    file_name="clients.csv", file_hash=uuid.uuid4().hex, record_count=1,
                    status="READY")
    db.add(snap)
    client = dict(client_id="CLI-000390", client_type="PP", client_first_name="Pierre",
                  client_last_name="Dubois", client_dob="1982-02-03", client_gender="M",
                  client_countries={"nationality": ["FR"], "residence": ["FR"],
                                    "birth_country": ["FR"], "registration_country": []})
    client.update(champs)
    db.add(ClientEntity(snapshot_id=snap.snapshot_id, entity_checksum=uuid.uuid4().hex, **client))
    db.commit()
    return client


def _liste_en_production(db, monkeypatch, **champs):
    """EU-0022 « Pierre DUBOIS », né en 1969, en production — et l'index de
    criblage qui la sert, sous la forme exacte du cache moteur (identifiant de
    ligne compris)."""
    snap = Snapshot(snapshot_id=f"eu-{uuid.uuid4().hex[:6]}", file_type="WATCHLIST_EU",
                    file_name="liste_ue.csv", file_hash=uuid.uuid4().hex, record_count=1,
                    status="READY")
    db.add(snap)
    donnees = dict(entity_id="EU-0022", entity_type="I", primary_name="Pierre DUBOIS",
                   individual_name_parsed={"first_name": "Pierre", "last_name": "DUBOIS",
                                           "maiden_name": ""},
                   aliases={"high_priority": [], "low_priority": []},
                   dates_of_birth=["1969-07-14"], gender="M",
                   countries={"citizenship": ["FR"], "residence": [], "birth_country": [],
                              "jurisdiction_country": []},
                   designation_reasons="Test homonyme", official_reference="EU/2026/22")
    donnees.update(champs)
    ligne = WatchlistEntity(snapshot_id=snap.snapshot_id,
                            entity_checksum=compute_checksum(donnees), **donnees)
    db.add(ligne)
    db.commit()
    fiche = {c.name: api_module._valeur_de_cache(getattr(ligne, c.name))
             for c in ligne.__table__.columns}
    fiche["_list_type"] = "WATCHLIST_EU"

    class _Index(dict):
        def get(self, cle, defaut=None):
            return [fiche]

    monkeypatch.setattr(api_module, "watchlist_index", _Index())
    monkeypatch.setattr(api_module, "watchlist_store", [fiche])
    monkeypatch.setattr(api_module, "watchlist_types", {"WATCHLIST_EU"})
    monkeypatch.setattr(api_module, "_ensure_watchlist_cache", lambda db: None)
    return ligne


def _ligne(comparaison, famille):
    lignes = [l for l in comparaison["lignes"] if l["famille"] == famille]
    assert len(lignes) == 1, comparaison["lignes"]
    return lignes[0]


def _valeurs(cote):
    return [v for entree in cote for v in entree["valeurs"]]


# ------------------ 1. LA COMPARAISON DANS L'ALERTE ------------------

def test_l_alerte_d_un_pierre_dubois_ne_en_1982_montre_l_ecart_de_naissance(db, api, monkeypatch):
    _referentiel(db)
    fiche = _liste_en_production(db, monkeypatch)
    criblage = api.post("/api/clients/CLI-000390/screen").json()
    assert criblage["alert_id"], criblage

    detail = api.get(f"/api/alerts/{criblage['alert_id']}").json()
    comparaison = detail["comparison"]
    assert comparaison["available"] is True
    assert comparaison["client_source"] == "REFERENTIEL"
    assert comparaison["watchlist_source"] == "FICHE_RENCONTREE"

    naissance = _ligne(comparaison, "dob")
    assert _valeurs(naissance["client"]) == ["1982-02-03"]
    assert _valeurs(naissance["fiche"]) == ["1969-07-14"]
    assert naissance["verdict"] == "ECART"
    assert naissance["verdict_libelle"] == "Écart"

    pays = _ligne(comparaison, "geography")
    assert pays["verdict"] == "CONCORDANCE"
    assert {e["libelle"] for e in pays["client"]} >= {"Nationalité", "Résidence"}

    noms = _ligne(comparaison, "names")
    assert "Pierre DUBOIS" in _valeurs(noms["fiche"])
    # Motif, programme et référence : ce que la fiche dit de la mesure
    contexte = {e["libelle"]: e["valeurs"] for e in comparaison["contexte"]}
    assert contexte["Référence de la fiche"] == ["EU-0022"]
    assert contexte["Motifs de la désignation"] == ["Test homonyme"]
    # Le lien ouvre la version rencontrée, toujours en base
    assert comparaison["watchlist_db_id"] == fiche.id
    assert comparaison["notes"] == []


def test_un_client_hors_referentiel_est_compare_depuis_sa_ligne_d_audit(db, api, monkeypatch):
    """Criblage manuel : le client n'est connu que par l'appel temps réel. Sa
    ligne d'audit portait son nom et rien d'autre — elle porte désormais
    l'identification que le moteur a comparée."""
    _liste_en_production(db, monkeypatch)
    criblage = api.post("/api/screen", json={
        "client_type": "PP", "client_first_name": "Pierre", "client_last_name": "Dubois",
        "client_dob": "1982-02-03", "client_countries": {"nationality": ["FR"]}}).json()
    ligne = db.query(AuditTrail).filter(AuditTrail.id == criblage["audit_trail_id"]).one()
    assert ligne.decision_tree["client_identification"]["client_dob"] == "1982-02-03"
    # La réponse ne la retransporte pas sur chaque correspondance
    assert all("client_identification" not in m for m in criblage["all_matches"])

    comparaison = api.get(f"/api/alerts/{criblage['alert_id']}").json()["comparison"]
    assert comparaison["client_source"] == "AUDIT"
    assert _valeurs(_ligne(comparaison, "dob")["client"]) == ["1982-02-03"]
    assert _ligne(comparaison, "dob")["verdict"] == "ECART"


def test_une_fiche_purgee_reste_comparee_dans_sa_version_rencontree(db, api, monkeypatch):
    _referentiel(db)
    fiche = _liste_en_production(db, monkeypatch)
    alerte = api.post("/api/clients/CLI-000390/screen").json()["alert_id"]
    db.query(WatchlistEntity).filter(WatchlistEntity.id == fiche.id).delete()
    db.commit()

    comparaison = api.get(f"/api/alerts/{alerte}").json()["comparison"]
    assert _valeurs(_ligne(comparaison, "dob")["fiche"]) == ["1969-07-14"]
    assert any("plus conservée en base" in n for n in comparaison["notes"])
    # Aucun lien qui promettrait une fiche disparue
    assert comparaison["watchlist_db_id"] is None


def test_un_client_modifie_depuis_le_criblage_est_signale(db, api, monkeypatch):
    _referentiel(db)
    _liste_en_production(db, monkeypatch)
    alerte = api.post("/api/clients/CLI-000390/screen").json()["alert_id"]
    rangee = db.query(ClientEntity).filter(ClientEntity.client_id == "CLI-000390").one()
    rangee.client_dob = "1969-07-14"
    db.commit()

    comparaison = api.get(f"/api/alerts/{alerte}").json()["comparison"]
    assert any("a changé depuis ce criblage" in n for n in comparaison["notes"])
    # Les marques portent sur ce qui est affiché : les deux dates concordent
    assert _ligne(comparaison, "dob")["verdict"] == "CONCORDANCE"


def test_un_identifiant_absent_d_un_cote_n_est_pas_un_ecart(db, api, monkeypatch):
    _referentiel(db, client_passport_documents=[{"number": "12AB34567", "issuing_country": "FR"}])
    _liste_en_production(db, monkeypatch,
                         passport_documents=[{"number": "99ZZ00011", "issuing_country": "FR"}])
    alerte = api.post("/api/clients/CLI-000390/screen").json()["alert_id"]
    comparaison = api.get(f"/api/alerts/{alerte}").json()["comparison"]
    passeport = [l for l in comparaison["lignes"] if l["libelle"] == "Passeport"]
    assert len(passeport) == 1 and passeport[0]["verdict"] == "ECART"
    # Le LEI n'est renseigné nulle part : pas de ligne qui crierait au loup
    assert not [l for l in comparaison["lignes"] if l["libelle"] == "LEI"]


def test_les_lignes_sont_celles_que_le_moteur_compare(monkeypatch):
    """Rien n'est recopié : un champ que le moteur apprend à comparer devient
    une ligne sans qu'on touche au tableau."""
    from fiskr import identification, scoring

    familles = {c.famille: c for c in identification.comparaisons_du_moteur()}
    assert set(familles) == {"names", "dob", "gender", "geography", "identifiers"}
    assert "client_dob" in familles["dob"].client and familles["dob"].fiche == ("dates_of_birth",)
    assert (("client_passport_documents",), ("passport_documents",)) in familles["identifiers"].paires
    assert "client_countries" in familles["geography"].client

    origine = scoring.match_entities

    def moteur_qui_compare_un_champ_de_plus(client, fiche, config):
        client.get("client_champ_appris")
        fiche.get("champ_appris")
        return origine(client, fiche, config)

    identification.comparaisons_du_moteur.cache_clear()
    monkeypatch.setattr(scoring, "match_entities", moteur_qui_compare_un_champ_de_plus)
    try:
        paires = [p for c in identification.comparaisons_du_moteur() for p in c.paires]
        assert (("client_champ_appris",), ("champ_appris",)) in paires
    finally:
        monkeypatch.undo()
        identification.comparaisons_du_moteur.cache_clear()


def test_chaque_champ_affiche_porte_un_libelle():
    """Un champ comparé sans libellé s'afficherait brut (« client_tax_id »)."""
    from fiskr.comparaison import comparer, champs_affiches
    from fiskr.config import config

    pleins = {}
    for champ in champs_affiches():
        if champ in ("client_countries",):
            pleins[champ] = {"nationality": ["FR"], "residence": ["FR"],
                             "birth_country": ["FR"], "registration_country": ["FR"]}
        elif champ == "countries":
            pleins[champ] = {"citizenship": ["FR"], "residence": ["FR"],
                             "birth_country": ["FR"], "jurisdiction_country": ["FR"]}
        elif champ == "aliases":
            pleins[champ] = {"high_priority": ["A B"], "low_priority": ["C"]}
        elif champ == "individual_name_parsed":
            pleins[champ] = {"first_name": "A", "last_name": "B", "maiden_name": "C"}
        elif champ.endswith(("_documents", "_ids")):
            pleins[champ] = [{"number": "X1", "issuing_country": "FR"}]
        else:
            pleins[champ] = "M" if "gender" in champ else "X"
    tableau = comparer({"client_type": "PP", **pleins}, dict(pleins), dict(config), "SCREENING")
    entrees = [e for l in tableau["lignes"] for e in l["client"] + l["fiche"]] + tableau["contexte"]
    bruts = [e["champ"] for e in entrees if e["libelle"] == e["champ"].split(".")[-1]]
    assert entrees and not bruts, bruts


# ------------------ 2. DES MOTIFS PROPRES À CHAQUE DÉCISION ------------------

def _alerte_ouverte(db, api, monkeypatch):
    _referentiel(db)
    _liste_en_production(db, monkeypatch)
    return api.post("/api/clients/CLI-000390/screen").json()["alert_id"]


def test_le_serveur_refuse_un_faux_positif_motive_par_un_motif_de_vrai_positif(db, api, monkeypatch):
    alerte = _alerte_ouverte(db, api, monkeypatch)
    refus = api.post(f"/api/alerts/{alerte}/propose", json={
        "decision": "FALSE_POSITIVE", "comment": f"Vu. {MOTIF_VRAI_POSITIF}"})
    assert refus.status_code == 400, refus.text
    assert "vrai positif" in refus.json()["detail"]
    db.expire_all()
    assert db.get(Alert, alerte).status == "OPEN"
    assert db.get(Alert, alerte).proposed_decision is None

    # Et l'inverse : l'homonymie ne motive pas une confirmation
    assert api.post(f"/api/alerts/{alerte}/propose", json={
        "decision": "CONFIRMED", "comment": MOTIF_HOMONYMIE}).status_code == 400
    # Un motif valable pour les deux décisions ne bloque rien, et le bon motif
    # passe — le contrôle ne refuse que la contradiction
    set_setting(db, SETTING_ALERT_CLOSE_REASONS, [
        {"motif": "Vu avec le chargé de clientèle.", "decisions": ["FALSE_POSITIVE", "CONFIRMED"]},
        {"motif": MOTIF_HOMONYMIE, "decisions": ["FALSE_POSITIVE"]},
        {"motif": MOTIF_VRAI_POSITIF, "decisions": ["CONFIRMED"]}])
    assert api.post(f"/api/alerts/{alerte}/propose", json={
        "decision": "FALSE_POSITIVE",
        "comment": f"Vu avec le chargé de clientèle. {MOTIF_HOMONYMIE}"}).status_code == 200


def test_chaque_motif_porte_sa_decision_et_les_anciens_sont_classes_sans_deviner(db):
    par_motif = {m["motif"]: m["decisions"] for m in DEFAULT_ALERT_CLOSE_REASONS}
    assert par_motif[MOTIF_VRAI_POSITIF] == ["CONFIRMED"]
    assert par_motif[MOTIF_HOMONYMIE] == ["FALSE_POSITIVE"]

    # Une bibliothèque enregistrée avant ce classement : de simples chaînes
    set_setting(db, SETTING_ALERT_CLOSE_REASONS, [MOTIF_HOMONYMIE, "Vu avec le chargé de clientèle."])
    lus = {m["motif"]: m for m in alert_close_reasons(db)}
    assert lus[MOTIF_HOMONYMIE] == {"motif": MOTIF_HOMONYMIE, "decisions": ["FALSE_POSITIVE"]}
    hérité = lus["Vu avec le chargé de clientèle."]
    assert hérité["decisions"] == ["FALSE_POSITIVE", "CONFIRMED"] and hérité["a_classer"] is True


def test_le_valideur_ne_clot_pas_une_proposition_motivee_par_l_autre_decision(db, api, monkeypatch, qui):
    """Une proposition écrite avant ce contrôle (ou par un chemin qui l'aurait
    contourné) ne se valide pas : le valideur ne peut pas la récrire, il la
    renvoie."""
    alerte = _alerte_ouverte(db, api, monkeypatch)
    ligne = db.get(Alert, alerte)
    ligne.status, ligne.proposed_decision = "PENDING_VALIDATION", "FALSE_POSITIVE"
    ligne.proposed_by, ligne.proposal_comment = ANALYSTE["username"], MOTIF_VRAI_POSITIF
    db.commit()
    qui["courant"] = REVISEUR
    refus = api.post(f"/api/alerts/{alerte}/validate", json={"approve": True})
    assert refus.status_code == 409, refus.text
    assert "Renvoyez" in refus.json()["detail"]


def test_le_dialogue_ne_propose_que_les_motifs_de_sa_decision():
    with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as f:
        app_js = f.read()
    debut = app_js.index("async function proposeAlertDecision(decision)")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "motifsDeLaDecision(await chargerMotifsDeCloture(), decision)" in corps


# ------------------ 3. MÉMORISER UN FAUX POSITIF À SA VALIDATION ------------------

def _proposer_puis_valider(db, api, monkeypatch, qui, **validation):
    set_setting(db, SETTING_ALERT_FOUR_EYES, True)
    alerte = _alerte_ouverte(db, api, monkeypatch)
    qui["courant"] = ANALYSTE
    assert api.post(f"/api/alerts/{alerte}/propose", json={
        "decision": "FALSE_POSITIVE", "comment": MOTIF_HOMONYMIE}).status_code == 200
    qui["courant"] = REVISEUR
    return alerte, api.post(f"/api/alerts/{alerte}/validate", json={"approve": True, **validation})


def test_valider_un_faux_positif_case_cochee_pose_la_paire_et_le_recriblage_n_ouvre_rien(
        db, api, monkeypatch, qui):
    """Le constat, rejoué : validée sans la case, l'alerte n° 20 laisse le
    re-criblage rouvrir la même paire (n° 28). Validée avec, plus rien ne se
    rouvre."""
    vingt, validation = _proposer_puis_valider(db, api, monkeypatch, qui)
    assert validation.status_code == 200
    assert db.query(WhitelistPair).count() == 0
    vingt_huit = api.post("/api/clients/CLI-000390/screen").json()["alert_id"]
    assert vingt_huit not in (None, vingt)

    qui["courant"] = ANALYSTE
    assert api.post(f"/api/alerts/{vingt_huit}/propose", json={
        "decision": "FALSE_POSITIVE", "comment": MOTIF_HOMONYMIE}).status_code == 200
    qui["courant"] = REVISEUR
    validation = api.post(f"/api/alerts/{vingt_huit}/validate",
                          json={"approve": True, "whitelist": {}})
    assert validation.status_code == 200, validation.text
    assert validation.json()["status"] == "CLOSED_FALSE_POSITIVE"

    paire = db.query(WhitelistPair).one()
    assert (paire.client_id, paire.watchlist_entity_id) == ("CLI-000390", "EU-0022")
    # Le flux de pose existant : justification (la motivation) et empreintes
    assert paire.justification == MOTIF_HOMONYMIE
    assert paire.entity_checksum and paire.client_fingerprint
    assert paire.created_by == REVISEUR["username"]
    assert "WHITELISTED" in [e.action for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == vingt_huit)]

    # « Recribler ce client », dix minutes plus tard : rien ne se rouvre
    recriblage = api.post("/api/clients/CLI-000390/screen").json()
    assert recriblage["whitelisted"] is True
    assert recriblage["alert_id"] is None
    assert db.query(Alert).count() == 2


def test_une_pose_refusee_ne_clot_pas_l_alerte(db, api, monkeypatch, qui):
    """Pièce exigée par le réglage : le JSON de la validation ne la porte pas.
    Refus franc, et l'alerte reste à valider — pas un faux positif clos que le
    prochain criblage rouvrirait."""
    set_setting(db, SETTING_WHITELIST_FILE_REQUIRED, True)
    alerte, validation = _proposer_puis_valider(db, api, monkeypatch, qui, whitelist={})
    assert validation.status_code == 400, validation.text
    db.expire_all()
    assert db.get(Alert, alerte).status == "PENDING_VALIDATION"
    assert db.query(WhitelistPair).count() == 0


def test_la_liste_blanche_ne_memorise_pas_un_vrai_positif(db, api, monkeypatch, qui):
    set_setting(db, SETTING_ALERT_FOUR_EYES, False)
    alerte = _alerte_ouverte(db, api, monkeypatch)
    qui["courant"] = REVISEUR
    refus = api.post(f"/api/alerts/{alerte}/propose", json={
        "decision": "CONFIRMED", "comment": MOTIF_VRAI_POSITIF, "whitelist": {}})
    assert refus.status_code == 400
    db.expire_all()
    assert db.get(Alert, alerte).status == "OPEN"


def test_quatre_yeux_coupe_la_cloture_directe_pose_aussi_la_paire(db, api, monkeypatch, qui):
    """En production le 4-yeux est coupé : la proposition clôt. C'est alors elle
    qui valide, et la case vaut là aussi — pour un réviseur."""
    set_setting(db, SETTING_ALERT_FOUR_EYES, False)
    alerte = _alerte_ouverte(db, api, monkeypatch)
    qui["courant"] = ANALYSTE
    assert api.post(f"/api/alerts/{alerte}/propose", json={
        "decision": "FALSE_POSITIVE", "comment": MOTIF_HOMONYMIE,
        "whitelist": {}}).status_code == 403
    qui["courant"] = REVISEUR
    reponse = api.post(f"/api/alerts/{alerte}/propose", json={
        "decision": "FALSE_POSITIVE", "comment": MOTIF_HOMONYMIE, "whitelist": {}})
    assert reponse.status_code == 200, reponse.text
    assert db.query(WhitelistPair).count() == 1


# ------------------ 5. UN IDENTIFIANT CLIENT STABLE EN CRIBLAGE MANUEL ------------------

PROFIL_MANUEL = {"client_type": "PP", "client_first_name": "Pierre", "client_last_name": "Dubois",
                 "client_dob": "1982-02-03", "client_countries": {"nationality": ["FR"]}}


def test_deux_criblages_manuels_identiques_sans_identifiant_retombent_sur_la_meme_alerte(
        db, api, monkeypatch):
    _liste_en_production(db, monkeypatch)
    premier = api.post("/api/screen", json=PROFIL_MANUEL).json()
    # Même personne, saisie autrement (casse, blancs)
    second = api.post("/api/screen", json={**PROFIL_MANUEL, "client_first_name": " pierre ",
                                           "client_last_name": "DUBOIS"}).json()
    # Le défaut d'abord : sans identifiant, chaque saisie ouvrait sa propre alerte
    assert db.query(Alert).count() == 1
    assert second["hits"]["redetected"] == 1 and second["hits"]["opened"] == 0
    assert second["alert_id"] == premier["alert_id"]
    # Et l'écran peut dire que l'identifiant est calculé
    assert premier["client_id_calcule"] is True
    assert premier["client_id"].startswith("CALC-")
    assert second["client_id"] == premier["client_id"]


def test_un_identifiant_fourni_est_garde_tel_quel(db, api, monkeypatch):
    _liste_en_production(db, monkeypatch)
    reponse = api.post("/api/screen", json={**PROFIL_MANUEL, "client_id": "CLI-000390"}).json()
    assert reponse["client_id"] == "CLI-000390" and reponse["client_id_calcule"] is False


def test_l_identifiant_calcule_distingue_naissance_et_type():
    from fiskr.identification import identifiant_calcule
    base = identifiant_calcule(PROFIL_MANUEL)
    assert identifiant_calcule({**PROFIL_MANUEL, "client_dob": "1969-07-14"}) != base
    assert identifiant_calcule({"client_type": "PM", "client_company_name": "Pierre Dubois"}) != base
    assert identifiant_calcule({**PROFIL_MANUEL, "client_first_name": "Pïerre"}) == base


def test_le_formulaire_ne_tire_plus_d_identifiant_au_hasard():
    with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as f:
        app_js = f.read()
    debut = app_js.index("async function handleScreening(event)")
    corps = app_js[debut:app_js.index("\nfunction renderScreeningResult", debut)]
    assert "Math.random" not in corps and "SCREEN-" not in corps
    assert re.search(r'getElementById\("client-id"\)', corps)

"""
Ce qui change l'univers ou le moteur atteint tous ceux qui en dépendent.

Cinq défauts, chacun reproduit avant d'être corrigé :

1. **Une fiche listée corrigée ou ajoutée à la main ne re-criblait personne.**
   Le re-criblage ne partait que d'une mise en production de lot. Reproduit :
   une fiche corrigée pour porter la nationalité d'un client, un homonyme
   ajouté à la main — aucune alerte sans lookback, et la couverture « durée
   inconnue » jusqu'à ce que quelqu'un en lance un. Le modèle du PATCH
   ignorait de surcroît les documents d'identité (« Aucune modification »), et
   un lookback qui rencontrait ensuite la fiche corrigée finissait en ÉCHEC
   (`modified_at`, une date que le journal d'audit refusait brute).
2. **Les équivalences de noms ne se rechargeaient que dans le processus qui les
   changeait** : un processus B dont le seul lien avec A est la base relisait
   son cache à l'époque suivante et scorait sans l'équivalence approuvée ; et
   l'auto-approbation de la fouille d'homonymes n'incrémentait pas l'époque.
3. **La table de rareté ne suivait pas le réglage des capacités** : elle compte
   les mots sous la forme que compare le moteur, que la translittération
   change. Le démon gardait la table d'avant la coupure — et ne relisait même
   pas ses capacités avant de re-cribler.
4. **Le suivi d'une approbation, remis en file après un arrêt, notifiait deux
   fois** (production : 560 notifications pour 251 dossiers).
5. **Le lookback ne rendait caduque aucune alerte** dont le couple ne franchit
   plus le seuil.

Base jetable par test : la couverture et les alertes se comptent sur TOUT le
référentiel, et les travaux (mode eager) ouvrent leur propre session — d'où la
fabrique de sessions redirigée vers la base du test.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fiskr.api as api_module
from fiskr import capabilities as caps
from fiskr import database, jobs, parametrage, rarete, resources, univers
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.config import config
from fiskr.database import (Alert, AlertEvent, AuditTrail, Base, ClientEntity, Job,
                            LearnedEquivalence, NotificationDelivery, ReviewRecord,
                            ScreeningPass, Snapshot, WatchlistEntity, compute_checksum, get_db)
from fiskr.scoring import match_entities
from fiskr.settings import (SETTING_AUTO_RESCREEN, SETTING_ENGINE_CAPABILITIES,
                            bump_watchlist_epoch, set_setting, watchlist_epoch)

REVISEUR = {"id": 1, "username": "reviseur-propagation", "full_name": "Réviseur",
            "role": "admin", "roles": ["admin"]}
PASSEPORT = [{"number": "77PA12345", "issuing_country": "RU"}]


def _pays(*nationalites):
    return {"nationality": list(nationalites), "residence": [], "birth_country": [],
            "registration_country": []}


def _citoyennete(*pays):
    return {"citizenship": list(pays), "residence": [], "birth_country": [],
            "jurisdiction_country": []}


# ------------------------------------------------------------ base isolée

@pytest.fixture
def db(tmp_path, monkeypatch):
    """Base jetable, vue par l'API, par les travaux et par la lecture des
    réglages (capacités, équivalences) qui ouvrent leur propre session. Le
    cache et le paramétrage de processus sont rendus à la base partagée."""
    engine = create_engine(f"sqlite:///{tmp_path / 'propagation.sqlite3'}")
    Base.metadata.create_all(engine)
    fabrique = sessionmaker(bind=engine)
    session = fabrique()
    session.fabrique = fabrique
    monkeypatch.setattr(database, "SessionLocal", fabrique)
    monkeypatch.setattr(rarete, "_TABLE", rarete._TABLE)
    resources.set_index(None)
    caps.invalidate_context()
    app.dependency_overrides[get_db] = lambda: session
    app.dependency_overrides[get_current_user] = lambda: REVISEUR
    yield session
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    session.close()
    monkeypatch.undo()
    # Le cache de criblage, le paramétrage et la référence d'univers de CE
    # processus décrivent maintenant la base jetable : ils sont relus sur la
    # base partagée, comme les autres modules l'attendent.
    resources.set_index(None)
    partagee = database.SessionLocal()
    try:
        api_module.load_watchlist_cache(partagee)
        api_module._last_epoch_seen = watchlist_epoch(partagee)
    finally:
        partagee.close()


@pytest.fixture
def api(db):
    return TestClient(app)


def _lot(db, snapshot_id, fiches, file_type="WATCHLIST_DGT"):
    db.add(Snapshot(snapshot_id=snapshot_id, file_type=file_type,
                    file_name=f"{snapshot_id}.json", file_hash=uuid.uuid4().hex,
                    record_count=len(fiches), status="READY", uploaded_at=datetime.utcnow()))
    for fiche in fiches:
        db.add(WatchlistEntity(snapshot_id=snapshot_id,
                               entity_checksum=compute_checksum(fiche), **fiche))
    db.commit()
    bump_watchlist_epoch(db)


def _fiche(entity_id, prenom, nom, dob, pays):
    return dict(entity_id=entity_id, entity_type="I", primary_name=f"{prenom} {nom}",
                individual_name_parsed={"first_name": prenom, "last_name": nom,
                                        "maiden_name": ""},
                aliases={"high_priority": [], "low_priority": []}, dates_of_birth=[dob],
                is_deceased=False, gender="M", countries=_citoyennete(pays))


def _referentiel(db, clients):
    db.add(Snapshot(snapshot_id="clients-1", file_type="CLIENT_BASE", file_name="clients.csv",
                    file_hash=uuid.uuid4().hex, record_count=len(clients), status="READY",
                    uploaded_at=datetime.utcnow()))
    for client in clients:
        db.add(ClientEntity(snapshot_id="clients-1", entity_checksum=compute_checksum(client),
                            **client))
    db.commit()


IGOR = dict(client_id="CUST-777", client_type="PP", client_first_name="IGOR",
            client_last_name="PETROV", client_dob="1965-03-12", client_gender="M",
            client_passport_documents=PASSEPORT, client_countries=_pays("RU"))
TARIQ = dict(client_id="CUST-888", client_type="PP", client_first_name="Tariq",
             client_last_name="AZIZ", client_dob="1960-01-01", client_gender="M",
             client_countries=_pays("IQ"))


def _couvert(db):
    """Un lookback complet : le référentiel est couvert contre l'univers."""
    from fiskr.rescreen import rescreen_lookback
    return rescreen_lookback(db)


def _pk(db, entity_id):
    return db.query(WatchlistEntity.id).filter(WatchlistEntity.entity_id == entity_id).scalar()


def _alertes(db):
    db.expire_all()
    return sorted((a.client_id, a.watchlist_entity_id) for a in db.query(Alert).all())


# ------------------------------------------------ 1. correction et ajout manuels

def test_corriger_une_fiche_pour_y_porter_le_passeport_d_un_client_ouvre_l_alerte_sans_lookback(db, api):
    """« Ivan PETROV » et le client « Igor PETROV » se rencontrent au blocking
    mais restent sous le seuil (74,3). La DGT publie le passeport : la fiche
    corrigée porte celui du client, correspondance certaine. Le PATCH ignorait
    le champ, et rien ne re-criblait de toute façon : zéro alerte jusqu'au
    prochain lookback, couverture « durée inconnue »."""
    from fiskr.couverture import couverture_du_criblage

    _lot(db, "dgt-v1", [_fiche("DGT-2", "Ivan", "PETROV", "1931-11-02", "RU")])
    _referentiel(db, [IGOR, TARIQ])
    assert _couvert(db)["new_alerts"] == 0

    reponse = api.patch(f"/api/watchlist/entity/{_pk(db, 'DGT-2')}",
                        json={"passport_documents": PASSEPORT})
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["changed_fields"] == ["passport_documents"]
    assert reponse.json()["rescreen"]["submitted"] is True
    assert reponse.json()["rescreen"]["status"] == "DONE", reponse.json()["rescreen"]

    assert _alertes(db) == [("CUST-777", "DGT-2")]
    passage = db.query(ScreeningPass).order_by(ScreeningPass.id.desc()).first()
    assert passage.nature == "MISE_A_JOUR_LISTE" and passage.status == "TERMINE"
    assert passage.scope["geste"] == "correction" and passage.entities_compared == 1
    # La couverture redevient exacte à la fin du passage : les deux clients,
    # couverts contre l'univers d'avant, le sont contre celui d'après.
    mesure = couverture_du_criblage(db)
    assert mesure["non_couverts"] == 0 and mesure["attente_inconnue"] is False, mesure


def test_ajouter_a_la_main_l_homonyme_d_un_client_ouvre_l_alerte(db, api):
    _lot(db, "dgt-v1", [_fiche("DGT-2", "Ivan", "PETROV", "1931-11-02", "RU")])
    _referentiel(db, [IGOR, TARIQ])
    _couvert(db)

    reponse = api.post("/api/watchlist/entity", json={
        "entity_type": "I", "primary_name": "Tariq AZIZ", "first_name": "Tariq",
        "last_name": "AZIZ", "dates_of_birth": "1960-01-01", "nationality": "IQ",
        "list_type": "WATCHLIST_DGT"})
    assert reponse.status_code == 200, reponse.text
    assert _alertes(db) == [("CUST-888", reponse.json()["entity_id"])]
    assert reponse.json()["rescreen"]["status"] == "DONE", reponse.json()["rescreen"]
    passage = db.query(ScreeningPass).order_by(ScreeningPass.id.desc()).first()
    assert passage.scope["geste"] == "ajout"


def test_une_correction_qui_fait_passer_le_couple_sous_le_seuil_rend_l_alerte_caduque(db, api):
    """Même chemin que le re-criblage post-delta, caducité comprise : l'alerte
    porte le score de la fiche corrigée et devient caduque — elle n'est pas
    close, un humain la clôt."""
    _lot(db, "dgt-v1", [_fiche("DGT-2", "Igor", "PETROV", "1965-03-12", "RU")])
    _referentiel(db, [IGOR])
    assert _couvert(db)["new_alerts"] == 1

    reponse = api.patch(f"/api/watchlist/entity/{_pk(db, 'DGT-2')}", json={
        "primary_name": "Igor PETROVSKIKH-ZAVADOVSKY", "dates_of_birth": ["1931-11-02"],
        "individual_name_parsed": {"first_name": "Igor", "last_name": "PETROVSKIKH-ZAVADOVSKY",
                                   "maiden_name": ""}})
    assert reponse.status_code == 200, reponse.text

    db.expire_all()
    alerte = db.query(Alert).one()
    assert alerte.status == "OPEN"
    assert alerte.lapsed_since is not None and "sous le seuil" in alerte.lapsed_reason
    assert alerte.latest_score < 75
    assert "LAPSED" in [e.action for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == alerte.id)]
    assert reponse.json()["rescreen"]["status"] == "DONE"


def test_un_lookback_apres_une_correction_ne_finit_plus_en_echec(db, api):
    """La fiche corrigée porte `modified_at` : le re-criblage la copiait telle
    quelle, et la ligne d'audit de sa correspondance — une date dans l'arbre
    de décision — était refusée. Le lookback finissait en ÉCHEC."""
    from fiskr.rescreen import rescreen_lookback

    set_setting(db, SETTING_AUTO_RESCREEN, False, updated_by="test")
    _lot(db, "dgt-v1", [_fiche("DGT-2", "Igor", "PETROV", "1965-03-12", "UA")])
    _referentiel(db, [IGOR])
    reponse = api.patch(f"/api/watchlist/entity/{_pk(db, 'DGT-2')}",
                        json={"countries": _citoyennete("RU")})
    assert reponse.status_code == 200

    resultat = rescreen_lookback(db)
    assert resultat["new_alerts"] == 1
    passage = db.get(ScreeningPass, resultat["screening_pass_id"])
    assert passage.status == "TERMINE"
    ligne = db.query(AuditTrail).filter(AuditTrail.watchlist_id == "DGT-2").one()
    assert ligne.decision_tree["watchlist_entity"]["modified_at"].startswith(
        str(datetime.utcnow().year))
    # Réglage coupé : la correction n'a re-criblé personne, et la réponse le dit
    assert reponse.json()["rescreen"]["submitted"] is False


# ------------------------------------------------ 5. le lookback et la caducité

def test_un_lookback_rend_caduque_l_alerte_dont_le_couple_ne_franchit_plus_le_seuil(db, api):
    from fiskr.rescreen import rescreen_lookback

    set_setting(db, SETTING_AUTO_RESCREEN, False, updated_by="test")
    _lot(db, "dgt-v1", [_fiche("DGT-2", "Igor", "PETROV", "1965-03-12", "RU")])
    _referentiel(db, [IGOR])
    assert rescreen_lookback(db)["new_alerts"] == 1
    api.patch(f"/api/watchlist/entity/{_pk(db, 'DGT-2')}", json={
        "primary_name": "Igor PETROVSKIKH-ZAVADOVSKY", "dates_of_birth": ["1931-11-02"],
        "individual_name_parsed": {"first_name": "Igor", "last_name": "PETROVSKIKH-ZAVADOVSKY",
                                   "maiden_name": ""}})
    db.expire_all()
    assert db.query(Alert).one().lapsed_since is None   # personne n'a re-criblé

    resultat = rescreen_lookback(db)
    assert resultat["lapsed_alerts"] == 1
    db.expire_all()
    alerte = db.query(Alert).one()
    assert alerte.lapsed_since is not None and alerte.status == "OPEN"
    assert "LAPSED" in [e.action for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == alerte.id)]


# ------------------------------------------------ 2. les équivalences, par l'époque

CLIENT_ZEB = {"client_id": "C-Z", "client_type": "PP", "client_first_name": "Zebulonix",
              "client_last_name": "SMITHERSONOV"}
FICHE_BAR = {"entity_id": "X-1", "entity_type": "I", "primary_name": "Bartholomeux SMITHERSONOV",
             "individual_name_parsed": {"first_name": "Bartholomeux",
                                        "last_name": "SMITHERSONOV"}}


def test_une_equivalence_approuvee_dans_un_processus_atteint_les_autres_par_l_epoque(db):
    """Processus B simulé : son seul lien avec A est la base. A approuve
    ZEBULONIX ≡ BARTHOLOMEUX (POST /api/resources/learned/{id}/decide : la
    décision en base, l'époque incrémentée). B relit son cache à l'époque
    suivante — et scorait toujours sans l'équivalence."""
    _lot(db, "dgt-v1", [_fiche("DGT-1", "Oleg", "SOKOLOV", "1970-01-01", "RU")])
    api_module._ensure_watchlist_cache(db)
    assert resources.get_index().canonical("Zebulonix", resources.FIELD_GIVEN_NAME) is None
    assert match_entities(CLIENT_ZEB, FICHE_BAR, config)["status"] == "NO_MATCH"

    db.add(LearnedEquivalence(field="given_name", class_id="BARTHOLOMEUX",
                              term_a="BARTHOLOMEUX", term_b="ZEBULONIX",
                              signature="given_name|BARTHOLOMEUX|ZEBULONIX", source="ANALYST",
                              status="APPROVED", occurrences=3, similarity=0.5,
                              confidence=0.9, discovered_at=datetime.utcnow()))
    db.commit()
    bump_watchlist_epoch(db)

    api_module._ensure_watchlist_cache(db)
    assert resources.get_index().canonical("Zebulonix", resources.FIELD_GIVEN_NAME) == "BARTHOLOMEUX"
    assert match_entities(CLIENT_ZEB, FICHE_BAR, config)["status"] == "ALERT"


def test_l_auto_approbation_de_la_fouille_incremente_l_epoque(db):
    """La fouille tourne dans le démon : elle rechargeait son propre index,
    et rien ne prévenait les processus web."""
    fiches = []
    for i in range(5):
        fiche = _fiche(f"DGT-{i}", "Vladislavo", f"KRUTOV{i}", "1970-01-01", "RU")
        fiche["aliases"] = {"high_priority": [f"Vladislavio KRUTOV{i}"], "low_priority": []}
        fiches.append(fiche)
    _lot(db, "dgt-v1", fiches)
    avant = watchlist_epoch(db)
    rapport = api_module.run_resource_mining(db)
    assert rapport["auto_approved"] == 1
    assert watchlist_epoch(db) == avant + 1


# ------------------------------------------------ 3. la rareté et les capacités

def _corpus_vladimir(db):
    """15 fiches écrivent VLADIMIR en latin, 15 autres seulement en cyrillique
    (alias d'origine des listes UE et ONU) : translittération active, le mot
    est porté par 30 fiches — répandu (seuil 25) ; coupée, par 15."""
    fiches = []
    for i in range(30):
        nom = "Vladimir" if i < 15 else "Владимир"
        fiches.append(dict(entity_id=f"EU-{i}", entity_type="I",
                           primary_name=f"{nom} Famille{chr(65 + i % 26)}{i}",
                           individual_name_parsed={}, aliases={"high_priority": [], "low_priority": []},
                           dates_of_birth=[], is_deceased=False, gender="M", countries={}))
    _lot(db, "eu-v1", fiches, file_type="WATCHLIST_EU")


def _couper_la_translitteration(db):
    """PUT /api/settings/engine dans un AUTRE processus : le réglage en base,
    l'époque incrémentée. Rien d'autre ne parvient au processus courant."""
    set_setting(db, SETTING_ENGINE_CAPABILITIES, {"SCREENING": {caps.CAP_TRANSLIT: False}},
                updated_by="test")
    bump_watchlist_epoch(db)


def test_la_table_de_rarete_suit_le_reglage_des_capacites(db):
    _corpus_vladimir(db)
    avant = rarete.table_pour(db)
    assert avant.df("VLADIMIR") == 30 and avant.repandu("VLADIMIR")

    # Une mesure d'impact sous surcharge ne reconstruit pas la table du
    # processus : les criblages servis en parallèle la lisent.
    sans_translitteration = caps.current_context() - {caps.CAP_TRANSLIT}
    with caps.use_context(caps.CHANNEL_SCREENING, sans_translitteration):
        assert rarete.table_pour(db) is avant

    _couper_la_translitteration(db)
    caps.invalidate_context()          # le réglage relu, comme à l'époque
    apres = rarete.table_pour(db)
    assert apres is not avant
    assert apres.df("VLADIMIR") == 15 and not apres.repandu("VLADIMIR")
    # Ce que lit une règle anti-faux positifs fondée sur la rareté
    assert not apres.profil("Vladimir Dupont", "Vladimir FamilleA0")["nom_repandu"]


def test_le_demon_relit_ses_capacites_a_l_epoque_avant_de_recribler(db, monkeypatch):
    """Le démon ne recharge pas le cache des listes : son re-criblage gardait
    les capacités lues au premier passage, et la table de rareté avec."""
    from fiskr.rescreen import rescreen_lookback

    monkeypatch.setattr(jobs, "IN_WORKER", True)
    _corpus_vladimir(db)
    _referentiel(db, [dict(client_id="C-1", client_type="PP", client_first_name="Paul",
                           client_last_name="NEUTRE")])
    rescreen_lookback(db)
    assert caps.is_active(caps.CAP_TRANSLIT)
    assert rarete.table_courante().df("VLADIMIR") == 30

    _couper_la_translitteration(db)
    rescreen_lookback(db)
    assert not caps.is_active(caps.CAP_TRANSLIT)
    assert rarete.table_courante().df("VLADIMIR") == 15


# ------------------------------------------------ 4. une approbation, une notification

def _approuver(db, api):
    set_setting(db, "ingestion.require_approval", True, updated_by="test")
    db.add(Snapshot(snapshot_id="eu-cand", file_type="WATCHLIST_EU", file_name="eu.xml",
                    file_hash=uuid.uuid4().hex, record_count=1, status="PENDING_REVIEW",
                    uploaded_at=datetime.utcnow()))
    fiche = _fiche("EU-1", "Boris", "NOTIFOV", "1960-01-01", "RU")
    db.add(WatchlistEntity(snapshot_id="eu-cand", entity_checksum=compute_checksum(fiche), **fiche))
    db.commit()
    reponse = api.post("/api/review/snapshots/eu-cand/approve", json={"comment": "ok"})
    assert reponse.status_code == 202, reponse.text
    db.expire_all()
    return db.query(Job).filter(Job.kind == "approve").one()


def _notifications(db):
    db.expire_all()
    return db.query(NotificationDelivery).filter(
        NotificationDelivery.event_key == "snapshot_approved").count()


def _repris_apres_un_arret(db, job):
    """Le démon est tué pendant le travail : la ligne reste RUNNING, battement
    de cœur périmé ; la reprise la remet en file, le démon la reprend."""
    job = db.get(Job, job.id)
    job.status, job.finished_at = "RUNNING", None
    job.attempts = min(job.attempts or 1, job.max_attempts - 1)
    job.heartbeat_at = datetime.utcnow() - timedelta(minutes=10)
    db.commit()
    assert jobs.requeue_stale(db, worker_present=True) == 1
    db.expire_all()
    job = db.get(Job, job.id)
    job.not_before = None
    db.commit()
    jobs.run_job(job.id)
    db.expire_all()
    return db.get(Job, job.id)


def test_le_suivi_d_approbation_execute_deux_fois_ne_notifie_qu_une_fois(db, api):
    job = _approuver(db, api)
    assert job.status == "DONE" and _notifications(db) == 1
    assert _repris_apres_un_arret(db, job).status == "DONE"
    assert db.query(ReviewRecord).count() == 1
    assert _notifications(db) == 1
    assert db.get(Snapshot, "eu-cand").approval_notified_at is not None


def test_un_premier_passage_en_echec_avant_d_emettre_ne_perd_pas_la_notification(db, api, monkeypatch):
    """Le premier passage tombe pendant le re-criblage, avant d'émettre :
    rien n'est marqué, le suivant notifie — et un troisième ne renotifie pas."""
    import fiskr.rescreen as rescreen_module
    vrai = rescreen_module.rescreen_after_snapshot_change
    appels = []

    def tombe_la_premiere_fois(*args, **kwargs):
        appels.append(1)
        if len(appels) == 1:
            raise RuntimeError("démon tué pendant le re-criblage")
        return vrai(*args, **kwargs)

    monkeypatch.setattr(rescreen_module, "rescreen_after_snapshot_change", tombe_la_premiere_fois)
    job = _approuver(db, api)
    assert job.status == "ERROR" and _notifications(db) == 0
    assert getattr(db.get(Snapshot, "eu-cand"), "approval_notified_at", None) is None

    assert _repris_apres_un_arret(db, job).status == "DONE"
    assert _notifications(db) == 1
    assert _repris_apres_un_arret(db, job).status == "DONE"
    assert _notifications(db) == 1

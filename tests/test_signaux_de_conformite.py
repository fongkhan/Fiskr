"""
Ce qui ne va pas se voit, même quand le courriel est en panne ; et ce que le
produit affiche est vrai.

Relevé de production en lecture seule, le 19 octobre 2026 :

* **47 jours sans homologation, et personne prévenu.** La notification de lot
  en attente était coupée, et rien d'autre ne portait le retard vers
  quelqu'un : la mesure existait, l'écran la montrait — à qui l'ouvrait ;
* les envois immédiats échouaient (`sync_error` : 0 réussite sur 335), et
  `/api/health` répondait « ok » : une sonde de vie ne dit rien de ce que vaut
  ce qu'on crible ;
* `kpi_digest` s'affichait « actif » et n'avait jamais rien envoyé — son envoi
  ne passait même pas par le journal ;
* une clé auditeur recevait 403 sur `GET /api/fprules`, alors que ce rôle
  promet la lecture intégrale ;
* 68 approbations unitaires manquaient au journal d'administration : seule
  l'homologation groupée y écrivait ;
* la liste UE comptait 320 fiches, toutes extraites du Journal Officiel — sa
  liste consolidée (eu_fsf) coupée faute de jeton — pendant que l'écran disait
  « 39 listes en production » ;
* deux affirmations sans vérification : une couverture en échec rendue comme
  « aucun référentiel », une fiche « OK » portant un avertissement.
"""
import inspect
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fiskr import api as api_mod
from fiskr import fraicheur
from fiskr import mise_en_service as mes
from fiskr.api import app
from fiskr.auth import get_current_user, hash_api_key, API_KEY_PREFIX_LEN
from fiskr.database import (AdminAuditLog, Alert, ApiKey, AuditTrail, Base,
                            NotificationDelivery, Snapshot, SyncReport, get_db)


# --------------------------------------------------------------------------
# Outillage : une base isolée. Une sonde de conformité porte sur TOUTE
# l'installation ; sur la base partagée des tests, ce que les autres y laissent
# déciderait du verdict, et le test parlerait d'eux.
# --------------------------------------------------------------------------

@pytest.fixture
def base(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'signaux.sqlite3'}")
    Base.metadata.create_all(engine)
    fabrique = sessionmaker(bind=engine)
    session = fabrique()
    yield session, fabrique
    session.close()
    engine.dispose()


@pytest.fixture
def client(base, monkeypatch):
    _, fabrique = base

    def _db():
        session = fabrique()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    # Le suivi d'une approbation (rechargement du cache, re-criblage) tourne
    # sur la base globale : hors sujet ici, et il n'y trouverait pas le lot.
    monkeypatch.setattr(api_mod, "_submit_job", lambda *a, **k: None)
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _lot(db, ftype, statut, il_y_a_heures, fiches=10, sid=None):
    sid = sid or f"sig-{uuid.uuid4().hex[:10]}"
    db.add(Snapshot(snapshot_id=sid, file_type=ftype, file_name=f"{sid}.csv",
                    file_hash=uuid.uuid4().hex, record_count=fiches, status=statut,
                    uploaded_at=datetime.utcnow() - timedelta(hours=il_y_a_heures)))
    db.commit()
    return sid


def _cle_api(db, role):
    complete = f"fsk_{uuid.uuid4().hex}"
    db.add(ApiKey(name=f"supervision-{role}", prefix=complete[:API_KEY_PREFIX_LEN],
                  key_hash=hash_api_key(complete), roles=role))
    db.commit()
    return complete


@pytest.fixture
def emissions(monkeypatch):
    """Les évènements émis, sans envoi réel."""
    vus = []
    import fiskr.notifier as notifier
    monkeypatch.setattr(notifier, "emit", lambda db, cle, charge=None, **k: vus.append((cle, charge)))
    return vus


@pytest.fixture
def secrets_poses(monkeypatch):
    """La base de tests garde les secrets du code source : sans cela, le
    contrôle « Secrets » (rejoué par la sonde) la mettrait en 503 à lui seul."""
    from fiskr import config as cfg
    monkeypatch.setattr(cfg, "INSECURE_DEFAULT_SECRET_KEY", False, raising=False)
    monkeypatch.setattr(cfg, "INSECURE_DEFAULT_ADMIN_PASSWORD", False, raising=False)


# --------------------------------------------------------------------------
# 1. Fraîcheur dépassée : un évènement immédiat, une fois par seuil franchi
# --------------------------------------------------------------------------

def test_franchir_le_seuil_attention_emet_un_evenement_et_un_seul(base, emissions):
    db, _ = base
    _lot(db, "WATCHLIST_UN", "READY", 24 * 10)
    _lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 50)

    premier = fraicheur.signaler_les_franchissements(db)
    assert [(s["type"], s["niveau"]) for s in premier] == [("WATCHLIST_UN", mes.ATTENTION)]
    assert [cle for cle, _ in emissions] == ["fraicheur_depassee"]
    assert emissions[0][1]["Liste"] == "WATCHLIST_UN"

    # Second passage, rien n'a changé : silence. Un signal répété à chaque
    # passage se filtre, et ne dit plus rien le jour où il compte.
    assert fraicheur.signaler_les_franchissements(db) == []
    assert len(emissions) == 1


def test_le_seuil_bloquant_se_signale_a_son_tour_puis_se_tait(base, emissions):
    db, _ = base
    _lot(db, "WATCHLIST_UN", "READY", 24 * 10)
    _lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 50)
    fraicheur.signaler_les_franchissements(db)
    plus_tard = datetime.utcnow() + timedelta(days=6)

    bloquant = fraicheur.signaler_les_franchissements(db, maintenant=plus_tard)
    assert [s["niveau"] for s in bloquant] == [mes.BLOQUANT]
    assert fraicheur.signaler_les_franchissements(db, maintenant=plus_tard) == []
    assert [c["Seuil franchi"].split(" ")[0] for _, c in emissions] == [mes.ATTENTION, mes.BLOQUANT]


def test_une_liste_qui_saute_au_bloquant_ne_se_signale_qu_une_fois(base, emissions):
    """Un démon arrêté une semaine retrouve une liste déjà au-delà des deux
    seuils : c'est UN franchissement, pas deux courriels."""
    db, _ = base
    _lot(db, "WATCHLIST_UN", "READY", 24 * 12)
    _lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 24 * 8)
    signales = fraicheur.signaler_les_franchissements(db)
    assert [s["niveau"] for s in signales] == [mes.BLOQUANT]
    assert len(emissions) == 1


def test_apres_homologation_le_retard_se_tait_et_se_resignale_s_il_revient(base, emissions):
    db, _ = base
    _lot(db, "WATCHLIST_UN", "READY", 24 * 10)
    attente = _lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 50)
    fraicheur.signaler_les_franchissements(db)

    lot = db.query(Snapshot).filter(Snapshot.snapshot_id == attente).one()
    lot.status = "READY"
    db.commit()
    # L'amélioration ne crie rien...
    assert fraicheur.signaler_les_franchissements(db) == []
    assert len(emissions) == 1
    # ...mais la mémoire est levée : un nouveau retard est un nouveau franchissement.
    _lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 49)
    assert [s["niveau"] for s in fraicheur.signaler_les_franchissements(db)] == [mes.ATTENTION]
    assert len(emissions) == 2


def test_les_seuils_de_l_evenement_sont_ceux_de_la_mise_en_service(base, emissions, monkeypatch):
    """Dérivés, pas recopiés : qu'on resserre le seuil de la mise en service,
    l'évènement le suit — sinon le courriel dirait ce que l'écran contredit."""
    db, _ = base
    monkeypatch.setattr(mes, "_RETARD_TOLERE_H", 10)
    _lot(db, "WATCHLIST_UN", "READY", 24 * 3)
    _lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 12)
    assert [s["niveau"] for s in fraicheur.signaler_les_franchissements(db)] == [mes.ATTENTION]
    assert mes._fraicheur_des_listes(db)["etat"] == mes.ATTENTION


def test_l_evenement_est_immediat_actif_et_vise_les_administrateurs():
    from fiskr.events import EVENT_CATALOG, IMMEDIATE
    evenement = EVENT_CATALOG["fraicheur_depassee"]
    assert evenement.urgency == IMMEDIATE
    assert evenement.default_enabled is True
    assert "admin" in evenement.audience


def test_le_tic_de_fraicheur_tourne_avec_ou_sans_demon():
    """Un seuil se franchit avec le temps : seul un tic périodique le voit au
    moment où il se produit. Il vit dans le démon, et dans l'API sans démon."""
    from fiskr import worker
    assert "_fraicheur_tick" in inspect.getsource(worker._start_schedulers)
    assert inspect.iscoroutinefunction(api_mod._fraicheur_scheduler)
    assert not inspect.iscoroutinefunction(api_mod._fraicheur_tick)


# --------------------------------------------------------------------------
# 2. Sonde de conformité : 503 quand un contrôle BLOQUANT le dit, sans courriel
# --------------------------------------------------------------------------

def test_la_sonde_rend_503_sur_une_production_vieille_de_8_jours_puis_200_apres_homologation(
        base, client, secrets_poses):
    db, _ = base
    auditeur = _cle_api(db, "auditor")
    _lot(db, "WATCHLIST_OFAC", "READY", 24 * 8)
    attente = _lot(db, "WATCHLIST_OFAC", "PENDING_REVIEW", 24 * 7 + 12)

    reponse = client.get("/api/health/conformite", headers={"X-API-Key": auditeur})
    assert reponse.status_code == 503, reponse.text
    corps = reponse.json()
    assert corps["status"] == "degraded"
    assert [b["cle"] for b in corps["bloquants"]] == ["fraicheur"]
    assert "OFAC" in corps["bloquants"][0]["constat"]
    # Pendant ce temps, la sonde de vie dit vrai sur ce qu'elle regarde — et
    # c'est tout ce qu'elle regarde. Elle reste ouverte, et distincte.
    assert client.get("/api/health").status_code == 200

    api_mod._approve_one_snapshot(db, attente, {"username": "relecteur"}, None)

    reponse = client.get("/api/health/conformite", headers={"X-API-Key": auditeur})
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["bloquants"] == []
    # La portée dit ce qui a été regardé : un 200 ne vaut que pour elle.
    assert {"fraicheur", "demon", "listes", "sources_reference", "secrets"} <= set(reponse.json()["portee"])


def test_la_sonde_est_fermee_a_un_compte_sans_role_de_controle(base, client):
    db, _ = base
    analyste = _cle_api(db, "user")
    assert client.get("/api/health/conformite", headers={"X-API-Key": analyste}).status_code == 403
    assert client.get("/api/health/conformite").status_code == 401


def test_un_controle_illisible_n_autorise_pas_la_sonde_a_dire_conforme(base, client, secrets_poses, monkeypatch):
    db, _ = base
    auditeur = _cle_api(db, "auditor")
    _lot(db, "WATCHLIST_OFAC", "READY", 1)

    def _casse(db):
        raise RuntimeError("base injoignable")
    monkeypatch.setattr(mes, "_CONTROLES_DE_LA_SONDE", mes._CONTROLES_DE_LA_SONDE + (_casse,))
    reponse = client.get("/api/health/conformite", headers={"X-API-Key": auditeur})
    assert reponse.status_code == 503
    assert "illisible" in reponse.json()["bloquants"][0]["constat"]


def test_tout_controle_capable_de_bloquer_est_rejoue_par_la_sonde():
    """
    Un contrôle qui peut conclure BLOQUANT et que la sonde ignore est une panne
    qu'aucune supervision ne verra. Si un contrôle nouveau en est capable,
    ajoutez-le à `_CONTROLES_DE_LA_SONDE` — ou ici, avec la raison, s'il est
    trop coûteux pour une sonde interrogée chaque minute.
    """
    HORS_SONDE = {}   # aucun à ce jour
    rejoues = set(mes._CONTROLES_DE_LA_SONDE) | set(mes._CONTROLES_DE_LA_SONDE_SANS_BASE)
    oublies = [f.__name__ for f in mes._CONTROLES_BASE + mes._CONTROLES_SANS_BASE
               if "BLOQUANT" in inspect.getsource(f) and f not in rejoues
               and f.__name__ not in HORS_SONDE]
    assert not oublies, f"contrôles capables de BLOQUANT absents de la sonde : {oublies}"
    assert len(rejoues) >= 4


# --------------------------------------------------------------------------
# 3. Le récapitulatif « actif » qui n'a jamais rien envoyé
# --------------------------------------------------------------------------

def test_le_catalogue_dit_le_dernier_envoi_reel_de_chaque_evenement(base, client):
    db, _ = base
    envoye_le = datetime(2026, 10, 12, 8, 0)
    db.add(NotificationDelivery(event_key="snapshot_approved", status="SENT",
                                urgency="immediate", sent_at=envoye_le))
    for _ in range(3):
        db.add(NotificationDelivery(event_key="sync_error", status="FAILED",
                                    urgency="immediate", error="timed out"))
    db.commit()
    _connecte("admin")
    catalogue = client.get("/api/settings/notifications/catalog").json()
    envois = catalogue["derniers_envois"]
    assert envois["snapshot_approved"]["dernier_envoi"].startswith("2026-10-12T08:00")
    assert envois["sync_error"] == {"dernier_envoi": None, "sans_envoi": 3}
    assert "kpi_digest" not in envois          # rien au journal : « jamais envoyé »
    assert catalogue["journal_ttl_jours"] >= 30


def test_le_digest_kpi_passe_par_le_journal_des_envois(base, monkeypatch):
    """
    Le digest partait par l'ancien `notify_event`, qui n'écrit rien au journal :
    « 0 envoi » ne voulait donc rien dire, ni qu'il était parti, ni qu'il ne
    l'était pas. Il passe désormais par `emit`, comme toute étape.
    """
    import fiskr.notifier as notifier
    _, fabrique = base

    def _db():
        session = fabrique()
        try:
            yield session
        finally:
            session.close()
    monkeypatch.setattr(api_mod, "get_db", _db)
    monkeypatch.setattr(api_mod, "digest_settings", lambda db: {"enabled": True, "cron": "* * * * *"})
    monkeypatch.setattr(api_mod, "build_kpi_digest", lambda db: {"Alertes ouvertes": 0})
    # L'envoi lui-même (fil de fond, session globale) n'est pas l'objet du test.
    monkeypatch.setattr(notifier, "_send_now", lambda *a, **k: None)

    api_mod._digest_tick(datetime(2026, 10, 19, 8, 0))

    verif = fabrique()
    try:
        assert verif.query(NotificationDelivery).filter(
            NotificationDelivery.event_key == "kpi_digest").count() == 1
    finally:
        verif.close()


def test_l_ecran_affiche_le_dernier_envoi_plutot_que_la_case_cochee():
    import os
    racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(racine, "fiskr", "static", "app.js"), encoding="utf-8") as f:
        app_js = f.read()
    with open(os.path.join(racine, "fiskr", "static", "index.html"), encoding="utf-8") as f:
        html = f.read()
    assert "derniers_envois" in app_js and "Jamais envoyé" in app_js
    assert "${dernierEnvoiHtml(ev.key" in app_js, "chaque ligne du catalogue porte son dernier envoi"
    assert 'id="digest-dernier-envoi"' in html


# --------------------------------------------------------------------------
# 4. L'auditeur lit les règles — et n'écrit rien
# --------------------------------------------------------------------------

def _connecte(role):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": f"compte_{role}", "full_name": role,
        "role": role, "roles": [role],
    }


def _alerte(db):
    trace = AuditTrail(client_id="C-1", client_name="Client", client_type="PP",
                       watchlist_id="W-1", watchlist_name="Listé", base_score=90.0,
                       final_score=90.0, status="MATCH", decision_tree={},
                       config_state={}, watchlist_version="v", watchlist_hash="h",
                       timestamp=datetime.utcnow())
    db.add(trace)
    db.commit()
    alerte = Alert(audit_id=trace.id, client_id="C-1", client_name="Client",
                   watchlist_entity_id="W-1", watchlist_name="Listé", final_score=90.0)
    db.add(alerte)
    db.commit()
    return alerte.id


@pytest.mark.parametrize("chemin", [
    "/api/fprules", "/api/fprules/templates", "/api/fprules/1/changes",
    "/api/fprules/1/tests", "/api/fprules/context-from-alert/{alerte}",
    "/api/settings/blocking", "/api/settings/engine",
])
def test_une_cle_auditeur_lit_les_regles_et_le_parametrage(base, client, chemin):
    db, _ = base
    auditeur = _cle_api(db, "auditor")
    reponse = client.get(chemin.format(alerte=_alerte(db)), headers={"X-API-Key": auditeur})
    assert reponse.status_code == 200, f"{chemin} : {reponse.status_code} {reponse.text[:120]}"


def test_ouvrir_la_lecture_n_ouvre_ni_l_ecriture_ni_les_autres_roles(base, client):
    """La lecture s'ouvre à l'auditeur ; l'écriture lui reste fermée (les deux
    verrous : garde de la route et refus général des méthodes mutantes), et un
    analyste ne lit toujours pas les règles."""
    db, _ = base
    auditeur, analyste = _cle_api(db, "auditor"), _cle_api(db, "user")
    assert client.get("/api/fprules", headers={"X-API-Key": auditeur}).status_code == 200
    for methode, chemin, corps in (
        ("POST", "/api/fprules", {"name": "r", "channel": "SCREENING",
                                  "code": "def rule(ctx):\n    return None"}),
        ("PUT", "/api/settings/blocking", {"screening_layout": ["COUNTRY_ISO"]}),
        ("POST", "/api/fprules/1/tests", {"name": "t", "ctx": {}, "expected": "KEEP"}),
    ):
        reponse = client.request(methode, chemin, json=corps, headers={"X-API-Key": auditeur})
        assert reponse.status_code == 403, f"{methode} {chemin} : {reponse.status_code}"
    assert client.get("/api/fprules", headers={"X-API-Key": analyste}).status_code == 403


def test_aucun_get_ne_reste_ferme_a_l_auditeur_sans_raison():
    """
    Inventaire dérivé des routes : toute lecture gardée par un rôle doit
    admettre l'auditeur, sauf celles listées ici avec leur raison. Le
    complément de `test_lecture_de_l_auditeur` (qui inventorie `require_admin`).
    """
    DELIBEREMENT_FERMEES = {
        # Ce GET ÉCRIT dans l'historique de l'alerte (STR_DRAFT_GENERATED) :
        # l'ouvrir ferait d'une lecture de contrôleur une ligne au dossier.
        "/api/alerts/{alert_id}/str-draft",
        "/api/alerts/{alert_id}/str-draft/print",
    }

    def _gardes(dependant, vus=None):
        vus = set() if vus is None else vus
        out = []
        for sous in dependant.dependencies:
            if sous.call is not None and hasattr(sous.call, "roles_admis"):
                out.append(sous.call)
            if id(sous) not in vus:
                vus.add(id(sous))
                out += _gardes(sous, vus)
        return out

    gardees, fermees = [], []
    for route in app.routes:
        if not hasattr(route, "dependant"):
            continue
        if set(getattr(route, "methods", []) or []) - {"HEAD", "OPTIONS"} != {"GET"}:
            continue
        gardes = _gardes(route.dependant)
        if gardes:
            gardees.append(route.path)
        if any("auditor" not in g.roles_admis for g in gardes) and route.path not in DELIBEREMENT_FERMEES:
            fermees.append(route.path)
    # Sans cette garde, une détection cassée rendrait le test vert à vide.
    assert DELIBEREMENT_FERMEES <= set(gardees), "l'inventaire ne voit plus les gardes de rôle"
    assert not fermees, "Lectures fermées à l'auditeur :\n  " + "\n  ".join(sorted(fermees))


# --------------------------------------------------------------------------
# 5. Chaque approbation au journal d'administration, une fois
# --------------------------------------------------------------------------

def _lignes(db, action):
    db.expire_all()
    return db.query(AdminAuditLog).filter(AdminAuditLog.action == action).all()


def test_une_approbation_unitaire_laisse_une_ligne_au_journal_d_administration(base, client):
    db, _ = base
    _connecte("reviewer")
    lot = _lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 2)
    reponse = client.post(f"/api/review/snapshots/{lot}/approve", json={"comment": "relu"})
    assert reponse.status_code == 202, reponse.text
    lignes = _lignes(db, "SNAPSHOT_APPROVED")
    assert len(lignes) == 1
    assert lignes[0].target == "WATCHLIST_UN"
    assert lignes[0].after["snapshot_id"] == lot
    assert lignes[0].after["homologation"] == "unitaire"
    assert lignes[0].username == "compte_reviewer"


def test_une_approbation_groupee_laisse_une_ligne_par_liste_et_une_seule(base, client):
    db, _ = base
    _connecte("reviewer")
    lots = [_lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 2),
            _lot(db, "WATCHLIST_OFAC", "PENDING_REVIEW", 2)]
    reponse = client.post("/api/review/snapshots/approve-bulk", json={"snapshot_ids": lots})
    assert reponse.status_code == 202, reponse.text
    approuvees = _lignes(db, "SNAPSHOT_APPROVED")
    assert sorted(l.after["snapshot_id"] for l in approuvees) == sorted(lots)
    assert {l.after["homologation"] for l in approuvees} == {"groupée"}
    # Le geste groupé garde sa ligne, sans relister les approbations.
    geste = _lignes(db, "SNAPSHOTS_BULK_APPROVED")
    assert len(geste) == 1
    assert not any(lot in str(geste[0].after) for lot in lots)


def test_un_rejet_figure_aussi_au_journal(base, client):
    db, _ = base
    _connecte("reviewer")
    lot = _lot(db, "WATCHLIST_UN", "PENDING_REVIEW", 2)
    assert client.post(f"/api/review/snapshots/{lot}/reject",
                       json={"comment": "source corrompue"}).status_code == 200
    lignes = _lignes(db, "SNAPSHOT_REJECTED")
    assert len(lignes) == 1 and lignes[0].detail == "source corrompue"


# --------------------------------------------------------------------------
# 6. Une liste de sanctions sans sa source de référence
# --------------------------------------------------------------------------

def test_une_liste_ue_nourrie_par_eurlex_sans_sa_consolidee_est_nommee(base):
    db, _ = base
    lot = _lot(db, "WATCHLIST_EU", "READY", 24, fiches=320)
    db.add(SyncReport(source="EURLEX", trigger="SCHEDULED", status="SUCCESS",
                      snapshot_id=lot, executed_at=datetime.utcnow()))
    db.commit()
    controle = mes._sources_de_reference(db)
    assert controle["etat"] == mes.BLOQUANT
    for attendu in ("EU (320 fiches)", "eu_fsf", "jeton absent", "EURLEX", "eurlex"):
        assert attendu in controle["constat"], (attendu, controle["constat"])
    assert "sync.eu_fsf.token" in controle["remede"]


def test_une_liste_dont_la_reference_tourne_n_est_pas_nommee(base):
    db, _ = base
    _lot(db, "WATCHLIST_OFAC", "READY", 24)
    assert mes._sources_de_reference(db)["etat"] == mes.OK


def test_une_source_coupee_des_passages_planifies_fige_sa_liste(base):
    from fiskr.settings import (SETTING_SYNC_AUTO_ENABLED, SETTING_SYNC_SOURCES_ENABLED,
                                set_setting)
    db, _ = base
    _lot(db, "WATCHLIST_UN", "READY", 24 * 20)
    set_setting(db, SETTING_SYNC_AUTO_ENABLED, True)
    set_setting(db, SETTING_SYNC_SOURCES_ENABLED, {"un": False})
    controle = mes._sources_de_reference(db)
    assert controle["etat"] == mes.ATTENTION
    assert "UN" in controle["constat"] and "20 jour(s)" in controle["constat"]


def test_une_liste_de_vigilance_n_est_pas_nommee(base):
    """Une liste PEP figée ne porte pas d'obligation de gel : la nommer ferait
    crier le contrôle sur ce qui ne demande pas d'action."""
    from fiskr.settings import (SETTING_SYNC_AUTO_ENABLED, SETTING_SYNC_SOURCES_ENABLED,
                                set_setting)
    db, _ = base
    _lot(db, "WATCHLIST_PEP", "READY", 24 * 20)
    set_setting(db, SETTING_SYNC_AUTO_ENABLED, True)
    set_setting(db, SETTING_SYNC_SOURCES_ENABLED, {"pep": False})
    assert mes._sources_de_reference(db)["etat"] == mes.OK


def test_la_table_des_sources_de_reference_est_celle_qu_ecrivent_les_executeurs(monkeypatch):
    """
    La table n'est pas crue sur parole : chaque exécuteur est appelé, et la
    liste qu'il REMPLACE réellement est relevée au point de passage commun.
    Une source ajoutée sans entrée, ou une entrée qui ment, fait tomber ce test.
    """
    from fiskr import sync

    class _Remplace(Exception):
        pass

    def _releve(db, **kwargs):
        raise _Remplace(kwargs["file_type"])

    vraie_config = sync.get_sync_config

    def _config_avec_jeton(db=None):
        cfg = vraie_config(db)
        cfg["eu_fsf"] = dict(cfg["eu_fsf"], token="jeton-de-test")
        return cfg

    monkeypatch.setattr(sync, "_run_list_replacement_sync", _releve)
    monkeypatch.setattr(sync, "get_sync_config", _config_avec_jeton)
    observe = {}
    for cle, executeur in api_mod._SYNC_RUNNERS.items():
        if cle in sync.SOURCES_COMPLEMENTAIRES:
            continue
        with pytest.raises(_Remplace) as remplace:
            executeur(None)
        observe[str(remplace.value)] = cle
    assert observe == sync.sources_de_reference()
    assert set(sync.SOURCES_COMPLEMENTAIRES) <= set(api_mod._SYNC_RUNNERS)


# --------------------------------------------------------------------------
# 8. Le badge « Hash actif » ne décode plus les rapports de cahier de tests
# --------------------------------------------------------------------------

def test_le_badge_ne_lit_que_l_identifiant_et_l_empreinte_des_lots(base):
    """
    `/api/watchlist/summary`, chargé à chaque ouverture de page, hydratait les
    39 lots en production — rapports de cahier de tests compris (~150 Kio de
    JSON chacun). Mesuré sur 39 lots et 670 864 fiches : 55 à 70 ms par appel
    déjà mémorisé, 2 à 3 ms en ne lisant que deux colonnes.
    """
    from sqlalchemy import event
    db, _ = base
    _lot(db, "WATCHLIST_OFAC", "READY", 1)
    lues = []

    def _ecoute(conn, cursor, statement, *a):
        if "FROM snapshots" in statement:
            lues.append(statement)
    event.listen(db.bind, "before_cursor_execute", _ecoute)
    try:
        rendu = api_mod._production_watchlist_summary(db)
    finally:
        event.remove(db.bind, "before_cursor_execute", _ecoute)
    assert rendu["hash"] and rendu["count"] == 0
    assert lues and not any("backtest_report" in sql for sql in lues), lues


# --------------------------------------------------------------------------
# 7. Deux affirmations sans vérification
# --------------------------------------------------------------------------

def test_une_couverture_en_echec_ne_dit_pas_aucun_referentiel():
    from fiskr.couverture import couverture_du_criblage, phrase_de_couverture

    class _SessionCassee:
        def query(self, *a, **k):
            raise RuntimeError("base injoignable")

    mesure = couverture_du_criblage(_SessionCassee())
    assert mesure["sans_referentiel"] is not True
    assert mesure["illisible"] is True
    assert "illisible" in phrase_de_couverture(mesure)


def test_la_mise_en_service_dit_mesure_illisible_et_non_rien_a_cribler(monkeypatch):
    import fiskr.couverture as couverture
    monkeypatch.setattr(couverture, "_snapshots_clients",
                        lambda db: (_ for _ in ()).throw(RuntimeError("délai dépassé")))
    controle = mes._couverture_du_criblage(None)
    assert controle["etat"] == mes.ATTENTION
    assert "illisible" in controle["constat"] and "Aucun référentiel" not in controle["constat"]


def test_une_fiche_portant_rule_i03_ne_ressort_pas_ok():
    from fiskr.quality import evaluate_and_clean
    rapport = evaluate_and_clean({
        "primary_name": "John Doe", "entity_type": "I",
        "individual_name_parsed": {"first_name": "John", "last_name": "Doe"},
        "dates_of_birth": ["1970-01-01"], "genders": ["M", "F"],
        "countries": {"citizenship": ["FR"]},
    })
    assert rapport["warnings"] == ["Rule_I03: Incohérence de Genre Multi-valué (Force le repli sur U)"]
    assert rapport["status"] == "DEGRADED"

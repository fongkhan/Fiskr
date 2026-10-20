"""
Les webhooks entrants criblent vraiment, et une cle d'idempotence ne rejoue
que sa propre requete.

Trois defauts mesures (inventaire de classe, octobre 2026) :

1. La cle d'idempotence etait cherchee dans toute la table, sans l'appelant,
   le point d'entree ni le contenu. Le SI A crible « Jean Dupont » sous la cle
   « 42 », le SI B envoie un liste sous « 42 » : 200, `X-Idempotency-Replayed`,
   la ligne d'audit de A, aucune ligne pour B, aucune alerte. Un upsert envoye
   sous « 42 » recevait la reponse du criblage et n'etait jamais applique.

2. Le webhook temps reel ne passait pas par le chemin de fraicheur du cache
   que prend /api/screen. Sous Passenger (pas de lifespan) : processus neuf +
   liste READY -> 409 « Aucune liste n'est en production » ; apres mise en
   production d'une DGT designant le client -> NO_MATCH journalise, la ou
   /api/screen rendait ALERT.

3. Un client cree ou modifie par webhook n'etait pas crible. Lignes
   directrices conjointes DGT/ACPR sur le gel des avoirs (18 mars 2026) :
   avant toute entree en relation (§122), « a chaque mise a jour des elements
   d'identification » (§141).
"""
import json
import uuid
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, Snapshot, WatchlistEntity, ClientEntity, Alert, AuditTrail,
    HookDelivery, compute_checksum,
)
from fiskr.settings import bump_watchlist_epoch

ADMIN = {"id": 1, "username": "admin_whk", "full_name": "Admin", "role": "admin",
         "roles": ["admin"]}


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _cle_api(client, nom=None):
    r = client.post("/api/apikeys", json={"name": nom or f"test_whk_{uuid.uuid4().hex[:6]}",
                                          "role": "user"})
    assert r.status_code == 200, r.text
    return r.json()["api_key"]


@contextmanager
def _sans_session_humaine():
    """Les webhooks sont reserves aux cles d'API : la session admin simulee
    est retiree le temps de l'appel."""
    sauve = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield
    finally:
        if sauve is not None:
            app.dependency_overrides[get_current_user] = sauve


def _hook(client, cle_api, corps=None, idem=None, chemin="/api/hooks/screening", brut=None):
    entetes = {"X-API-Key": cle_api, "Content-Type": "application/json"}
    if idem:
        entetes["X-Idempotency-Key"] = idem
    with _sans_session_humaine():
        return client.post(chemin, content=brut if brut is not None else json.dumps(corps),
                           headers=entetes)


def _liste(client, nom, dob="1960-05-05", pays="RU"):
    """Une liste en production portant `nom` ; rend l'identifiant de la fiche."""
    entity_id = f"WHK-{uuid.uuid4().hex[:8]}"
    corps = ("entity_id,entity_type,primary_name,nationality,dob\n"
             f"{entity_id},I,{nom},{pays},{dob}\n")
    r = client.post("/api/ingest", data={"file_type": "WATCHLIST_EU"},
                    files={"file": (f"test_whk_wl_{uuid.uuid4().hex[:6]}.csv", corps, "text/csv")})
    assert r.status_code == 200, r.text
    return entity_id


def _referentiel(client):
    r = client.post("/api/ingest", data={"file_type": "CLIENT_BASE"},
                    files={"file": (f"test_whk_cb_{uuid.uuid4().hex[:6]}.csv",
                                    "client_id,client_type,client_last_name\n"
                                    f"test_whk_socle_{uuid.uuid4().hex[:6]},PP,Socle\n",
                                    "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _profil(client_id, prenom, nom, dob="1960-05-05", pays="RU"):
    return {"client_id": client_id, "client_type": "PP", "client_first_name": prenom,
            "client_last_name": nom, "client_dob": dob, "client_gender": "M",
            "client_countries": {"nationality": [pays], "residence": [],
                                 "birth_country": [], "registration_country": []}}


def _audits(client_id):
    db = next(get_db())
    try:
        return db.query(AuditTrail).filter(AuditTrail.client_id == client_id).count()
    finally:
        db.close()


# ------------------ 1. IDEMPOTENCE : UNE CLE NE REJOUE QUE SA REQUETE ------------------

def test_la_cle_d_un_autre_appelant_ne_lui_rejoue_pas_le_criblage_d_un_autre_client(client):
    tag = uuid.uuid4().hex[:6]
    listed = f"Hookov{tag}"
    _liste(client, f"Igor {listed}")
    si_a, si_b = _cle_api(client), _cle_api(client)
    cle = f"42-{tag}"

    ra = _hook(client, si_a, _profil(f"test_whk_a{tag}", "Jean", f"Dupont{tag}", "1970-01-01", "FR"), cle)
    assert ra.status_code == 200, ra.text
    rb = _hook(client, si_b, _profil(f"test_whk_b{tag}", "Igor", listed), cle)
    assert rb.status_code == 200, rb.text
    assert "X-Idempotency-Replayed" not in rb.headers, "le SI B a reçu la réponse du SI A"
    assert rb.json()["audit_trail_id"] != ra.json()["audit_trail_id"]
    assert rb.json()["best_match"]["status"] == "ALERT"
    assert rb.json()["alert_id"] is not None
    assert _audits(f"test_whk_b{tag}") >= 1, "aucune ligne d'audit pour le client du SI B"


def test_la_meme_cle_avec_un_autre_corps_est_refusee_en_422(client):
    tag = uuid.uuid4().hex[:6]
    _liste(client, f"Igor Corpsov{tag}")
    si = _cle_api(client)
    cle = f"req-{tag}"
    premier = _hook(client, si, _profil(f"test_whk_c1{tag}", "Jean", f"Dupont{tag}", "1970-01-01", "FR"), cle)
    assert premier.status_code == 200, premier.text

    autre = _hook(client, si, _profil(f"test_whk_c2{tag}", "Igor", f"Corpsov{tag}"), cle)
    assert autre.status_code == 422, autre.text
    assert "DIFFÉRENTE" in autre.json()["detail"]
    assert "X-Idempotency-Replayed" not in autre.headers
    assert _audits(f"test_whk_c2{tag}") == 0, "rien ne doit avoir été criblé"
    # Le refus est visible en supervision, comme tout refus post-authentification
    db = next(get_db())
    try:
        assert db.query(HookDelivery).filter(HookDelivery.status_code == 422).count() >= 1
    finally:
        db.close()


def test_meme_cle_meme_corps_meme_appelant_rejoue_et_le_rejeu_se_compte(client):
    """Le contrat d'origine est inchangé : la retransmission rejoue. Et elle
    se voit désormais en supervision."""
    tag = uuid.uuid4().hex[:6]
    _liste(client, f"Igor Rejouov{tag}")
    si = _cle_api(client)
    profil = _profil(f"test_whk_r{tag}", "Jean", f"Dupont{tag}", "1970-01-01", "FR")
    cle = f"rejeu-{tag}"
    avant = client.get("/api/hooks/stats").json()["totals"].get("replays", 0)

    premier = _hook(client, si, profil, cle)
    assert premier.status_code == 200, premier.text
    audits = _audits(f"test_whk_r{tag}")
    # Le même objet réécrit dans un autre ordre reste la même requête
    reserialise = json.dumps(dict(reversed(list(profil.items()))), indent=2)
    for brut in (json.dumps(profil), reserialise):
        rejeu = _hook(client, si, idem=cle, brut=brut)
        assert rejeu.status_code == 200, rejeu.text
        assert rejeu.headers.get("X-Idempotency-Replayed") == "true"
        assert rejeu.json() == premier.json()
    assert _audits(f"test_whk_r{tag}") == audits, "un rejeu ne recrible pas"

    stats = client.get("/api/hooks/stats").json()
    assert stats["totals"]["replays"] == avant + 2
    assert stats["recent"][0]["replayed"] is True
    assert stats["recent"][0]["idempotent"] is True
    ecran = next(e for e in stats["by_endpoint"] if e["endpoint"] == "screening")
    assert ecran["replays"] >= 2


def test_une_cle_ne_traverse_pas_les_points_d_entree(client):
    """Un upsert envoyé sous la clé d'un criblage est APPLIQUÉ, pas servi avec
    la réponse du criblage."""
    tag = uuid.uuid4().hex[:6]
    base = _referentiel(client)
    si = _cle_api(client)
    cle = f"evt-{tag}"
    assert _hook(client, si, _profil(f"test_whk_x{tag}", "Jean", f"Dupont{tag}",
                                     "1970-01-01", "FR"), cle).status_code == 200
    up = _hook(client, si, {"client_id": f"test_whk_new{tag}", "client_type": "PP",
                            "client_last_name": f"Nouveau{tag}"}, cle,
               chemin="/api/hooks/client-upsert")
    assert up.status_code == 200, up.text
    assert "X-Idempotency-Replayed" not in up.headers
    assert up.json()["operation"] == "created"
    db = next(get_db())
    try:
        assert db.query(ClientEntity).filter(
            ClientEntity.snapshot_id == base["snapshot_id"],
            ClientEntity.client_id == f"test_whk_new{tag}").count() == 1
    finally:
        db.close()


def test_une_livraison_anterieure_sans_empreinte_ne_rejoue_plus_pour_personne(client):
    """Les lignes d'avant la mesure portent la clé brute et aucune empreinte :
    on ne peut pas prouver qu'une retransmission est la même requête. Elles ne
    rejouent plus — ni pour un autre appelant, ni pour le leur."""
    tag = uuid.uuid4().hex[:6]
    _liste(client, f"Igor Legatov{tag}")
    nom = f"test_whk_si_{tag}"
    si, autre_si = _cle_api(client, nom), _cle_api(client)
    db = next(get_db())
    try:
        # Telle que l'ecrivait le code d'avant : cle brute, aucune empreinte
        for cle in (f"legacy-{tag}", f"legacy2-{tag}"):
            db.add(HookDelivery(idempotency_key=cle, endpoint="screening",
                                caller=f"apikey:{nom}", status_code=200,
                                response_json={"forge": True}))
        db.commit()
    finally:
        db.close()
    for appelant, cle in ((autre_si, f"legacy-{tag}"), (si, f"legacy2-{tag}")):
        r = _hook(client, appelant, _profil(f"test_whk_l{tag}", "Igor", f"Legatov{tag}"), cle)
        assert r.status_code == 200, r.text
        assert "X-Idempotency-Replayed" not in r.headers
        assert "forge" not in r.json()
        assert r.json()["best_match"]["status"] == "ALERT"


def test_la_migration_ajoute_les_colonnes_a_une_table_existante(tmp_path):
    """Base existante : les deux colonnes s'AJOUTENT (filet générique, type
    DDL compilé par dialecte — SQLite ici, PostgreSQL par le même chemin), les
    lignes déjà là restent, sans empreinte."""
    from sqlalchemy import create_engine, inspect, text
    from sqlalchemy.dialects import postgresql
    from fiskr import database

    moteur = create_engine(f"sqlite:///{tmp_path / 'ancienne.sqlite3'}")
    with moteur.begin() as conn:
        conn.execute(text(
            "CREATE TABLE hook_deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " idempotency_key VARCHAR(200) NOT NULL UNIQUE, endpoint VARCHAR(100) NOT NULL,"
            " caller VARCHAR(200), status_code INTEGER NOT NULL, response_json JSON,"
            " created_at DATETIME)"))
        conn.execute(text(
            "INSERT INTO hook_deliveries (idempotency_key, endpoint, caller, status_code)"
            " VALUES ('42', 'screening', 'apikey:si-a', 200)"))
    ajoutees = database._add_missing_nullable_columns(moteur, inspect(moteur))
    assert {"hook_deliveries.request_sha256", "hook_deliveries.replay_of"} <= set(ajoutees)
    with moteur.connect() as conn:
        ligne = conn.execute(text(
            "SELECT idempotency_key, request_sha256, replay_of FROM hook_deliveries")).one()
    assert tuple(ligne) == ("42", None, None)
    for nom in ("request_sha256", "replay_of"):
        colonne = database.HookDelivery.__table__.c[nom]
        assert colonne.nullable
        assert colonne.type.compile(postgresql.dialect())
    moteur.dispose()


def test_une_reponse_portant_une_date_se_stocke_telle_que_l_appelant_l_a_recue():
    """La fiche listée d'une correspondance porte ses dates (`modified_at`
    d'une fiche corrigée) : la trace de la livraison doit les accepter, et
    rejouer ce que FastAPI a sérialisé — pas lever après coup."""
    quand = datetime(2026, 10, 19, 8, 30, 0)
    db = next(get_db())
    try:
        cle = (f"sha256:test-whk-{uuid.uuid4().hex}", "0" * 64)
        api._hook_store_delivery(db, cle, "screening", "apikey:test_whk_date", 200,
                                 {"best_match": {"watchlist_entity": {"modified_at": quand}}})
        ligne = db.query(HookDelivery).filter(HookDelivery.idempotency_key == cle[0]).one()
        assert ligne.response_json == {"best_match": {"watchlist_entity": {
            "modified_at": "2026-10-19T08:30:00"}}}
        assert ligne.request_sha256 == "0" * 64
    finally:
        db.close()


# ------------------ 2. FRAICHEUR : LE MEME CHEMIN QUE /api/screen ------------------

def test_un_processus_neuf_ne_dit_pas_qu_aucune_liste_n_est_en_production(client, monkeypatch):
    """Sous Passenger le lifespan ne tourne pas : un processus web démarre
    cache vide. Une liste READY en base doit suffire au webhook."""
    tag = uuid.uuid4().hex[:6]
    _liste(client, f"Igor Froidov{tag}")
    si = _cle_api(client)
    monkeypatch.setattr(api, "watchlist_index", {})
    monkeypatch.setattr(api, "watchlist_store", [])
    monkeypatch.setattr(api, "watchlist_types", set())
    monkeypatch.setattr(api, "_last_epoch_seen", None)
    r = _hook(client, si, _profil(f"test_whk_f{tag}", "Igor", f"Froidov{tag}"))
    assert r.status_code == 200, r.text
    assert r.json()["best_match"]["status"] == "ALERT"


def test_le_webhook_crible_contre_l_epoque_courante(client):
    """Le démon met en production une DGT désignant le client et incrémente
    l'époque ; ce processus-ci n'en sait rien. Le webhook doit la voir."""
    tag = uuid.uuid4().hex[:6].upper()
    si = _cle_api(client)
    db = next(get_db())
    sid = f"test-whk-dgt-{tag}"
    try:
        api._ensure_watchlist_cache(db)    # cache chargé, époque mémorisée
        db.add(Snapshot(snapshot_id=sid, file_type="WATCHLIST_DGT", file_name=f"test_whk_dgt_{tag}.xml",
                        file_hash=uuid.uuid4().hex, record_count=1, status="READY",
                        uploaded_at=datetime.utcnow()))
        fiche = {"entity_id": f"DGT-WHK-{tag}", "entity_type": "I",
                 "primary_name": f"Ivan Nouveaulistov{tag}",
                 "individual_name_parsed": {"first_name": "Ivan", "last_name": f"Nouveaulistov{tag}"},
                 "dates_of_birth": ["1961-03-03"], "gender": "M",
                 "countries": {"citizenship": ["RU"]}, "aliases": {}}
        db.add(WatchlistEntity(snapshot_id=sid, entity_checksum=compute_checksum(fiche), **fiche))
        db.commit()
        bump_watchlist_epoch(db)            # ce que fait le démon après mise en production
        db.commit()

        r = _hook(client, si, _profil(f"test_whk_e{tag}", "Ivan", f"Nouveaulistov{tag}", "1961-03-03"))
        assert r.status_code == 200, r.text
        best = r.json()["best_match"] or {}
        assert best.get("status") == "ALERT", r.json()
        assert best["watchlist_entity"]["entity_id"] == f"DGT-WHK-{tag}"
    finally:
        db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == sid).delete(synchronize_session=False)
        db.query(Snapshot).filter(Snapshot.snapshot_id == sid).delete(synchronize_session=False)
        bump_watchlist_epoch(db)
        db.commit()
        db.close()


def test_aucune_fonction_ne_crible_sans_le_chemin_de_fraicheur():
    """Garde-fou sur l'arbre syntaxique : toute fonction qui appelle le moteur
    sur le cache de CE processus doit d'abord en garantir la fraîcheur. Le
    webhook temps réel était la seule qui ne le faisait pas."""
    import ast
    import inspect as _inspect

    arbre = ast.parse(_inspect.getsource(api).lstrip())
    moteur = {"screen_client_profile", "screen_payment_message", "_screen_preview"}
    fautives = []
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        appels = {n.func.id for n in ast.walk(noeud)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if appels & moteur and "_ensure_watchlist_cache" not in appels:
            fautives.append(noeud.name)
    assert not fautives, f"criblage sans garantie de fraîcheur du cache : {sorted(fautives)}"


# ------------------ 3. UN CLIENT CREE OU MODIFIE PAR WEBHOOK EST CRIBLE ------------------

def test_l_upsert_d_un_client_liste_ouvre_une_alerte_et_journalise(client):
    _referentiel(client)
    entity_id = _liste(client, "Igor PETROV", dob="1965-04-04")
    si = _cle_api(client)
    cid = f"test_whk_ip{uuid.uuid4().hex[:6]}"
    r = _hook(client, si, _profil(cid, "Igor", "Petrov", "1965-04-04"),
              chemin="/api/hooks/client-upsert")
    assert r.status_code == 200, r.text
    criblage = r.json()["screening"]
    assert criblage["performed"] is True and criblage["reason"] == "CREATED"
    assert criblage["status"] == "ALERT", criblage["summary"]
    assert criblage["alert_id"] is not None
    assert "criblée" in r.json()["message"]
    db = next(get_db())
    try:
        assert db.query(AuditTrail).filter(AuditTrail.client_id == cid).count() >= 1
        assert db.query(Alert).filter(Alert.client_id == cid,
                                      Alert.watchlist_entity_id == entity_id).count() == 1
    finally:
        db.close()


def test_un_upsert_ne_touchant_qu_un_champ_neutre_ne_recrible_pas(client):
    _referentiel(client)
    si = _cle_api(client)
    cid = f"test_whk_n{uuid.uuid4().hex[:6]}"
    profil = _profil(cid, "Jean", "Neutrov", "1970-01-01", "FR")
    assert _hook(client, si, profil, chemin="/api/hooks/client-upsert").json()["screening"]["performed"]
    audits = _audits(cid)

    r = _hook(client, si, {**profil, "client_email": "jean@example.test"},
              chemin="/api/hooks/client-upsert")
    assert r.status_code == 200, r.text
    assert r.json()["changed_fields"] == ["client_email"]
    criblage = r.json()["screening"]
    assert criblage["performed"] is False
    assert criblage["reason"] == "NO_IDENTIFICATION_CHANGE"
    assert "client_email" in criblage["summary"]
    assert _audits(cid) == audits, "un champ sans effet sur le criblage a été recriblé"

    # Un élément d'identification, lui, recrible
    r = _hook(client, si, {**profil, "client_email": "jean@example.test", "client_dob": "1971-01-01"},
              chemin="/api/hooks/client-upsert")
    criblage = r.json()["screening"]
    assert criblage["performed"] is True and criblage["reason"] == "IDENTIFICATION_CHANGED"
    assert criblage["identification_fields_changed"] == ["client_dob"]
    assert _audits(cid) > audits


def test_sans_liste_en_production_l_upsert_est_applique_et_le_dit(client, monkeypatch):
    base = _referentiel(client)
    si = _cle_api(client)
    cid = f"test_whk_v{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(api, "_ensure_watchlist_cache", lambda db: None)
    monkeypatch.setattr(api, "watchlist_types", set())
    r = _hook(client, si, _profil(cid, "Igor", "Videov"), chemin="/api/hooks/client-upsert")
    assert r.status_code == 200, r.text
    criblage = r.json()["screening"]
    assert criblage["performed"] is False
    assert criblage["reason"] == "NO_LIST_IN_PRODUCTION"
    assert "NON criblée" in r.json()["message"]
    assert "status" not in criblage, "pas de verdict pour une comparaison qui n'a pas eu lieu"
    assert _audits(cid) == 0
    db = next(get_db())
    try:
        assert db.query(ClientEntity).filter(ClientEntity.snapshot_id == base["snapshot_id"],
                                             ClientEntity.client_id == cid).count() == 1
    finally:
        db.close()


def test_les_elements_d_identification_sont_releves_sur_le_moteur(monkeypatch):
    """La liste n'est pas recopiée : elle est relevée sur ce que le moteur lit.
    Un champ que le moteur apprend à lire y entre sans qu'on y touche."""
    from fiskr import identification, scoring

    lus = identification.champs_lus_par_le_moteur()
    for champ in ("client_first_name", "client_last_name", "client_maiden_name",
                  "client_company_name", "client_aliases", "client_dob", "client_gender",
                  "client_countries", "client_country", "client_tax_id", "client_bic",
                  "client_lei_number", "client_passport_documents", "client_iban"):
        assert champ in lus, champ
    for champ in ("client_email", "client_phone", "client_segment", "client_risk_rating",
                  "client_pep_flag", "client_status", "client_relationship_start",
                  "client_activity_sector", "client_address"):
        assert champ not in lus, champ

    origine = scoring.match_entities

    def moteur_qui_lit_un_champ_de_plus(client, fiche, config):
        client.get("client_champ_appris")
        return origine(client, fiche, config)

    identification.champs_lus_par_le_moteur.cache_clear()
    monkeypatch.setattr(scoring, "match_entities", moteur_qui_lit_un_champ_de_plus)
    try:
        assert "client_champ_appris" in identification.champs_lus_par_le_moteur()
    finally:
        monkeypatch.undo()
        identification.champs_lus_par_le_moteur.cache_clear()
    assert "client_champ_appris" not in identification.champs_lus_par_le_moteur()


def test_un_champ_hors_releve_ne_change_aucun_score():
    """Contre-épreuve : modifier un champ que le relevé déclare neutre ne
    change ni le verdict ni le score d'un rapprochement réel."""
    from fiskr.config import config
    from fiskr.identification import champs_lus_par_le_moteur
    from fiskr.scoring import match_entities

    fiche = {"entity_id": "X", "entity_type": "I", "primary_name": "Igor Petrov",
             "dates_of_birth": ["1965-04-04"], "gender": "M",
             "countries": {"citizenship": ["RU"]}}
    base = _profil("C", "Igor", "Petrov", "1965-04-04")
    reference = match_entities(dict(base), dict(fiche), config)
    neutres = [c for c in api.HookClientUpsert.model_fields
               if c not in champs_lus_par_le_moteur()]
    assert neutres, "le relevé déclare tout champ d'upsert identifiant : suspect"
    for champ in neutres:
        variante = match_entities({**base, champ: "VALEUR-CHANGEE"}, dict(fiche), config)
        assert (variante["status"], variante["final_score"]) == \
            (reference["status"], reference["final_score"]), champ

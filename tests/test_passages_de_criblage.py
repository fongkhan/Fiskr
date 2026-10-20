"""
Pour chaque client : quand il a été criblé, et contre quelle version des listes.

Lignes directrices conjointes DGT/ACPR sur le gel des avoirs (18 mars 2026) :
cribler avant toute entrée en relation (§122), « à chaque mise à jour des
éléments d'identification » (§141), sans délai à chaque modification des
mesures (§123). Trois défauts mesurés empêchaient de le prouver :

1. **Le lookback annonçait « 3 clients criblés » sans laisser de preuve.** Le
   journal d'audit n'écrit une ligne que pour une correspondance, et la
   couverture se mesurait sur ce journal : trois clients propres restaient
   « jamais criblés » après le lookback, et après chaque relance. Relevé dans
   un vrai navigateur : 406 clients, lookback complet, « 382 clients jamais
   criblés » pour toujours. Une ligne NO_MATCH par client n'est pas la
   réponse (mesuré : 5,5 Ko la ligne, 4,2 Go par passage à 750 000 clients) :
   un passage écrit UNE ligne et date chaque fiche en masse.
2. **Un lookback restreint à une liste absente se terminait sur « Lookback
   exécuté »**, zéro client — le refus `exiger_un_univers` ne s'y appliquait pas.
3. **Un import de référentiel ne criblait personne** : « Successfully imported
   406 items », et un client arrivé après la dernière mise à jour des listes
   n'était confronté à rien. Couper `ingestion.auto_rescreen` arrêtait tout
   re-criblage sans qu'aucun contrôle de mise en service ne le relève.
"""
import os
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.api import app
from fiskr.settings import bump_watchlist_epoch
from fiskr.auth import get_current_user
from fiskr.database import (Base, Alert, AuditTrail, ClientEntity, Job, ScreeningPass,
                            Snapshot, WatchlistEntity, get_db)

ADMIN = {"id": 1, "username": "admin_passages", "full_name": "Admin", "role": "admin",
         "roles": ["admin"]}


# ------------------------------------------------------------ base isolée

@pytest.fixture
def db(tmp_path):
    """La couverture est un compte sur TOUT le référentiel en production : sur
    la base partagée, ce que les autres tests y laissent ferait dériver le
    chiffre."""
    engine = create_engine(f"sqlite:///{tmp_path / 'passages.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _referentiel(db, clients, snapshot_id=None):
    snapshot_id = snapshot_id or f"clients-{uuid.uuid4().hex[:6]}"
    if not db.get(Snapshot, snapshot_id):
        db.add(Snapshot(snapshot_id=snapshot_id, file_type="CLIENT_BASE",
                        file_name="clients.csv", file_hash=uuid.uuid4().hex,
                        record_count=len(clients), status="READY",
                        uploaded_at=datetime.utcnow()))
    for client_id, prenom, nom, dob, pays in clients:
        db.add(ClientEntity(
            snapshot_id=snapshot_id, client_id=client_id, client_type="PP",
            client_first_name=prenom, client_last_name=nom, client_dob=dob,
            client_gender="M",
            client_countries={"nationality": [pays], "residence": [],
                              "birth_country": [], "registration_country": []},
            entity_checksum=uuid.uuid4().hex))
    db.commit()
    return snapshot_id


def _liste(db, snapshot_id, entity_id="DGT-1", nom=("Igor", "PETROV"),
           dob="1965-03-12", pays="RU", uploaded_at=None):
    db.add(Snapshot(snapshot_id=snapshot_id, file_type="WATCHLIST_DGT",
                    file_name=f"{snapshot_id}.json", file_hash=uuid.uuid4().hex,
                    record_count=1, status="READY",
                    uploaded_at=uploaded_at or datetime.utcnow()))
    db.add(WatchlistEntity(
        snapshot_id=snapshot_id, entity_id=entity_id, entity_type="I",
        primary_name=" ".join(nom),
        individual_name_parsed={"first_name": nom[0], "last_name": nom[1], "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=[dob], is_deceased=False, gender="M",
        countries={"citizenship": [pays], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=f"chk-{snapshot_id}-{entity_id}"))
    db.commit()
    # Comme tout geste qui change la production : l'epoque avance, et la
    # reference de l'univers (memorisee par epoque, fiskr/univers.py) se relit.
    bump_watchlist_epoch(db)


PROPRES = [("C-0", "Alice", "MARTIN", "1980-01-01", "FR"),
           ("C-1", "Bruno", "DURAND", "1975-06-02", "FR"),
           ("C-2", "Camille", "PETIT", "1990-11-23", "FR")]


def _controle_couverture(db):
    from fiskr.mise_en_service import _couverture_du_criblage
    return _couverture_du_criblage(db)


# ------------------------------------------------ 1. le lookback laisse sa trace

def test_un_lookback_sans_correspondance_date_chaque_client_et_eteint_jamais_cribles(db):
    """Le défaut prouvé : 3 clients sans correspondance, lookback « 3 clients
    criblés », couverture toujours « 3 jamais criblés », 0 ligne d'audit."""
    from fiskr.couverture import couverture_du_criblage
    from fiskr.rescreen import reference_de_l_univers, rescreen_lookback

    _liste(db, "dgt-v1")
    _referentiel(db, PROPRES)
    assert couverture_du_criblage(db)["jamais_cribles"] == 3

    resultat = rescreen_lookback(db)

    assert resultat["clients_screened"] == 3
    mesure = couverture_du_criblage(db)
    assert mesure["jamais_cribles"] == 0, "le lookback n'a laissé aucune trace"
    assert mesure["non_couverts"] == 0
    assert _controle_couverture(db)["etat"] == "OK"

    _version, empreinte, _composition = reference_de_l_univers(db)
    passage = db.query(ScreeningPass).one()
    assert passage.id == resultat["screening_pass_id"]
    assert (passage.nature, passage.status, passage.clients_screened) == ("LOOKBACK", "TERMINE", 3)
    assert passage.watchlist_hash == empreinte
    for fiche in db.query(ClientEntity).all():
        assert fiche.screened_at is not None
        assert fiche.screening_pass_id == passage.id
        assert fiche.covered_universe_hash == empreinte
    # Le volume tenable : une ligne de passage, aucune ligne NO_MATCH
    assert db.query(AuditTrail).count() == 0


def test_un_lookback_restreint_a_une_liste_absente_est_refuse_sans_rien_ecrire(db):
    from fiskr.rescreen import UniversAbsent, rescreen_lookback

    _liste(db, "dgt-v1")
    _referentiel(db, PROPRES)
    with pytest.raises(UniversAbsent):
        rescreen_lookback(db, "WATCHLIST_PEP")
    assert db.query(ScreeningPass).count() == 0
    assert all(f.screened_at is None for f in db.query(ClientEntity).all())


def test_un_lookback_restreint_date_les_fiches_sans_les_dire_couvertes(db):
    """Un lookback sur UN type de liste a vraiment criblé — les fiches sont
    datées — mais il n'a pas vu tout l'univers : il ne couvre personne."""
    from fiskr.couverture import couverture_du_criblage
    from fiskr.rescreen import rescreen_lookback

    _liste(db, "dgt-v1")
    _referentiel(db, PROPRES)
    rescreen_lookback(db, "WATCHLIST_DGT")
    mesure = couverture_du_criblage(db)
    assert mesure["jamais_cribles"] == 0
    assert mesure["non_couverts"] == 3


# ------------------------------- 2. une mise à jour de liste prolonge, sans inventer

def test_une_mise_a_jour_de_liste_prolonge_la_couverture_sans_l_inventer(db):
    """
    Après une mise à jour, le re-criblage ne compare qu'aux fiches nouvelles :
    il prolonge la couverture des clients couverts contre la version d'avant.
    Un client que personne n'avait criblé contre elle est daté — il a bien été
    comparé aux fiches nouvelles — mais il n'est PAS dit couvert.
    """
    from fiskr.couverture import couverture_du_criblage
    from fiskr.rescreen import (reference_de_l_univers, rescreen_after_snapshot_change,
                                rescreen_lookback)

    _liste(db, "dgt-v1", uploaded_at=datetime.utcnow() - timedelta(hours=2))
    snap = _referentiel(db, PROPRES[:1])
    rescreen_lookback(db)
    _referentiel(db, [("C-9", "Denis", "LEROY", "1970-04-04", "FR")], snapshot_id=snap)

    avant = reference_de_l_univers(db)[1]
    _liste(db, "dgt-v2", entity_id="DGT-2", nom=("Oleg", "SMIRNOV"))
    db.get(Snapshot, "dgt-v1").status = "SUPERSEDED"
    db.commit()
    bump_watchlist_epoch(db)
    apres = reference_de_l_univers(db)[1]
    assert apres != avant

    rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v2", "dgt-v1",
                                   reference_avant=avant)

    couvert = db.query(ClientEntity).filter(ClientEntity.client_id == "C-0").one()
    nouveau = db.query(ClientEntity).filter(ClientEntity.client_id == "C-9").one()
    assert couvert.covered_universe_hash == apres
    assert nouveau.screened_at is not None, "il a été confronté aux fiches nouvelles"
    assert nouveau.covered_universe_hash is None, "mais pas à l'univers d'avant"
    mesure = couverture_du_criblage(db)
    assert (mesure["jamais_cribles"], mesure["non_couverts"]) == (0, 1)


# ------------------------------------------------- 3. la mise en service le dit

def test_le_re_criblage_automatique_coupe_est_signale_a_la_mise_en_service(db):
    from fiskr.rescreen import rescreen_lookback
    from fiskr.settings import SETTING_AUTO_RESCREEN, set_setting

    _liste(db, "dgt-v1")
    _referentiel(db, PROPRES)
    rescreen_lookback(db)
    assert _controle_couverture(db)["etat"] == "OK"

    set_setting(db, SETTING_AUTO_RESCREEN, False)
    controle = _controle_couverture(db)
    assert controle["etat"] == "ATTENTION"
    assert "re-criblage automatique est coupé" in controle["constat"]
    assert controle["lien"] == "#settings/settings-governance"


def test_un_ecart_qui_dure_plus_d_une_semaine_bloque(db):
    """
    Les seuils sont ceux de la fraîcheur des listes, pour la même raison : une
    liste en retard et un client non recriblé exposent au même gel manqué.
    Le délai court depuis le premier passage qui a vu la version en production.
    """
    from fiskr.mise_en_service import _RETARD_BLOQUANT_H, _RETARD_TOLERE_H
    from fiskr.rescreen import rescreen_lookback

    _liste(db, "dgt-v1")
    _referentiel(db, PROPRES)
    rescreen_lookback(db)
    db.query(ClientEntity).filter(ClientEntity.client_id == "C-0").update(
        {"covered_universe_hash": "univers-d-avant"})
    passage = db.query(ScreeningPass).one()

    passage.started_at = datetime.utcnow() - timedelta(hours=_RETARD_TOLERE_H - 1)
    db.commit()
    # Sous le seuil, sans travail en cours : l'écart ne se résorbera pas seul
    assert _controle_couverture(db)["etat"] == "ATTENTION"

    passage.started_at = datetime.utcnow() - timedelta(hours=_RETARD_BLOQUANT_H + 1)
    db.commit()
    controle = _controle_couverture(db)
    assert controle["etat"] == "BLOQUANT"
    assert "pas contre la version des listes en production" in controle["constat"]


def test_un_client_jamais_crible_attend_depuis_son_arrivee(db):
    """Un référentiel importé il y a plus d'une semaine et jamais confronté à
    rien : le délai court depuis son arrivée, et il bloque."""
    from fiskr.mise_en_service import _RETARD_BLOQUANT_H

    _liste(db, "dgt-v1")
    snap = _referentiel(db, PROPRES)
    db.get(Snapshot, snap).uploaded_at = datetime.utcnow() - timedelta(hours=_RETARD_BLOQUANT_H + 2)
    db.commit()
    controle = _controle_couverture(db)
    assert controle["etat"] == "BLOQUANT"
    assert "jamais été criblés" in controle["constat"]


def test_une_version_qu_aucun_passage_n_a_vue_n_a_pas_de_duree_inventee(db):
    """Une fiche listée ajoutée sans re-criblage change l'univers sans qu'aucun
    passage ne l'ait vu : la mesure dit que la durée est inconnue plutôt que
    d'annoncer zéro, et le contrôle demande d'agir."""
    from fiskr.couverture import couverture_du_criblage
    from fiskr.rescreen import rescreen_lookback

    _liste(db, "dgt-v1", uploaded_at=datetime.utcnow() - timedelta(hours=1))
    _referentiel(db, PROPRES)
    rescreen_lookback(db)
    _liste(db, "dgt-ajout", entity_id="DGT-9", nom=("Oleg", "SMIRNOV"))

    mesure = couverture_du_criblage(db)
    assert mesure["non_couverts"] == 3
    assert mesure["attente_inconnue"] is True and mesure["en_attente_depuis"] is None
    controle = _controle_couverture(db)
    assert controle["etat"] == "ATTENTION"
    assert "Aucun passage n'a encore confronté le référentiel" in controle["constat"]


# ------------------------------------------- 4. l'import crible ce qui a changé

@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _liste_en_production(client, nom, dob="1960-05-05", pays="RU"):
    corps = ("entity_id,entity_type,primary_name,nationality,dob\n"
             f"PAS-{uuid.uuid4().hex[:8]},I,{nom},{pays},{dob}\n")
    r = client.post("/api/ingest", data={"file_type": "WATCHLIST_EU"},
                    files={"file": (f"test_pas_wl_{uuid.uuid4().hex[:6]}.csv", corps, "text/csv")})
    assert r.status_code == 200, r.text


def _importer(client, lignes):
    corps = "client_id,client_type,client_first_name,client_last_name,client_dob,nationality,client_phone\n"
    corps += "".join(",".join(l) + "\n" for l in lignes)
    r = client.post("/api/ingest", data={"file_type": "CLIENT_BASE"},
                    files={"file": (f"test_pas_cb_{uuid.uuid4().hex[:6]}.csv", corps, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _trois_clients(tag):
    return [[f"test_pas_h{tag}", "PP", "Igor", f"Corpsov{tag}", "1960-05-05", "RU", "0101"],
            [f"test_pas_b{tag}", "PP", "Maelys", f"Fontvieille{tag}", "1987-02-11", "FR", "0102"],
            [f"test_pas_c{tag}", "PP", "Theophane", f"Brugnonet{tag}", "1979-09-30", "FR", "0103"]]


def _fiches(snapshot_id):
    db = next(get_db())
    try:
        return {f.client_id: f for f in db.query(ClientEntity).filter(
            ClientEntity.snapshot_id == snapshot_id).all()}
    finally:
        db.close()


def _compte(modele, *criteres):
    db = next(get_db())
    try:
        return db.query(modele).filter(*criteres).count()
    finally:
        db.close()


def test_l_import_de_trois_nouveaux_clients_les_crible_et_alerte_sur_l_homonyme(client):
    from fiskr.rescreen import reference_de_l_univers

    tag = uuid.uuid4().hex[:6]
    _liste_en_production(client, f"Igor Corpsov{tag}")
    lignes = _trois_clients(tag)

    corps = _importer(client, lignes)

    criblage = corps["criblage"]
    assert criblage["launched"] and criblage["status"] == "DONE", criblage
    assert criblage["result"]["clients_screened"] == 3
    assert "Criblage" in corps["message"], "le message d'import doit le dire"
    fiches = _fiches(corps["snapshot_id"])
    db = next(get_db())
    try:
        empreinte = reference_de_l_univers(db)[1]
    finally:
        db.close()
    assert len({f.screening_pass_id for f in fiches.values()}) == 1
    for fiche in fiches.values():
        assert fiche.screened_at is not None and fiche.covered_universe_hash == empreinte
    homonyme, autres = lignes[0][0], [lignes[1][0], lignes[2][0]]
    assert _compte(Alert, Alert.client_id == homonyme) == 1
    assert _compte(Alert, Alert.client_id.in_(autres)) == 0


def test_un_reimport_identique_ne_recrible_personne(client):
    tag = uuid.uuid4().hex[:6]
    _liste_en_production(client, f"Igor Corpsov{tag}")
    lignes = _trois_clients(tag)
    premier = _importer(client, lignes)
    passages = _compte(ScreeningPass)
    audits = _compte(AuditTrail, AuditTrail.client_id.in_([l[0] for l in lignes]))

    # Le même référentiel dans un nouvel export (un fichier identique à
    # l'octet près est écarté plus tôt, par son empreinte) : mêmes clients,
    # autre ordre.
    second = _importer(client, list(reversed(lignes)))

    resultat = second["criblage"]["result"]
    assert (resultat["clients_screened"], resultat["unchanged_clients"]) == (0, 3)
    assert _compte(ScreeningPass) == passages, "aucun passage pour un import inchangé"
    assert _compte(AuditTrail, AuditTrail.client_id.in_([l[0] for l in lignes])) == audits
    anciennes, nouvelles = _fiches(premier["snapshot_id"]), _fiches(second["snapshot_id"])
    for reference, fiche in nouvelles.items():
        # La fiche hérite de sa trace : elle n'est pas remise à « jamais criblée »
        assert fiche.screening_pass_id == anciennes[reference].screening_pass_id
        assert fiche.screened_at == anciennes[reference].screened_at
        assert fiche.covered_universe_hash == anciennes[reference].covered_universe_hash


def test_un_client_modifie_sur_sa_date_de_naissance_est_recrible(client):
    tag = uuid.uuid4().hex[:6]
    _liste_en_production(client, f"Igor Corpsov{tag}")
    lignes = _trois_clients(tag)
    premier = _importer(client, lignes)
    lignes[1][4] = "1988-02-11"

    second = _importer(client, lignes)

    resultat = second["criblage"]["result"]
    assert (resultat["clients_screened"], resultat["unchanged_clients"]) == (1, 2)
    anciennes, nouvelles = _fiches(premier["snapshot_id"]), _fiches(second["snapshot_id"])
    modifie = lignes[1][0]
    assert nouvelles[modifie].screening_pass_id == resultat["screening_pass_id"]
    assert nouvelles[modifie].screening_pass_id != anciennes[modifie].screening_pass_id
    for reference in (lignes[0][0], lignes[2][0]):
        assert nouvelles[reference].screening_pass_id == anciennes[reference].screening_pass_id


def test_un_champ_neutre_modifie_ne_recrible_pas(client):
    """Un téléphone n'est pas un élément d'identification : le relevé du
    moteur (fiskr/identification.py) le dit, et l'import le suit."""
    tag = uuid.uuid4().hex[:6]
    _liste_en_production(client, f"Igor Corpsov{tag}")
    lignes = _trois_clients(tag)
    _importer(client, lignes)
    lignes[2][6] = "0999"

    resultat = _importer(client, lignes)["criblage"]["result"]
    assert (resultat["clients_screened"], resultat["unchanged_clients"]) == (0, 3)


def test_recribler_un_client_date_sa_fiche(client):
    """Le criblage unitaire a sa ligne d'audit pour pièce ; la fiche en reprend
    la référence — sans quoi un client recriblé à la main resterait « non
    criblé contre la version en production »."""
    tag = uuid.uuid4().hex[:6]
    _liste_en_production(client, f"Igor Corpsov{tag}")
    lignes = _trois_clients(tag)
    corps = _importer(client, lignes)
    db = next(get_db())
    try:
        db.query(ClientEntity).filter(ClientEntity.snapshot_id == corps["snapshot_id"]).update(
            {"screened_at": None, "screening_pass_id": None, "covered_universe_hash": None,
             "screened_identity_sha256": None})
        db.commit()
    finally:
        db.close()

    r = client.post(f"/api/clients/{lignes[1][0]}/screen")
    assert r.status_code == 200, r.text

    fiche = _fiches(corps["snapshot_id"])[lignes[1][0]]
    db = next(get_db())
    try:
        ligne = db.get(AuditTrail, r.json()["audit_trail_id"])
        assert fiche.screened_at is not None
        assert fiche.covered_universe_hash == ligne.watchlist_hash
    finally:
        db.close()


def test_l_api_refuse_le_lookback_restreint_a_une_liste_absente(client):
    import fiskr.api as api
    from fiskr.database import WATCHLIST_FILE_TYPES

    _liste_en_production(client, f"Igor Absentov{uuid.uuid4().hex[:6]}")
    db = next(get_db())
    try:
        api._ensure_watchlist_cache(db)
        absente = next(t for t in WATCHLIST_FILE_TYPES if t not in api.watchlist_types)
    finally:
        db.close()
    avant = _compte(Job, Job.kind == "lookback")

    r = client.post("/api/rescreen/run", json={"file_type": absente})

    assert r.status_code == 409, r.text
    assert absente in r.json()["detail"]
    assert _compte(Job, Job.kind == "lookback") == avant, "aucun travail ne doit partir"


# ------------------------------------------------------------- 5. l'interface

def _app_js():
    chemin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "fiskr", "static", "app.js")
    with open(chemin, encoding="utf-8") as f:
        return f.read()


def test_l_import_d_un_referentiel_dit_que_le_criblage_est_lance_et_ou_le_suivre():
    app_js = _app_js()
    debut = app_js.index("async function handleIngestion(")
    assert "signalerCriblageDesClients(data.criblage)" in app_js[debut:debut + 6000]
    debut = app_js.index("async function signalerCriblageDesClients(")
    corps = app_js[debut:debut + 2500]
    assert "onOperationDone(criblage.job_token" in corps
    assert 'switchSubTab("screening", "screening-batch")' in corps


def test_la_carte_se_relit_apres_un_lookback_termine_dans_la_requete():
    """Exécuté dans la requête (pas de jeton), le lookback est déjà fini : la
    carte attendait une fin qui avait déjà eu lieu, et restait à l'ancien
    chiffre."""
    app_js = _app_js()
    debut = app_js.index("async function lancerLookback(")
    corps = app_js[debut:debut + 2000]
    assert "else chargerCouvertureDuCriblage();" in corps

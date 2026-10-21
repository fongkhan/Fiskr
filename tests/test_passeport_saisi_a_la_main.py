"""
Le passeport d'une fiche ajoutée à la main porte le pays saisi, et un document
sans pays ne se fait pas passer pour « XX ».

Constat (rapport du lot « re-criblage des gestes manuels ») : l'ajout manuel de
fiche (`POST /api/watchlist/entity`) écrivait `issuing_country: "XX"` pour TOUT
passeport et toute pièce d'identité. Le formulaire demandait pourtant le pays
du passeport : l'écran le lisait (`manual-passport-country`) et ne l'envoyait
pas, l'API n'avait aucun champ pour le recevoir. Le hard match comparant numéro
ET pays (fiskr/scoring.py), une fiche saisie à la main ne rapprochait jamais un
client par son passeport.

Ce que ces tests tiennent :
- le pays émetteur est demandé, envoyé et enregistré ; « XX » saisi est refusé ;
- un document sans pays est enregistré SANS pays, et la réponse le dit ;
- le hard match ne rapproche que des documents au pays ÉTABLI : « XX » (le
  marqueur des lecteurs de listes) et l'absence ne sont pas un pays — ni contre
  un pays, ni l'un contre l'autre. Le nom, lui, reste comparé par le score.
"""
import os
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import Alert, AlertEvent, AuditTrail, WatchlistEntity, get_db
from fiskr.scoring import check_hard_matches, pays_emetteur_etabli

UID = uuid.uuid4().hex[:6].upper()
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")
ADMIN = {"id": 1, "username": f"admin_passeport_{UID}", "full_name": "Admin",
         "role": "admin,reviewer", "roles": ["admin", "reviewer"]}
PRENOM = f"Zorvald{UID.lower()}"


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    ajoutees, clients = [], []
    try:
        with TestClient(app) as c:
            yield {"http": c, "fiches": ajoutees, "clients": clients}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        session = next(get_db())
        try:
            # Les fiches de ce test quittent la production : un homonyme d'un
            # autre module les rencontrerait sinon.
            session.query(WatchlistEntity).filter(
                WatchlistEntity.entity_id.in_(ajoutees or ["-"])).delete(synchronize_session=False)
            alertes = [a.id for a in session.query(Alert).filter(
                Alert.client_id.in_(clients or ["-"])).all()]
            session.query(AlertEvent).filter(AlertEvent.alert_id.in_(alertes or [-1])) \
                .delete(synchronize_session=False)
            session.query(Alert).filter(Alert.id.in_(alertes or [-1])).delete(synchronize_session=False)
            session.query(AuditTrail).filter(AuditTrail.client_id.in_(clients or ["-"])) \
                .delete(synchronize_session=False)
            session.commit()
            from fiskr.api import load_watchlist_cache
            from fiskr.settings import bump_watchlist_epoch
            bump_watchlist_epoch(session)
            load_watchlist_cache(session)
        finally:
            session.close()


def _ajouter(ctx, nom, **documents):
    r = ctx["http"].post("/api/watchlist/entity", json={
        "list_type": "WATCHLIST_DGT", "entity_type": "I",
        "primary_name": f"{PRENOM} {nom}", "first_name": PRENOM, "last_name": nom,
        "nationality": "RU", "gender": "M", **documents})
    if r.status_code == 200:
        ctx["fiches"].append(r.json()["entity_id"])
    return r


def _fiche(entity_id):
    session = next(get_db())
    try:
        return session.query(WatchlistEntity).filter(WatchlistEntity.entity_id == entity_id).one()
    finally:
        session.close()


def _cribler(ctx, nom, passeport):
    """Un client au même prénom (même bloc de candidats), au nom DIFFÉRENT : seul
    le passeport peut le rapprocher de la fiche."""
    client_id = f"PASS-{UID}-{uuid.uuid4().hex[:4]}"
    ctx["clients"].append(client_id)
    r = ctx["http"].post("/api/screen", json={
        "client_id": client_id, "client_type": "PP", "client_first_name": PRENOM,
        "client_last_name": nom, "client_gender": "M",
        "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [],
                             "registration_country": []},
        "client_passport_documents": passeport})
    assert r.status_code == 200, r.text
    return r.json()


def test_un_ajout_manuel_avec_un_passeport_fr_rapproche_le_client_au_meme_passeport(client):
    """Avant : le passeport était enregistré sous « XX », le client au passeport
    FR n'était pas rapproché (hard match False, score flou seul)."""
    r = _ajouter(client, f"Quintavalle{UID}", passport_documents="12AB34567",
                 passport_issuing_country="fr")
    assert r.status_code == 200, r.text
    resultat = _cribler(client, f"Bystrokovitch{UID}",
                        [{"number": "12-AB-34567", "issuing_country": "FR"}])
    meilleur = resultat["best_match"]
    assert meilleur["watchlist_entity"]["entity_id"] == r.json()["entity_id"]
    assert meilleur["hard_match_triggered"] is True, meilleur.get("hard_match_details")
    assert meilleur["final_score"] == 100.0 and resultat["alert_id"]
    assert "12AB34567 - FR" in meilleur["hard_match_details"]
    assert _fiche(r.json()["entity_id"]).passport_documents == [
        {"number": "12AB34567", "issuing_country": "FR"}]
    assert r.json()["documents_sans_pays"] == []


def test_un_passeport_sans_pays_est_enregistre_sans_pays_et_ne_fait_pas_de_hard_match(client):
    r = _ajouter(client, f"Sanspays{UID}", passport_documents="77ZZ12345",
                 national_id_documents="CNI-998877")
    assert r.status_code == 200, r.text
    fiche = _fiche(r.json()["entity_id"])
    assert fiche.passport_documents == [{"number": "77ZZ12345", "issuing_country": None}]
    assert "XX" not in str(fiche.national_id_documents)
    assert r.json()["documents_sans_pays"] == ["77ZZ12345", "CNI-998877"], \
        "la réponse dit ce qui ne pourra pas rapprocher"

    # Ni contre un pays, ni contre un autre passeport sans pays : le numéro seul
    # n'établit pas que c'est le même document
    for passeport in ([{"number": "77ZZ12345", "issuing_country": "FR"}],
                      [{"number": "77ZZ12345"}]):
        meilleur = _cribler(client, f"Autrenom{UID}", passeport)["best_match"] or {}
        assert not meilleur.get("hard_match_triggered"), meilleur.get("hard_match_details")


def test_xx_saisi_comme_pays_est_refuse_et_un_code_illisible_aussi(client):
    r = _ajouter(client, f"Marqueur{UID}", passport_documents="55XX00001",
                 passport_issuing_country="XX")
    assert r.status_code == 400 and "n'est pas un pays" in r.json()["detail"]
    r = _ajouter(client, f"Illisible{UID}", passport_documents="55XX00002",
                 passport_issuing_country="France")
    assert r.status_code == 400 and "ISO à deux lettres" in r.json()["detail"]
    assert not client["fiches"]


def test_le_hard_match_ne_tient_pour_pays_que_ce_qui_est_etabli():
    """Le marqueur des lecteurs de listes (« XX ») et l'absence ne sont pas un
    pays. Avant : « XX » = « XX » et vide = vide déclenchaient une
    correspondance exacte à 100 %, sans rien d'établi."""
    assert pays_emetteur_etabli(" fr ") == "FR"
    assert pays_emetteur_etabli("XX") == "" and pays_emetteur_etabli(None) == ""
    for client_pays, fiche_pays in (("XX", "XX"), (None, None), ("FR", "XX"), ("FR", None)):
        for cle_client, cle_fiche in (("client_passport_documents", "passport_documents"),
                                      ("client_national_id_documents", "national_id_documents")):
            correspond, _ = check_hard_matches(
                {cle_client: [{"number": "A1234567", "issuing_country": client_pays}]},
                {cle_fiche: [{"number": "A1234567", "issuing_country": fiche_pays}]})
            assert not correspond, (cle_client, client_pays, fiche_pays)
    correspond, motif = check_hard_matches(
        {"client_passport_documents": [{"number": "A1234567", "issuing_country": "fr"}]},
        {"passport_documents": [{"number": "A-1234567", "issuing_country": "FR"}]})
    assert correspond and "A1234567 - FR" in motif


def test_les_formulaires_envoient_le_pays_et_n_ecrivent_plus_xx():
    with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as f:
        js = f.read()
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        page = f.read()
    assert "passport_issuing_country: passportNum && passportCountry" in js
    assert "national_id_issuing_country:" in js
    assert 'id="manual-national-id-country"' in page
    assert '"issuing_country": "XX"' not in js and "|| \"XX\"" not in js

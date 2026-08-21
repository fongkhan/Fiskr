"""
Ce que le dispositif absorbe doit se voir, et ne pas se compter comme du travail.

Une campagne batch publiait deux chiffres : les clients en alerte, et
`hits_count` — présenté partout comme « alertes ouvertes », en-tête de colonne
comprise. Or `hits_count` compte les correspondances **trouvées** au-dessus du
seuil, liste blanche et règles anti-faux positifs **comprises**. Le commentaire
du modèle l'affirmait même : « le criblage ouvre une alerte chacune ». C'est
faux d'exactement ce que le dispositif absorbe.

Une correspondance en liste blanche n'ouvre rien — c'est toute la raison d'être
d'un « Good Guy ». Une correspondance close par règle est ouverte puis
refermée, avec le nom de la règle en clair. Compter les deux comme du travail
ouvert surestime la charge et, surtout, rend invisible la seule chose que ces
deux mécanismes produisent : l'écart.

`opened_count` porte désormais les alertes réellement ouvertes. C'est une
colonne additive : les campagnes antérieures portent NULL, et un écran qui ne
sait pas affiche « — » plutôt qu'un zéro qui se lirait comme une mesure.
"""
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, BatchCampaign, ClientEntity, WhitelistPair

PUTIN_ENTITY_ID = "WL-001"  # seed watchlist.json
CSV = ("client_id,client_type,client_first_name,client_last_name,"
       "client_dob,client_gender,nationality\n"
       "{cid},PP,Vladimir,Putin,1952-10-07,M,RU\n")


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "full_name": "Admin",
        "role": "admin", "roles": ["admin"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _campagne(client, cid, nom):
    r = client.post("/api/batch/campaigns", data={"name": nom},
                    files={"file": (f"{nom}.csv", CSV.format(cid=cid), "text/csv")})
    assert r.status_code == 200, r.text
    identifiant = r.json()["id"]
    limite = time.time() + 30
    while time.time() < limite:
        data = client.get(f"/api/batch/campaigns/{identifiant}").json()
        if data["status"] != "RUNNING":
            assert data["status"] == "DONE", data
            return data
        time.sleep(0.3)
    raise AssertionError("la campagne n'a pas terminé")


def _nettoie(cids):
    session = next(get_db())
    try:
        for cid in cids:
            session.query(Alert).filter(Alert.client_id == cid).delete(synchronize_session=False)
            session.query(WhitelistPair).filter(
                WhitelistPair.client_id == cid).delete(synchronize_session=False)
            session.query(ClientEntity).filter(
                ClientEntity.client_id == cid).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def test_une_paire_en_liste_blanche_est_trouvee_mais_pas_ouverte(client):
    """
    Le cœur du sujet. Deux clients identiques, l'un avec une paire en liste
    blanche : ils trouvent AUTANT de correspondances, et le second en ouvre
    une de moins. Confondus, ces deux chiffres faisaient disparaître l'effet
    du « Good Guy » du compte rendu de campagne.
    """
    tag = uuid.uuid4().hex[:8]
    sans, avec = f"vabs_{tag}_a", f"vabs_{tag}_b"
    try:
        reference = _campagne(client, sans, f"vabs_ref_{tag}")
        assert reference["alert_count"] == 1, reference
        assert reference["hits_count"] >= 1, reference
        assert reference["opened_count"] == reference["hits_count"], (
            "sans liste blanche, tout ce qui est trouvé est ouvert", reference)

        pose = client.post("/api/whitelist", data={
            "client_id": avec, "watchlist_entity_id": PUTIN_ENTITY_ID,
            "justification": "Homonyme avéré (test du volume absorbé).",
        })
        assert pose.status_code == 200, pose.text

        blanchie = _campagne(client, avec, f"vabs_wl_{tag}")
        assert blanchie["hits_count"] == reference["hits_count"], (
            "la liste blanche ne fait pas disparaître la correspondance : "
            "elle est trouvée, journalisée, et non ouverte")
        assert blanchie["opened_count"] == reference["opened_count"] - 1, (
            "une alerte de moins ouverte", blanchie, reference)
    finally:
        _nettoie([sans, avec])


def test_l_ecart_entre_trouve_et_ouvert_n_est_jamais_negatif(client):
    """Garde-fou d'exactitude : on ne peut pas ouvrir plus qu'on n'a trouvé."""
    tag = uuid.uuid4().hex[:8]
    cid = f"vabs_{tag}_c"
    try:
        campagne = _campagne(client, cid, f"vabs_ecart_{tag}")
        assert 0 <= campagne["opened_count"] <= campagne["hits_count"], campagne
    finally:
        _nettoie([cid])


def test_une_campagne_anterieure_ne_ment_pas_par_zero(client):
    """
    `opened_count` est une colonne additive : les campagnes lancées avant elle
    portent NULL. L'API doit rendre `null`, pas `0` — un zéro se lirait comme
    « aucune alerte ouverte », ce qui serait une mesure, alors qu'il n'y en a
    aucune.
    """
    tag = uuid.uuid4().hex[:8]
    cid = f"vabs_{tag}_d"
    try:
        campagne = _campagne(client, cid, f"vabs_null_{tag}")
        session = next(get_db())
        try:
            ligne = session.query(BatchCampaign).filter(
                BatchCampaign.id == campagne["id"]).one()
            ligne.opened_count = None          # comme une campagne d'avant
            session.commit()
        finally:
            session.close()
        relue = client.get(f"/api/batch/campaigns/{campagne['id']}").json()
        assert relue["opened_count"] is None, relue
    finally:
        _nettoie([cid])


def test_les_ecrans_ne_disent_plus_alertes_pour_des_correspondances():
    """
    L'en-tête de colonne annonçait « Alertes ouvertes » au-dessus de
    `hits_count`. Le libellé doit dire ce que la colonne tient, et l'écart
    doit être lisible.
    """
    import os

    racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(racine, "fiskr", "static", "index.html"), encoding="utf-8") as f:
        index = f.read()
    with open(os.path.join(racine, "fiskr", "static", "app.js"), encoding="utf-8") as f:
        app_js = f.read()

    entete = index[index.index("Clients en alerte"):index.index("Clients en alerte") + 400]
    assert "Correspondances" in entete, (
        "la colonne de hits_count doit nommer des correspondances")
    assert "opened_count" in app_js, (
        "les alertes réellement ouvertes doivent atteindre l'écran")


def test_le_modele_ne_pretend_plus_qu_une_correspondance_ouvre_une_alerte():
    """
    Le commentaire du modèle était la source de la confusion : il affirmait
    que le criblage « ouvre une alerte chacune ». Une garde de source, parce
    qu'un commentaire faux se recopie plus vite qu'il ne se corrige.
    """
    import inspect

    from fiskr.database import BatchCampaign as Modele

    import re

    source = inspect.getsource(inspect.getmodule(Modele))
    debut = source.index("class BatchCampaign")
    bloc = source[debut:source.index("no_match_count", debut)]
    # Blancs normalisés : un commentaire se replie sur plusieurs lignes sans
    # cesser d'être la même phrase.
    plat = re.sub(r"\s*#\s*", " ", re.sub(r"\s+", " ", bloc))
    assert "opened_count" in bloc
    assert "liste blanche et regles anti-FP comprises" in plat, (
        "le modèle doit dire ce que hits_count compte réellement")

"""
Connexion : le chronomètre ne doit pas dire si un compte existe.

`verify_password` dérive PBKDF2-SHA256 sur 100 000 itérations — **67 ms
mesurées**. Le test `if not user or not verify_password(...)` court-circuitait
sur un compte inexistant : la réponse revenait en quelques millisecondes au
lieu de 67. Un écart pareil s'observe trivialement sur le réseau, et il suffit
à **énumérer les comptes valides**.

Le message d'erreur, lui, était déjà identique dans les deux cas — c'est le
seul chronomètre qui parlait. C'est le genre de fuite qui ne casse rien, ne
lève aucune alerte, et rend le verrouillage anti-brute-force beaucoup moins
utile : un attaquant qui sait quels comptes existent concentre ses tentatives
au lieu de les disperser.

Un compte inconnu fait désormais vérifier une empreinte factice : même calcul,
même durée, même réponse.
"""
import statistics
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app, _EMPREINTE_FACTICE
from fiskr.database import get_db, User, hash_password

TAG = uuid.uuid4().hex[:8]
UTILISATEUR = f"enum-{TAG}"
MOT_DE_PASSE = "MotDePasseDeTest123!"


@pytest.fixture()
def contexte():
    db = next(get_db())
    empreinte, sel = hash_password(MOT_DE_PASSE)
    db.add(User(username=UTILISATEUR, hashed_password=empreinte, salt=sel,
                full_name="Compte de test", role="user"))
    db.commit()
    yield TestClient(app)
    db.query(User).filter(User.username == UTILISATEUR).delete()
    db.commit()
    db.close()


def _chrono(client, nom, mot_de_passe, tours=7):
    """Médiane du temps de réponse, en millisecondes."""
    mesures = []
    for _ in range(tours):
        depart = time.perf_counter()
        client.post("/api/auth/login",
                    json={"username": nom, "password": mot_de_passe})
        mesures.append((time.perf_counter() - depart) * 1000)
    return statistics.median(mesures)


def test_l_empreinte_factice_existe_et_ne_vaut_aucun_compte():
    empreinte, sel = _EMPREINTE_FACTICE
    assert empreinte and sel
    from fiskr.database import verify_password
    assert not verify_password(MOT_DE_PASSE, empreinte, sel)
    assert not verify_password("", empreinte, sel)


def test_le_message_ne_distingue_pas_les_deux_cas(contexte):
    client = contexte
    inconnu = client.post("/api/auth/login", json={
        "username": f"inexistant-{TAG}", "password": MOT_DE_PASSE})
    mauvais = client.post("/api/auth/login", json={
        "username": UTILISATEUR, "password": "MauvaisMotDePasse!"})
    assert inconnu.status_code == mauvais.status_code == 401
    assert inconnu.json() == mauvais.json(), (
        "le message distingue compte inconnu et mot de passe faux")


def test_le_temps_de_reponse_ne_distingue_pas_les_deux_cas(contexte):
    """La mesure : sans la vérification factice, l'écart valait la durée d'un
    PBKDF2 complet (67 ms). On tolère un facteur 2 — le bruit d'un test est
    largement au-dessus d'un écart réel de 67 ms sur ce chemin."""
    client = contexte
    inconnu = _chrono(client, f"inexistant-{TAG}", MOT_DE_PASSE)
    connu = _chrono(client, UTILISATEUR, "MauvaisMotDePasse!")
    rapport = max(inconnu, connu) / max(1e-6, min(inconnu, connu))
    assert rapport < 2.0, (
        f"compte inconnu {inconnu:.0f} ms, compte connu {connu:.0f} ms "
        f"(rapport {rapport:.1f}) : le chronomètre trahit l'existence du compte")


def test_les_deux_chemins_paient_bien_une_derivation(contexte):
    """Le pendant : si les deux devenaient instantanés, l'égalité serait
    respectée mais le hachage aurait disparu."""
    client = contexte
    assert _chrono(client, f"inexistant-{TAG}", MOT_DE_PASSE, tours=3) > 10.0, (
        "un compte inconnu ne paie plus de dérivation : le hachage a disparu")


def test_le_verrouillage_reste_actif(contexte):
    """La correction ne doit pas avoir désarmé l'anti-brute-force."""
    client = contexte
    from fiskr.auth import security_config
    seuil = security_config()["max_login_failures"]
    for _ in range(seuil):
        client.post("/api/auth/login",
                    json={"username": UTILISATEUR, "password": "Faux!"})
    reponse = client.post("/api/auth/login",
                          json={"username": UTILISATEUR, "password": MOT_DE_PASSE})
    assert reponse.status_code == 423, "le compte devrait être verrouillé"

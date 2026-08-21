"""
Longueur des noms entrant dans le moteur.

La distance de Damerau-Levenshtein est **linéaire** en la longueur du nom
criblé — l'autre côté, la fiche listée, est court. Mesuré :

| Longueur du nom | Damerau-Levenshtein | Score de base complet |
|---:|---:|---:|
| 100 | 0,33 ms | 1,19 ms |
| 1 000 | 2,67 ms | 4,30 ms |
| 5 000 | 13,85 ms | 20,31 ms |
| 20 000 | 56,02 ms | 82,55 ms |

Multiplié par les candidats d'un seau — **415 en moyenne** sur la production —
un seul nom de 20 000 caractères vaut **34 secondes** de calcul pour une
requête. Et ce calcul est perdu d'avance : la base stocke `String(1000)`, donc
un nom plus long ne peut de toute façon pas être persisté.

Le plus long nom **réel** du corpus de production mesure **310 caractères**
(un établissement pénitentiaire russe, mesuré sur un échantillon de 12 500
fiches). La borne de 1 000 en laisse plus de trois fois autant, et ISO 20022
plafonne `<Nm>` à 140.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import LONGUEUR_MAX_NOM, app
from fiskr.auth import get_current_user
from fiskr.database import AuditTrail, Alert
from fiskr.transactions import (LONGUEUR_MAX_NOM_PARTIE, _distinct_parties,
                                parse_iso20022_payment)


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": f"len{uuid.uuid4().hex[:4]}", "full_name": "len",
        "role": "admin", "roles": ["admin"]}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_current_user, None)


# ------------------ LA BORNE EST CELLE DE LA BASE ------------------

def test_la_borne_est_celle_que_la_base_sait_stocker():
    """Borner ailleurs qu'à la capacité de la colonne serait arbitraire : ici,
    au-delà de la borne, le nom ne peut PAS être écrit."""
    assert AuditTrail.__table__.c.client_name.type.length == LONGUEUR_MAX_NOM
    assert Alert.__table__.c.client_name.type.length == LONGUEUR_MAX_NOM
    assert Alert.__table__.c.watchlist_name.type.length == LONGUEUR_MAX_NOM


# ------------------ CRIBLAGE UNITAIRE ------------------

def test_un_nom_de_longueur_reelle_passe(client):
    """310 caractères : le plus long nom réel du corpus de production."""
    reponse = client.post("/api/screen", json={
        "client_id": "C-LONG", "client_type": "PM",
        "client_company_name": "A" * 310})
    assert reponse.status_code == 200, reponse.text


def test_un_nom_a_la_borne_passe(client):
    reponse = client.post("/api/screen", json={
        "client_id": "C-LONG", "client_type": "PM",
        "client_company_name": "A" * LONGUEUR_MAX_NOM})
    assert reponse.status_code == 200, reponse.text


@pytest.mark.parametrize("champ", [
    "client_first_name", "client_last_name", "client_maiden_name",
    "client_company_name",
])
def test_un_nom_au_dela_de_la_borne_est_refuse(client, champ):
    reponse = client.post("/api/screen", json={
        "client_id": "C-LONG", "client_type": "PP",
        champ: "A" * (LONGUEUR_MAX_NOM + 1)})
    assert reponse.status_code == 422, reponse.text


def test_le_refus_arrive_avant_le_criblage(client):
    """Un 422 de validation coûte zéro calcul : le nom n'atteint jamais le
    moteur. C'est tout l'intérêt de borner au bord plutôt qu'au centre."""
    import time
    depart = time.perf_counter()
    reponse = client.post("/api/screen", json={
        "client_id": "C-LONG", "client_type": "PM",
        "client_company_name": "A" * 200_000})
    duree = time.perf_counter() - depart
    assert reponse.status_code == 422
    assert duree < 2.0, f"{duree:.1f} s pour refuser : le moteur a travaillé"


# ------------------ FILTRAGE TRANSACTIONNEL ------------------

_MSG = ('<Document><CstmrCdtTrfInitn><PmtInf><Dbtr><Nm>{nom}</Nm></Dbtr>'
        '<CdtTrfTxInf/></PmtInf></CstmrCdtTrfInitn></Document>')


def test_le_nom_d_une_partie_est_borne_pas_rejete():
    """
    Un message de paiement n'est pas refusé pour un nom trop long : le refuser
    le laisserait NON CRIBLÉ, ce qui est pire. Le nom est ramené à ce que la
    base sait stocker — sept fois le plafond de la norme ISO 20022.
    """
    parsed = parse_iso20022_payment(_MSG.format(nom="A" * 100_000).encode())
    parties = _distinct_parties(parsed)
    assert len(parties) == 1
    assert len(parties[0]["name"]) == LONGUEUR_MAX_NOM_PARTIE


def test_un_nom_de_partie_normal_n_est_pas_touche():
    nom = "FEDERAL GOVERNMENT INSTITUTION PRETRIAL DETENTION CENTER NO 1"
    parsed = parse_iso20022_payment(_MSG.format(nom=nom).encode())
    assert _distinct_parties(parsed)[0]["name"] == nom


def test_le_nom_d_un_agent_bancaire_est_borne_aussi():
    message = ('<Document><FIToFICstmrCdtTrf><CdtTrfTxInf><DbtrAgt><FinInstnId>'
               f'<Nm>{"B" * 50_000}</Nm></FinInstnId></DbtrAgt>'
               '</CdtTrfTxInf></FIToFICstmrCdtTrf></Document>')
    parties = _distinct_parties(parse_iso20022_payment(message.encode()))
    assert len(parties) == 1
    assert len(parties[0]["name"]) == LONGUEUR_MAX_NOM_PARTIE


def test_les_deux_bornes_sont_alignees():
    """Deux canaux, une seule limite : sinon un nom passerait par l'un et pas
    par l'autre, et les deux files d'alertes divergeraient."""
    assert LONGUEUR_MAX_NOM_PARTIE == LONGUEUR_MAX_NOM

"""
Alias du client : la moitié manquante de la correspondance par alias.

État constaté avant ce correctif :

    alias du LISTÉ   -> blocking OUI, scoring OUI   (fonctionnait déjà)
    alias du CLIENT  -> blocking NON, scoring OUI   (mais le champ n'existait pas)

Le moteur savait cribler les alias d'un profil client — `fiskr/scoring.py` les
lit — mais AUCUNE porte d'entrée ne permettait d'en porter : ni colonne au
référentiel, ni champ d'API, ni colonne d'import. La branche était donc morte.

Deux choses comptent ici, et la seconde plus que la première :

1. le champ existe et traverse toutes les portes (import CSV, campagne batch,
   appel direct, re-criblage) ;
2. l'alias produit une CLÉ DE BLOCKING. Sans elle, la paire ne serait jamais
   candidate et le scoring ne la verrait pas — l'alias serait accepté en base
   et ignoré au criblage, ce qui est pire que de ne pas l'accepter.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr import capabilities as caps
from fiskr.api import app, _client_alias_list, _batch_row_to_profile
from fiskr.auth import get_current_user
from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys
from fiskr.config import config
from fiskr.database import get_db, ClientEntity, Snapshot
from fiskr.scoring import match_entities

TAG = uuid.uuid4().hex[:6].upper()


def _cfg(layout=("ENTITY_TYPE", "PHONETIC_FIRST")):
    return {"blocking": {"custom_key_layout": list(layout), "channel": "SCREENING"}}


# ------------------ NORMALISATION DES ENTRÉES ------------------

@pytest.mark.parametrize("brut,attendu", [
    ("Vladimir Poutine; Vova", ["Vladimir Poutine", "Vova"]),
    ("Vladimir Poutine, Vova", ["Vladimir Poutine", "Vova"]),
    (["Vladimir Poutine", " Vova "], ["Vladimir Poutine", "Vova"]),
    ("", None),
    (None, None),
    ("   ;  ", None),
])
def test_aliases_are_accepted_from_every_shape(brut, attendu):
    """CSV (séparé par « ; » ou « , »), liste JSON d'API : une seule règle."""
    assert _client_alias_list({"client_aliases": brut}) == attendu


def test_empty_column_is_indistinguishable_from_no_column():
    """Une colonne vide ne doit pas créer une liste vide en base."""
    assert _client_alias_list({}) is None
    assert _client_alias_list({"aliases": ""}) is None


def test_batch_csv_column_becomes_a_profile_field():
    """La campagne batch doit porter les alias comme les autres colonnes."""
    profil = _batch_row_to_profile({
        "client_id": "C1", "type": "PP", "first_name": "Vova",
        "last_name": "Kremlin", "aliases": "Vladimir Poutine; V. Poutine"})
    assert profil["client_aliases"] == ["Vladimir Poutine", "V. Poutine"]


# ------------------ BLOCKING : SANS LUI, RIEN NE SE RENCONTRE ------------------

def test_a_client_alias_produces_a_blocking_key():
    """LE point : l'alias doit produire une clé, sinon la paire n'est jamais
    candidate et le scoring — qui, lui, saurait la traiter — ne la voit pas."""
    client = {"client_type": "PP", "client_id": "C1",
              "client_first_name": "Vova", "client_last_name": "Kremlin",
              "client_aliases": ["Vladimir Poutine"],
              "client_countries": {}}
    liste = {"entity_type": "I", "primary_name": "Vladimir Poutine",
             "aliases": {"high_priority": [], "low_priority": []}, "countries": {}}

    communes = (lookup_blocking_keys(client, _cfg())
                & generate_blocking_keys(liste, _cfg()))
    assert communes, (
        "l'alias du client ne produit aucune clé commune : le listé reste "
        "inatteignable, et l'alias accepté en base serait ignoré au criblage")


def test_without_the_alias_the_pair_is_not_candidate():
    """Contrôle : c'est bien l'alias qui fait la rencontre, pas autre chose."""
    sans = {"client_type": "PP", "client_id": "C1",
            "client_first_name": "Vova", "client_last_name": "Kremlin",
            "client_countries": {}}
    liste = {"entity_type": "I", "primary_name": "Vladimir Poutine",
             "aliases": {"high_priority": [], "low_priority": []}, "countries": {}}
    assert not (lookup_blocking_keys(sans, _cfg())
                & generate_blocking_keys(liste, _cfg()))


def test_cutting_the_capability_cuts_blocking_and_scoring_together():
    """L'index et la sonde doivent rester cohérents : la même capacité commande
    les deux, sinon on indexerait sur un alias qu'on ne crible pas."""
    client = {"client_type": "PP", "client_id": "C1",
              "client_first_name": "Vova", "client_last_name": "Kremlin",
              "client_aliases": ["Vladimir Poutine"], "client_countries": {}}
    actives = set(caps.defaults_for_channel(caps.CHANNEL_SCREENING))
    actives = {c for c, on in caps.defaults_for_channel(
        caps.CHANNEL_SCREENING).items() if on}
    actives.discard(caps.CAP_NAMES_ALIASES_CLIENT)
    with caps.use_context(caps.CHANNEL_SCREENING, actives):
        cles = generate_blocking_keys(client, _cfg())
    liste = {"entity_type": "I", "primary_name": "Vladimir Poutine",
             "aliases": {"high_priority": [], "low_priority": []}, "countries": {}}
    assert not (cles & generate_blocking_keys(liste, _cfg()))


# ------------------ SCORING ------------------

def test_the_client_alias_raises_the_alert():
    """Bout en bout du moteur : un client dont l'ALIAS est le nom du listé."""
    client = {"client_type": "PP", "client_first_name": "Vova",
              "client_last_name": "Kremlin",
              "client_aliases": ["Vladimir Poutine"],
              "client_dob": None, "client_gender": "U",
              "client_countries": {"nationality": [], "residence": [],
                                   "birth_country": [], "registration_country": []}}
    liste = {"entity_type": "I", "primary_name": "Vladimir Poutine",
             "aliases": {"high_priority": [], "low_priority": []},
             "dates_of_birth": [], "gender": None,
             "countries": {"citizenship": [], "residence": [],
                           "birth_country": [], "jurisdiction_country": []}}
    r = match_entities(client, liste, config)
    assert r["status"] == "ALERT", r["final_score"]
    assert r["best_client_name"] == "Vladimir Poutine", (
        "le nom retenu doit être l'alias, pas le nom principal")


# ------------------ RÉFÉRENTIEL : LA COLONNE EXISTE ET SE REMPLIT ------------------

def test_csv_import_stores_the_aliases(tmp_path):
    """Import du référentiel client : la colonne arrive en base."""
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "al", "full_name": "al", "role": "admin",
        "roles": ["admin"]}
    db = next(get_db())
    try:
        with TestClient(app) as c:
            corps = ("client_id,client_type,client_first_name,client_last_name,"
                     "client_aliases\n"
                     f"CLI-{TAG},PP,Vova,Kremlin-{TAG},Vladimir Poutine; V. Poutine\n")
            r = c.post("/api/ingest", data={"file_type": "CLIENT_BASE"},
                       files={"file": (f"cli_{TAG}.csv", corps, "text/csv")})
            assert r.status_code == 200, r.text
        ligne = db.query(ClientEntity).filter(
            ClientEntity.client_id == f"CLI-{TAG}").first()
        assert ligne is not None
        assert ligne.client_aliases == ["Vladimir Poutine", "V. Poutine"]
    finally:
        app.dependency_overrides.clear()
        db.query(ClientEntity).filter(
            ClientEntity.client_id == f"CLI-{TAG}").delete(synchronize_session=False)
        db.query(Snapshot).filter(
            Snapshot.file_name.like(f"%{TAG}%")).delete(synchronize_session=False)
        db.commit()
        db.close()

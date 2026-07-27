"""
Joker « pays inconnu » à l'interrogation de l'index de criblage.

Le problème corrigé : l'index est bâti sur les clés des fiches LISTÉES, le
client l'interroge avec les siennes. `COUNTRY_ISO` étant une composante de la
clé, une fiche listée dont la source ne publie aucun pays tombe dans la
partition « XX », que ne rejoint AUCUN client ayant un pays. Elle était donc
structurellement inatteignable — et ce n'est pas un cas de bord : les listes
d'alerte de régulateurs ne publient presque jamais de pays.

La correction se pose du côté REQUÊTE : le client interroge en plus la variante
« pays inconnu » de ses propres clés. Les deux propriétés qui rendent la chose
acceptable sont testées ici — c'est strictement additif (aucune alerte ne peut
être perdue) et le partitionnement est préservé (une fiche qui porte un pays
reste atteinte par les seuls clients de ce pays).
"""
import pytest

from fiskr.blocking import generate_blocking_keys, lookup_blocking_keys


LAYOUT_AVEC_PAYS = {"blocking": {"custom_key_layout":
                                 ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"]}}
LAYOUT_SANS_PAYS = {"blocking": {"custom_key_layout": ["ENTITY_TYPE", "PHONETIC_FIRST"]}}
JOKER_DESACTIVE = {"blocking": {"custom_key_layout":
                                ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"],
                                "country_wildcard": False}}


def _listee(nom="GOLDEN DRAGON CAPITAL LTD", pays=None, etype="E"):
    return {
        "entity_id": "E1", "entity_type": etype, "primary_name": nom,
        "individual_name_parsed": {"first_name": "", "last_name": "", "maiden_name": ""},
        "aliases": {"high_priority": [], "low_priority": []},
        "countries": {"citizenship": [], "residence": [], "birth_country": [],
                      "jurisdiction_country": list(pays or [])},
    }


def _client(nom="GOLDEN DRAGON CAPITAL LTD", pays=("HK",)):
    return {
        "client_id": "C1", "client_type": "PM", "client_company_name": nom,
        "client_countries": {"nationality": [], "residence": [], "birth_country": [],
                             "registration_country": list(pays)},
    }


def _atteint(listee, client, config) -> bool:
    """L'index est bâti sur les clés du listé, la requête sur celles du client."""
    return bool(set(generate_blocking_keys(listee, config))
                & set(lookup_blocking_keys(client, config)))


# ------------------ LE TROU, ET SA FERMETURE ------------------

def test_a_listed_record_without_a_country_is_now_reachable():
    sans_pays = _listee(pays=[])
    client = _client(pays=["HK"])
    # Preuve du trou : avec les seules clés propres du client, aucune rencontre
    assert not (set(generate_blocking_keys(sans_pays, LAYOUT_AVEC_PAYS))
                & set(generate_blocking_keys(client, LAYOUT_AVEC_PAYS)))
    # ...et le joker la rend possible
    assert _atteint(sans_pays, client, LAYOUT_AVEC_PAYS)


def test_the_partition_is_preserved_for_records_that_do_carry_a_country():
    """Le joker ne doit pas dissoudre le partitionnement : une fiche qui PORTE
    un pays reste atteinte par les seuls clients de ce pays."""
    listee_hk = _listee(pays=["HK"])
    assert _atteint(listee_hk, _client(pays=["HK"]), LAYOUT_AVEC_PAYS)
    assert not _atteint(listee_hk, _client(pays=["FR"]), LAYOUT_AVEC_PAYS)


def test_lookup_keys_are_a_superset_so_no_alert_can_be_lost():
    """Strictement additif : toute clé interrogée hier l'est encore."""
    for pays in ([], ["HK"], ["HK", "CN"]):
        client = _client(pays=pays or ["FR"])
        propres = set(generate_blocking_keys(client, LAYOUT_AVEC_PAYS))
        requete = set(lookup_blocking_keys(client, LAYOUT_AVEC_PAYS))
        assert propres <= requete, pays


def test_a_client_without_a_country_still_reaches_country_less_records():
    """Le cas symétrique — référentiel client incomplet — marchait déjà et ne
    doit pas régresser."""
    assert _atteint(_listee(pays=[]), _client(pays=[]), LAYOUT_AVEC_PAYS)


# ------------------ PORTÉE ET INTERRUPTEUR ------------------

def test_the_wildcard_is_a_no_op_when_country_is_not_in_the_layout():
    client = _client()
    assert (set(lookup_blocking_keys(client, LAYOUT_SANS_PAYS))
            == set(generate_blocking_keys(client, LAYOUT_SANS_PAYS)))


def test_the_wildcard_can_be_switched_off():
    """C'est un paramètre de criblage : il se règle, il ne s'impose pas."""
    sans_pays, client = _listee(pays=[]), _client(pays=["HK"])
    assert _atteint(sans_pays, client, LAYOUT_AVEC_PAYS)
    assert not _atteint(sans_pays, client, JOKER_DESACTIVE)


def test_the_wildcard_is_enabled_by_default_in_the_shipped_config():
    from fiskr.config import config
    assert (config.get("blocking", {}) or {}).get("country_wildcard") is True


# ------------------ COÛT : BORNÉ PAR LES FICHES SANS PAYS ------------------

def test_the_extra_candidates_come_only_from_country_less_records():
    """
    Le surcoût annoncé n'est pas une intuition : les candidats ajoutés sont
    exactement les fiches de la partition « pays inconnu ». Une base dont
    toutes les fiches portent un pays ne paie rien.
    """
    avec_pays = [_listee(nom=f"ALPHA TRADING {i} LTD", pays=["RU"]) for i in range(20)]
    sans_pays = [dict(_listee(nom=f"ALPHA TRADING {i} LTD", pays=[]),
                      entity_id=f"S{i}") for i in range(5)]
    for i, e in enumerate(avec_pays):
        e["entity_id"] = f"A{i}"

    index = {}
    for e in avec_pays + sans_pays:
        for k in generate_blocking_keys(e, LAYOUT_AVEC_PAYS):
            index.setdefault(k, []).append(e)

    client = _client(nom="ALPHA TRADING 3 LTD", pays=["FR"])
    avant = {e["entity_id"] for k in generate_blocking_keys(client, LAYOUT_AVEC_PAYS)
             for e in index.get(k, [])}
    apres = {e["entity_id"] for k in lookup_blocking_keys(client, LAYOUT_AVEC_PAYS)
             for e in index.get(k, [])}
    gagnes = apres - avant
    # Rien de ce qui porte un pays n'entre par le joker
    assert all(g.startswith("S") for g in gagnes), gagnes
    assert gagnes, "le joker doit bien ramener les fiches sans pays"

    # Un univers entièrement renseigné ne coûte rien
    index_complet = {}
    for e in avec_pays:
        for k in generate_blocking_keys(e, LAYOUT_AVEC_PAYS):
            index_complet.setdefault(k, []).append(e)
    a = {e["entity_id"] for k in generate_blocking_keys(client, LAYOUT_AVEC_PAYS)
         for e in index_complet.get(k, [])}
    b = {e["entity_id"] for k in lookup_blocking_keys(client, LAYOUT_AVEC_PAYS)
         for e in index_complet.get(k, [])}
    assert a == b


# ------------------ APPLICATION AU CRIBLAGE REEL ------------------

@pytest.fixture
def api():
    from fastapi.testclient import TestClient
    from fiskr.api import app
    from fiskr.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin_wc", "full_name": "admin_wc",
        "role": "admin", "roles": ["admin"],
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_end_to_end_a_country_less_alert_record_now_raises_an_alert(api):
    """
    Le cas qui a révélé le défaut : une liste d'alerte sans géographie, criblée
    par un client qui, lui, a un pays.
    """
    import uuid
    marker = uuid.uuid4().hex[:6].upper()
    page = (
        "<html><body><table>"
        "<tr><th>Name</th><th>Type</th><th>Date of publication</th></tr>"
        f"<tr><td>Silent Harbour {marker} Ltd</td><td>Entity</td><td>2026-03-01</td></tr>"
        "</table></body></html>"
    )
    response = api.post(
        "/api/ingest",
        data={"file_type": "WATCHLIST_HK_SFC"},
        files={"file": (f"sfc_{marker}.html", page, "text/html")},
    )
    assert response.status_code == 200, response.text

    result = api.post("/api/screen", json={
        "client_id": f"test_wc_{marker}",
        "client_type": "PM",
        "client_company_name": f"Silent Harbour {marker} Ltd",
        # Client français : sans le joker, il ne rencontrerait jamais une fiche
        # dont la juridiction est HK... et sans juridiction du tout, personne.
        "client_countries": {"nationality": [], "residence": [],
                             "birth_country": [], "registration_country": ["FR"]},
        "screening_lists": ["WATCHLIST_HK_SFC"],
    })
    assert result.status_code == 200, result.text
    best = result.json()["best_match"]
    assert best is not None, result.json()
    assert best["status"] == "ALERT"
    assert best["watchlist_entity"]["_list_type"] == "WATCHLIST_HK_SFC"

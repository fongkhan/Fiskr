"""
Re-criblage parallèle : ce qui traverse entre les processus.

Depuis que **toutes** les correspondances remontent au parent, ce que le pool
renvoie a changé d'échelle. Mesuré sur un résultat de correspondance réel :

| Ce qui traverse | Par correspondance | 500 clients × 100 hits | × 2 976 hits |
|---|---:|---:|---:|
| avec la fiche listée jointe | 863 o | 41 Mo | **1 225 Mo** |
| avec l'identifiant seul | 355 o | 17 Mo | 504 Mo |

La fiche n'a aucune raison de traverser : le parent a **le même index en
mémoire**, hérité par les enfants au `fork`. Il la rattache par identifiant —
c'est le même objet, pas une relecture qui pourrait différer.

Ces tests vérifient que le rattachement est exact (mêmes fiches, même ordre,
mêmes scores qu'un calcul séquentiel) et qu'une correspondance orpheline tombe
bruyamment plutôt que de produire une ligne d'audit muette.
"""
import logging

import pytest

from fiskr import screenpool


def _fiche(n, nom="MOHAMMED ALI", list_type="WATCHLIST_PEP"):
    return {"id": 5000 + n, "entity_id": f"PL-{n}", "entity_type": "I",
            "primary_name": nom,
            "aliases": {"high_priority": [], "low_priority": []},
            "dates_of_birth": [], "gender": "U",
            "countries": {"citizenship": [], "residence": [],
                          "birth_country": [], "jurisdiction_country": []},
            "_list_type": list_type}


CLIENT = {"client_id": "C-1", "client_type": "PP",
          "client_first_name": "MOHAMMED", "client_last_name": "ALI",
          "client_gender": "U"}


def _cfg():
    from fiskr.config import config
    return dict(config)


def _index(fiches, client=None):
    """Index de blocking RÉEL : les vraies clés du client, toutes pointant sur
    le même jeu de fiches. Un dictionnaire bidon ne serait jamais interrogé —
    l'enfant passe par `lookup_blocking_keys`."""
    from fiskr.blocking import lookup_blocking_keys
    from fiskr.settings import blocking_config_for
    layout = (_cfg().get("blocking", {}) or {}).get(
        "custom_key_layout", ["COUNTRY_ISO", "ENTITY_TYPE", "PHONETIC_FIRST"])
    cles = lookup_blocking_keys(client or CLIENT, blocking_config_for(layout))
    return {cle: list(fiches) for cle in cles}


def _chunk_orphelin(bounds):
    """Remplaçant de `_match_chunk` renvoyant une correspondance dont la fiche
    n'existe pas dans l'index du parent. Défini au niveau du module : le pool
    pickle la référence de fonction."""
    return {"_chunk_index": 0, "hits": [
        (0, [{"status": "ALERT", "final_score": 90.0, "_entity_id": "INEXISTANT"}])]}


def test_l_enfant_ne_renvoie_pas_les_fiches_listees():
    """Le point de la mesure : ce sont les fiches qui pèsent, et elles sont
    déjà des deux côtés du fork."""
    import inspect
    code = inspect.getsource(screenpool._match_chunk)
    assert '"_entity_id"' in code
    assert 'score["watchlist_entity"] = ent' not in code


def test_le_parent_rattache_la_meme_fiche_que_celle_qui_a_servi_au_calcul():
    """Pas une relecture : le même objet. Une fiche relue pourrait différer de
    celle qui a produit le score, et la ligne d'audit mentirait."""
    import inspect
    code = inspect.getsource(screenpool.parallel_match)
    assert "par_id" in code and "watchlist_entity" in code
    # Le rattachement part de l'index du parent, pas d'une requete
    assert "for bucket in index.values()" in code


def test_le_re_criblage_parallele_rend_le_meme_resultat_que_le_sequentiel(monkeypatch):
    """La garantie qui compte : deux chemins, un résultat. Le pool est forcé à
    un seul processus pour rester déterministe dans un test."""
    fiches = [_fiche(i) for i in range(6)]
    index = _index(fiches)
    clients = [dict(CLIENT, client_id=f"C-{i}") for i in range(4)]

    obtenu = screenpool.parallel_match(clients, index, _cfg(), processes=1)

    # Référence : le même calcul, en séquentiel, dans ce processus
    from fiskr.scoring import match_entities
    attendu = []
    for i, client in enumerate(clients):
        trouvees = []
        for ent in fiches:
            score = match_entities(client, ent, _cfg())
            if score.get("status") == "ALERT":
                score["watchlist_entity"] = ent
                trouvees.append(score)
        if trouvees:
            trouvees.sort(key=lambda s: -s["final_score"])
            attendu.append((i, trouvees))

    assert [i for i, _ in obtenu] == [i for i, _ in attendu]
    for (i_o, hits_o), (i_a, hits_a) in zip(obtenu, attendu):
        assert i_o == i_a
        assert len(hits_o) == len(hits_a)
        for ho, ha in zip(hits_o, hits_a):
            assert ho["final_score"] == ha["final_score"]
            assert ho["watchlist_entity"]["entity_id"] == ha["watchlist_entity"]["entity_id"]
            # Le MÊME objet, rattaché depuis l'index du parent
            assert any(ho["watchlist_entity"] is f for bucket in index.values()
                       for f in bucket)


def test_toutes_les_correspondances_remontent_pas_seulement_la_meilleure():
    fiches = [_fiche(i) for i in range(6)]
    resultat = screenpool.parallel_match([CLIENT], _index(fiches), _cfg(), processes=1)
    assert len(resultat) == 1
    _, trouvees = resultat[0]
    assert len(trouvees) == 6, "une seule correspondance remontée : le re-criblage perdrait les autres"
    scores = [t["final_score"] for t in trouvees]
    assert scores == sorted(scores, reverse=True)


def test_une_correspondance_orpheline_tombe_bruyamment(monkeypatch, caplog):
    """Une correspondance sans sa fiche produirait une ligne d'audit muette.
    Elle doit disparaître avec un message, pas en silence."""
    fiches = [_fiche(i) for i in range(3)]
    monkeypatch.setattr(screenpool, "_match_chunk", _chunk_orphelin)
    with caplog.at_level(logging.ERROR, logger="fiskr.screenpool"):
        resultat = screenpool.parallel_match([CLIENT], _index(fiches), _cfg(), processes=1)
    assert resultat == [], "une correspondance sans fiche ne doit pas être persistée"
    assert any("INEXISTANT" in r.message for r in caplog.records), \
        "la perte doit être journalisée"


def test_un_client_sans_correspondance_ne_remonte_pas():
    fiches = [_fiche(i, nom="ZZZZZZ QQQQQQ") for i in range(3)]
    assert screenpool.parallel_match([CLIENT], _index(fiches), _cfg(), processes=1) == []

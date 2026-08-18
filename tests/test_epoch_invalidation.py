"""
Toute modification du référentiel de production doit prévenir les AUTRES processus.

Le cache moteur vit dans la mémoire de chaque processus. L'époque
(`review.watchlist_epoch`, une simple ligne en base) est le SEUL canal
d'invalidation entre processus : le démon travailleur ne peut pas toucher la
mémoire d'un processus web, il ne fait que constater un entier qui a changé.

Quatre endpoints modifiaient le référentiel de production en ne rechargeant que
LEUR cache : ajout manuel d'un listé, ajout en lot, correction d'une fiche, et
purge de rétention. Conséquence en production — où Passenger lance plusieurs
processus web et où le démon porte les campagnes batch et le re-criblage :

    un listé saisi à la main n'était pas criblé par les campagnes du démon,
    tant qu'aucune synchronisation ne venait bousculer l'époque.

C'est un manque de criblage, pas un simple retard d'affichage. Ces tests
verrouillent l'incrément pour chacun de ces chemins.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, WatchlistEntity
from fiskr.settings import watchlist_epoch

TAG = uuid.uuid4().hex[:6].upper()


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "epoque", "full_name": "Époque", "role": "admin",
        "roles": ["admin"]}
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    db = next(get_db())
    try:
        db.query(WatchlistEntity).filter(
            WatchlistEntity.entity_id.like(f"%{TAG}%")).delete(synchronize_session=False)
        db.query(WatchlistEntity).filter(
            WatchlistEntity.primary_name.like(f"%{TAG}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _epoque():
    db = next(get_db())
    try:
        return watchlist_epoch(db)
    finally:
        db.close()


def test_manual_entity_bumps_the_epoch(client):
    """Un listé ajouté à la main est un acte de décision humaine : il entre
    en production immédiatement, donc TOUS les processus doivent le voir."""
    avant = _epoque()
    r = client.post("/api/watchlist/entity", json={
        "entity_id": f"MAN-{TAG}-1", "entity_type": "I",
        "primary_name": f"Ajout Manuel {TAG}"})
    assert r.status_code == 200, r.text
    assert _epoque() > avant, (
        "époque inchangée : le démon et les autres processus web ne "
        "cribleront pas contre ce listé")


def test_batch_manual_entities_bump_the_epoch(client):
    """Même garantie pour l'ajout en lot."""
    avant = _epoque()
    r = client.post("/api/watchlist/entities/batch", json={"entities": [
        {"entity_id": f"MAN-{TAG}-B1", "entity_type": "I",
         "primary_name": f"Lot Un {TAG}"},
        {"entity_id": f"MAN-{TAG}-B2", "entity_type": "I",
         "primary_name": f"Lot Deux {TAG}"}]})
    assert r.status_code in (200, 201), r.text
    assert _epoque() > avant


def test_entity_correction_bumps_the_epoch(client):
    """Corriger une fiche en production change ce contre quoi on crible :
    une correction invisible du démon laisserait celui-ci cribler l'ancienne
    orthographe."""
    creation = client.post("/api/watchlist/entity", json={
        "entity_id": f"MAN-{TAG}-C", "entity_type": "I",
        "primary_name": f"Nom Avant {TAG}"})
    assert creation.status_code == 200, creation.text

    # L'identifiant retenu est celui que rend l'API : le moteur peut le
    # normaliser ou le prefixer, le deviner rendrait le test fragile.
    entity_id = creation.json()["entity_id"]
    db = next(get_db())
    try:
        pk = db.query(WatchlistEntity.id).filter(
            WatchlistEntity.entity_id == entity_id).scalar()
    finally:
        db.close()
    assert pk, f"fiche introuvable pour {entity_id}"

    avant = _epoque()
    r = client.patch(f"/api/watchlist/entity/{pk}",
                     json={"primary_name": f"Nom Apres {TAG}"})
    assert r.status_code == 200, r.text
    assert _epoque() > avant


def test_every_production_mutation_goes_through_the_shared_channel():
    """Garde-fou de régression, sur l'arbre syntaxique : aucune route qui
    modifie le référentiel ne doit se contenter de recharger SON cache.

    `load_watchlist_cache` recharge la mémoire locale ; seul
    `_refresh_production_cache` incrémente l'époque et prévient les autres
    processus. Contrôle sur les fonctions de route qui écrivent."""
    import ast
    import inspect as _inspect
    import fiskr.api as api_mod

    arbre = ast.parse(_inspect.getsource(api_mod).lstrip())
    fautifs = []
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        ecrit = any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and d.func.attr in ("post", "put", "patch", "delete")
            for d in noeud.decorator_list)
        if not ecrit:
            continue
        appels = {n.func.id for n in ast.walk(noeud)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "load_watchlist_cache" in appels and "_refresh_production_cache" not in appels:
            fautifs.append(noeud.name)
    assert not fautifs, (
        f"routes qui rechargent leur cache local sans prévenir les autres "
        f"processus : {sorted(fautifs)}")

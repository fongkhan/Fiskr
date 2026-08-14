"""
Processus web sans lifespan : le cache moteur y demarre VIDE.

Sous Passenger, l'application est servie par `a2wsgi.ASGIMiddleware`, qui ne
construit qu'un scope `http` par requete et n'implemente pas le protocole ASGI
`lifespan`. Le demarrage FastAPI — donc `load_watchlist_cache()` — ne tourne
jamais dans un processus web. Constate en production :

- `GET /api/watchlist/summary` renvoyait `hash: "N/A"`, `count: 0` alors que la
  base portait plus de trente snapshots WATCHLIST_* en production (le badge
  « Hash Actif » de la barre laterale affichait « N/A ») ;
- `GET /api/search/quick` batissait le cache DANS la requete : mesure a plus de
  100 s sans reponse, la palette Ctrl+K etait hors service ;
- et surtout `POST /api/screen` lisait un `watchlist_index` vide sans le dire :
  aucun candidat, donc NO_MATCH sur tout le monde. Un listé rendu non listé,
  sans erreur, sans trace.

Ces tests reproduisent la condition (globaux vides) et verrouillent le
comportement attendu de chaque endpoint dans cet etat.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app, load_watchlist_cache
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, WatchlistEntity

UID = uuid.uuid4().hex[:8].upper()
SNAP = f"test-web-{UID.lower()}"
HASH = uuid.uuid4().hex


@pytest.fixture()
def ctx():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "test_web_admin", "full_name": "test_web_admin",
        "role": "admin", "roles": ["admin"],
    }
    db = next(get_db())
    db.add(Snapshot(snapshot_id=SNAP, file_type="WATCHLIST_DGT",
                    file_name=f"{SNAP}.json", file_hash=HASH,
                    record_count=1, status="READY"))
    db.add(WatchlistEntity(
        snapshot_id=SNAP, entity_id=f"WEB1-{UID}", entity_type="E",
        primary_name=f"Silent Harbour {UID} Ltd",
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=[], is_deceased=False,
        countries={"citizenship": [], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=f"chk-web-{UID}",
    ))
    db.commit()
    load_watchlist_cache(db)
    yield {"db": db, "client": TestClient(app)}
    app.dependency_overrides.pop(get_current_user, None)
    try:
        db.query(WatchlistEntity).filter(
            WatchlistEntity.snapshot_id == SNAP).delete(synchronize_session=False)
        db.query(Snapshot).filter(
            Snapshot.snapshot_id == SNAP).delete(synchronize_session=False)
        db.commit()
        load_watchlist_cache(db)
    finally:
        db.close()


@pytest.fixture()
def cache_vide(monkeypatch):
    """Un processus web tel que Passenger le sert : aucun cache charge."""
    monkeypatch.setattr(api, "watchlist_store", [])
    monkeypatch.setattr(api, "watchlist_index", {})
    monkeypatch.setattr(api, "watchlist_search_index", [])
    monkeypatch.setattr(api, "watchlist_hash", "N/A")
    monkeypatch.setattr(api, "_last_epoch_seen", None)


# ------------------ BADGE « HASH ACTIF » ------------------

def test_summary_reads_the_database_not_the_process_memory(ctx, cache_vide):
    """Le hash actif est une propriete du snapshot en production — « la version
    exacte du referentiel, la reference a citer dans un dossier » — pas de
    l'etat d'un processus. Cache vide : le badge doit tout de meme etre juste."""
    data = ctx["client"].get("/api/watchlist/summary").json()
    assert data["hash"] != "N/A", "le badge affichait la valeur initiale du module"
    assert data["hash"], "un hash de production doit etre rendu"
    assert data["count"] > 0, "le referentiel n'est pas vide en base"


def test_summary_hash_is_the_latest_ready_snapshot(ctx, cache_vide):
    """Meme regle que le cache moteur : le snapshot READY le plus recent."""
    db = ctx["db"]
    attendu = db.query(Snapshot).filter(
        Snapshot.file_type.in_(api.WATCHLIST_FILE_TYPES),
        Snapshot.status == "READY",
    ).order_by(Snapshot.uploaded_at.desc()).first()
    data = ctx["client"].get("/api/watchlist/summary").json()
    assert data["hash"] == attendu.file_hash


def test_summary_never_invents_a_hash_without_production(ctx, cache_vide, monkeypatch):
    """Sans referentiel en production, le badge affiche son etat vide (le
    frontal rend « NONE ») — jamais un hash fabrique."""
    monkeypatch.setattr(api, "WATCHLIST_FILE_TYPES", ["WATCHLIST_TYPE_INEXISTANT"])
    data = ctx["client"].get("/api/watchlist/summary").json()
    assert data["hash"] is None and data["count"] == 0


# ------------------ PALETTE Ctrl+K ------------------

def test_quick_search_answers_without_building_the_cache(ctx, cache_vide, monkeypatch):
    """La palette ne doit JAMAIS batir le cache dans la requete : c'est ce qui
    la laissait sans reponse en production (>100 s). Elle repond depuis la
    base."""
    def _interdit(_db):
        raise AssertionError("le cache moteur ne doit pas etre construit ici")
    monkeypatch.setattr(api, "load_watchlist_cache", _interdit)

    data = ctx["client"].get(f"/api/search/quick?q=Silent Harbour {UID}").json()
    items = data["watchlist"]["items"]
    assert any(i["entity_id"] == f"WEB1-{UID}" for i in items), data
    assert data["watchlist"]["total"] >= 1


def test_db_fallback_returns_the_same_keys_as_the_memory_path(ctx, cache_vide):
    """Le frontal (modale de details) attend les memes cles quel que soit le
    chemin emprunte."""
    par_base = ctx["client"].get(
        f"/api/search/quick?q=Silent Harbour {UID}").json()["watchlist"]["items"][0]
    load_watchlist_cache(ctx["db"])  # repeuple : chemin memoire
    par_memoire = ctx["client"].get(
        f"/api/search/quick?q=Silent Harbour {UID}").json()["watchlist"]["items"][0]
    manquantes = set(par_memoire) - set(par_base)
    assert not manquantes, f"cles absentes du repli base : {sorted(manquantes)}"
    assert par_base["list_type"] == par_memoire["list_type"] == "WATCHLIST_DGT"
    assert par_base["entity_id"] == par_memoire["entity_id"]


# ------------------ CRIBLAGE : LE POINT CRITIQUE ------------------

def test_screening_never_clears_a_listed_party_on_an_empty_cache(ctx, cache_vide):
    """LE defaut a ne jamais laisser revenir : sur un index vide, le criblage
    ne trouvait aucun candidat et rendait NO_MATCH — un listé declare non
    listé, sans erreur ni trace. L'endpoint doit garantir son cache."""
    reponse = ctx["client"].post("/api/screen", json={
        "client_id": f"test_web_{UID}",
        "client_type": "PM",
        "client_company_name": f"Silent Harbour {UID} Ltd",
        "client_countries": {"nationality": [], "residence": [],
                             "birth_country": [], "registration_country": ["FR"]},
        "screening_lists": ["WATCHLIST_DGT"],
    })
    assert reponse.status_code == 200, reponse.text
    best = reponse.json()["best_match"]
    assert best is not None, "cache vide : le listé etait rendu invisible"
    assert best["watchlist_entity"]["entity_id"] == f"WEB1-{UID}"


def test_screening_endpoints_all_guarantee_their_cache():
    """Garde-fou de regression : tout endpoint qui crible doit s'assurer du
    cache. Controle sur l'arbre syntaxique — une mention en commentaire ne
    compte pas, seul un APPEL compte."""
    import ast
    import inspect as _inspect

    arbre = ast.parse(_inspect.getsource(api).lstrip())
    routes = {}
    for noeud in ast.walk(arbre):
        if not isinstance(noeud, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        chemins = [
            d.args[0].value for d in noeud.decorator_list
            if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
            and d.args and isinstance(d.args[0], ast.Constant)
            and isinstance(d.args[0].value, str) and d.args[0].value.startswith("/api/")
        ]
        for chemin in chemins:
            routes[chemin] = noeud

    criblants = ["/api/screen", "/api/screen/preview", "/api/transactions/screen"]
    for chemin in criblants:
        noeud = routes.get(chemin)
        assert noeud is not None, f"route {chemin} introuvable"
        appels = {n.func.id for n in ast.walk(noeud)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_ensure_watchlist_cache" in appels, (
            f"{chemin} crible sans garantir son cache : sur un index vide il "
            f"rend NO_MATCH silencieusement")

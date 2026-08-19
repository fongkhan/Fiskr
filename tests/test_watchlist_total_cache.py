"""
Consultation des listes : le COUNT du périmètre, et l'index qui manquait.

Deux mesures sur la production (11,2 M lignes, 895 157 fiches en production) :

* `GET /api/watchlist/db?scope=production` répond en **3,6 à 3,9 s quelle que
  soit la taille de page** — 1 ligne comme 200 (2 ms par ligne au-delà). Le
  coût n'est donc pas le rendu des lignes mais le COUNT du périmètre, refait à
  chaque changement de page ;
* `scope=EXCLUDED` mettait **21 à 35 s pour rendre zéro ligne**. L'index
  partiel existant ne couvre que `excluded IS NOT TRUE` : il n'aide en rien la
  requête symétrique, qui n'avait aucun index et parcourait toute la table.

Le compte de production est désormais mémorisé par **signature de la
production** — époque de la watchlist plus un relevé direct des snapshots
`READY` (nombre, dernier téléversement, somme des compteurs). Ce relevé porte
sur 42 lignes et capte une mise en production **au commit**, sans attendre le
travail de fond qui recharge le cache. Les fiches ne sont jamais mémorisées :
seul le total l'est.

Ce fichier vérifie que le compte reste juste — c'est-à-dire qu'il se
recalcule à chaque événement qui change l'univers de production.
"""
import re
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

import fiskr.api as api_module
from fiskr.api import (app, _WL_TOTAL_CACHE, _production_signature,
                       _production_watchlist_summary,
                       _WL_ROW_SELECT, _watchlist_row_from_tuple,
                       _serialize_watchlist_row, _wl_scope_query)
from fiskr.auth import get_current_user
from fiskr.database import (get_db, Snapshot, WatchlistEntity, _PERFORMANCE_INDEXES)
from fiskr.settings import bump_watchlist_epoch

TAG = uuid.uuid4().hex[:6].upper()
SID = f"total-{TAG}"


def _fiche(n: int) -> WatchlistEntity:
    return WatchlistEntity(snapshot_id=SID, entity_id=f"TC-{TAG}-{n}",
                           entity_type="I", primary_name=f"Total {TAG} {n}",
                           entity_checksum=f"tc-{TAG}-{n}")


@pytest.fixture()
def contexte():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "tc", "full_name": "tc", "role": "admin",
        "roles": ["admin"]}
    db = next(get_db())
    db.add(Snapshot(snapshot_id=SID, file_type="WATCHLIST_EU",
                    file_name=f"{SID}.csv", file_hash=uuid.uuid4().hex,
                    record_count=2, status="READY"))
    db.add_all([_fiche(1), _fiche(2)])
    db.commit()
    _WL_TOTAL_CACHE.clear()
    yield db, TestClient(app)
    db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == SID).delete()
    db.query(Snapshot).filter(Snapshot.snapshot_id == SID).delete()
    db.commit()
    db.close()
    _WL_TOTAL_CACHE.clear()
    app.dependency_overrides.pop(get_current_user, None)


def _total(client, **params) -> int:
    params.setdefault("scope", "production")
    params.setdefault("page_size", "1")
    q = "&".join(f"{k}={v}" for k, v in params.items())
    reponse = client.get(f"/api/watchlist/db?{q}")
    assert reponse.status_code == 200, reponse.text
    return reponse.json()["total"]


# --------------------------- l'index qui manquait ---------------------------

def test_le_perimetre_des_exclusions_a_son_index():
    """L'index partiel sur `excluded IS NOT TRUE` ne sert QUE la négation.
    Sans son symétrique, `scope=EXCLUDED` parcourt la table entière."""
    par_nom = {i.name: i for i in _PERFORMANCE_INDEXES}
    assert "ix_wl_entities_excluded" in par_nom, \
        "index partiel manquant pour le périmètre EXCLUDED"
    from sqlalchemy.schema import CreateIndex
    from sqlalchemy.dialects import postgresql
    sql = str(CreateIndex(par_nom["ix_wl_entities_excluded"])
              .compile(dialect=postgresql.dialect()))
    assert "watchlist_entities" in sql
    assert "excluded IS true" in sql, sql
    # ... et celui d'origine reste sur la négation : les deux sont partiels et
    # disjoints, donc aucun des deux ne coûte le prix de la table entière.
    negatif = str(CreateIndex(par_nom["ix_wl_entities_production"])
                  .compile(dialect=postgresql.dialect()))
    assert "excluded IS NOT true" in negatif, negatif


# ----------------------- le compte reste juste -----------------------

def test_le_compte_est_memorise_entre_deux_pages(contexte):
    db, client = contexte
    premier = _total(client)
    assert premier >= 2
    signature = _production_signature(db)
    assert signature in _WL_TOTAL_CACHE
    assert _total(client) == premier


def test_une_fiche_ajoutee_en_production_change_le_compte(contexte):
    """Ajout manuel d'un listé : l'appelant remonte l'époque, le compte doit
    suivre. Un compte figé afficherait un univers de criblage faux."""
    db, client = contexte
    avant = _total(client)
    db.add(_fiche(3))
    db.commit()
    bump_watchlist_epoch(db)
    db.commit()
    assert _total(client) == avant + 1


def test_une_liste_mise_en_production_change_le_compte_sans_attendre(contexte):
    """La mise en production commit le passage en READY, puis délègue le
    rechargement du cache (donc la remontée d'époque) à un travail de fond.
    Le relevé des snapshots capte la bascule AU COMMIT : le compte ne peut pas
    rester en retard d'une homologation le temps que le travail passe."""
    db, client = contexte
    avant = _total(client)
    autre = f"{SID}-b"
    db.add(Snapshot(snapshot_id=autre, file_type="WATCHLIST_EU",
                    file_name=f"{autre}.csv", file_hash=uuid.uuid4().hex,
                    record_count=1, status="PENDING_REVIEW"))
    db.add(WatchlistEntity(snapshot_id=autre, entity_id=f"TC-{TAG}-b",
                           entity_type="I", primary_name=f"Total {TAG} b",
                           entity_checksum=f"tc-{TAG}-b"))
    db.commit()
    assert _total(client) == avant, "un snapshot en attente n'est pas en production"

    db.query(Snapshot).filter(Snapshot.snapshot_id == autre).first().status = "READY"
    db.commit()  # aucune remontee d'epoque : c'est tout l'interet du releve
    try:
        assert _total(client) == avant + 1
    finally:
        db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == autre).delete()
        db.query(Snapshot).filter(Snapshot.snapshot_id == autre).delete()
        db.commit()


def test_une_liste_retiree_de_la_production_change_le_compte(contexte):
    db, client = contexte
    avant = _total(client)
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == SID).first()
    snap.status = "SUPERSEDED"
    db.commit()
    try:
        assert _total(client) == avant - 2
    finally:
        snap.status = "READY"
        db.commit()


def test_chaque_filtre_de_liste_a_son_propre_compte(contexte):
    """Le compte dépend du filtre : mémoriser un seul chiffre pour tous les
    filtres afficherait le total global sur une liste filtrée."""
    _, client = contexte
    global_ = _total(client)
    eu = _total(client, list_type="WATCHLIST_EU")
    ofac = _total(client, list_type="WATCHLIST_OFAC")
    assert eu >= 2
    assert eu + ofac <= global_ or global_ >= eu


def test_le_cache_ne_garde_qu_un_seul_etat(contexte):
    """Une entrée par signature laisserait le cache grossir à chaque
    synchronisation. Il est vidé dès que la production bouge."""
    db, client = contexte
    _total(client)
    _total(client, list_type="WATCHLIST_EU")
    assert len(_WL_TOTAL_CACHE) == 1
    bump_watchlist_epoch(db)
    db.commit()
    _total(client)
    assert len(_WL_TOTAL_CACHE) == 1


def test_les_autres_perimetres_ne_sont_pas_memorises(contexte):
    """Les exclusions se posent et se retirent sur des snapshots en attente,
    sans remontée d'époque : leur compte doit rester calculé à chaque appel."""
    db, client = contexte
    _total(client)  # amorce le cache sur production
    empreinte = dict(_WL_TOTAL_CACHE)
    for scope in ("EXCLUDED", "PENDING_REVIEW", "all"):
        reponse = client.get(f"/api/watchlist/db?scope={scope}&page_size=1")
        assert reponse.status_code == 200, reponse.text
    assert _WL_TOTAL_CACHE == empreinte, "un autre périmètre a été mémorisé"


def test_une_recherche_n_est_jamais_servie_depuis_le_cache(contexte):
    """Le compte mémorisé est celui du périmètre entier : servi tel quel sur
    une recherche, il annoncerait des milliers de résultats pour trois."""
    _, client = contexte
    reponse = client.get(
        f"/api/watchlist/db?scope=production&page_size=1&search=Total {TAG} 1")
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert corps["match_mode"] in ("exact", "fuzzy", "fuzzy_pending")
    if corps["match_mode"] == "exact":
        assert corps["total"] == 1


# --------------------- la mesure : le COUNT n'est plus refait ---------------------

def _compte_les_count(client, appels):
    """Nombre de COUNT sur `watchlist_entities` émis pendant `appels`."""
    vus = []

    def _ecoute(conn, cursor, statement, params, context, executemany):
        texte = " ".join(statement.split()).lower()
        if texte.startswith("select count(") and "watchlist_entities" in texte:
            vus.append(texte)

    event.listen(Engine, "before_cursor_execute", _ecoute)
    try:
        appels(client)
    finally:
        event.remove(Engine, "before_cursor_execute", _ecoute)
    return len(vus)


def test_changer_de_page_ne_refait_pas_le_compte(contexte):
    """Le point de la mesure : sur la production, ce COUNT porte sur 895 157
    fiches et coûte 3,6 s. Il était refait à chaque page."""
    _, client = contexte

    def _trois_pages(c):
        for page in (1, 2, 3):
            c.get(f"/api/watchlist/db?scope=production&page={page}&page_size=1")

    assert _compte_les_count(client, _trois_pages) == 1


def test_le_compte_est_refait_quand_la_production_bouge(contexte):
    """Le pendant du test précédent : mémoriser ne vaut que si l'oubli est
    déclenché par le bon événement."""
    db, client = contexte
    client.get("/api/watchlist/db?scope=production&page_size=1")
    bump_watchlist_epoch(db)
    db.commit()

    def _une_page(c):
        c.get("/api/watchlist/db?scope=production&page_size=1")

    assert _compte_les_count(client, _une_page) == 1


# ------------- la ligne servie, sans hydrater l'entite complete -------------

def test_la_ligne_legere_est_identique_a_la_ligne_ORM(contexte):
    """La consultation ne demande plus que les colonnes qu'elle rend. Ce test
    compare les deux chemins champ par champ : si la version légère perd une
    clé, l'écran perd une colonne sans que rien ne casse."""
    db, _ = contexte
    base = _wl_scope_query(db, "production", "WATCHLIST_EU")
    par_orm = [_serialize_watchlist_row(e, s) for e, s in base.limit(3).all()]
    par_tuple = [_watchlist_row_from_tuple(r)
                 for r in base.with_entities(*_WL_ROW_SELECT).limit(3).all()]
    assert par_orm and len(par_orm) == len(par_tuple)
    assert par_orm == par_tuple


def test_le_tri_serveur_survit_a_la_selection_reduite(contexte):
    """Trois des colonnes triables (`origin`, `country`, `official_reference`)
    ne font PAS partie de la ligne servie : trier dessus demande un ORDER BY
    sur une colonne non sélectionnée."""
    _, client = contexte
    for colonne in ("primary_name", "entity_id", "origin", "country",
                    "listed_on", "official_reference", "bic_swift", "entity_type"):
        for sens in ("asc", "desc"):
            reponse = client.get(
                f"/api/watchlist/db?scope=production&page_size=5"
                f"&sort_by={colonne}&sort_dir={sens}")
            assert reponse.status_code == 200, f"{colonne} {sens} : {reponse.text}"
            assert reponse.json()["items"], f"{colonne} {sens} : aucune ligne"


def test_le_tri_par_defaut_ordonne_toujours_par_date_de_lot(contexte):
    _, client = contexte
    items = client.get("/api/watchlist/db?scope=production&page_size=20").json()["items"]
    dates = [i["snapshot_uploaded_at"] for i in items if i["snapshot_uploaded_at"]]
    assert dates == sorted(dates, reverse=True)


# ------------- le badge « Hash actif » compte le meme univers -------------

def test_le_badge_et_la_consultation_comptent_le_meme_univers(contexte):
    """Les deux partagent la même mémorisation sous la clé vide. S'ils ne
    comptaient pas exactement le même périmètre, le premier appelé imposerait
    son chiffre à l'autre — un total faux, selon l'ordre des écrans."""
    db, client = contexte
    _WL_TOTAL_CACHE.clear()
    par_consultation = _total(client)
    _WL_TOTAL_CACHE.clear()
    par_badge = _production_watchlist_summary(db)["count"]
    assert par_badge == par_consultation

    # ... et dans l'autre sens, pour que l'ordre n'y change rien
    _WL_TOTAL_CACHE.clear()
    assert _production_watchlist_summary(db)["count"] == _total(client)


def test_le_badge_ne_recompte_pas_a_chaque_ouverture_de_page(contexte):
    """Le badge est chargé à CHAQUE ouverture de page et son COUNT porte sur
    895 157 fiches en production — 1,3 s de travail à chaque fois."""
    _, client = contexte

    def _trois_ouvertures(c):
        for _ in range(3):
            c.get("/api/watchlist/summary")

    assert _compte_les_count(client, _trois_ouvertures) == 1


def test_le_badge_suit_la_production(contexte):
    db, client = contexte
    avant = client.get("/api/watchlist/summary").json()["count"]
    db.add(_fiche(9))
    db.commit()
    bump_watchlist_epoch(db)
    db.commit()
    assert client.get("/api/watchlist/summary").json()["count"] == avant + 1

"""
Historique des lots : pagination et filtres serveur.

`GET /api/snapshots` vidait la table entière. Mesuré sur la production :
**547 lots pour 282 Ko**, et il en naît un par source et par jour — 42
sources. Cette réponse était rechargée après chaque import, synchronisation,
homologation et purge. Le poids n'était pas dans un champ à retirer (la ligne
avait déjà été allégée de son rapport de cahier de tests) : il était dans le
nombre de lignes, qui grossit tout seul.

Le tableau est donc paginé (50 lignes), son filtre par type passe côté serveur
— paginé, un filtre client ne verrait que la page chargée — et les listes
déroulantes de comparaison, qui ont besoin de TOUT l'historique parce qu'on
compare volontiers un lot ancien, ont leur propre source réduite à quatre
colonnes.

`GET /api/snapshots` **change de forme** : l'enveloppe
`{total, page, page_size, items}` remplace la liste nue, comme le journal
d'audit avant lui.
"""
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot

TAG = uuid.uuid4().hex[:6].upper()
APP_JS = Path("fiskr/static/app.js").read_text(encoding="utf-8")
INDEX = Path("fiskr/static/index.html").read_text(encoding="utf-8")
BASE = datetime(2026, 5, 1, 8, 0, 0)

# 120 lots : de quoi dépasser une page de 50 et distinguer les types
JEU = [("WATCHLIST_EU", "READY", 60),
       ("WATCHLIST_OFAC", "SUPERSEDED", 40),
       ("WATCHLIST_DGT", "PENDING_REVIEW", 20)]


@pytest.fixture()
def contexte():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "hl", "full_name": "hl", "role": "admin",
        "roles": ["admin"]}
    db = next(get_db())
    rang = 0
    for file_type, statut, combien in JEU:
        for i in range(combien):
            rang += 1
            db.add(Snapshot(snapshot_id=f"hl-{TAG}-{rang}", file_type=file_type,
                            file_name=f"lot-{rang}.csv", file_hash=uuid.uuid4().hex,
                            record_count=rang, status=statut,
                            uploaded_at=BASE + timedelta(minutes=rang)))
    db.commit()
    yield db, TestClient(app)
    db.query(Snapshot).filter(Snapshot.snapshot_id.like(f"hl-{TAG}-%")).delete(
        synchronize_session=False)
    db.commit()
    db.close()
    app.dependency_overrides.pop(get_current_user, None)


def _lots(client, requete=""):
    reponse = client.get(f"/api/snapshots{requete}")
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def _les_notres(items):
    return [i for i in items if i["snapshot_id"].startswith(f"hl-{TAG}-")]


# ----------------------------- l'enveloppe -----------------------------

def test_la_reponse_est_une_enveloppe_paginee(contexte):
    _, client = contexte
    corps = _lots(client)
    assert set(corps) == {"total", "page", "page_size", "items"}
    assert corps["page"] == 1 and corps["page_size"] == 50
    assert len(corps["items"]) == 50
    assert corps["total"] >= 120


def test_la_page_demandee_est_bien_servie(contexte):
    _, client = contexte
    page1 = _lots(client, "?file_type=WATCHLIST_EU&page=1&page_size=25")
    page2 = _lots(client, "?file_type=WATCHLIST_EU&page=2&page_size=25")
    assert page1["total"] >= 60
    assert len(page1["items"]) == 25 and len(page2["items"]) == 25
    ids1 = {i["snapshot_id"] for i in page1["items"]}
    ids2 = {i["snapshot_id"] for i in page2["items"]}
    assert not (ids1 & ids2), "deux pages se recouvrent"

    # Toutes les pages reunies rendent l'historique complet, sans trou
    total = page1["total"]
    vus = set()
    for page in range(1, (total // 25) + 2):
        vus |= {i["snapshot_id"]
                for i in _lots(client, f"?file_type=WATCHLIST_EU&page={page}&page_size=25")["items"]}
    assert len(vus) == total


def test_le_tri_reste_du_plus_recent_au_plus_ancien(contexte):
    _, client = contexte
    dates = [i["uploaded_at"] for i in _lots(client, "?page_size=200")["items"]]
    assert dates == sorted(dates, reverse=True)


def test_la_taille_de_page_est_bornee(contexte):
    _, client = contexte
    assert client.get("/api/snapshots?page_size=501").status_code == 422
    assert client.get("/api/snapshots?page=0").status_code == 422


# ----------------------------- filtres serveur -----------------------------

def test_le_filtre_par_type_porte_sur_tout_l_historique(contexte):
    """Le point de la bascule côté serveur : un filtre client ne verrait que
    les 50 lignes chargées et manquerait les 70 autres. Nos 40 lots OFAC sont
    tous rendus alors qu'ils arrivent après 60 lots EU dans l'ordre global."""
    _, client = contexte
    corps = _lots(client, "?file_type=WATCHLIST_OFAC&page_size=500")
    assert len(_les_notres(corps["items"])) == 40
    assert all(i["file_type"] == "WATCHLIST_OFAC" for i in corps["items"])
    # Sans filtre serveur, une page de 50 n'en contient qu'une partie : c'est
    # exactement ce qu'un filtre client aurait eu sous la main.
    page = _lots(client, "?page_size=50")
    sur_la_page = [i for i in _les_notres(page["items"])
                   if i["file_type"] == "WATCHLIST_OFAC"]
    assert len(sur_la_page) < 40


def test_le_filtre_accepte_plusieurs_types(contexte):
    _, client = contexte
    corps = _lots(client, "?file_type=WATCHLIST_OFAC,WATCHLIST_DGT&page_size=500")
    assert len(_les_notres(corps["items"])) == 60
    assert {i["file_type"] for i in corps["items"]} <= {"WATCHLIST_OFAC", "WATCHLIST_DGT"}


def test_le_filtre_par_statut_est_disponible(contexte):
    _, client = contexte
    corps = _lots(client, "?status=PENDING_REVIEW&file_type=WATCHLIST_DGT&page_size=500")
    assert len(_les_notres(corps["items"])) == 20
    assert all(i["status"] == "PENDING_REVIEW" for i in corps["items"])


def test_le_total_suit_le_filtre_et_pas_la_page(contexte):
    """Un total qui ignorerait le filtre donnerait un nombre de pages faux —
    et un pager qui promet des pages vides."""
    _, client = contexte
    filtre = _lots(client, "?file_type=WATCHLIST_DGT&page_size=10")
    global_ = _lots(client, "?page_size=10")
    assert filtre["total"] < global_["total"]
    assert filtre["total"] == len(_lots(client, "?file_type=WATCHLIST_DGT&page_size=500")["items"])


# ------------------------ listes de comparaison ------------------------

def test_les_options_de_comparaison_couvrent_tout_l_historique(contexte):
    """Non paginées à dessein : comparer suppose de pouvoir choisir n'importe
    quel couple, y compris un lot ancien."""
    _, client = contexte
    options = client.get("/api/snapshots/options").json()
    assert isinstance(options, list)
    assert len(_les_notres(options)) == 120


def test_une_option_ne_transporte_que_ce_qu_une_liste_deroulante_affiche(contexte):
    _, client = contexte
    option = _les_notres(client.get("/api/snapshots/options").json())[0]
    assert set(option) == {"snapshot_id", "file_type", "file_name", "uploaded_at"}


def test_une_option_pese_moins_qu_une_ligne_complete(contexte):
    """Sur les données de production : 519 octets par lot dans la liste
    complète, 176 dans les options — la liste entière tombe de 284 Ko à 96 Ko,
    et l'ouverture de l'écran (une page de 50 plus les options) à 122 Ko."""
    _, client = contexte
    options = client.get("/api/snapshots/options").json()
    complet = client.get("/api/snapshots?page_size=200").json()["items"]
    par_lot_option = len(client.get("/api/snapshots/options").content) / len(options)
    par_lot_complet = len(client.get("/api/snapshots?page_size=200").content) / len(complet)
    assert par_lot_option < par_lot_complet, (
        f"{par_lot_complet:.0f} o/lot contre {par_lot_option:.0f} o/lot")


def test_les_options_ne_sont_rechargees_qu_apres_un_changement_de_lot():
    """Elles couvrent tout l'historique (96 Ko en production) et ne changent
    qu'à la création, l'homologation, le rejet ou la purge d'un lot — alors
    que revenir sur l'onglet est fréquent."""
    corps = APP_JS[APP_JS.index("async function fetchSnapshotOptions()"):]
    corps = corps[:corps.index("\n}\n")]
    assert "if (optionsDeComparaisonAJour) return;" in corps
    assert "optionsDeComparaisonAJour = true;" in corps

    # ... et le drapeau retombe a chaque changement de lot
    signal = APP_JS[APP_JS.index("function signalerChangementDeLots()"):]
    signal = signal[:signal.index("\n}\n")]
    assert "optionsDeComparaisonAJour = false;" in signal
    # tous les chemins d'operation passent par la
    assert APP_JS.count("signalerChangementDeLots()") >= 4


def test_le_drapeau_est_declare_avant_ses_utilisateurs():
    """Déclaré en `let` après la fonction qui le lit, il tomberait dans la
    zone morte temporelle au premier appel précoce."""
    declaration = APP_JS.index("let optionsDeComparaisonAJour")
    assert declaration < APP_JS.index("function signalerChangementDeLots()")
    assert declaration < APP_JS.index("async function fetchSnapshotOptions()")


def test_la_route_des_options_n_avale_pas_la_liste(contexte):
    """Piège de routage déjà rencontré : un sous-chemin littéral déclaré après
    une route à paramètre disparaît. Les deux doivent répondre."""
    _, client = contexte
    assert client.get("/api/snapshots").status_code == 200
    assert client.get("/api/snapshots/options").status_code == 200


# ----------------------------- côté frontal -----------------------------

def test_le_frontal_demande_une_page_et_porte_son_filtre_au_serveur():
    corps = APP_JS[APP_JS.index("async function fetchSnapshots(page = 1)"):]
    corps = corps[:corps.index("\n}\n")]
    assert 'params.set("file_type"' in corps
    assert "page_size" in corps
    assert "data.items" in corps
    assert "renderSnapshotsPagination" in corps


def test_le_filtre_de_l_ecran_relance_la_requete_page_1():
    """S'il continuait à filtrer le tableau déjà chargé, il ne verrait que
    50 lots sur 547."""
    corps = APP_JS[APP_JS.index("function renderSnapshotsFiltered()"):]
    corps = corps[:corps.index("\n}\n")]
    assert "fetchSnapshots(1)" in corps
    assert "activeSnapshots.filter" not in corps


def test_les_listes_de_comparaison_ont_leur_propre_source():
    assert "async function fetchSnapshotOptions()" in APP_JS
    assert '"/api/snapshots/options"' in APP_JS
    corps = APP_JS[APP_JS.index("async function fetchSnapshots(page = 1)"):]
    corps = corps[:corps.index("\n}\n")]
    assert "populateCompareSelects" not in corps, (
        "les listes déroulantes seraient remplies avec la page courante : "
        "on ne pourrait plus comparer un lot ancien")


def test_la_pagination_ne_recharge_pas_les_listes_de_comparaison():
    """Changer de page ne change pas la liste des lots comparables."""
    corps = APP_JS[APP_JS.index("function renderSnapshotsPagination("):]
    corps = corps[:corps.index("\n}\n")]
    assert "fetchSnapshots(" in corps
    assert "fetchSnapshotOptions" not in corps


def test_l_ecran_a_un_conteneur_de_pagination():
    assert 'id="snapshots-pagination"' in INDEX

"""
Écran des listés : la ligne ne transporte que ce qu'elle affiche.

Mesuré sur la production : une page de 100 fiches pesait 255 Ko, soit
2 615 octets par fiche, alors que le tableau n'affiche qu'une poignée de
colonnes. L'écrasante majorité du poids — alias, motifs de désignation,
adresses, documents d'identité — n'apparaît que dans la modale de détails,
ouverte sur UNE fiche à la fois.

La liste sert désormais les seules colonnes affichées (392 octets par fiche,
−85 %) et la modale charge le détail au moment où on l'ouvre.

L'allègement n'est correct que si le tableau garde TOUT ce qu'il lit. Le test
central dérive cette liste du code du frontal : ajouter une colonne au tableau
sans l'ajouter à la ligne servie échoue ici.
"""
import re
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app, _WL_ROW_COLUMNS
from fiskr.auth import get_current_user
from fiskr.database import get_db, Snapshot, WatchlistEntity

TAG = uuid.uuid4().hex[:6].upper()
APP_JS = Path("fiskr/static/app.js").read_text(encoding="utf-8")

# Champs lourds, réservés à la modale
CHAMPS_DE_DETAIL = ("aliases", "designation_reasons", "additional_informations",
                    "individual_name_parsed", "alternative_addresses",
                    "passport_documents", "national_id_documents")


@pytest.fixture()
def contexte():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "wl", "full_name": "wl", "role": "admin",
        "roles": ["admin"]}
    db = next(get_db())
    sid = f"wlrow-{TAG}"
    db.add(Snapshot(snapshot_id=sid, file_type="WATCHLIST_EU", file_name=f"{sid}.csv",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    ent = WatchlistEntity(
        snapshot_id=sid, entity_id=f"WL-{TAG}", entity_type="I",
        primary_name=f"Fiche Complete {TAG}",
        aliases={"high_priority": [f"Alias {TAG}"], "low_priority": []},
        designation_reasons="Motif de désignation très long " * 20,
        individual_name_parsed={"first_name": "Fiche", "last_name": f"Complete {TAG}",
                                "maiden_name": ""},
        dates_of_birth=["1970-01-01"], is_deceased=False,
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=f"chk-{TAG}")
    db.add(ent)
    db.commit()
    pk = ent.id
    yield db, TestClient(app), pk
    app.dependency_overrides.clear()
    db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == sid).delete(synchronize_session=False)
    db.query(Snapshot).filter(
        Snapshot.snapshot_id == sid).delete(synchronize_session=False)
    db.commit()
    db.close()


def _champs_lus_par_le_tableau():
    """Les `item.<champ>` du rendu du tableau, extraits du frontal."""
    debut = APP_JS.index("tr.onclick = () => showWatchlistDetails(item);")
    bloc = APP_JS[max(0, debut - 3000):debut + 200]
    champs = set(re.findall(r"item\.([a-z_]+)", bloc))
    return {c for c in champs if not c.startswith("_")}  # _fuzzy_score : ajouté serveur


def test_row_serves_every_field_the_table_reads(contexte):
    """LE garde-fou : tout champ lu par le tableau doit être servi. Sinon la
    colonne s'affiche vide, sans erreur — le pire des échecs."""
    db, client, _ = contexte
    with client:
        items = client.get("/api/watchlist/db?scope=production").json()["items"]
    ligne = next(i for i in items if i["entity_id"] == f"WL-{TAG}")
    manquants = sorted(c for c in _champs_lus_par_le_tableau() if c not in ligne)
    assert not manquants, (
        f"champs lus par le tableau mais absents de la ligne servie : {manquants}")


def test_row_drops_the_heavy_detail_fields(contexte):
    """…et rien de plus : les champs lourds ne voyagent plus pour cent lignes."""
    db, client, _ = contexte
    with client:
        items = client.get("/api/watchlist/db?scope=production").json()["items"]
    ligne = next(i for i in items if i["entity_id"] == f"WL-{TAG}")
    presents = [c for c in CHAMPS_DE_DETAIL if c in ligne]
    assert not presents, f"champs de détail encore transportés : {presents}"


def test_detail_endpoint_serves_the_full_record(contexte):
    """La modale doit trouver tout ce que la ligne n'a plus."""
    db, client, pk = contexte
    with client:
        fiche = client.get(f"/api/watchlist/db/entity/{pk}").json()
    for champ in CHAMPS_DE_DETAIL:
        assert champ in fiche, f"{champ} absent du détail"
    assert fiche["aliases"]["high_priority"] == [f"Alias {TAG}"]
    assert fiche["entity_id"] == f"WL-{TAG}"
    assert fiche["_list_type"] == "WATCHLIST_EU"


def test_unknown_entity_is_a_clean_404(contexte):
    db, client, _ = contexte
    with client:
        assert client.get("/api/watchlist/db/entity/99999999").status_code == 404


def test_the_modal_fetches_the_detail_when_the_row_is_light():
    """Le frontal doit demander le détail, sinon la modale afficherait des
    champs vides. Le discriminant est `aliases`, absent de la ligne."""
    debut = APP_JS.index("async function showWatchlistDetails(")
    bloc = APP_JS[debut:debut + 800]
    assert "/api/watchlist/db/entity/" in bloc, "la modale ne charge pas le détail"
    assert "aliases === undefined" in bloc, (
        "sans discriminant, une fiche déjà complète (palette Ctrl+K) serait "
        "rechargée pour rien")


def test_row_columns_stay_a_small_set():
    """L'intérêt de l'allègement disparaît si la liste regrossit sans qu'on
    s'en aperçoive."""
    assert len(_WL_ROW_COLUMNS) <= 16, (
        f"{len(_WL_ROW_COLUMNS)} colonnes servies : la ligne redevient lourde")


def test_detail_route_does_not_shadow_the_fuzzy_route(contexte):
    """Piège de routage, payé une fois : déclarée « /api/watchlist/db/{id} »,
    la route de détail captait « /api/watchlist/db/fuzzy » — FastAPI y voyait
    un identifiant, et le balayage flou répondait 422. Un chemin explicite
    (« /entity/{id} ») rend la collision impossible, y compris pour un futur
    sous-chemin littéral."""
    db, client, _ = contexte
    with client:
        flou = client.get("/api/watchlist/db/fuzzy?search=abcd")
    assert flou.status_code != 422, (
        "le balayage flou est capté par la route de détail")
    assert "matches" in flou.json() or flou.status_code == 400, flou.text

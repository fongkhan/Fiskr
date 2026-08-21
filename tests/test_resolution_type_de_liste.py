"""
Résolution du type de liste d'une fiche : par la base, plus par balayage.

Le type de liste d'origine (`WATCHLIST_OFAC`, `WATCHLIST_PEP`…) était résolu
par **balayage linéaire du cache mémoire** :

    next(e["_list_type"] for e in watchlist_store if e["entity_id"] == ...)

soit 832 470 comparaisons par identifiant sur la production. Dans la mise en
liste blanche en masse depuis un rapport de cahier de tests, ce balayage était
**dans la boucle** : 500 paires proposées valaient 416 millions de
comparaisons pour un seul appel.

La base porte un index sur `entity_id` (`ix_wl_entities_entity_id`) : une
requête unique résout tout le lot. Ces tests fixent le résultat — la
production l'emporte sur une fiche en attente, un identifiant inconnu ne rend
rien — et vérifient qu'un lot ne coûte qu'un nombre borné de requêtes.
"""
import uuid

import pytest

from fiskr.api import _list_types_map
from fiskr.database import get_db, Snapshot, WatchlistEntity

TAG = uuid.uuid4().hex[:6].upper()


def _entite(db, entity_id, snapshot_id, nom="FICHE"):
    db.add(WatchlistEntity(
        snapshot_id=snapshot_id, entity_id=entity_id, primary_name=nom,
        entity_type="I", entity_checksum=uuid.uuid4().hex))


@pytest.fixture()
def base():
    db = next(get_db())
    prod = f"S-PROD-{TAG}"
    attente = f"S-WAIT-{TAG}"
    db.add(Snapshot(snapshot_id=prod, file_name="ofac.xml", file_hash=f"h1{TAG}",
                    file_type="WATCHLIST_OFAC", status="READY", record_count=2))
    db.add(Snapshot(snapshot_id=attente, file_name="pep.csv", file_hash=f"h2{TAG}",
                    file_type="WATCHLIST_PEP", status="PENDING_REVIEW", record_count=1))
    _entite(db, f"E1-{TAG}", prod)
    _entite(db, f"E2-{TAG}", prod)
    # MEME identifiant des deux cotes : la production doit l'emporter
    _entite(db, f"E1-{TAG}", attente)
    db.commit()
    yield db
    db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_([prod, attente])).delete(synchronize_session=False)
    db.query(Snapshot).filter(
        Snapshot.snapshot_id.in_([prod, attente])).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_resout_le_type_de_liste(base):
    resolu = _list_types_map(base, [f"E1-{TAG}", f"E2-{TAG}"])
    assert resolu[f"E2-{TAG}"] == "WATCHLIST_OFAC"


def test_la_production_l_emporte_sur_une_fiche_en_attente(base):
    """Une fiche présente dans un lot homologué ET dans un lot en attente doit
    rendre le type du lot EN PRODUCTION : c'est celui qui a criblé."""
    assert _list_types_map(base, [f"E1-{TAG}"])[f"E1-{TAG}"] == "WATCHLIST_OFAC"


def test_un_identifiant_inconnu_ne_rend_rien(base):
    resolu = _list_types_map(base, [f"INCONNU-{TAG}"])
    assert f"INCONNU-{TAG}" not in resolu
    # Et l'appelant retombe sur None sans lever
    assert resolu.get(f"INCONNU-{TAG}") is None


def test_une_liste_vide_ne_touche_pas_la_base(base):
    for entree in ([], [None], ["", "  "], None or []):
        assert _list_types_map(base, entree) == {}


def test_un_lot_ne_coute_qu_un_nombre_borne_de_requetes(base):
    """Le point de la correction : 500 identifiants ne doivent pas produire
    500 lectures — ni 500 balayages de 832 470 fiches."""
    from sqlalchemy import event

    requetes = []
    moteur = base.get_bind()

    def compte(conn, cursor, statement, params, contexte, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            requetes.append(statement)

    event.listen(moteur, "before_cursor_execute", compte)
    try:
        ids = [f"E1-{TAG}", f"E2-{TAG}"] + [f"X{i}-{TAG}" for i in range(498)]
        _list_types_map(base, ids)
    finally:
        event.remove(moteur, "before_cursor_execute", compte)

    # 500 identifiants, tranches de 800 -> une seule requete
    assert len(requetes) == 1, f"{len(requetes)} requêtes pour 500 identifiants"


def test_les_gros_lots_sont_tranches(base):
    """Au-delà de la taille de tranche, plusieurs requêtes — jamais une
    clause IN de plusieurs milliers d'éléments, qu'aucun moteur n'aime."""
    from sqlalchemy import event

    requetes = []
    moteur = base.get_bind()

    def compte(conn, cursor, statement, params, contexte, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            requetes.append(statement)

    event.listen(moteur, "before_cursor_execute", compte)
    try:
        _list_types_map(base, [f"Y{i}-{TAG}" for i in range(1700)])
    finally:
        event.remove(moteur, "before_cursor_execute", compte)
    assert len(requetes) == 3, f"{len(requetes)} requêtes pour 1 700 identifiants"


def test_plus_aucun_balayage_du_cache_pour_un_type_de_liste():
    """Garde-fou : le motif supprimé ne doit pas revenir par une autre porte."""
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "fiskr" / "api.py").read_text()
    assert '_list_type") for e in watchlist_store' not in source, (
        "un balayage linéaire du cache a été réintroduit pour résoudre un "
        "type de liste : passer par _list_types_map()."
    )

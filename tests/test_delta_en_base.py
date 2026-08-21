"""
Calcul du delta EN BASE, à mémoire bornée.

Le chemin d'origine chargeait les **deux instantanés entiers** en mémoire, sous
forme de dictionnaires à soixante-dix colonnes, pour n'en publier que **cent
lignes par catégorie** (`MAX_REPORT_DETAILS`). Mesuré sur 40 000 fiches contre
40 000, dont la moitié modifiées :

| | Temps | Pic mémoire |
|---|---:|---:|
| chargement des deux instantanés + `calculate_delta` | 5,90 s | +94 Mo |
| `calculate_delta_db` | **0,22 s** | **+0 Mo** |

Extrapolé à la plus grosse liste de la production — WATCHLIST_PEP,
**709 511 fiches** — l'ancien chemin demande **~1,66 Go et ~105 s**, dans une
requête HTTP synchrone, sur un hébergement mutualisé : l'écran d'examen d'un
import manuel de cette liste ne pouvait pas s'ouvrir.

L'équivalence des empreintes n'est pas une approximation : `compute_checksum`
et `find_differences` excluent **exactement** les mêmes trois clés (`id`,
`snapshot_id`, `entity_checksum`), donc « empreintes différentes » et « au
moins un champ comparé diffère » sont la même chose.
"""
import uuid

import pytest

from fiskr.database import (compute_checksum, get_db, Snapshot, WatchlistEntity)
from fiskr.delta import calculate_delta, calculate_delta_db
from fiskr.sync import (MAX_REPORT_DETAILS, _snapshot_entity_dicts,
                        _truncate_delta_details)

TAG = uuid.uuid4().hex[:6].upper()


def _ajoute(db, snapshot_id, entity_id, **champs):
    donnees = {"entity_id": entity_id, "primary_name": champs.pop("nom", "NOM"),
               "entity_type": "I"}
    donnees.update(champs)
    db.add(WatchlistEntity(snapshot_id=snapshot_id,
                           entity_checksum=compute_checksum(donnees), **donnees))


@pytest.fixture()
def deux_lots():
    db = next(get_db())
    ancien, nouveau = f"dl-old-{TAG}", f"dl-new-{TAG}"
    for sid in (ancien, nouveau):
        db.add(Snapshot(snapshot_id=sid, file_name="x", file_hash=uuid.uuid4().hex,
                        file_type="WATCHLIST_OFAC", status="READY", record_count=0))
    # 5 inchangées, 3 modifiées, 3 retirées, 4 ajoutées
    for i in range(5):
        _ajoute(db, ancien, f"E{i}", nom=f"NOM {i}")
        _ajoute(db, nouveau, f"E{i}", nom=f"NOM {i}")
    for i in range(5, 8):
        _ajoute(db, ancien, f"E{i}", nom=f"NOM {i}", country="FR")
        _ajoute(db, nouveau, f"E{i}", nom=f"NOM {i}", country="DE")
    for i in range(8, 11):
        _ajoute(db, ancien, f"E{i}", nom=f"NOM {i}")
    for i in range(11, 15):
        _ajoute(db, nouveau, f"E{i}", nom=f"NOM {i}")
    db.commit()
    yield db, ancien, nouveau
    db.query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id.in_([ancien, nouveau])).delete(synchronize_session=False)
    db.query(Snapshot).filter(
        Snapshot.snapshot_id.in_([ancien, nouveau])).delete(synchronize_session=False)
    db.commit()
    db.close()


def _reference(db, ancien, nouveau):
    """Le chemin historique, qui fait foi."""
    return _truncate_delta_details(calculate_delta(
        _snapshot_entity_dicts(db, ancien), _snapshot_entity_dicts(db, nouveau),
        "entity_id"))


# ------------------ ÉQUIVALENCE AVEC LE CHEMIN HISTORIQUE ------------------

def test_les_compteurs_sont_identiques(deux_lots):
    db, ancien, nouveau = deux_lots
    attendu = _reference(db, ancien, nouveau)["summary"]
    assert calculate_delta_db(db, ancien, nouveau)["summary"] == attendu
    assert attendu == {"added_count": 4, "removed_count": 3, "modified_count": 3}


@pytest.mark.parametrize("categorie", ["added", "removed", "modified"])
def test_les_memes_fiches_sont_publiees(deux_lots, categorie):
    db, ancien, nouveau = deux_lots
    attendu = _reference(db, ancien, nouveau)["details"][categorie]
    obtenu = calculate_delta_db(db, ancien, nouveau)["details"][categorie]
    assert [x["id"] for x in obtenu] == [x["id"] for x in attendu]


def test_le_champ_a_champ_est_identique(deux_lots):
    """C'est ce qu'un réviseur lit : la comparaison doit être au caractère
    près celle du chemin historique."""
    db, ancien, nouveau = deux_lots
    attendu = _reference(db, ancien, nouveau)["details"]["modified"]
    obtenu = calculate_delta_db(db, ancien, nouveau)["details"]["modified"]
    assert obtenu == attendu
    assert attendu[0]["changes_detected"] == ["country"]
    assert attendu[0]["before"]["country"] == "FR"
    assert attendu[0]["after"]["country"] == "DE"


# ------------------ BORNES ------------------

def test_le_detail_est_borne_et_le_reste_est_compte(deux_lots):
    db, ancien, nouveau = deux_lots
    rapport = calculate_delta_db(db, ancien, nouveau, limite=2)
    assert len(rapport["details"]["added"]) == 2
    assert rapport["details"]["added_truncated"] == 2       # 4 ajouts, 2 publiés
    assert rapport["summary"]["added_count"] == 4           # le compteur reste EXACT
    assert rapport["details"]["removed_truncated"] == 1
    assert rapport["details"]["modified_truncated"] == 1


def test_le_plafond_par_defaut_est_celui_du_rapport(deux_lots):
    """Deux plafonds différents couperaient à deux endroits différents."""
    from fiskr.delta import _plafond_details
    assert _plafond_details() == MAX_REPORT_DETAILS


def test_la_troncature_est_idempotente(deux_lots):
    """`_truncate_delta_details` s'applique encore aux rapports produits ici :
    reconstruire ses compteurs de zéro les effacerait, et le rapport dirait
    « deux ajouts » là où il y en a quatre."""
    db, ancien, nouveau = deux_lots
    rapport = calculate_delta_db(db, ancien, nouveau, limite=2)
    retronque = _truncate_delta_details(rapport)
    assert retronque["summary"] == rapport["summary"]
    assert retronque["details"]["added_truncated"] == 2
    assert retronque["details"]["removed_truncated"] == 1


# ------------------ CAS LIMITES ------------------

def test_un_premier_import_est_entierement_un_ajout(deux_lots):
    db, _, nouveau = deux_lots
    rapport = calculate_delta_db(db, None, nouveau)
    assert rapport["summary"] == {"added_count": 12, "removed_count": 0,
                                  "modified_count": 0}


def test_deux_lots_identiques_ne_produisent_aucun_delta(deux_lots):
    db, ancien, _ = deux_lots
    rapport = calculate_delta_db(db, ancien, ancien)
    assert rapport["summary"] == {"added_count": 0, "removed_count": 0,
                                  "modified_count": 0}
    assert rapport["details"] == {"added": [], "removed": [], "modified": []}


def test_un_lot_vide_face_a_un_lot_plein(deux_lots):
    db, ancien, _ = deux_lots
    vide = f"dl-vide-{TAG}"
    db.add(Snapshot(snapshot_id=vide, file_name="x", file_hash=uuid.uuid4().hex,
                    file_type="WATCHLIST_OFAC", status="READY", record_count=0))
    db.commit()
    try:
        rapport = calculate_delta_db(db, ancien, vide)
        assert rapport["summary"]["removed_count"] == 11
        assert rapport["summary"]["added_count"] == 0
    finally:
        db.query(Snapshot).filter(Snapshot.snapshot_id == vide).delete(
            synchronize_session=False)
        db.commit()


# ------------------ RÉFÉRENTIEL CLIENTS ------------------

def test_le_referentiel_clients_suit_les_memes_regles():
    """La comparaison de deux bases clients passe par le même chemin — avec
    ses propres noms de colonnes — et doit rendre exactement ce que rendait
    `calculate_delta`."""
    from fiskr.database import ClientEntity
    from fiskr.delta import _descripteur

    db = next(get_db())
    ancien, nouveau = f"dlc-old-{TAG}", f"dlc-new-{TAG}"
    for sid in (ancien, nouveau):
        db.add(Snapshot(snapshot_id=sid, file_name="x", file_hash=uuid.uuid4().hex,
                        file_type="CLIENT_BASE", status="READY", record_count=0))

    def _client(sid, cid, nom, ville=None):
        donnees = {"client_id": cid, "client_type": "PP",
                   "client_last_name": nom, "client_city": ville}
        db.add(ClientEntity(snapshot_id=sid,
                            entity_checksum=compute_checksum(donnees), **donnees))

    try:
        for i in range(3):
            _client(ancien, f"C{i}", f"NOM{i}")
            _client(nouveau, f"C{i}", f"NOM{i}")
        _client(ancien, "C3", "MODIF", "PARIS")
        _client(nouveau, "C3", "MODIF", "LYON")
        _client(ancien, "C4", "RETIRE")
        _client(nouveau, "C5", "AJOUTE")
        db.commit()

        def _dicts(sid):
            return [{c.name: getattr(e, c.name) for c in ClientEntity.__table__.columns}
                    for e in db.query(ClientEntity).filter(ClientEntity.snapshot_id == sid)]

        attendu = calculate_delta(_dicts(ancien), _dicts(nouveau), "client_id")
        obtenu = calculate_delta_db(db, ancien, nouveau, cle="client_id")
        assert obtenu["summary"] == attendu["summary"]
        assert obtenu["summary"] == {"added_count": 1, "removed_count": 1,
                                     "modified_count": 1}
        assert obtenu["details"]["modified"] == attendu["details"]["modified"]
        assert obtenu["details"]["modified"][0]["changes_detected"] == ["client_city"]
        # Le nom affiché vient des colonnes du référentiel CLIENTS
        assert obtenu["details"]["added"][0]["primary_name"] == "AJOUTE"
        assert _descripteur("client_id")[0] is ClientEntity
    finally:
        db.query(ClientEntity).filter(
            ClientEntity.snapshot_id.in_([ancien, nouveau])).delete(synchronize_session=False)
        db.query(Snapshot).filter(
            Snapshot.snapshot_id.in_([ancien, nouveau])).delete(synchronize_session=False)
        db.commit()
        db.close()


# ------------------ L'INDEX QUI REND LA JOINTURE UTILISABLE ------------------

def test_le_couple_lot_identifiant_est_indexe():
    """C'est la clé de rapprochement du calcul : les deux index séparés ne
    servent chacun qu'une moitié de la condition."""
    from fiskr import database
    par_nom = {ix.name: ix for ix in database._PERFORMANCE_INDEXES}
    assert "ix_wl_entities_snapshot_entity" in par_nom
    assert [c.name for c in par_nom["ix_wl_entities_snapshot_entity"].columns] == \
        ["snapshot_id", "entity_id"]

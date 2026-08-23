"""
Un re-criblage doit dire ce qu'il a fait, pas ce qu'il a vu.

`open_or_redetect_alerts` — le nom le dit — ouvre **ou** re-détecte. Il rend le
détail exact de ce qu'il a fait de chaque correspondance : `opened`,
`redetected`, `closed_by_rule`. Le re-criblage jetait cette valeur de retour et
tenait son propre compte, en incrémentant « nouvelles alertes » à chaque
correspondance qui passait les règles et la liste blanche.

Une correspondance qui retombe sur une alerte **déjà ouverte** ne crée rien.
Le cas n'est pas marginal, c'est le cas courant : à chaque rafraîchissement de
liste, une fiche modifiée qui touche toujours le même client produit une
re-détection. Et un **lookback** — qui repasse toute la production — n'en
produit pratiquement que.

Le chiffre remonte jusqu'à trois destinataires (le mail d'étape après une mise
en production, le mail d'homologation, le message de fin de synchronisation),
tous sous le libellé « nouvelles alertes ». C'est le nombre que lit celui qui
vient d'approuver une liste, pour juger de l'effet de son approbation.
"""
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.database import (Base, Alert, AlertEvent, ClientEntity, Snapshot,
                            WatchlistEntity)
from fiskr.rescreen import (compteurs_de_recriblage, rescreen_after_snapshot_change,
                            rescreen_lookback)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'recriblage.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _client(db):
    snap = Snapshot(snapshot_id=f"clients-{uuid.uuid4().hex[:6]}", file_type="CLIENT_BASE",
                    file_name="clients.csv", file_hash=uuid.uuid4().hex,
                    record_count=1, status="READY")
    db.add(snap)
    db.add(ClientEntity(
        snapshot_id=snap.snapshot_id, client_id="CUST-777", client_type="PP",
        client_first_name="IGOR", client_last_name="PETROV", client_dob="1965-03-12",
        client_gender="M",
        client_countries={"nationality": ["RU"], "residence": [],
                          "birth_country": [], "registration_country": []},
        entity_checksum=uuid.uuid4().hex))
    db.commit()


def _fiche(snap_id, entity_id, checksum=None):
    return WatchlistEntity(
        snapshot_id=snap_id, entity_id=entity_id, entity_type="I",
        primary_name="Igor PETROV",
        individual_name_parsed={"first_name": "Igor", "last_name": "PETROV",
                                "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=["1965-03-12"], is_deceased=False, gender="M",
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=checksum or f"chk-{entity_id}")


def _liste(db, snap_id, fiches):
    db.add(Snapshot(snapshot_id=snap_id, file_type="WATCHLIST_DGT",
                    file_name=f"{snap_id}.json", file_hash=uuid.uuid4().hex,
                    record_count=len(fiches), status="READY"))
    for f in fiches:
        db.add(f)
    db.commit()


# ----------------------------------------------------- le compte est-il juste

def test_un_lookback_qui_ne_cree_rien_n_annonce_rien(db):
    """
    Le pire cas, et le plus courant : un lookback repasse TOUTE la production.
    Tout ce qu'il trouve existe déjà. Il annonçait autant de « nouvelles
    alertes » qu'il trouvait de correspondances.
    """
    _client(db)
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-1")])

    premier = rescreen_lookback(db, "WATCHLIST_DGT")
    assert premier["new_alerts"] == 1, "le premier passage ouvre bien"
    assert premier["redetected_alerts"] == 0

    ouvertes = db.query(Alert).count()
    second = rescreen_lookback(db, "WATCHLIST_DGT")

    assert db.query(Alert).count() == ouvertes, "aucune alerte de plus en base"
    assert second["new_alerts"] == 0, (
        "un lookback qui ne crée rien ne doit annoncer aucune nouveauté")
    assert second["redetected_alerts"] == 1
    actions = [e.action for e in db.query(AlertEvent).order_by(AlertEvent.id).all()]
    assert actions == ["CREATED", "REDETECTED"]


def test_une_fiche_modifiee_qui_touche_le_meme_client_est_une_re_detection(db):
    """
    Le cas de tous les jours : la fiche du listé change (nouvelle adresse, un
    programme de plus), elle entre donc dans le delta et se fait re-cribler —
    mais elle touche le même client, dont l'alerte est déjà ouverte.
    """
    _client(db)
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-1", checksum="avant")])
    premier = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v1")
    assert premier["new_alerts"] == 1

    _liste(db, "dgt-v2", [_fiche("dgt-v2", "DGT-1", checksum="apres")])
    second = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v2", "dgt-v1")

    assert second["changed_entities"] == 1, "la fiche est bien dans le delta"
    assert second["new_alerts"] == 0
    assert second["redetected_alerts"] == 1
    assert db.query(Alert).count() == 1


def test_les_trois_comptes_ne_debordent_pas_du_nombre_de_correspondances(db):
    """Garde-fou d'exactitude : ouvertes + re-détectées + closes par règle ne
    peut pas dépasser ce que le criblage a trouvé."""
    _client(db)
    _liste(db, "dgt-v1", [_fiche("dgt-v1", f"DGT-{i}") for i in range(1, 4)])
    r = rescreen_lookback(db, "WATCHLIST_DGT")
    total = r["new_alerts"] + r["redetected_alerts"] + r["closed_by_rule"]
    assert total == db.query(Alert).count() == 3


# -------------------------------------------------- la forme ne peut pas fuir

def test_le_compte_rendu_porte_toujours_les_memes_cles(db):
    """
    `rule_suppressed` n'apparaissait dans le résultat QUE si une règle avait
    tranché, et le retour à vide de `rescreen_lookback` ne le portait pas du
    tout. Un destinataire ne pouvait pas distinguer « aucune règle n'a joué »
    de « personne ne me l'a dit ».
    """
    attendues = set(compteurs_de_recriblage())
    assert attendues >= {"new_alerts", "redetected_alerts", "closed_by_rule",
                         "rule_suppressed", "whitelisted_suppressed",
                         "clients_screened", "changed_entities"}

    _client(db)
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-1")])
    assert set(rescreen_lookback(db, "WATCHLIST_DGT")) == attendues
    # Aucune liste en production de ce type : le chemin de retour a vide
    assert set(rescreen_lookback(db, "WATCHLIST_OFAC")) == attendues
    assert set(rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v1")) == attendues


def test_aucune_regle_qui_joue_vaut_zero_et_se_dit(db):
    """Zéro doit s'écrire zéro : une clé absente se lit « — » sur un écran et
    « rien » dans un mail, ce qui n'est pas la même information."""
    _client(db)
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-1")])
    r = rescreen_lookback(db, "WATCHLIST_DGT")
    assert r["rule_suppressed"] == 0 and r["closed_by_rule"] == 0


# ------------------------------------------------- les destinataires suivent

def test_le_compte_vient_de_ce_qui_a_ete_ecrit_pas_d_un_decompte_parallele():
    """
    Garde de source : tenir un second compte à côté de celui que
    `open_or_redetect_alerts` rend, c'est se donner deux vérités — et c'est
    exactement comme ça que « nouvelles » a cessé de vouloir dire créées.
    """
    import inspect

    from fiskr import rescreen

    code = inspect.getsource(rescreen._screen_clients_against)
    assert 'compte.get("opened"' in code, (
        "le compte d'alertes ouvertes doit venir de open_or_redetect_alerts")
    assert 'compte.get("redetected"' in code
    assert 'result["new_alerts"] += 1' not in code, (
        "décompte parallèle : c'est le défaut corrigé ici")


def test_les_ecrans_distinguent_ouverte_et_re_detectee():
    """Le chiffre n'a d'intérêt que s'il arrive jusqu'au lecteur."""
    import inspect
    import os

    from fiskr import api, tasks

    racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(racine, "fiskr", "static", "app.js"), encoding="utf-8") as f:
        app_js = f.read()
    assert "redetected_alerts" in app_js, (
        "le message de fin de synchronisation doit distinguer les re-détections")
    assert "Alertes re-détectées" in inspect.getsource(api), (
        "le mail d'étape doit distinguer les re-détections")
    assert "Alertes re-détectées (re-criblage)" in inspect.getsource(tasks), (
        "le mail d'homologation doit distinguer les re-détections")

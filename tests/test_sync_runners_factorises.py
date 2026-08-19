"""
Fusion des cycles OFAC et DGT sur le moteur générique de remplacement de liste.

`run_ofac_sync` (138 lignes) et `run_dgt_sync` (124 lignes) rejouaient à la main
le cycle déjà écrit dans `_run_list_replacement_sync` (148 lignes) : 98 lignes
identiques sur 138 entre OFAC et DGT (71 %), 90 et 91 lignes identiques avec le
générique. Trois copies du chemin d'ingestion le plus critique de
l'application, donc trois endroits à corriger à chaque correctif — et de fait
elles avaient divergé : le générique clôt la progression persistée du snapshot
(`processed_count` final, `phase` DELTA puis DONE) et sait faire un
téléchargement conditionnel, les deux copies non.

Mesuré avant d'en faire un argument : le téléchargement conditionnel est
**inerte** sur ces deux sources aujourd'hui. OFAC annonce un `Last-Modified`
mais ne l'honore pas (requête conditionnelle avec la date exacte : 200 et
126 Mo transférés), DGT n'envoie ni `Last-Modified` ni `ETag` (12 Mo). Aucun
gain de bande passante, et aucun risque de 304 abusif non plus — le jour où
l'un des deux publie de vrais validateurs, il en profite sans code nouveau.

Une seule différence de fond justifiait la copie OFAC : elle rafraîchit le
graphe de détentions (ProfileRelationships) au même rythme que la liste. Elle
passe maintenant par le crochet `after_persist` du moteur générique.

Ce fichier verrouille ce que la fusion ne doit pas avoir changé (identifiants
de snapshot, noms de fichiers archivés, graphe de détentions) et ce qu'elle
apporte aux deux sources (304, progression).
"""
import inspect
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import fiskr.sync as sync_mod
from fiskr.database import (Base, Snapshot, EntityRelationship, SyncReport,
                            WatchlistEntity)
from fiskr.sync import run_ofac_sync, run_dgt_sync, _run_list_replacement_sync


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'runners.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _fetcher(contenu: str):
    def fetch(url, dest_path):
        Path(dest_path).write_text(contenu, encoding="utf-8")
    return fetch


def _profil(entity_id: str, nom: str):
    return {"entity_id": entity_id, "entity_type": "I", "primary_name": nom,
            "source": "OFAC", "list_type": "OFAC"}


# ------------------------- le graphe de détentions -------------------------

def test_le_graphe_de_detentions_ofac_est_rafraichi_avec_la_liste(db, monkeypatch):
    """La seule spécificité OFAC. Si le crochet `after_persist` n'est pas
    branché, les relations disparaissent en silence : l'agrégation des
    détentions (règle des 50 %) ne trouverait plus aucun actionnaire."""
    def parseur(chemin, relations_out=None):
        if relations_out is not None:
            relations_out.append({
                "from_entity_id": "CIBLE-1", "to_entity_id": "OWNER-1",
                "relation_type": "OWNERSHIP", "relation_label": "détient 60 %"})
        return iter([_profil("CIBLE-1", "Cible Un"), _profil("OWNER-1", "Owner Un")])

    monkeypatch.setattr(sync_mod, "parse_ofac_advanced_xml", parseur)
    rapport = run_ofac_sync(db, fetcher=_fetcher("<xml/>"))

    assert rapport.status == "SUCCESS", rapport.message
    liens = db.query(EntityRelationship).filter(
        EntityRelationship.source == "OFAC").all()
    assert len(liens) == 1
    assert (liens[0].from_entity_id, liens[0].to_entity_id) == ("CIBLE-1", "OWNER-1")
    assert liens[0].relation_type == "OWNERSHIP"


def test_le_graphe_est_remplace_et_non_cumule(db, monkeypatch):
    """`refresh_source_relationships` est idempotent : une relation retirée de
    la source officielle doit disparaître du graphe, pas s'y sédimenter."""
    relations = [{"from_entity_id": "CIBLE-1", "to_entity_id": "OWNER-1",
                  "relation_type": "OWNERSHIP", "relation_label": "60 %"},
                 {"from_entity_id": "CIBLE-1", "to_entity_id": "OWNER-2",
                  "relation_type": "OWNERSHIP", "relation_label": "40 %"}]

    def parseur(chemin, relations_out=None, _etat={"n": 0}):
        _etat["n"] += 1
        if relations_out is not None:
            relations_out.extend(relations if _etat["n"] == 1 else relations[:1])
        return iter([_profil("CIBLE-1", f"Cible Un v{_etat['n']}")])

    monkeypatch.setattr(sync_mod, "parse_ofac_advanced_xml", parseur)
    run_ofac_sync(db, fetcher=_fetcher("<xml v='1'/>"))
    assert db.query(EntityRelationship).count() == 2

    run_ofac_sync(db, fetcher=_fetcher("<xml v='2'/>"))
    assert db.query(EntityRelationship).count() == 1


def test_dgt_ne_touche_pas_au_graphe(db, monkeypatch):
    """Le crochet est optionnel : une source sans relations n'en écrit aucune
    et n'efface pas celles d'une autre source."""
    db.add(EntityRelationship(from_entity_id="A", to_entity_id="B",
                              relation_type="OWNERSHIP", source="OFAC"))
    db.commit()
    monkeypatch.setattr(sync_mod, "parse_dgt_gels_json",
                        lambda chemin: iter([_profil("DGT-1", "Gelé Un")]))
    rapport = run_dgt_sync(db, fetcher=_fetcher("{}"))
    assert rapport.status == "SUCCESS", rapport.message
    assert db.query(EntityRelationship).count() == 1


# ---------------- ce que l'archivage ne doit pas avoir bougé ----------------

@pytest.mark.parametrize("runner, parseur_nom, contenu, prefixe, fichier, type_fichier", [
    (run_ofac_sync, "parse_ofac_advanced_xml", "<xml/>", "ofac-sync-",
     "SDN_ADVANCED_", "WATCHLIST_OFAC"),
    (run_dgt_sync, "parse_dgt_gels_json", "{}", "dgt-sync-",
     "Registre_gels_DGT_", "WATCHLIST_DGT"),
])
def test_identifiants_et_noms_de_fichiers_inchanges(
        db, monkeypatch, runner, parseur_nom, contenu, prefixe, fichier, type_fichier):
    """Les identifiants de snapshot et les noms de fichiers archivés sont cités
    dans des rapports d'homologation déjà produits : ils ne doivent pas changer
    de forme sous prétexte d'une refonte interne."""
    def parseur(chemin, **kwargs):
        return iter([_profil("X-1", "Profil Un")])

    monkeypatch.setattr(sync_mod, parseur_nom, parseur)
    rapport = runner(db, fetcher=_fetcher(contenu))
    assert rapport.status == "SUCCESS", rapport.message

    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == rapport.snapshot_id).first()
    assert snap.snapshot_id.startswith(prefixe)
    assert snap.file_name.startswith(fichier)
    assert snap.file_name.endswith(".xml" if type_fichier == "WATCHLIST_OFAC" else ".json")
    assert snap.file_type == type_fichier
    assert snap.status == "READY"
    assert snap.record_count == 1


@pytest.mark.parametrize("runner, parseur_nom, contenu, source", [
    (run_ofac_sync, "parse_ofac_advanced_xml", "<xml/>", "OFAC"),
    (run_dgt_sync, "parse_dgt_gels_json", "{}", "DGT"),
])
def test_le_delta_et_le_rechargement_du_cache_restent_cables(
        db, monkeypatch, runner, parseur_nom, contenu, source):
    monkeypatch.setattr(sync_mod, parseur_nom,
                        lambda chemin, **kw: iter([_profil("X-1", "Profil Un")]))
    recharges = []
    r1 = runner(db, fetcher=_fetcher(contenu), reload_cache=lambda: recharges.append(1))
    assert r1.status == "SUCCESS" and r1.added_count == 1
    assert recharges == [1], "la liste passée en production sans recharger le cache"

    monkeypatch.setattr(sync_mod, parseur_nom,
                        lambda chemin, **kw: iter([_profil("X-1", "Profil Un"),
                                                   _profil("X-2", "Profil Deux")]))
    r2 = runner(db, fetcher=_fetcher(contenu + " "), reload_cache=lambda: recharges.append(2))
    assert r2.added_count == 1 and r2.removed_count == 0
    assert r2.previous_snapshot_id == r1.snapshot_id
    assert db.query(SyncReport).filter(SyncReport.source == source).count() == 2


@pytest.mark.parametrize("runner, parseur_nom, contenu", [
    (run_ofac_sync, "parse_ofac_advanced_xml", "<xml/>"),
    (run_dgt_sync, "parse_dgt_gels_json", "{}"),
])
def test_fichier_identique_reste_un_no_change(db, monkeypatch, runner, parseur_nom, contenu):
    monkeypatch.setattr(sync_mod, parseur_nom,
                        lambda chemin, **kw: iter([_profil("X-1", "Profil Un")]))
    assert runner(db, fetcher=_fetcher(contenu)).status == "SUCCESS"
    r2 = runner(db, fetcher=_fetcher(contenu))
    assert r2.status == "NO_CHANGE"
    assert r2.snapshot_id is None


# ------------------------ ce que la fusion leur apporte ------------------------

def test_ofac_et_dgt_suivent_la_progression_persistee(db, monkeypatch):
    """Le snapshot porte la progression persistée, seul canal qui traverse les
    processus. Les deux copies laissaient `phase` bloquée sur PERSIST une fois
    la synchronisation finie, et ne fixaient `processed_count` qu'au rythme des
    commits périodiques (donc jamais pour une liste de moins de 1 000 fiches).
    Le générique clôt les deux."""
    monkeypatch.setattr(sync_mod, "parse_ofac_advanced_xml",
                        lambda chemin, **kw: iter([_profil("X-1", "Profil Un")]))
    rapport = run_ofac_sync(db, fetcher=_fetcher("<xml/>"))
    snap = db.query(Snapshot).filter(Snapshot.snapshot_id == rapport.snapshot_id).first()
    assert snap.processed_count == 1
    assert snap.phase == "DONE"


def test_les_deux_runners_delegent_au_moteur_generique():
    """Garde-fou de non-régression de la refonte : si quelqu'un recopie le
    cycle dans l'un des deux runners, la duplication revient sans bruit."""
    for runner in (run_ofac_sync, run_dgt_sync):
        code = inspect.getsource(runner)
        assert "_run_list_replacement_sync(" in code
        # Aucun des deux ne doit refaire le cycle a la main
        for motif in ("hashlib.sha256(", "_supersede_previous_snapshots(",
                      "_existing_snapshot_with_hash(", "calculate_delta("):
            assert motif not in code, f"{runner.__name__} refait {motif}"
    assert len(inspect.getsource(run_ofac_sync).splitlines()) < 45
    assert len(inspect.getsource(run_dgt_sync).splitlines()) < 30


def test_le_crochet_after_persist_est_optionnel():
    signature = inspect.signature(_run_list_replacement_sync)
    assert signature.parameters["after_persist"].default is None

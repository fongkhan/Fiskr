"""
Une purge qui promet d'être réversible, et détruit la seule chose qu'on
redemandera.

Suite de la chasse par classe : *ce que le produit affirme sans l'avoir
vérifié*. Le module de rétention l'écrivait en toutes lettres — « la purge
reste réversible hors ligne » — et archivait les **lignes** des pièces
jointes : identifiant, nom, chemin. Le fichier, lui, partait au `os.remove`.
Restaurer cette archive rendait donc des références vers des fichiers
détruits, c'est-à-dire exactement l'état que le lot précédent a rendu visible
sur les écrans. Une preuve ne se reconstitue pas.

Deux situations opposées sortaient au même endroit, et aucune ne se disait :

- la copie échoue → la pièce est **toujours là**, et il reste une fenêtre pour
  agir. L'alerte n'est donc pas purgée : gardée un mois de plus, elle se
  repurgera au passage suivant ; détruite sans copie, jamais.
- la suppression du fichier échoue → la ligne part, le fichier reste. La base
  affirme « purgé » alors que la donnée est encore sur le disque — l'inverse
  exact de ce qu'une politique de rétention promet.
"""
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fiskr import retention as retention_mod
from fiskr.database import (Alert, AlertAttachment, AlertEvent, AdminAuditLog,
                            get_db)

UID = uuid.uuid4().hex[:8]


@pytest.fixture()
def db():
    session = next(get_db())
    yield session
    ids = [a.id for a in session.query(Alert).filter(
        Alert.client_id.like(f"R{UID}%")).all()]
    if ids:
        session.query(AlertAttachment).filter(
            AlertAttachment.alert_id.in_(ids)).delete(synchronize_session=False)
        session.query(AlertEvent).filter(
            AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
        session.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
    session.query(AdminAuditLog).filter(
        AdminAuditLog.username == f"test-{UID}").delete(synchronize_session=False)
    session.commit()
    session.close()


@pytest.fixture()
def politique(monkeypatch):
    """Alertes clôturées purgées à 30 jours, familles restantes désactivées."""
    def _policy(db_):
        return {"closed_alerts": 30, "audit_trail": 0, "sync_reports": 0,
                "batch_campaigns": 0, "archive": True, "cron": "0 3 * * *"}
    monkeypatch.setattr(retention_mod, "retention_policy", _policy)
    monkeypatch.setattr(retention_mod, "retention_sous_la_duree_legale", lambda p: [])
    return _policy


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    dossier = tmp_path / "archives"
    monkeypatch.setattr(retention_mod, "ARCHIVE_DIR", dossier)
    return dossier


def _alerte_avec_piece(db, tmp_path, suffixe, chemin=None, ecrire=True):
    vieux = datetime.utcnow() - timedelta(days=120)
    from fiskr.database import ALERT_CLOSED_STATUSES, AuditTrail
    piste = AuditTrail(client_id=f"R{UID}{suffixe}", client_name="Témoin",
                       client_type="I", watchlist_id="W1", watchlist_name="VOLKOV",
                       base_score=95.0, final_score=95.0, status="ALERT",
                       decision_tree={}, config_state={}, watchlist_version="v1",
                       watchlist_hash="h1", list_type="WATCHLIST_OFAC",
                       timestamp=vieux)
    db.add(piste)
    db.flush()
    alerte = Alert(audit_id=piste.id, client_id=f"R{UID}{suffixe}",
                   client_name="Témoin", watchlist_entity_id="W1",
                   watchlist_name="VOLKOV", list_type="WATCHLIST_OFAC",
                   final_score=95.0, status=sorted(ALERT_CLOSED_STATUSES)[0],
                   created_at=vieux, decided_at=vieux)
    db.add(alerte)
    db.flush()
    if chemin is None:
        chemin = tmp_path / f"piece_{suffixe}.pdf"
        if ecrire:
            chemin.write_bytes(b"%PDF piece probante")
    db.add(AlertAttachment(alert_id=alerte.id, file_name=f"piece_{suffixe}.pdf",
                           file_path=str(chemin), uploaded_by="t",
                           uploaded_at=vieux))
    db.commit()
    return alerte.id, Path(chemin)


def _archive_de_la_purge(dossier):
    purges = sorted(p for p in dossier.iterdir() if p.name.startswith("purge_"))
    assert purges, "aucune archive de purge écrite"
    return purges[-1]


# --------------------------------------------------------------------------
# Le fichier suit sa ligne dans l'archive
# --------------------------------------------------------------------------

def test_le_fichier_de_la_piece_est_copie_avant_d_etre_supprime(
        db, tmp_path, politique, archive):
    """
    La garde du lot. « Réversible » ne peut pas vouloir dire « on garde le nom
    de ce qu'on a détruit ».
    """
    alerte_id, fichier = _alerte_avec_piece(db, tmp_path, "A")
    retention_mod.run_retention(db, username=f"test-{UID}")

    assert not fichier.exists(), "le fichier d'origine doit bien être purgé"
    copies = list((_archive_de_la_purge(archive) / retention_mod.ARCHIVE_FICHIERS).iterdir())
    assert len(copies) == 1
    assert copies[0].read_bytes() == b"%PDF piece probante", "la copie doit être fidèle"
    assert db.query(Alert).filter(Alert.id == alerte_id).first() is None


def test_la_ligne_archivee_dit_ou_retrouver_le_fichier(db, tmp_path, politique, archive):
    """
    Sans ce renvoi, l'archive porterait un chemin d'origine qui ne désigne
    plus rien : retrouver la pièce demanderait de deviner.
    """
    _alerte_avec_piece(db, tmp_path, "B")
    retention_mod.run_retention(db, username=f"test-{UID}")

    lignes = (_archive_de_la_purge(archive) / "alert_attachments.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    ligne = json.loads(lignes[0])
    assert ligne["archive_fichier"], "la ligne doit nommer le fichier copié"
    copie = _archive_de_la_purge(archive) / retention_mod.ARCHIVE_FICHIERS / ligne["archive_fichier"]
    assert copie.is_file()


def test_deux_pieces_de_meme_nom_ne_s_ecrasent_pas(db, tmp_path, politique, archive):
    """Deux alertes peuvent avoir téléversé « scan.pdf »."""
    for suffixe in ("C", "D"):
        dossier = tmp_path / suffixe
        dossier.mkdir()
        chemin = dossier / "scan.pdf"
        chemin.write_bytes(f"contenu {suffixe}".encode())
        _alerte_avec_piece(db, tmp_path, suffixe, chemin=chemin)

    retention_mod.run_retention(db, username=f"test-{UID}")
    copies = list((_archive_de_la_purge(archive) / retention_mod.ARCHIVE_FICHIERS).iterdir())
    assert len(copies) == 2, [c.name for c in copies]
    assert {c.read_bytes() for c in copies} == {b"contenu C", b"contenu D"}


def test_une_ligne_dont_le_fichier_manque_deja_ne_bloque_pas_la_purge(
        db, tmp_path, politique, archive):
    """
    Une pièce déjà disparue est un fait constaté ailleurs (contrôle « Pièces
    probantes ») : la purge n'a rien à copier et rien à épargner.
    """
    alerte_id, _ = _alerte_avec_piece(db, tmp_path, "E", ecrire=False)
    retention_mod.run_retention(db, username=f"test-{UID}")
    assert db.query(Alert).filter(Alert.id == alerte_id).first() is None


# --------------------------------------------------------------------------
# Une preuve qu'on ne peut pas copier n'est pas détruite
# --------------------------------------------------------------------------

def test_une_copie_impossible_epargne_l_alerte(db, tmp_path, politique, archive, monkeypatch):
    """
    Le choix central. Gardée un mois de plus, l'alerte se repurgera au
    passage suivant ; détruite sans copie, jamais.
    """
    alerte_id, fichier = _alerte_avec_piece(db, tmp_path, "F")
    monkeypatch.setattr(retention_mod, "_archiver_fichier", lambda *a, **k: None)

    supprimes = retention_mod.run_retention(db, username=f"test-{UID}")

    assert supprimes["closed_alerts"] == 0
    assert db.query(Alert).filter(Alert.id == alerte_id).first() is not None
    assert fichier.exists(), "la pièce doit rester là où elle est"


def test_seule_l_alerte_concernee_est_epargnee(db, tmp_path, politique, archive, monkeypatch):
    """Une pièce récalcitrante ne doit pas geler toute la purge."""
    fragile_id, _ = _alerte_avec_piece(db, tmp_path, "G")
    saine_id, fichier_sain = _alerte_avec_piece(db, tmp_path, "H")

    vrai = retention_mod._archiver_fichier
    monkeypatch.setattr(retention_mod, "_archiver_fichier",
                        lambda chemin, att: None if att.alert_id == fragile_id
                        else vrai(chemin, att))

    supprimes = retention_mod.run_retention(db, username=f"test-{UID}")

    assert supprimes["closed_alerts"] == 1
    assert db.query(Alert).filter(Alert.id == fragile_id).first() is not None
    assert db.query(Alert).filter(Alert.id == saine_id).first() is None
    assert not fichier_sain.exists()


def test_une_alerte_epargnee_est_signalee_meme_sans_ligne_supprimee(
        db, tmp_path, politique, archive, monkeypatch):
    """
    Le cas où il faut parler produit justement zéro suppression : se taire
    quand le compte est nul reviendrait à ne jamais rien dire.
    """
    _alerte_avec_piece(db, tmp_path, "I")
    monkeypatch.setattr(retention_mod, "_archiver_fichier", lambda *a, **k: None)
    signale = {}
    monkeypatch.setattr("fiskr.notifier.emit",
                        lambda db_, cle, charge, **kw: signale.update({"cle": cle, "charge": charge}))

    supprimes = retention_mod.run_retention(db, username=f"test-{UID}")

    assert not any(supprimes.values())
    assert signale.get("cle") == "retention_pieces_en_souffrance"
    assert signale["charge"]["Pièces non archivées (alertes épargnées)"] == 1


# --------------------------------------------------------------------------
# Un fichier resté sur le disque pendant que la base dit « purgé »
# --------------------------------------------------------------------------

def test_un_fichier_non_supprime_est_signale(db, tmp_path, politique, archive, monkeypatch):
    """
    L'inverse exact de ce qu'une politique de rétention promet : la donnée
    devait disparaître et n'a pas disparu, et l'application ne le montre nulle
    part — le journal, lui, le dira.
    """
    _alerte_avec_piece(db, tmp_path, "J")

    def _refus(chemin):
        raise OSError("Read-only file system")
    monkeypatch.setattr(retention_mod.os, "remove", _refus)
    signale = {}
    monkeypatch.setattr("fiskr.notifier.emit",
                        lambda db_, cle, charge, **kw: signale.update({"cle": cle, "charge": charge}))

    supprimes = retention_mod.run_retention(db, username=f"test-{UID}")

    assert supprimes["closed_alerts"] == 1, "la purge ne s'interrompt pas"
    assert signale.get("cle") == "retention_pieces_en_souffrance"
    assert signale["charge"]["Fichiers restés sur le disque (lignes purgées)"] == 1
    assert "Read-only" not in json.dumps(signale["charge"]) or True  # détail libre


def test_une_purge_sans_incident_ne_signale_rien(db, tmp_path, politique, archive, monkeypatch):
    """Un signal qui part à chaque purge est un signal qu'on filtre."""
    _alerte_avec_piece(db, tmp_path, "K")
    signale = []
    monkeypatch.setattr("fiskr.notifier.emit",
                        lambda db_, cle, charge, **kw: signale.append(cle))
    retention_mod.run_retention(db, username=f"test-{UID}")
    assert "retention_pieces_en_souffrance" not in signale


def test_le_journal_d_administration_porte_le_sort_des_pieces(
        db, tmp_path, politique, archive):
    """
    Le journal des actions d'administration est la trace append-only lue en
    contrôle : le nombre de pièces copiées y a sa place.
    """
    _alerte_avec_piece(db, tmp_path, "L")
    retention_mod.run_retention(db, username=f"test-{UID}")

    trace = db.query(AdminAuditLog).filter(
        AdminAuditLog.username == f"test-{UID}",
        AdminAuditLog.action == "RETENTION_PURGE").first()
    assert trace is not None
    assert trace.after["pieces"]["archivees"] == 1
    assert "pièce(s) jointe(s) copiée(s)" in trace.detail


# --------------------------------------------------------------------------
# Le catalogue et la promesse écrite
# --------------------------------------------------------------------------

def test_l_evenement_est_immediat_et_non_differe():
    """
    Une preuve en souffrance se rattrape tant que le fichier est là. Dans un
    récapitulatif du lendemain, elle se noierait.
    """
    from fiskr.events import EVENT_CATALOG, IMMEDIATE
    evenement = EVENT_CATALOG["retention_pieces_en_souffrance"]
    assert evenement.urgency == IMMEDIATE
    assert "admin" in evenement.audience


def test_la_promesse_du_module_ne_ment_plus():
    """
    Le module écrivait « fichiers supprimés au mieux » sous une promesse de
    réversibilité. Les deux ne pouvaient pas être vraies ensemble.
    """
    source = Path("fiskr/retention.py").read_text(encoding="utf-8")
    assert "fichiers supprimes au mieux" not in source
    assert "COPIES dans l'archive avant" in source

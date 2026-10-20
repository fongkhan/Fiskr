"""
Ce que l'homologation et les alertes ouvertes affirment reste vrai après un
changement de listes.

Quatre constats, une même classe de défaut — une affirmation qu'un geste
ultérieur rendait fausse, sans que rien ne le dise :

1. Un cahier de tests « OK » restait valable après des exclusions posées
   ensuite. Reproduit sur base jetable : cahier OK (5 interceptions sur 5),
   exclusion des 5 fiches DGT, homologation 202, dossier « APPROVED, verdict
   OK », 0 fiche DGT en production.
2. Un verdict WARN s'approuvait sans un mot : 46 des 200 dernières
   approbations de production, aucune commentée.
3. Une fiche RETIRÉE à la mise en production d'une version ne déclenchait
   rien : alerte ouverte sans mention, client gelé jamais signalé (lignes
   directrices DGT/ACPR du 18 mars 2026, §87 et §123).
4. Après re-criblage, les alertes que le cahier annonçait « résolues »
   restaient ouvertes, et la modale lisait l'arbre figé à la création
   (« 100 % · DOB : donnée manquante » pour une re-détection à 95).
"""
import json
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.backtest import (TEST_PANEL_FILE_TYPE, empreinte_testee, reset_shared_pass_memo,
                            validite_du_cahier)
from fiskr.database import (AppSetting, Alert, AlertEvent, AuditTrail, Base, ClientEntity,
                            MesureDeGel, ReviewRecord, Snapshot, WatchlistEntity,
                            compute_checksum, get_db)
from fiskr.settings import SETTING_BACKTEST_REQUIRED, set_setting
from tests.conftest import post_and_wait

UID = uuid.uuid4().hex[:6].upper()
# Lettres seules dans les noms : un chiffre en changerait la similarité
NOM_DE_FAMILLE = "SMIRNOVSKAIA" + "".join(chr(65 + int(c, 16) % 26) for c in UID)
T0 = datetime.utcnow() - timedelta(hours=6)


def _connecter(username="relecteur_affirm"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": "admin,reviewer", "roles": ["admin", "reviewer"]}


def _fiche(eid, prenom, nom, dob=None, pays="RU", reference=None):
    valeurs = {"entity_id": eid, "entity_type": "I", "primary_name": f"{prenom} {nom}",
               "individual_name_parsed": {"first_name": prenom, "last_name": nom,
                                          "maiden_name": ""},
               "aliases": {"high_priority": [], "low_priority": []},
               "dates_of_birth": [dob] if dob else [], "gender": "M",
               "countries": {"citizenship": [pays], "residence": [], "birth_country": [],
                             "jurisdiction_country": []}}
    if reference:
        valeurs["official_reference"] = reference
    return valeurs


# ======================================================================
# 1-2. HOMOLOGATION (base de la suite, par l'API)
# ======================================================================

@pytest.fixture()
def homol():
    reset_shared_pass_memo()
    db = next(get_db())
    avant = {r.key: r.value for r in db.query(AppSetting).filter(
        AppSetting.key == SETTING_BACKTEST_REQUIRED)}
    crees = []

    def lot(ftype, statut, fiches, heures=0):
        sid = f"affirm-{statut.lower()[:4]}-{uuid.uuid4().hex[:8]}"
        db.add(Snapshot(snapshot_id=sid, file_type=ftype, file_name=f"{sid}.xml",
                        file_hash=uuid.uuid4().hex, record_count=len(fiches), status=statut,
                        uploaded_at=T0 + timedelta(hours=heures)))
        for f in fiches:
            db.add(WatchlistEntity(snapshot_id=sid, entity_checksum=compute_checksum(f), **f))
        db.commit()
        crees.append(sid)
        return db.query(Snapshot).filter(Snapshot.snapshot_id == sid).one()

    def panel(prenoms):
        sid = f"affirm-panel-{uuid.uuid4().hex[:8]}"
        db.add(Snapshot(snapshot_id=sid, file_type=TEST_PANEL_FILE_TYPE, file_name="panel.gen",
                        file_hash=uuid.uuid4().hex, record_count=len(prenoms), status="READY",
                        uploaded_at=T0))
        for i, prenom in enumerate(prenoms):
            c = {"client_id": f"AFF-{UID}-{i}", "client_type": "PP",
                 "client_first_name": prenom, "client_last_name": NOM_DE_FAMILLE,
                 "client_dob": f"196{i}-01-01", "client_gender": "M",
                 "client_countries": {"nationality": ["RU"], "residence": [],
                                      "birth_country": [], "registration_country": []}}
            db.add(ClientEntity(snapshot_id=sid, entity_checksum=compute_checksum(c), **c))
        db.commit()
        crees.append(sid)
        return sid

    _connecter()
    client = TestClient(app)
    yield {"db": db, "client": client, "lot": lot, "panel": panel}
    app.dependency_overrides.pop(get_current_user, None)
    reset_shared_pass_memo()
    db.rollback()
    db.query(ReviewRecord).filter(ReviewRecord.snapshot_id.in_(crees)).delete(
        synchronize_session=False)
    db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id.in_(crees)).delete(
        synchronize_session=False)
    db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(crees)).delete(
        synchronize_session=False)
    db.query(Snapshot).filter(Snapshot.snapshot_id.in_(crees)).delete(synchronize_session=False)
    ligne = db.query(AppSetting).filter(AppSetting.key == SETTING_BACKTEST_REQUIRED).first()
    if SETTING_BACKTEST_REQUIRED in avant:
        if ligne is not None:
            ligne.value = avant[SETTING_BACKTEST_REQUIRED]
    elif ligne is not None:
        db.delete(ligne)
    db.commit()
    from fiskr.api import load_watchlist_cache
    load_watchlist_cache(db)
    db.close()


PRENOMS = ("Ivan", "Oleg", "Boris", "Pavel", "Igor")


def _dgt(h, statut, heures=0, ftype="WATCHLIST_DGT"):
    return h["lot"](ftype, statut, [_fiche(f"AFF-{UID}-{i}", p, NOM_DE_FAMILLE, f"196{i}-01-01")
                                    for i, p in enumerate(PRENOMS)], heures)


def _cahier(h, snap, panel):
    reponse = post_and_wait(h["client"], f"/api/review/snapshots/{snap.snapshot_id}/backtest",
                            json={"panel_snapshot_id": panel})
    assert reponse.status_code == 202, reponse.text
    h["db"].expire_all()
    return h["db"].query(Snapshot).filter(Snapshot.snapshot_id == snap.snapshot_id).one()


def _rapport(h, snap, verdict="OK"):
    """Rapport au verdict donné, qui porte l'empreinte de ce qu'il « a testé »
    (l'état présent) — pour éprouver le gate sans relancer un criblage."""
    snap.backtest_report = {"verdict": verdict, "gap_pct": 30.0 if verdict == "WARN" else 0.0,
                            "threshold_pct": 20.0, "gap_direction": "INCREASE",
                            "tested_scope": empreinte_testee(h["db"], [snap])}
    h["db"].commit()


def test_un_cahier_ok_puis_une_exclusion_rend_l_homologation_impossible(homol):
    """La reproduction elle-même : cahier OK, puis exclusion des cinq fiches —
    l'homologation rendait 202 et vidait la DGT en production."""
    h = homol
    set_setting(h["db"], SETTING_BACKTEST_REQUIRED, True)
    h["db"].commit()
    _dgt(h, "READY")
    candidat = _dgt(h, "PENDING_REVIEW", heures=1)
    from fiskr.api import load_watchlist_cache
    load_watchlist_cache(h["db"])
    candidat = _cahier(h, candidat, h["panel"](PRENOMS))
    assert candidat.backtest_report["verdict"] == "OK"
    assert validite_du_cahier(h["db"], candidat)["stale"] is False

    ids = [e.id for e in h["db"].query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == candidat.snapshot_id).all()]
    pose = h["client"].post(f"/api/review/snapshots/{candidat.snapshot_id}/exclusions",
                            data={"entity_ids": json.dumps(ids), "justification": "homonymes"})
    assert pose.status_code == 200, pose.text
    assert "périmé" in pose.json()["message"], "celui qui exclut doit l'apprendre là"

    ligne = next(r for r in h["client"].get("/api/review/pending").json()["pending"]
                 if r["snapshot_id"] == candidat.snapshot_id)
    assert ligne["backtest_verdict"] == "OK" and ligne["backtest_stale"] is True
    assert any("Exclusions de WATCHLIST_DGT" in m for m in ligne["backtest_stale_reasons"])

    refus = post_and_wait(h["client"], f"/api/review/snapshots/{candidat.snapshot_id}/approve",
                          json={"comment": "Relu, conforme au cahier de tests."})
    assert refus.status_code == 400, refus.text
    assert "périmé" in refus.json()["detail"]
    h["db"].expire_all()
    assert h["db"].query(Snapshot).filter(
        Snapshot.snapshot_id == candidat.snapshot_id).one().status == "PENDING_REVIEW"
    assert not h["db"].query(ReviewRecord).filter(
        ReviewRecord.snapshot_id == candidat.snapshot_id).count()


def test_une_exclusion_levee_apres_le_cahier_le_rend_aussi_perime(homol):
    """« Posée ou levée » : le cahier a testé une exclusion qui n'existe plus."""
    h = homol
    _dgt(h, "READY")
    candidat = _dgt(h, "PENDING_REVIEW", heures=1)
    une = h["db"].query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == candidat.snapshot_id).first()
    une.excluded = True
    h["db"].commit()
    _rapport(h, candidat)
    assert validite_du_cahier(h["db"], candidat)["stale"] is False

    retrait = h["client"].post(f"/api/review/snapshots/{candidat.snapshot_id}/exclusions/remove",
                               json={"entity_ids": [une.id]})
    assert retrait.status_code == 200, retrait.text
    validite = validite_du_cahier(h["db"], candidat)
    assert validite["stale"] is True
    assert "(1 → 0 fiche(s) exclue(s))" in " ".join(validite["reasons"])


def test_un_changement_de_production_rend_le_cahier_perime_sauf_ses_propres_candidats(homol):
    """Une autre liste mise en production change l'univers que le cahier a vu.
    Un candidat du MÊME cahier (vague consolidée) mis en production, non : sans
    cette exception, approuver la première liste d'une vague rendait périmé le
    cahier de toutes les autres."""
    h = homol
    db = h["db"]
    # Types qu'aucun autre test n'emploie : la base de la suite est partagée,
    # et une approbation remplace TOUS les lots en production de sa liste.
    _dgt(h, "READY", ftype="WATCHLIST_NZ")
    dgt = _dgt(h, "PENDING_REVIEW", heures=1, ftype="WATCHLIST_NZ")
    ancien_un = _dgt(h, "READY", ftype="WATCHLIST_TR_MASAK")
    un = _dgt(h, "PENDING_REVIEW", heures=1, ftype="WATCHLIST_TR_MASAK")
    rapport = {"verdict": "OK", "gap_pct": 0.0, "threshold_pct": 20.0,
               "tested_scope": empreinte_testee(db, [dgt, un])}
    dgt.backtest_report = rapport
    un.backtest_report = rapport
    db.commit()
    assert validite_du_cahier(db, dgt)["stale"] is False

    # Le candidat UN du même cahier passe en production : rien n'a changé
    # par rapport à l'univers candidat testé.
    un.status, ancien_un.status = "READY", "SUPERSEDED"
    db.commit()
    assert validite_du_cahier(db, dgt) == {"stale": False, "reasons": []}

    # Une liste que ce cahier n'a pas testée entre en production : périmé.
    h["lot"]("WATCHLIST_EG_TERROR", "READY", [_fiche(f"AFF-{UID}-EG", "Yuri", NOM_DE_FAMILLE)])
    validite = validite_du_cahier(db, dgt)
    assert validite["stale"] is True
    assert validite["reasons"] == ["Production modifiée depuis le cahier : WATCHLIST_EG_TERROR."]


def test_un_rapport_sans_empreinte_ne_vaut_pas_ok(homol):
    """Un rapport antérieur au suivi : rien n'établit ce qu'il a testé."""
    h = homol
    candidat = _dgt(h, "PENDING_REVIEW", heures=1)
    candidat.backtest_report = {"verdict": "OK", "gap_pct": 0.0}
    h["db"].commit()
    validite = validite_du_cahier(h["db"], candidat)
    assert validite["stale"] is True and "antérieur au suivi" in validite["reasons"][0]


def test_un_verdict_warn_sans_commentaire_est_refuse_et_le_motif_va_au_dossier(homol):
    """Sans cahier obligatoire, un WARN s'approuvait sans un mot."""
    h = homol
    _dgt(h, "READY", ftype="WATCHLIST_GB_FCDO")
    candidat = _dgt(h, "PENDING_REVIEW", heures=1, ftype="WATCHLIST_GB_FCDO")
    _rapport(h, candidat, verdict="WARN")
    url = f"/api/review/snapshots/{candidat.snapshot_id}/approve"

    for commentaire in (None, "", "ok"):
        refus = post_and_wait(h["client"], url, json={"comment": commentaire})
        assert refus.status_code == 400, (commentaire, refus.text)
        assert "commentaire motivé" in refus.json()["detail"]

    motif = "Hausse attendue : nouvelles désignations du 18/10, homonymes vérifiés."
    accepte = post_and_wait(h["client"], url, json={"comment": motif})
    assert accepte.status_code == 202, accepte.text
    dossier = h["db"].query(ReviewRecord).filter(
        ReviewRecord.snapshot_id == candidat.snapshot_id).one()
    assert dossier.comment == motif
    assert dossier.backtest_validity["comment_required"] == "cahier de tests au verdict WARN"
    assert dossier.backtest_validity["stale"] is False


def test_un_rapport_perime_exige_un_motif_et_le_dossier_dit_qu_il_l_etait(homol):
    """Sans cahier obligatoire, un rapport périmé n'empêche pas d'approuver —
    mais il ne passe plus pour un OK : motif exigé, péremption au dossier."""
    h = homol
    _dgt(h, "READY", ftype="WATCHLIST_IL_CRYPTO")
    candidat = _dgt(h, "PENDING_REVIEW", heures=1, ftype="WATCHLIST_IL_CRYPTO")
    _rapport(h, candidat)
    une = h["db"].query(WatchlistEntity).filter(
        WatchlistEntity.snapshot_id == candidat.snapshot_id).first()
    une.excluded, une.exclusion_justification = True, "homonyme"
    h["db"].commit()
    url = f"/api/review/snapshots/{candidat.snapshot_id}/approve"
    refus = post_and_wait(h["client"], url, json={"comment": ""})
    assert refus.status_code == 400 and "périmé" in refus.json()["detail"]
    motif = "Exclusion d'un homonyme avéré, cahier à relancer après mise en production."
    assert post_and_wait(h["client"], url, json={"comment": motif}).status_code == 202
    dossier = h["db"].query(ReviewRecord).filter(
        ReviewRecord.snapshot_id == candidat.snapshot_id).one()
    assert dossier.backtest_validity["stale"] is True
    assert dossier.backtest_validity["verdict"] == "OK"
    assert dossier.comment == motif
    historique = h["client"].get(f"/api/review/history/{dossier.id}").json()
    assert historique["backtest_stale"] is True


def test_en_groupe_le_motif_commun_doit_nommer_les_lots_qu_il_couvre(homol):
    """Homologation groupée : un motif commun est recevable s'il est EXPLICITE —
    l'écran nomme les lots WARN ou périmés, et la requête les désigne
    (`comment_covers`). Un lot non désigné est refusé, les autres passent."""
    h = homol
    db = h["db"]
    _dgt(h, "READY", ftype="WATCHLIST_AU_DFAT")
    warn = _dgt(h, "PENDING_REVIEW", heures=1, ftype="WATCHLIST_AU_DFAT")
    _rapport(h, warn, verdict="WARN")
    sans_cahier = h["lot"]("WATCHLIST_ID_DTTOT", "PENDING_REVIEW",
                           [_fiche(f"AFF-{UID}-B", "Yuri", NOM_DE_FAMILLE)], heures=1)
    motif = "Écart revu liste par liste : désignations nouvelles, homonymes écartés."

    sans_motif = post_and_wait(h["client"], "/api/review/snapshots/approve-bulk",
                               json={"snapshot_ids": [warn.snapshot_id, sans_cahier.snapshot_id]})
    corps = sans_motif.json()
    assert [a["snapshot_id"] for a in corps["approved"]] == [sans_cahier.snapshot_id]
    assert corps["refused"][0]["snapshot_id"] == warn.snapshot_id
    assert "commentaire motivé" in corps["refused"][0]["reason"]

    non_designe = post_and_wait(h["client"], "/api/review/snapshots/approve-bulk",
                                json={"snapshot_ids": [warn.snapshot_id], "comment": motif})
    assert non_designe.json()["refused"][0]["snapshot_id"] == warn.snapshot_id
    assert "ne le couvre pas" in non_designe.json()["refused"][0]["reason"]

    designe = post_and_wait(h["client"], "/api/review/snapshots/approve-bulk",
                            json={"snapshot_ids": [warn.snapshot_id], "comment": motif,
                                  "comment_covers": [warn.snapshot_id]})
    assert [a["snapshot_id"] for a in designe.json()["approved"]] == [warn.snapshot_id]
    dossier = db.query(ReviewRecord).filter(ReviewRecord.snapshot_id == warn.snapshot_id).one()
    assert dossier.comment == motif
    assert dossier.backtest_validity["comment_shared_by"] == 1


# ======================================================================
# 3-4. RADIATIONS ET CADUCITÉ (base jetable)
# ======================================================================

@pytest.fixture
def base(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'affirmations.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    appels = []
    import fiskr.notifier as notifier_mod
    monkeypatch.setattr(notifier_mod, "emit",
                        lambda db, cle, charge=None, **kw: appels.append((cle, charge or {})))
    session.notifications = appels
    yield session
    session.close()


def _lot_dgt(db, sid, fiches, statut="READY", heures=0):
    db.add(Snapshot(snapshot_id=sid, file_type="WATCHLIST_DGT", file_name=f"{sid}.xml",
                    file_hash=uuid.uuid4().hex, record_count=len(fiches), status=statut,
                    uploaded_at=T0 + timedelta(hours=heures)))
    for f in fiches:
        db.add(WatchlistEntity(snapshot_id=sid, entity_checksum=compute_checksum(f), **f))
    db.commit()


def _referentiel(db, clients):
    sid = f"clients-{uuid.uuid4().hex[:6]}"
    db.add(Snapshot(snapshot_id=sid, file_type="CLIENT_BASE", file_name="clients.csv",
                    file_hash=uuid.uuid4().hex, record_count=len(clients), status="READY"))
    for client_id, prenom, nom, statut, *pays in clients:
        db.add(ClientEntity(
            snapshot_id=sid, client_id=client_id, client_type="PP", client_first_name=prenom,
            client_last_name=nom, client_dob="1960-01-01", client_gender="M",
            client_status=statut,
            client_countries={"nationality": pays[0] if pays else ["RU"], "residence": [],
                              "birth_country": [], "registration_country": []},
            entity_checksum=uuid.uuid4().hex))
    db.commit()


def _alerte(db, client_id, entity_id, statut="OPEN"):
    audit = AuditTrail(client_id=client_id, client_name=client_id, client_type="PP",
                       watchlist_id=entity_id, watchlist_name=entity_id, base_score=100.0,
                       final_score=100.0, status="ALERT", decision_tree={}, config_state={},
                       watchlist_version="v1", watchlist_hash="h", list_type="WATCHLIST_DGT")
    db.add(audit)
    db.flush()
    alerte = Alert(audit_id=audit.id, client_id=client_id, client_name=client_id,
                   watchlist_entity_id=entity_id, watchlist_name=entity_id, final_score=100.0,
                   list_type="WATCHLIST_DGT", status=statut, channel="SCREENING",
                   priority="HIGH")
    db.add(alerte)
    db.commit()
    return alerte


def _mettre_en_production(db, nouveau, precedent):
    """Ce que fait l'approbation : la version précédente quitte la production."""
    db.query(Snapshot).filter(Snapshot.snapshot_id == precedent).update(
        {"status": "SUPERSEDED"})
    db.commit()


def _actions(db, alerte):
    return [e.action for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == alerte.id).order_by(AlertEvent.id).all()]


def test_v2_sans_la_fiche_dgt2_signale_l_alerte_ouverte_et_la_mesure_de_gel(base):
    """L'alerte ouverte reçoit DELISTED et devient caduque ; la mesure de gel
    en vigueur devient « radiation à examiner », une notification immédiate
    part — et rien n'est levé : statut de la mesure et client gelé intacts."""
    from fiskr.caducite import constater_les_radiations
    db = base
    v1 = [_fiche("DGT-1", "Oleg", "SOKOLOVSKY", "1961-01-01"),
          _fiche("DGT-2", "Ivan", "PETROVSKY", "1962-01-01", reference="FR-GEL-2")]
    _lot_dgt(db, "dgt-v1", v1)
    _lot_dgt(db, "dgt-v2", v1[:1], heures=1)
    _referentiel(db, [("CLI-OUVERT", "Ivan", "PETROVSKY", "ACTIF"),
                      ("CLI-GELE", "Ivan", "PETROVSKY", "GELE")])
    ouverte = _alerte(db, "CLI-OUVERT", "DGT-2")
    confirmee = _alerte(db, "CLI-GELE", "DGT-2", statut="CLOSED_CONFIRMED")
    mesure = MesureDeGel(alert_id=confirmee.id, statut="DECLAREE", client_id="CLI-GELE",
                         client_name="CLI-GELE", watchlist_entity_id="DGT-2",
                         watchlist_name="Ivan PETROVSKY", list_type="WATCHLIST_DGT",
                         ouverte_par="analyste", client_gele_ecrit=True)
    db.add(mesure)
    _mettre_en_production(db, "dgt-v2", "dgt-v1")

    compte = constater_les_radiations(db, "WATCHLIST_DGT", "dgt-v2", "dgt-v1")
    assert compte == {"removed": 1, "delisted": 1, "reidentified": 0,
                      "open_alerts_delisted": 1, "gel_measures_flagged": 1}
    db.expire_all()
    assert _actions(db, ouverte) == ["DELISTED"]
    assert ouverte.status == "OPEN", "un humain clôt, le produit propose"
    assert ouverte.delisted_at is not None and ouverte.lapsed_since is not None
    assert "radiée de WATCHLIST_DGT" in ouverte.lapsed_reason
    assert mesure.radiation_constatee_le is not None
    assert mesure.statut == "DECLAREE", "rien n'est levé automatiquement"
    assert "GEL_RADIATION_A_EXAMINER" in _actions(db, confirmee)
    assert db.query(ClientEntity).filter(
        ClientEntity.client_id == "CLI-GELE").one().client_status == "GELE"
    envois = [c for c, _ in db.notifications if c == "gel_radiation_a_examiner"]
    assert envois == ["gel_radiation_a_examiner"]

    # Idempotent : une seconde passe (job relancé) ne signale rien de plus
    assert constater_les_radiations(db, "WATCHLIST_DGT", "dgt-v2", "dgt-v1")[
        "gel_measures_flagged"] == 0
    assert _actions(db, ouverte) == ["DELISTED"]
    assert len(db.notifications) == 1


def test_un_identifiant_renomme_n_annonce_pas_de_radiation(base):
    """Le delta identifie une fiche par son entity_id : un identifiant renommé
    y apparaît retiré PUIS ajouté. Ce n'est pas la personne qui sort de la
    liste — aucune radiation, la mesure de gel n'est pas signalée, l'alerte
    dit le rapprochement. Un homonyme né une autre année, lui, ne masque pas
    la radiation."""
    from fiskr.caducite import constater_les_radiations
    db = base
    _lot_dgt(db, "dgt-v1", [_fiche("DGT-2", "Ivan", "PETROVSKY", "1962-01-01"),
                            _fiche("DGT-3", "Boris", "KUZNETSOVY", "1963-01-01")])
    _lot_dgt(db, "dgt-v2", [_fiche("FR-2026-0002", "PETROVSKY", "Ivan", "1962-01-01"),
                            _fiche("FR-2026-0003", "Boris", "KUZNETSOVY", "1980-05-05")],
             heures=1)
    renommee = _alerte(db, "CLI-1", "DGT-2")
    radiee = _alerte(db, "CLI-2", "DGT-3")
    confirmee = _alerte(db, "CLI-3", "DGT-2", statut="CLOSED_CONFIRMED")
    db.add(MesureDeGel(alert_id=confirmee.id, statut="DECLAREE", client_id="CLI-3",
                       client_name="CLI-3", watchlist_entity_id="DGT-2",
                       watchlist_name="Ivan PETROVSKY", list_type="WATCHLIST_DGT",
                       ouverte_par="analyste"))
    _mettre_en_production(db, "dgt-v2", "dgt-v1")

    compte = constater_les_radiations(db, "WATCHLIST_DGT", "dgt-v2", "dgt-v1")
    assert compte["removed"] == 2
    assert compte["reidentified"] == 1 and compte["delisted"] == 1
    assert compte["gel_measures_flagged"] == 0
    db.expire_all()
    assert _actions(db, renommee) == ["REIDENTIFIED"]
    assert renommee.delisted_at is None and renommee.lapsed_since is None
    detail = db.query(AlertEvent).filter(AlertEvent.alert_id == renommee.id).one().detail
    assert "FR-2026-0002" in detail and "aucune radiation" in detail
    assert _actions(db, radiee) == ["DELISTED"]
    assert not db.notifications


def test_une_fiche_encore_portee_par_un_autre_lot_n_est_pas_radiee(base):
    """Retirée de cette version, mais portée par un lot d'ajouts manuels encore
    en production : la personne reste criblée, rien n'est annoncé."""
    from fiskr.caducite import constater_les_radiations
    db = base
    _lot_dgt(db, "dgt-v1", [_fiche("DGT-2", "Ivan", "PETROVSKY", "1962-01-01")])
    _lot_dgt(db, "dgt-v2", [_fiche("DGT-9", "Oleg", "SOKOLOVSKY", "1961-01-01")], heures=1)
    _lot_dgt(db, "MANUAL-dgt", [_fiche("DGT-2", "Ivan", "PETROVSKY", "1962-01-01")])
    ouverte = _alerte(db, "CLI-1", "DGT-2")
    _mettre_en_production(db, "dgt-v2", "dgt-v1")
    compte = constater_les_radiations(db, "WATCHLIST_DGT", "dgt-v2", "dgt-v1")
    assert compte["removed"] == 1 and compte["delisted"] == 0
    assert _actions(db, ouverte) == []


def _criblage_v1_puis_v2(db, client_nom, pays=("RU",)):
    """v1 sans date de naissance (l'alerte s'ouvre au re-criblage), puis v2 qui
    ajoute une date de naissance d'une autre année que celle du client."""
    from fiskr.rescreen import rescreen_after_snapshot_change
    _referentiel(db, [("CLI-DOB", client_nom[0], client_nom[1], "ACTIF", list(pays))])
    _lot_dgt(db, "dgt-v1", [_fiche("DGT-2", "Igor", "PETROVSKY")])
    premier = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v1")
    assert premier["new_alerts"] == 1, premier
    alerte = db.query(Alert).one()
    _lot_dgt(db, "dgt-v2", [_fiche("DGT-2", "Igor", "PETROVSKY", "1975-06-01")], heures=1)
    db.query(Snapshot).filter(Snapshot.snapshot_id == "dgt-v1").update({"status": "SUPERSEDED"})
    db.commit()
    second = rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v2", "dgt-v1")
    db.expire_all()
    return alerte, second


def test_apres_ajout_de_la_date_de_naissance_l_alerte_porte_le_dernier_score(base):
    """Le cas observé : 100 % à la création, re-détectée à 95. L'alerte porte
    son DERNIER score et son dernier arbre ; elle franchit encore le seuil, elle
    n'est pas caduque."""
    db = base
    alerte, second = _criblage_v1_puis_v2(db, ("Igor", "PETROVSKY"))
    assert second["redetected_alerts"] == 1 and second["lapsed_alerts"] == 0
    assert alerte.final_score == 100.0, "le score d'origine reste celui de la création"
    assert alerte.latest_score == 95.0
    dernier = db.query(AuditTrail).filter(AuditTrail.id == alerte.latest_audit_id).one()
    assert dernier.id != alerte.audit_id
    assert "hors fenêtre" in dernier.decision_tree["adjustments"]["dob"]["description"]
    assert alerte.lapsed_since is None


def test_apres_ajout_de_la_date_de_naissance_l_alerte_sous_le_seuil_est_caduque(base):
    """Le client né une autre année tombe sous le seuil : l'alerte porte le
    nouveau score et le nouvel arbre, devient caduque avec le motif proposé —
    et reste OUVERTE. La modale sert le dernier arbre, l'original à côté, et la
    file la filtre."""
    from fiskr.database import get_db as vrai_get_db
    db = base
    alerte, second = _criblage_v1_puis_v2(db, ("Igor", "PROTASOV"))
    assert second["lapsed_alerts"] == 1, second
    assert alerte.status == "OPEN"
    assert alerte.latest_score < 75.0 and alerte.lapsed_since is not None
    assert "sous le seuil" in alerte.lapsed_reason
    dernier = db.query(AuditTrail).filter(AuditTrail.id == alerte.latest_audit_id).one()
    assert dernier.status == "NO_MATCH" and dernier.final_score == alerte.latest_score
    assert "LAPSED" in _actions(db, alerte)

    def _session():
        yield db
    app.dependency_overrides[vrai_get_db] = _session
    _connecter()
    try:
        client = TestClient(app)
        detail = client.get(f"/api/alerts/{alerte.id}").json()
        assert detail["lapsed"] is True and detail["latest_score"] == alerte.latest_score
        assert "hors fenêtre" in detail["decision_tree"]["adjustments"]["dob"]["description"]
        assert "manquante" in detail["decision_tree_origin"]["adjustments"]["dob"]["description"]
        assert detail["decision_tree_audit_id"] == alerte.latest_audit_id
        file = client.get("/api/alerts?channel=SCREENING&lapsed=true").json()
        assert [a["id"] for a in file["items"]] == [alerte.id]
    finally:
        app.dependency_overrides.pop(vrai_get_db, None)
        app.dependency_overrides.pop(get_current_user, None)


def test_un_couple_qui_refranchit_le_seuil_n_est_plus_caduque(base):
    """La date de naissance retirée en v3 : le couple franchit de nouveau le
    seuil, la caducité s'efface et l'historique le dit."""
    from fiskr.rescreen import rescreen_after_snapshot_change
    db = base
    alerte, _ = _criblage_v1_puis_v2(db, ("Igor", "PROTASOV"))
    assert alerte.lapsed_since is not None
    _lot_dgt(db, "dgt-v3", [_fiche("DGT-2", "Igor", "PETROVSKY")], heures=2)
    db.query(Snapshot).filter(Snapshot.snapshot_id == "dgt-v2").update({"status": "SUPERSEDED"})
    db.commit()
    rescreen_after_snapshot_change(db, "WATCHLIST_DGT", "dgt-v3", "dgt-v2")
    db.expire_all()
    assert alerte.lapsed_since is None and alerte.lapsed_reason is None
    assert _actions(db, alerte)[-2:] == ["REDETECTED", "LAPSE_CLEARED"]


def test_la_retention_ne_purge_pas_le_dernier_criblage_d_une_alerte(base):
    """Le dernier criblage est l'arbre que lit le valideur : la rétention le
    tient pour référencé, comme la ligne de création."""
    from fiskr.retention import _purgeable_audit_query
    db = base
    alerte, _ = _criblage_v1_puis_v2(db, ("Igor", "PROTASOV"))
    dans_un_an = datetime.utcnow() + timedelta(days=365)
    purgeables = {l.id for l in _purgeable_audit_query(db, dans_un_an).all()}
    assert alerte.audit_id not in purgeables
    assert alerte.latest_audit_id not in purgeables


def test_l_approbation_d_une_version_constate_ses_radiations(homol):
    """Le chemin réel : la mise en production d'une version (job « approve »)
    constate ses fiches retirées, hors du réglage de re-criblage."""
    h = homol
    db = h["db"]
    eid = f"AFF-{UID}-RAD"
    liste = "WATCHLIST_EG_TERROR"
    h["lot"](liste, "READY", [_fiche(eid, "Ivan", NOM_DE_FAMILLE, "1962-01-01")])
    v2 = h["lot"](liste, "PENDING_REVIEW",
                  [_fiche(f"AFF-{UID}-AUTRE", "Oleg", NOM_DE_FAMILLE, "1961-01-01")], heures=1)
    audit = AuditTrail(client_id=f"AFF-{UID}-CLI", client_name="Client", client_type="PP",
                       watchlist_id=eid, watchlist_name=eid, base_score=90.0, final_score=90.0,
                       status="ALERT", decision_tree={}, config_state={},
                       watchlist_version="v1", watchlist_hash="h", list_type=liste)
    db.add(audit)
    db.flush()
    alerte = Alert(audit_id=audit.id, client_id=audit.client_id, client_name="Client",
                   watchlist_entity_id=eid, watchlist_name=eid, final_score=90.0,
                   list_type=liste, status="OPEN", channel="SCREENING")
    db.add(alerte)
    db.commit()
    try:
        reponse = post_and_wait(h["client"], f"/api/review/snapshots/{v2.snapshot_id}/approve",
                                json={"comment": "Version du jour."})
        assert reponse.status_code == 202, reponse.text
        db.expire_all()
        assert "DELISTED" in _actions(db, alerte)
        assert alerte.delisted_at is not None
    finally:
        db.query(AlertEvent).filter(AlertEvent.alert_id == alerte.id).delete()
        db.query(Alert).filter(Alert.id == alerte.id).delete()
        db.query(AuditTrail).filter(AuditTrail.id == audit.id).delete()
        db.commit()

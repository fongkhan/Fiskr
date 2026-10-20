"""
Cinq ans à compter de la fin de la relation — et un SI amont ne dégèle personne.

Constats (enquête de conformité, octobre 2026) :

- la purge de rétention comptait depuis la date de la ligne et ne regardait
  jamais si le client était encore en relation. Réglée à 1 825 jours, elle
  détruisait le dossier d'une alerte confirmée décidée il y a 1 826 jours chez
  un client toujours en portefeuille ;
- le référentiel ne portait aucune date de fin de relation : la question ne
  pouvait pas même se poser ;
- 5 × 365 = 1 825 jours, c'est TOUJOURS moins que cinq ans : toute fenêtre de
  cinq ans contient un 29 février ;
- le webhook `client-upsert` réécrivait `client_status` comme n'importe quel
  champ : un SI amont qui renvoyait « actif » dégelait un client dont la
  mesure de gel était en vigueur au registre.

CMF L.561-12 : « conservent pendant cinq ans à compter de la clôture de leurs
comptes ou de la cessation de leurs relations avec eux » ; directive (UE)
2015/849, art. 40 §1 : « pendant cinq ans après la fin de la relation
d'affaires avec le client ou après la date de la transaction conclue à titre
occasionnel ».
"""
import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

import fiskr.api as api
from fiskr import conservation, gels
from fiskr import retention as retention_mod
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, AdminAuditLog, Alert, AlertAttachment, AlertEvent, ApiKey, AppSetting, AuditTrail,
    ClientEntity, MesureDeGel, Snapshot, _add_missing_nullable_columns,
)
from fiskr.settings import (DUREE_LEGALE_JOURS, SETTING_RETENTION,
                            retention_sous_la_duree_legale)

UID = uuid.uuid4().hex[:8].upper()
P = f"test_cons_{UID}"
ADMIN = {"id": 1, "username": f"{P}_admin", "full_name": "Admin", "role": "admin",
         "roles": ["admin"]}


def _il_y_a(annees=0, jours=0, maintenant=None):
    """Arithmétique de date écrite ici, indépendamment du module testé."""
    maintenant = maintenant or datetime.utcnow()
    try:
        recule = maintenant.replace(year=maintenant.year - annees)
    except ValueError:  # 29 février
        recule = maintenant.replace(year=maintenant.year - annees, day=28)
    return recule - timedelta(days=jours)


def _politique(alertes=0, audit=0):
    return {"closed_alerts": alertes, "audit_trail": audit, "sync_reports": 0,
            "batch_campaigns": 0, "archive": True, "cron": "0 3 * * *"}


class _Base:
    def __init__(self, db):
        self.db = db
        self.lots = []

    def referentiel(self, *fiches, status="READY", uploaded_at=None):
        """Un lot CLIENT_BASE ; chaque fiche : (client_id, fin_de_relation, statut)."""
        snap_id = f"CONS-{uuid.uuid4().hex[:12]}"
        self.db.add(Snapshot(snapshot_id=snap_id, file_type="CLIENT_BASE",
                             file_name=f"{P}_ref.csv", file_hash=uuid.uuid4().hex * 2,
                             record_count=len(fiches), status=status,
                             uploaded_at=uploaded_at or datetime.utcnow()))
        for client_id, fin, statut in fiches:
            self.db.add(ClientEntity(snapshot_id=snap_id, client_id=client_id, client_type="PP",
                                     client_first_name="Jeanne", client_last_name=f"Conserve{UID}",
                                     client_relationship_end=fin, client_status=statut,
                                     entity_checksum="c" * 64))
        self.db.commit()
        self.lots.append(snap_id)
        return snap_id

    def ligne_d_audit(self, client_id, le):
        ligne = AuditTrail(client_id=client_id, client_name=f"Client {UID}", client_type="PP",
                           watchlist_id=f"W-{UID}", watchlist_name=f"Listé {UID}",
                           base_score=90.0, final_score=90.0, status="ALERT",
                           decision_tree={}, config_state={}, watchlist_version="t",
                           watchlist_hash="t", list_type="WATCHLIST_PEP", timestamp=le)
        self.db.add(ligne)
        self.db.commit()
        return ligne

    def alerte(self, client_id, decidee_le, list_type="WATCHLIST_PEP"):
        piste = self.ligne_d_audit(client_id, decidee_le)
        alerte = Alert(audit_id=piste.id, client_id=client_id, client_name=piste.client_name,
                       watchlist_entity_id=piste.watchlist_id, watchlist_name=piste.watchlist_name,
                       final_score=90.0, list_type=list_type, status="CLOSED_CONFIRMED",
                       created_at=decidee_le, decided_at=decidee_le)
        self.db.add(alerte)
        self.db.commit()
        return alerte

    def mesure_en_vigueur(self, alerte):
        """Gel effectif, déclaré, inscrit sur la fiche : il tient le client."""
        mesure = MesureDeGel(alert_id=alerte.id, type_action="GEL", statut=gels.STATUT_DECLAREE,
                             client_id=alerte.client_id, client_name=alerte.client_name,
                             watchlist_entity_id=alerte.watchlist_entity_id,
                             watchlist_name=alerte.watchlist_name, ouverte_par=P,
                             date_effet=datetime.utcnow() - timedelta(hours=2),
                             declaree_le=datetime.utcnow() - timedelta(hours=1),
                             client_gele_ecrit=True, statut_client_avant="ACTIF")
        self.db.add(mesure)
        self.db.commit()
        return mesure

    def fiche(self, snap_id, client_id):
        self.db.expire_all()
        return self.db.query(ClientEntity).filter(ClientEntity.snapshot_id == snap_id,
                                                  ClientEntity.client_id == client_id).one()


@pytest.fixture()
def base(monkeypatch, tmp_path):
    db = next(get_db())
    monkeypatch.setattr(retention_mod, "ARCHIVE_DIR", tmp_path / "archives")
    b = _Base(db)
    yield b
    db.rollback()
    try:
        ids = [a.id for a in db.query(Alert).filter(Alert.client_id.like(f"{P}%")).all()]
        if ids:
            db.query(MesureDeGel).filter(MesureDeGel.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertAttachment).filter(AlertAttachment.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like(f"{P}%")).delete(synchronize_session=False)
        lots = b.lots + [s.snapshot_id for s in db.query(Snapshot).filter(
            Snapshot.file_name.like(f"{P}%")).all()]
        if lots:
            db.query(ClientEntity).filter(ClientEntity.snapshot_id.in_(lots)).delete(synchronize_session=False)
            db.query(Snapshot).filter(Snapshot.snapshot_id.in_(lots)).delete(synchronize_session=False)
        db.query(AdminAuditLog).filter(AdminAuditLog.username.like(f"{P}%")).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.name.like(f"{P}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def client_admin():
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    db = next(get_db())
    avant = db.query(AppSetting).filter(AppSetting.key == SETTING_RETENTION).first()
    valeur_avant = None if avant is None else avant.value
    db.close()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    db = next(get_db())
    try:
        ligne = db.query(AppSetting).filter(AppSetting.key == SETTING_RETENTION).first()
        if valeur_avant is None and ligne is not None:
            db.delete(ligne)
        elif ligne is not None:
            ligne.value = valeur_avant
        db.commit()
    finally:
        db.close()


def _purgeables(db, politique, famille="closed_alerts", maintenant=None):
    limites = (retention_mod._cutoffs(politique, maintenant) if maintenant
               else retention_mod._cutoffs(politique))
    limite = limites[famille]
    if famille == "closed_alerts":
        return set(retention_mod._purgeable_alert_ids(db, limite))
    return {l.id for l in retention_mod._purgeable_audit_query(db, limite).all()}


# ---------------------------------------------- le délai court depuis la fin

def test_une_alerte_decidee_il_y_a_1826_jours_reste_tant_que_le_client_est_en_relation(base, monkeypatch):
    """Le cas du constat : politique à 1 825 jours, alerte confirmée décidée il
    y a 1 826 jours, client toujours en portefeuille. Purgée jusqu'ici."""
    cid = f"{P}_en_relation"
    base.referentiel((cid, None, "ACTIF"))
    alerte = base.alerte(cid, datetime.utcnow() - timedelta(days=1826))
    temoin = base.alerte(f"{P}_sans_fiche", _il_y_a(6))
    politique = _politique(alertes=1825)

    purgeables = _purgeables(base.db, politique)
    assert temoin.id in purgeables, "le témoin prouve que la limite purge bien"
    assert alerte.id not in purgeables, "le dossier d'un client en relation était purgé"

    monkeypatch.setattr(retention_mod, "retention_policy", lambda db_: politique)
    garde, purge = alerte.id, temoin.id
    retention_mod.run_retention(base.db, username=f"{P}_purge")
    base.db.expire_all()
    assert base.db.get(Alert, garde) is not None
    assert base.db.get(Alert, purge) is None


def test_un_client_sorti_de_relation_il_y_a_cinq_ans_et_un_jour_est_purgeable(base):
    """L'autre sens : la loi garde cinq ans, pas toujours — la directive impose
    d'effacer à l'issue (art. 40 §1, second alinéa)."""
    cid = f"{P}_sorti_5a1j"
    base.referentiel((cid, _il_y_a(5, 1).date().isoformat(), "CLOTURE"))
    alerte = base.alerte(cid, _il_y_a(6))
    assert alerte.id in _purgeables(base.db, _politique(alertes=1825))


def test_un_client_sorti_depuis_quatre_ans_et_364_jours_garde_son_dossier(base):
    cid = f"{P}_sorti_4a364j"
    base.referentiel((cid, _il_y_a(4, 364).date().isoformat(), "CLOTURE"))
    alerte = base.alerte(cid, _il_y_a(6))
    assert alerte.id not in _purgeables(base.db, _politique(alertes=1825))


def test_une_politique_plus_longue_compte_aussi_depuis_la_fin(base):
    """Dix ans réglés : dix ans après la fin, pas après la décision."""
    cid = f"{P}_sorti_6a"
    base.referentiel((cid, _il_y_a(6).date().isoformat(), "CLOTURE"))
    alerte = base.alerte(cid, _il_y_a(12))
    assert alerte.id not in _purgeables(base.db, _politique(alertes=3653))
    assert alerte.id in _purgeables(base.db, _politique(alertes=1827))


def test_la_fiche_la_plus_recente_dit_la_relation(base):
    """Les lots CLIENT_BASE en production s'accumulent : c'est la fiche la plus
    récente qui dit si le client est sorti — comme la vue client et le registre
    des gels. Revenu en relation, il redevient conservé."""
    sorti, revenu = f"{P}_sorti_recemment_dit", f"{P}_revenu"
    hier = datetime.utcnow() - timedelta(days=1)
    base.referentiel((sorti, None, "ACTIF"), (revenu, _il_y_a(6).date().isoformat(), "CLOTURE"),
                     uploaded_at=hier)
    base.referentiel((sorti, _il_y_a(6).date().isoformat(), "CLOTURE"), (revenu, None, "ACTIF"))
    a_sorti, a_revenu = base.alerte(sorti, _il_y_a(7)), base.alerte(revenu, _il_y_a(7))
    purgeables = _purgeables(base.db, _politique(alertes=1825))
    assert a_sorti.id in purgeables
    assert a_revenu.id not in purgeables


def test_le_plancher_compte_les_29_fevrier(base):
    """
    Du 2 mars 2023 au 1er mars 2028 : 1 826 jours, et pas cinq ans (2024 et
    2028 ont un 29 février). Une politique à 1 825 jours purgeait.
    """
    maintenant = datetime(2028, 3, 1, 12, 0)
    pas_echue = base.alerte(f"{P}_bissextile", datetime(2023, 3, 2, 12, 0))
    echue = base.alerte(f"{P}_bissextile_echue", datetime(2023, 3, 1, 11, 0))
    assert (maintenant - pas_echue.decided_at).days == 1826
    purgeables = _purgeables(base.db, _politique(alertes=1825), maintenant=maintenant)
    assert pas_echue.id not in purgeables
    assert echue.id in purgeables
    # Le plancher affiché en jours est la plus longue fenêtre, plus 5 × 365
    assert DUREE_LEGALE_JOURS == (datetime(2028, 3, 1) - datetime(2023, 3, 1)).days
    assert retention_sous_la_duree_legale({"closed_alerts": 1825}), \
        "1 825 jours passaient pour cinq ans"


def test_le_criblage_d_un_prospect_sans_fiche_suit_le_delai_d_une_operation_occasionnelle(base):
    """
    Une ligne d'audit de criblage temps réel d'un prospect sans fiche : cinq
    ans à compter de la ligne (art. 40 : « date de la transaction conclue à
    titre occasionnel »). La relation se lit à la purge : un prospect devenu
    client sous le même identifiant suit le délai de sa relation.
    """
    jeune = base.ligne_d_audit(f"{P}_prospect", _il_y_a(4))
    ancienne = base.ligne_d_audit(f"{P}_prospect_ancien", _il_y_a(6))
    devenu = base.ligne_d_audit(f"{P}_devenu_client", _il_y_a(6))
    base.referentiel((f"{P}_devenu_client", None, "ACTIF"))
    purgeables = _purgeables(base.db, _politique(audit=30), famille="audit_trail")
    assert ancienne.id in purgeables
    assert jeune.id not in purgeables, "un criblage de quatre ans partait à trente jours"
    assert devenu.id not in purgeables


# ------------------------------------------ ce que l'écran en dit, sans alarme

def test_l_ecran_dit_combien_sont_conservees_et_pourquoi(base):
    """Le compte et la purge lisent la même expression : rien de ce qui est
    annoncé « conservé » ne part, et un NULL (client sans date de fin) ne
    disparaît pas du compte comme le ferait une négation SQL."""
    politique = _politique(alertes=1825, audit=30)
    avant = retention_mod.conservees_au_titre_de_la_relation(base.db, politique)
    base.referentiel((f"{P}_r1", None, "ACTIF"),
                     (f"{P}_r2", _il_y_a(2).date().isoformat(), "CLOTURE"))
    base.alerte(f"{P}_r1", _il_y_a(6))
    base.alerte(f"{P}_r2", _il_y_a(6))
    base.ligne_d_audit(f"{P}_prospect_r3", _il_y_a(5, -30))   # 4 ans et 11 mois
    apres = retention_mod.conservees_au_titre_de_la_relation(base.db, politique)

    delta = {f: {m: apres[f][m] - avant[f][m] for m in conservation.MOTIFS} for f in apres}
    assert delta["closed_alerts"] == {conservation.EN_RELATION: 1,
                                      conservation.FIN_DE_RELATION_RECENTE: 1,
                                      conservation.OPERATION_OCCASIONNELLE: 0}
    assert delta["audit_trail"][conservation.OPERATION_OCCASIONNELLE] == 1


def test_la_retenue_d_un_client_en_relation_ne_crie_pas_au_loup(base, client_admin):
    """Un client en portefeuille garde son dossier : c'est le fonctionnement
    normal. L'écran le compte ; la mise en service reste OK."""
    from fiskr.mise_en_service import etat_de_mise_en_service, OK
    assert client_admin.put("/api/settings/retention", json={
        "closed_alerts": DUREE_LEGALE_JOURS, "audit_trail": DUREE_LEGALE_JOURS}).status_code == 200
    base.referentiel((f"{P}_portefeuille", None, "ACTIF"))
    base.alerte(f"{P}_portefeuille", _il_y_a(6))

    lecture = client_admin.get("/api/admin/retention").json()
    assert lecture["conservees"]["closed_alerts"][conservation.EN_RELATION] >= 1
    assert lecture["motifs_de_conservation"] == conservation.MOTIFS
    assert lecture["sous_la_duree_legale"] == []
    controles = {c["cle"]: c for c in etat_de_mise_en_service(base.db)["controles"]}
    assert controles["conservation"]["etat"] == OK


def test_une_politique_a_1825_jours_est_signalee_a_la_mise_en_service(base, client_admin):
    """Le contrôle existait (ATTENTION sous le plancher) ; il laissait passer
    1 825 jours, et son remède recopiait « 1825 »."""
    from fiskr.mise_en_service import etat_de_mise_en_service, ATTENTION
    client_admin.put("/api/settings/retention", json={"closed_alerts": 1825})
    controles = {c["cle"]: c for c in etat_de_mise_en_service(base.db)["controles"]}
    assert controles["conservation"]["etat"] == ATTENTION
    assert str(DUREE_LEGALE_JOURS) in controles["conservation"]["remede"]


def test_la_purge_garde_archive_et_journal_ce_qu_elle_epargne(base, monkeypatch, tmp_path):
    """La purge d'un client sorti depuis plus de cinq ans passe toujours par
    l'archive du lot précédent — fichier compris —, et le journal dit sous quel
    plancher elle a compté et ce qu'elle a gardé."""
    garde, sorti = f"{P}_garde", f"{P}_sorti_archive"
    base.referentiel((garde, None, "ACTIF"), (sorti, _il_y_a(5, 10).date().isoformat(), "CLOTURE"))
    a_garde, a_sorti = base.alerte(garde, _il_y_a(6)).id, base.alerte(sorti, _il_y_a(6)).id
    piece = tmp_path / "piece.pdf"
    piece.write_bytes(b"%PDF piece probante")
    base.db.add(AlertAttachment(alert_id=a_sorti, file_name="piece.pdf", file_path=str(piece),
                                uploaded_by=P, uploaded_at=_il_y_a(6)))
    base.db.commit()
    politique = _politique(alertes=1825)
    monkeypatch.setattr(retention_mod, "retention_policy", lambda db_: politique)

    retention_mod.run_retention(base.db, username=f"{P}_purge")
    base.db.expire_all()
    assert base.db.get(Alert, a_garde) is not None
    assert base.db.get(Alert, a_sorti) is None
    archives = sorted((tmp_path / "archives").iterdir())
    lignes = [json.loads(l) for l in (archives[-1] / "alert_attachments.jsonl").read_text(
        encoding="utf-8").splitlines()]
    copie = next(l for l in lignes if l["alert_id"] == a_sorti)["archive_fichier"]
    assert (archives[-1] / retention_mod.ARCHIVE_FICHIERS / copie).read_bytes() == b"%PDF piece probante"

    trace = base.db.query(AdminAuditLog).filter(
        AdminAuditLog.username == f"{P}_purge",
        AdminAuditLog.action == "RETENTION_PURGE").order_by(AdminAuditLog.id.desc()).first()
    assert trace.after["plancher_legal"]["article"] == "CMF L.561-12"
    assert trace.after["conservees_au_titre_de_la_relation"]["closed_alerts"][conservation.EN_RELATION] >= 1


# ----------------------------------------------- la date de fin, aux entrées

def test_la_date_de_fin_se_lit_en_une_seule_forme():
    assert conservation.date_de_fin_de_relation("31/12/2020") == "2020-12-31"
    assert conservation.date_de_fin_de_relation("2020-12-31T09:00:00Z") == "2020-12-31"
    # Une date impossible n'est pas une date : écrite telle quelle, elle se
    # comparerait comme une chaîne et rendrait le client purgeable
    assert conservation.date_de_fin_de_relation("2019-13-45") is None
    assert conservation.date_de_fin_de_relation("bientôt") is None
    assert conservation.date_de_fin_de_relation("") is None


def test_l_import_et_son_apercu_lisent_la_date_de_fin_par_la_table_des_colonnes(base, client_admin):
    assert "client_relationship_end" in api._APERCU_CHAMPS["CLIENT_BASE"]
    contenu = ("client_id,client_type,client_last_name,relationship_end\n"
               f"{P}_imp1,PP,Importee{UID},31/12/2020\n"
               f"{P}_imp2,PP,Illisible{UID},bientôt\n"
               f"{P}_imp3,PP,Relation{UID},\n")
    apercu = client_admin.post("/api/ingest/preview", data={"file_type": "CLIENT_BASE", "delimiter": ","},
                               files={"file": ("essai.csv", contenu, "text/csv")})
    assert apercu.status_code == 200, apercu.text
    compris = [l["compris"]["client_relationship_end"] for l in apercu.json()["lignes"]]
    assert compris == ["2020-12-31", "", ""], "l'aperçu montre la date LUE, vide si illisible"

    r = client_admin.post("/api/ingest", data={"file_type": "CLIENT_BASE", "delimiter": ","},
                          files={"file": (f"{P}_import.csv", contenu, "text/csv")})
    assert r.status_code == 200, r.text
    assert r.json()["relationship_end_unreadable"] == 1
    assert "illisible" in r.json()["message"]
    snap_id = r.json()["snapshot_id"]
    fins = {f.client_id: f.client_relationship_end for f in base.db.query(ClientEntity).filter(
        ClientEntity.snapshot_id == snap_id).all()}
    assert fins == {f"{P}_imp1": "2020-12-31", f"{P}_imp2": None, f"{P}_imp3": None}


@contextmanager
def _sans_session_humaine():
    sauve = app.dependency_overrides.pop(get_current_user, None)
    try:
        yield
    finally:
        if sauve is not None:
            app.dependency_overrides[get_current_user] = sauve


def _upsert(client, corps):
    cle = client.post("/api/apikeys", json={"name": f"{P}_{uuid.uuid4().hex[:6]}", "role": "user"})
    assert cle.status_code == 200, cle.text
    with _sans_session_humaine():
        r = client.post("/api/hooks/client-upsert", content=json.dumps(corps),
                        headers={"X-API-Key": cle.json()["api_key"],
                                 "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    return r.json()


def test_l_upsert_porte_la_date_de_fin_sans_l_effacer_quand_il_l_ignore(base, client_admin):
    """Le remplacement est complet : un appelant qui ne connaît pas le champ
    effacerait la date posée par l'import. Absent, il est laissé tel quel."""
    cid = f"{P}_upsert_fin"
    snap = base.referentiel((cid, None, "ACTIF"))
    socle = {"client_id": cid, "client_type": "PP", "client_last_name": f"Upsert{UID}"}
    _upsert(client_admin, {**socle, "client_relationship_end": "30/06/2021"})
    assert base.fiche(snap, cid).client_relationship_end == "2021-06-30"
    _upsert(client_admin, {**socle, "client_email": "neuf@exemple.fr"})
    assert base.fiche(snap, cid).client_relationship_end == "2021-06-30"
    corps = _upsert(client_admin, {**socle, "client_relationship_end": "fin 2021"})
    assert any("illisible" in a for a in corps["warnings"])
    assert base.fiche(snap, cid).client_relationship_end is None


# ------------------------------------------------ un SI amont ne dégèle pas

def test_un_upsert_actif_ne_degele_pas_un_client_sous_mesure_et_le_dit(base, client_admin):
    cid = f"{P}_gele"
    snap = base.referentiel((cid, None, gels.STATUT_CLIENT_GELE))
    mesure = base.mesure_en_vigueur(base.alerte(cid, datetime.utcnow(), "WATCHLIST_DGT"))
    socle = {"client_id": cid, "client_type": "PP", "client_last_name": f"Gel{UID}"}

    corps = _upsert(client_admin, {**socle, "client_status": "actif",
                                   "client_email": "neuf@exemple.fr"})
    fiche = base.fiche(snap, cid)
    assert fiche.client_status == gels.STATUT_CLIENT_GELE, "le SI amont a dégelé le client"
    assert fiche.client_email == "neuf@exemple.fr", "le reste de la fiche s'applique"
    assert corps["frozen"]["applied_status"] == gels.STATUT_CLIENT_GELE
    assert corps["frozen"]["requested_status"] == "actif"
    assert corps["frozen"]["freeze_measures"] == [mesure.id]
    assert f"#{mesure.id}" in corps["message"] and "GELE" in corps["message"]
    assert "client_status" not in corps["changed_fields"]
    trace = base.db.query(AdminAuditLog).filter(
        AdminAuditLog.action == "CLIENT_UPSERT_HOOK", AdminAuditLog.target == cid,
    ).order_by(AdminAuditLog.id.desc()).first()
    assert trace.after["status_kept_frozen"]["freeze_measures"] == [mesure.id]

    # Sans statut du tout : le remplacement complet l'aurait effacé, pareil
    _upsert(client_admin, {**socle, "client_email": "autre@exemple.fr"})
    assert base.fiche(snap, cid).client_status == gels.STATUT_CLIENT_GELE


def test_apres_la_levee_de_la_mesure_l_upsert_s_applique(base, client_admin):
    cid = f"{P}_leve"
    snap = base.referentiel((cid, None, gels.STATUT_CLIENT_GELE))
    mesure = base.mesure_en_vigueur(base.alerte(cid, datetime.utcnow(), "WATCHLIST_DGT"))
    socle = {"client_id": cid, "client_type": "PP", "client_last_name": f"Leve{UID}"}
    _upsert(client_admin, {**socle, "client_status": "ACTIF"})
    assert base.fiche(snap, cid).client_status == gels.STATUT_CLIENT_GELE

    # Seule la levée au registre rend le statut…
    gels.lever(base.db, mesure, username=f"{P}_reviseur", motif="ABROGATION",
               justification="Règlement d'exécution (UE) 2026/999 abrogeant la désignation.")
    base.db.commit()
    assert base.fiche(snap, cid).client_status == "ACTIF"
    # … et ensuite le SI amont reprend la main
    corps = _upsert(client_admin, {**socle, "client_status": "CLOTURE"})
    assert corps["frozen"] is None
    assert base.fiche(snap, cid).client_status == "CLOTURE"


def test_un_referentiel_repare_au_demarrage_ne_degele_personne(base):
    """Un import resté en PROCESSING et réparé au démarrage entre en
    production comme un import abouti : il ne dégèle personne non plus."""
    from fiskr.api import _repair_stuck_snapshots
    cid = f"{P}_repare"
    base.referentiel((cid, None, gels.STATUT_CLIENT_GELE))
    base.mesure_en_vigueur(base.alerte(cid, datetime.utcnow(), "WATCHLIST_DGT"))
    coince = base.referentiel((cid, None, "ACTIF"), status="PROCESSING",
                              uploaded_at=datetime.utcnow() - timedelta(hours=3))
    _repair_stuck_snapshots(base.db)
    assert base.db.get(Snapshot, coince).status == "READY"
    assert base.fiche(coince, cid).client_status == gels.STATUT_CLIENT_GELE


# --------------------------------------------------------------- migration

def test_la_colonne_de_fin_de_relation_s_ajoute_a_une_base_existante(tmp_path):
    moteur = create_engine(f"sqlite:///{tmp_path / 'migr.sqlite3'}")
    try:
        with moteur.begin() as conn:
            conn.execute(text("CREATE TABLE client_entities (id INTEGER PRIMARY KEY, "
                              "snapshot_id VARCHAR(50) NOT NULL, client_id VARCHAR(100) NOT NULL, "
                              "client_type VARCHAR(10) NOT NULL, entity_checksum VARCHAR(64) NOT NULL)"))
            conn.execute(text("INSERT INTO client_entities (snapshot_id, client_id, client_type, "
                              "entity_checksum) VALUES ('s', 'C1', 'PP', 'x')"))
        _add_missing_nullable_columns(moteur, inspect(moteur))
        colonnes = {c["name"] for c in inspect(moteur).get_columns("client_entities")}
        assert "client_relationship_end" in colonnes
        with moteur.connect() as conn:
            assert conn.execute(text("SELECT client_id FROM client_entities")).scalar() == "C1"
    finally:
        moteur.dispose()

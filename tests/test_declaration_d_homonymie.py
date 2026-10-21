"""
Une correspondance qu'on ne peut ni lever ni confirmer a sa procédure : la
déclaration d'homonymie à la DG Trésor.

Avant : une alerte se closait en faux positif ou en vrai positif, rien
d'autre. Le registre des gels l'avait laissé de côté à dessein (« The
homonymy declaration (§167) is not covered »). Or les lignes directrices
conjointes DGT/ACPR du 18 mars 2026 le prévoient :
- §167 : après toutes les diligences, l'alerte qui « ne peut toujours pas être
  levée » se déclare « dans les plus brefs délais » à la DG Trésor, qui répond
  de trois façons — homonyme (l'alerte est levée), même personne (gel
  immédiat), doute persistant (la décision revient à l'organisme, Tracfin si
  L.561-15, information de la DG Trésor) ;
- §168 : les échanges avec la DG Trésor se conservent ;
- §162 : sur un flux, l'opération reste suspendue jusqu'au traitement complet ;
- §169 : une liste blanche ne vaut que pour un homonyme établi.

Ces tests tiennent : l'attente (ni close, ni dans la file ordinaire), la
réponse qui seule la fait sortir, par le chemin de clôture existant, le
périmètre SANCTION, le délai de transmission qui sonne et bloque — et celui de
la réponse qui ne sonne pas —, la conservation.
"""
import io
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fiskr import homonymie
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (
    get_db, Alert, AlertAttachment, AlertEvent, AppSetting, AuditTrail, DeclarationHomonymie,
    DossierPPE, MesureDeGel, ALERT_OPEN_STATUSES, ALERT_STATUS_ATTENTE_DGT,
)
from fiskr.settings import (
    SETTING_ALERT_FOUR_EYES, SETTING_HOMONYMIE_DELAI, SETTING_PERIMETERS, set_setting,
)

UID = uuid.uuid4().hex[:8].upper()
PREFIXE = f"test_hom_{UID}"
RACINE = Path(__file__).resolve().parent.parent
DILIGENCES = ("Pièce d'identité demandée au client le 12/10, non fournie ; date de naissance "
              "absente de la fiche ; recherche JORF et registre national sans élément discriminant.")


def _connecter(username, roles=("admin", "reviewer")):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": ",".join(roles), "roles": list(roles),
    }


def _espionner_emit(monkeypatch):
    appels = []
    espion = lambda db, key, payload=None, **kw: appels.append((key, payload or {}))
    import fiskr.api as api_mod
    import fiskr.notifier as notifier_mod
    monkeypatch.setattr(api_mod, "emit", espion)
    monkeypatch.setattr(notifier_mod, "emit", espion)
    return appels


def _piece(nom="courriel_dgt.eml"):
    return {"file": (nom, io.BytesIO(b"From: homonymies-gels@dgtresor.gouv.fr"), "message/rfc822")}


@pytest.fixture()
def ctx():
    db = next(get_db())
    _connecter(f"{PREFIXE}_analyste")
    reglages = (SETTING_ALERT_FOUR_EYES, SETTING_HOMONYMIE_DELAI, SETTING_PERIMETERS)
    avant = {r.key: r.value for r in db.query(AppSetting).filter(AppSetting.key.in_(reglages))}

    def alerte(list_type="WATCHLIST_DGT", nom="Alerte", canal="SCREENING", client_id=None,
               entite=None, statut="IN_PROGRESS"):
        client_id = client_id or f"{PREFIXE}_{nom}"
        entite = entite or f"DGT-{UID}{nom}"
        audit = AuditTrail(client_id=client_id, client_name=f"Client {nom} {UID}",
                           client_type="PP", watchlist_id=entite,
                           watchlist_name=f"Listé {nom} {UID}", base_score=91.0,
                           final_score=91.0, status="ALERT",
                           decision_tree={"hard_match_triggered": False},
                           config_state={}, watchlist_version="test", watchlist_hash="test",
                           list_type=list_type)
        db.add(audit)
        db.commit()
        a = Alert(audit_id=audit.id, client_id=client_id, client_name=audit.client_name,
                  watchlist_entity_id=entite, watchlist_name=audit.watchlist_name,
                  final_score=91.0, list_type=list_type, status=statut, channel=canal,
                  priority="HIGH", due_at=datetime.utcnow() - timedelta(hours=1))
        db.add(a)
        db.commit()
        return a

    set_setting(db, SETTING_ALERT_FOUR_EYES, True)
    client = TestClient(app)
    yield {"db": db, "client": client, "alerte": alerte}
    app.dependency_overrides.pop(get_current_user, None)
    try:
        db.rollback()
        ids = [a.id for a in db.query(Alert).filter(Alert.client_id.like(f"{PREFIXE}%")).all()]
        if ids:
            # SQLite recycle les identifiants : une declaration orpheline
            # retiendrait l'alerte qui reprendrait le sien
            db.query(DeclarationHomonymie).filter(
                DeclarationHomonymie.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(MesureDeGel).filter(MesureDeGel.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(DossierPPE).filter(DossierPPE.alert_id.in_(ids)).delete(synchronize_session=False)
            for piece in db.query(AlertAttachment).filter(AlertAttachment.alert_id.in_(ids)).all():
                Path(piece.file_path).unlink(missing_ok=True)
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertAttachment).filter(AlertAttachment.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like(f"{PREFIXE}%")).delete(synchronize_session=False)
        for cle in reglages:
            ligne = db.query(AppSetting).filter(AppSetting.key == cle).first()
            if cle in avant:
                if ligne is not None:
                    ligne.value = avant[cle]
            elif ligne is not None:
                db.delete(ligne)
        db.commit()
    finally:
        db.close()


def _douter(ctx, a, **champs):
    corps = {"diligences": DILIGENCES, **champs}
    return ctx["client"].post(f"/api/alerts/{a.id}/homonymie", json=corps)


def _declaration(ctx, a):
    r = ctx["client"].get(f"/api/homonymies?alert_id={a.id}")
    assert r.status_code == 200, r.text
    assert r.json()["items"], "aucune déclaration au registre"
    return r.json()["items"][0]


def _transmettre(ctx, declaration_id, avec_piece=True, **champs):
    _connecter(f"{PREFIXE}_reviseur")
    r = ctx["client"].post(f"/api/homonymies/{declaration_id}/transmission",
                           files=_piece("envoi_formulaire.eml") if avec_piece else None,
                           data={"reference": "HOM-2026-07", **champs})
    _connecter(f"{PREFIXE}_analyste")
    return r


def _repondre(ctx, declaration_id, issue, avec_piece=True, **champs):
    _connecter(f"{PREFIXE}_reviseur")
    r = ctx["client"].post(f"/api/homonymies/{declaration_id}/reponse",
                           files=_piece("reponse_dgt.eml") if avec_piece else None,
                           data={"issue": issue, **champs})
    _connecter(f"{PREFIXE}_analyste")
    return r


def _douter_et_transmettre(ctx, a):
    assert _douter(ctx, a).status_code == 200
    d = _declaration(ctx, a)
    assert _transmettre(ctx, d["id"]).status_code == 200
    return d


# ---------------------------------------------- le doute : une attente, pas une clôture

def test_une_alerte_dgt_en_doute_attend_la_dg_tresor_et_ne_se_clot_pas_sans_reponse(ctx):
    db, client = ctx["db"], ctx["client"]
    a = ctx["alerte"]("WATCHLIST_DGT", "Attente")
    r = _douter(ctx, a)
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == homonymie.STATUT_A_TRANSMETTRE
    db.expire_all()
    assert db.get(Alert, a.id).status == ALERT_STATUS_ATTENTE_DGT == "PENDING_DGT"

    # Ni proposition (avec ou sans 4-yeux), ni escalade, ni proposition en masse
    for quatre_yeux in (True, False):
        set_setting(db, SETTING_ALERT_FOUR_EYES, quatre_yeux)
        for decision in ("FALSE_POSITIVE", "CONFIRMED"):
            r = client.post(f"/api/alerts/{a.id}/propose",
                            json={"decision": decision, "comment": "Clôture tentée sans réponse."})
            assert r.status_code == 409, (quatre_yeux, decision, r.text)
            assert "DG Trésor" in r.json()["detail"]
    assert client.post(f"/api/alerts/{a.id}/escalate", json={"comment": "x"}).status_code == 409
    masse = client.post("/api/alerts/bulk", json={"ids": [a.id], "action": "propose",
                                                  "decision": "FALSE_POSITIVE", "comment": "Lot."})
    assert masse.status_code == 200, masse.text
    assert [e["code"] for e in masse.json()["skipped"]] == ["ATTENTE_DG_TRESOR"]
    # La garde vit dans la clôture unique : tout chemin, présent ou à venir
    from fastapi import HTTPException
    from fiskr.api import _close_alert
    with pytest.raises(HTTPException) as refus:
        _close_alert(db, db.get(Alert, a.id), "FALSE_POSITIVE", "x", "x")
    assert refus.value.status_code == 409
    db.rollback()
    db.expire_all()
    assert db.get(Alert, a.id).status == "PENDING_DGT"

    # Hors de la file ordinaire, mais filtrable ; et pas « en retard SLA »
    ordinaire = client.get(f"/api/alerts?status=OPEN,IN_PROGRESS,ESCALATED,PENDING_VALIDATION"
                           f"&search={PREFIXE}_Attente").json()
    assert a.id not in [x["id"] for x in ordinaire["items"]]
    attente = client.get(f"/api/alerts?status=PENDING_DGT&search={PREFIXE}_Attente").json()
    assert [x["id"] for x in attente["items"]] == [a.id]
    assert attente["items"][0]["overdue"] is False, "l'échéance SLA est dépassée, mais la DG Trésor a son rythme"
    detail = client.get(f"/api/alerts/{a.id}").json()
    assert detail["homonymie_possible"] is False
    assert "HOMONYMIE_DECLAREE" in [e["action"] for e in detail["events"]]


def test_le_doute_exige_le_bilan_des_diligences(ctx):
    a = ctx["alerte"]("WATCHLIST_OFAC", "SansDiligences")
    r = ctx["client"].post(f"/api/alerts/{a.id}/homonymie", json={"diligences": "   "})
    assert r.status_code == 400, r.text
    ctx["db"].expire_all()
    assert ctx["db"].get(Alert, a.id).status == "IN_PROGRESS"


def test_une_alerte_hors_perimetre_sanction_ne_se_declare_pas_en_homonymie(ctx):
    db = ctx["db"]
    pep = ctx["alerte"]("WATCHLIST_PEP", "Pep")
    r = _douter(ctx, pep)
    assert r.status_code == 409, r.text
    assert "SANCTION" in r.json()["detail"]
    db.expire_all()
    assert db.get(Alert, pep.id).status == "IN_PROGRESS"
    assert ctx["client"].get(f"/api/alerts/{pep.id}").json()["homonymie_possible"] is False
    # Le périmètre est lu, surcharges comprises — jamais une liste recopiée
    set_setting(db, SETTING_PERIMETERS, {"WATCHLIST_OFAC": "HORS_SANCTION",
                                         "WATCHLIST_PEP": "SANCTION"})
    ofac = ctx["alerte"]("WATCHLIST_OFAC", "OfacDeclasse")
    assert _douter(ctx, ofac).status_code == 409
    assert _douter(ctx, pep).status_code == 200


def test_sur_un_flux_le_journal_dit_que_l_operation_reste_suspendue(ctx):
    a = ctx["alerte"]("WATCHLIST_UN", "Flux", canal="FILTERING", client_id=f"{PREFIXE}_TXN")
    assert _douter(ctx, a, mesures_conservatoires="Virement MT103 bloqué en file.").status_code == 200
    detail = ctx["client"].get(f"/api/alerts/{a.id}").json()
    journal = next(e["detail"] for e in detail["events"] if e["action"] == "HOMONYMIE_DECLAREE")
    assert "LD §162" in journal and "suspendue" in journal
    base = ctx["alerte"]("WATCHLIST_UN", "Base")
    assert _douter(ctx, base).status_code == 200
    journal = next(e["detail"] for e in ctx["client"].get(f"/api/alerts/{base.id}").json()["events"]
                   if e["action"] == "HOMONYMIE_DECLAREE")
    assert "§87" in journal and "pas de gel conservatoire" in journal


# ---------------------------------------------- les trois réponses de la DG Trésor

def test_reponse_homonyme_clot_en_faux_positif_motive_par_la_reponse_et_sa_piece(ctx):
    db, client = ctx["db"], ctx["client"]
    a = ctx["alerte"]("WATCHLIST_DGT", "Homonyme")
    assert _douter(ctx, a).status_code == 200
    d = _declaration(ctx, a)
    # Une réponse ne précède pas la transmission ; la transmission exige sa pièce
    assert _repondre(ctx, d["id"], "HOMONYME").status_code == 409
    assert _transmettre(ctx, d["id"], avec_piece=False).status_code == 400
    r = _transmettre(ctx, d["id"])
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == homonymie.STATUT_TRANSMISE
    assert r.json()["piece_transmission"]["file_name"] == "envoi_formulaire.eml"
    # Pas de réponse sans sa pièce : la clôture citerait une réponse que personne n'a vue
    assert _repondre(ctx, d["id"], "HOMONYME", avec_piece=False).status_code == 400
    db.expire_all()
    assert db.get(Alert, a.id).status == "PENDING_DGT"

    r = _repondre(ctx, d["id"], "HOMONYME", resume="Date de naissance distincte au registre.")
    assert r.status_code == 200, r.text
    db.expire_all()
    alerte = db.get(Alert, a.id)
    assert alerte.status == "CLOSED_FALSE_POSITIVE"
    assert alerte.decided_by == f"{PREFIXE}_reviseur"
    assert "Réponse de la DG Trésor" in alerte.decision_comment
    assert "reponse_dgt.eml" in alerte.decision_comment
    assert db.query(MesureDeGel).filter(MesureDeGel.alert_id == a.id).count() == 0
    fin = _declaration(ctx, a)
    assert fin["statut"] == homonymie.STATUT_TERMINEE and fin["issue"] == "HOMONYME"
    assert fin["piece_reponse"]["file_name"] == "reponse_dgt.eml"
    assert fin["reponse_le"] and fin["transmise_le"] <= fin["reponse_le"]
    # Homonyme établi par la DG Trésor : la liste blanche redevient légitime (§169)
    assert homonymie.refus_de_liste_blanche(db, a.client_id, a.watchlist_entity_id) is None


def test_une_reponse_saisie_dans_la_minute_de_la_transmission_n_est_pas_anterieure(ctx):
    """Constaté en navigateur : l'écran saisit à la minute (datetime-local), et
    une réponse enregistrée dans la minute de la transmission était refusée
    « antérieure à la transmission »."""
    db = ctx["db"]
    a = ctx["alerte"]("WATCHLIST_DGT", "Minute")
    d = _douter_et_transmettre(ctx, a)
    ligne = db.get(DeclarationHomonymie, d["id"])
    ligne.transmise_le = (datetime.utcnow() - timedelta(minutes=2)).replace(second=30, microsecond=0)
    db.commit()
    saisie = ligne.transmise_le.replace(second=0).isoformat()
    r = _repondre(ctx, d["id"], "HOMONYME", recue_le=saisie)
    assert r.status_code == 200, r.text
    avant = (ligne.transmise_le - timedelta(minutes=1)).isoformat()
    b = ctx["alerte"]("WATCHLIST_DGT", "Minute2")
    d2 = _douter_et_transmettre(ctx, b)
    assert _repondre(ctx, d2["id"], "HOMONYME", recue_le=avant).status_code == 400


def test_reponse_meme_personne_confirme_et_ouvre_la_mesure_de_gel(ctx):
    db = ctx["db"]
    a = ctx["alerte"]("WATCHLIST_EU", "MemePersonne")
    d = _douter_et_transmettre(ctx, a)
    r = _repondre(ctx, d["id"], "MEME_PERSONNE")
    assert r.status_code == 200, r.text
    assert "Mesure de gel" in r.json()["message"]
    db.expire_all()
    alerte = db.get(Alert, a.id)
    assert alerte.status == "CLOSED_CONFIRMED"
    mesures = db.query(MesureDeGel).filter(MesureDeGel.alert_id == a.id).all()
    assert len(mesures) == 1 and mesures[0].statut == "A_DECLARER"
    # Le délai de déclaration du gel court depuis la confirmation : la réponse
    assert mesures[0].ouverte_le == alerte.decided_at
    events = [e.action for e in db.query(AlertEvent).filter(AlertEvent.alert_id == a.id)]
    assert {"HOMONYMIE_REPONSE", "GEL_OUVERT", "HOMONYMIE_TERMINEE"} <= set(events)
    refus = ctx["client"].post("/api/whitelist", data={
        "client_id": a.client_id, "watchlist_entity_id": a.watchlist_entity_id,
        "justification": "x"})
    assert refus.status_code == 409 and "personne désignée" in refus.json()["detail"]


def test_doute_persistant_la_decision_revient_a_l_organisme_et_se_motive(ctx):
    db, client = ctx["db"], ctx["client"]
    a = ctx["alerte"]("WATCHLIST_OFAC", "Doute")
    d = _douter_et_transmettre(ctx, a)
    r = _repondre(ctx, d["id"], "DOUTE_PERSISTANT")
    assert r.status_code == 200, r.text
    assert r.json()["statut"] == homonymie.STATUT_A_DECIDER
    db.expire_all()
    assert db.get(Alert, a.id).status == "PENDING_DGT", "la réponse ne tranche pas : l'organisme décide"

    _connecter(f"{PREFIXE}_reviseur")
    complet = {"decision": "NE_PAS_GELER", "motif": "Profil du client sans lien avec le régime.",
               "vigilance": "Vigilance renforcée, revue trimestrielle des opérations.",
               "tracfin": "NON_REQUISE", "tracfin_detail": "Aucun élément de soupçon (L.561-15)."}
    for manque in ("vigilance", "tracfin", "motif"):
        corps = {k: v for k, v in complet.items() if k != manque}
        r = client.post(f"/api/homonymies/{d['id']}/decision", data=corps,
                        files=_piece("information_dgt.eml"))
        assert r.status_code == 400, (manque, r.text)
    sans_piece = client.post(f"/api/homonymies/{d['id']}/decision", data=complet)
    assert sans_piece.status_code == 400 and "informée" in sans_piece.json()["detail"]
    r = client.post(f"/api/homonymies/{d['id']}/decision", data=complet,
                    files=_piece("information_dgt.eml"))
    assert r.status_code == 200, r.text
    db.expire_all()
    alerte = db.get(Alert, a.id)
    assert alerte.status == "CLOSED_FALSE_POSITIVE"
    assert "Doute persistant" in alerte.decision_comment and "information_dgt.eml" in alerte.decision_comment
    assert db.query(MesureDeGel).filter(MesureDeGel.alert_id == a.id).count() == 0
    # Pas établi homonyme : pas de liste blanche (LD §169), ni par l'écran ni à la clôture
    refus = client.post("/api/whitelist", data={
        "client_id": a.client_id, "watchlist_entity_id": a.watchlist_entity_id,
        "justification": "x"})
    assert refus.status_code == 409 and "§169" in refus.json()["detail"]
    assert client.get(f"/api/alerts/{a.id}").json()["homonymie_liste_blanche_refus"]
    _connecter(f"{PREFIXE}_analyste")

    # Le même couple, re-détecté plus tard : la déclaration suivante est une relance
    suivante = ctx["alerte"]("WATCHLIST_OFAC", "Doute2", client_id=a.client_id,
                             entite=a.watchlist_entity_id)
    assert _douter(ctx, suivante).json()["saisine"] == "NOUVELLE"


def test_doute_persistant_et_decision_de_geler_ouvre_la_mesure_de_gel(ctx):
    db = ctx["db"]
    a = ctx["alerte"]("WATCHLIST_DGT", "Geler")
    d = _douter_et_transmettre(ctx, a)
    assert _repondre(ctx, d["id"], "DOUTE_PERSISTANT").status_code == 200
    _connecter(f"{PREFIXE}_reviseur")
    r = ctx["client"].post(f"/api/homonymies/{d['id']}/decision", data={
        "decision": "GELER", "motif": "Faisceau d'indices concordant.", "tracfin": "DECLAREE",
        "tracfin_detail": "DS ERMES n° 2026-118."})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Alert, a.id).status == "CLOSED_CONFIRMED"
    assert db.query(MesureDeGel).filter(MesureDeGel.alert_id == a.id).count() == 1


def test_les_echanges_avec_la_dg_tresor_se_conservent_dates_avec_leur_piece(ctx):
    a = ctx["alerte"]("WATCHLIST_DGT", "Echanges")
    d = _douter_et_transmettre(ctx, a)
    url = f"/api/homonymies/{d['id']}/echanges"
    assert ctx["client"].post(url, data={"sens": "RECU", "objet": "Demande de passeport"}).status_code == 400
    r = ctx["client"].post(url, data={"sens": "RECU", "objet": "Demande de copie du passeport"},
                           files=_piece("demande_dgt.eml"))
    assert r.status_code == 200, r.text
    echange = r.json()["echanges"][0]
    assert echange["sens"] == "RECU" and echange["le"] and echange["piece"]["file_name"] == "demande_dgt.eml"
    assert echange["piece"]["file_present"] is True


def test_une_declaration_transmise_ne_s_abandonne_pas(ctx):
    db, client = ctx["db"], ctx["client"]
    a = ctx["alerte"]("WATCHLIST_DGT", "Abandon", statut="ESCALATED")
    assert _douter(ctx, a).status_code == 200
    d = _declaration(ctx, a)
    assert client.post(f"/api/homonymies/{d['id']}/abandon", json={"motif": " "}).status_code == 400
    r = client.post(f"/api/homonymies/{d['id']}/abandon",
                    json={"motif": "Passeport reçu : l'alerte se lève sans la DG Trésor."})
    assert r.status_code == 200, r.text
    db.expire_all()
    assert db.get(Alert, a.id).status == "ESCALATED", "l'alerte retrouve son état d'avant"
    b = ctx["alerte"]("WATCHLIST_DGT", "AbandonTard")
    d2 = _douter_et_transmettre(ctx, b)
    assert client.post(f"/api/homonymies/{d2['id']}/abandon", json={"motif": "x"}).status_code == 409


# ---------------------------------------------- délais : la transmission sonne, l'attente non

def test_une_declaration_preparee_non_transmise_au_dela_du_delai_sonne_une_fois_et_bloque(ctx, monkeypatch):
    from fiskr.mise_en_service import BLOQUANT, _declarations_d_homonymie, etat_de_conformite
    db = ctx["db"]
    emissions = _espionner_emit(monkeypatch)
    a = ctx["alerte"]("WATCHLIST_DGT", "Retard")
    assert _douter(ctx, a).status_code == 200
    d = db.query(DeclarationHomonymie).filter(DeclarationHomonymie.alert_id == a.id).one()
    d.preparee_le = datetime.utcnow() - timedelta(hours=30)
    db.commit()

    assert homonymie.detecter_retards(db) >= 1
    assert homonymie.detecter_retards(db) == 0, "une fois par déclaration"
    signalees = [p for k, p in emissions if k == "homonymie_non_transmise"]
    assert [p["Déclaration"] for p in signalees].count(f"#{d.id}") == 1
    events = [e.action for e in db.query(AlertEvent).filter(AlertEvent.alert_id == a.id)]
    assert "HOMONYMIE_NON_TRANSMISE" in events

    controle = _declarations_d_homonymie(db)
    assert controle["etat"] == BLOQUANT and f"#{d.id}" in controle["constat"]
    assert a.client_name in controle["constat"]
    # La sonde la rejoue, sans transmettre le nom d'une personne
    sonde = [b for b in etat_de_conformite(db)["bloquants"] if b["cle"] == "homonymies"]
    assert sonde and a.client_name not in sonde[0]["constat"]
    filtre = ctx["client"].get(f"/api/homonymies?statut={homonymie.FILTRE_NON_TRANSMISE_HORS_DELAI}").json()
    assert d.id in [x["id"] for x in filtre["items"]]

    # Transmise : le défaut est réparé, le contrôle ne la nomme plus
    assert _transmettre(ctx, d.id).status_code == 200
    apres = _declarations_d_homonymie(db)
    assert f"#{d.id}" not in apres["constat"]
    # (BLOQUANT ne subsiste que si une AUTRE déclaration est en retard)
    assert apres["etat"] != BLOQUANT or homonymie.non_transmises_en_retard(db)


def test_une_longue_attente_de_la_reponse_se_voit_et_ne_sonne_pas(ctx, monkeypatch):
    from fiskr.mise_en_service import _declarations_d_homonymie
    db = ctx["db"]
    emissions = _espionner_emit(monkeypatch)
    a = ctx["alerte"]("WATCHLIST_DGT", "Longue")
    d = _douter_et_transmettre(ctx, a)
    ligne = db.get(DeclarationHomonymie, d["id"])
    ligne.preparee_le = datetime.utcnow() - timedelta(days=61)
    ligne.transmise_le = datetime.utcnow() - timedelta(days=60)
    db.commit()
    homonymie.detecter_retards(db)
    assert not [p for k, p in emissions if k == "homonymie_non_transmise" and p["Déclaration"] == f"#{d['id']}"]
    assert f"#{d['id']}" not in _declarations_d_homonymie(db)["constat"]
    registre = ctx["client"].get(f"/api/homonymies?statut={homonymie.FILTRE_ATTENTE_LONGUE}").json()
    vue = next(x for x in registre["items"] if x["id"] == d["id"])
    assert vue["attente_longue"] is True and vue["attente_secondes"] >= 60 * 86400
    assert registre["synthese"]["long_wait"] >= 1


def test_le_delai_de_transmission_est_un_reglage_borne(ctx):
    client = ctx["client"]
    for hors in (0, 500):
        assert client.put("/api/settings/ingestion",
                          json={"homonymy_transmission_delay_hours": hors}).status_code == 400
    r = client.put("/api/settings/ingestion", json={"homonymy_transmission_delay_hours": 12})
    assert r.status_code == 200, r.text
    assert r.json()["homonymy_transmission_delay_hours"] == 12


# ---------------------------------------------- conservation

def test_l_alerte_en_attente_n_est_pas_purgeable_ni_close_sur_sa_declaration(ctx):
    from fiskr.retention import _purgeable_alert_ids
    db = ctx["db"]
    a = ctx["alerte"]("WATCHLIST_DGT", "Purge", client_id=f"{PREFIXE}_PURGE_SANS_FICHE")
    a.created_at = datetime.utcnow() - timedelta(days=4000)
    db.commit()
    d = _douter_et_transmettre(ctx, a)
    futur = datetime.utcnow() + timedelta(days=1)
    assert a.id not in _purgeable_alert_ids(db, futur), "une alerte en attente n'est pas close"
    assert _repondre(ctx, d["id"], "HOMONYME").status_code == 200
    db.expire_all()
    alerte = db.get(Alert, a.id)
    assert alerte.status == "CLOSED_FALSE_POSITIVE"
    alerte.decided_at = datetime.utcnow() - timedelta(days=4000)
    db.commit()
    # Close et ancienne : la déclaration la tient — ses échanges avec la DG
    # Trésor se conservent dans tous les cas (§168)
    assert a.id not in _purgeable_alert_ids(db, futur)


# ---------------------------------------------- le projet et l'écran

def test_le_projet_suit_les_rubriques_du_formulaire_de_la_dg_tresor(ctx):
    a = ctx["alerte"]("WATCHLIST_DGT", "Projet")
    assert _douter(ctx, a, commentaire="Client depuis 2011.").status_code == 200
    d = _declaration(ctx, a)
    # Lecture sans écriture : l'auditeur y a accès
    _connecter(f"{PREFIXE}_auditeur", roles=("auditor",))
    projet = ctx["client"].get(f"/api/homonymies/{d['id']}/draft")
    assert projet.status_code == 200, projet.text
    corps = projet.json()
    assert corps["type"] == "PROJET_DECLARATION_HOMONYMIE"
    assert corps["destinataire"] == "homonymies-gels@dgtresor.gouv.fr"
    assert corps["formulaire"]["titre"].endswith(".xlsx")
    assert corps["sur_qui"]["numero_registre_national"] == f"{UID}Projet"
    assert corps["traitement"]["bilan_des_recherches"] == DILIGENCES
    assert corps["traitement"]["saisine"].startswith("Saisine initiale")
    # Ce qui manque est nommé, jamais inventé
    assert any("rôle de l'opérateur" in m for m in corps["informations_a_completer"])
    imprimable = ctx["client"].get(f"/api/homonymies/{d['id']}/draft/print")
    assert imprimable.status_code == 200
    for rubrique in ("1. Qui alerte ?", "2. Sur qui porte l'alerte ?",
                     "3. Sur quelle opération porte l'alerte ?",
                     "4. Traitement de l'alerte et de la déclaration d'homonymie",
                     "Bilan des recherches effectuées", "homonymies-gels@dgtresor.gouv.fr"):
        assert rubrique in imprimable.text, rubrique
    assert ctx["client"].post(f"/api/homonymies/{d['id']}/transmission").status_code == 403


def test_l_attente_dgt_a_son_libelle_son_filtre_et_ses_libelles_d_historique():
    from fiskr.narrative import ALERT_STATUS_LABELS
    assert set(ALERT_OPEN_STATUSES) <= set(ALERT_STATUS_LABELS)
    app_js = (RACINE / "fiskr" / "static" / "app.js").read_text(encoding="utf-8")
    index = (RACINE / "fiskr" / "static" / "index.html").read_text(encoding="utf-8")
    bloc = re.search(r"const STATUS_LABELS = \{(.*?)\n\};", app_js, re.S).group(1)
    assert "PENDING_DGT:" in bloc
    defaut = re.search(r'const DEFAULT_ALERT_FILTER = "([^"]+)"', app_js).group(1)
    assert "PENDING_DGT" not in defaut.split(","), "la file ordinaire ne la montre pas"
    # Un filtre par file : criblage et filtrage
    assert index.count("setAlertFilter('SCREENING','PENDING_DGT')") == 1
    assert index.count("setAlertFilter('FILTERING','PENDING_DGT')") == 1
    assert "blocDeclarationsHomonymie(a, isReviewer)" in app_js
    assert 'id="homonymies-table"' in index

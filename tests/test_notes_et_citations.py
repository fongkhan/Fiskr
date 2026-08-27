"""
Ce que l'analyste sait ne doit pas rester dans sa tête.

Deux manques du même ordre :

1. **Citer un collègue** (@nom) dans un commentaire d'alerte ne prévenait
   personne. Le piège n'est pas la notification, c'est le SILENCE : « j'ai
   cité Marie, elle n'a jamais répondu » a trois causes — nom mal écrit,
   compte sans adresse, ou rien du tout — et l'auteur doit pouvoir les
   distinguer avant de croire sa question posée.

2. **La note interne sur une fiche listée** n'existait pas. « Homonymie
   établie pour le client X » finissait dans le commentaire d'un dossier clos
   que personne ne relit, et le suivant refaisait le travail. Deux choix
   structurants sont testés ici : la note est ancrée sur l'identifiant MÉTIER
   (elle survit à la synchronisation qui remplace tout l'instantané), et elle
   n'a AUCUN effet sur le criblage — ce qui la distingue de la liste blanche,
   et doit se lire sur l'écran, sinon quelqu'un croira l'alerte éteinte.
"""
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (get_db, Alert, AlertEvent, AlertFollower, AuditTrail,
                            Snapshot, User, WatchlistEntity, WatchlistNote, hash_password)
from fiskr.events import AUDIENCE_MENTIONED, EVENT_CATALOG

UID = uuid.uuid4().hex[:8].upper()
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _spy_emit(monkeypatch):
    calls = []
    spy = lambda db, key, payload=None, **kw: calls.append((key, payload or {}, kw))
    import fiskr.api as api_mod
    import fiskr.notifier as notifier_mod
    monkeypatch.setattr(api_mod, "emit", spy)
    monkeypatch.setattr(notifier_mod, "emit", spy)
    return calls


def _connecter(username):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": "admin", "roles": ["admin", "reviewer"],
    }


@pytest.fixture()
def ctx():
    db = next(get_db())
    # « marie » a une adresse, « paul » n'en a pas : deux issues différentes
    for nom, email in ((f"marie_{UID}", f"marie_{UID}@exemple.test"),
                       (f"paul_{UID}", None),
                       (f"auteur_{UID}", f"auteur_{UID}@exemple.test")):
        if not db.query(User).filter(User.username == nom).first():
            hashed, salt = hash_password("MotDePasse#1234")
            db.add(User(username=nom, hashed_password=hashed, salt=salt,
                        email=email, role="admin"))
    db.commit()
    _connecter(f"auteur_{UID}")

    audit = AuditTrail(client_id=f"test_nc_{UID}", client_name=f"Client NC-{UID}",
                       client_type="PP", watchlist_id=f"NC-{UID}",
                       watchlist_name=f"Listé NC-{UID}", base_score=91.0,
                       final_score=91.0, status="ALERT", decision_tree={},
                       config_state={}, watchlist_version="test", watchlist_hash="test")
    db.add(audit)
    db.commit()
    alerte = Alert(audit_id=audit.id, client_id=audit.client_id, client_name=audit.client_name,
                   watchlist_entity_id=f"NC-{UID}", watchlist_name=audit.watchlist_name,
                   final_score=91.0, status="OPEN", channel="SCREENING")
    db.add(alerte)
    db.commit()

    yield {"db": db, "client": TestClient(app), "alerte": alerte, "entity_id": f"NC-{UID}"}

    app.dependency_overrides.pop(get_current_user, None)
    try:
        ids = [a.id for a in db.query(Alert).filter(Alert.client_id.like(f"test_nc_{UID}%")).all()]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertFollower).filter(AlertFollower.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like(f"test_nc_{UID}%")).delete(synchronize_session=False)
        db.query(WatchlistNote).filter(WatchlistNote.entity_id.like(f"NC-{UID}%")).delete(synchronize_session=False)
        db.query(User).filter(User.username.like(f"%_{UID}")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------- les citations

def test_citer_un_collegue_le_previent(ctx, monkeypatch):
    calls = _spy_emit(monkeypatch)
    r = ctx["client"].post(f"/api/alerts/{ctx['alerte'].id}/comment",
                           json={"comment": f"@marie_{UID} peux-tu confirmer l'homonymie ?"})
    assert r.status_code == 200, r.text
    assert r.json()["mentions_notifiees"] == [f"marie_{UID}"]
    cites = [(p, kw) for k, p, kw in calls if k == "alert_mentioned"]
    assert len(cites) == 1
    assert cites[0][1]["recipients_override"] == [f"marie_{UID}@exemple.test"]


def test_un_nom_mal_ecrit_ne_previent_personne_et_le_dit(ctx, monkeypatch):
    """C'est le cœur du lot : sans ce retour, l'auteur croit sa question posée
    et attend une réponse qui ne viendra jamais."""
    calls = _spy_emit(monkeypatch)
    r = ctx["client"].post(f"/api/alerts/{ctx['alerte'].id}/comment",
                           json={"comment": "@mrie tu confirmes ?"})
    assert r.json()["mentions_inconnues"] == ["mrie"]
    assert r.json()["mentions_notifiees"] == []
    assert not [c for c in calls if c[0] == "alert_mentioned"]


def test_un_compte_sans_adresse_est_signale_a_part(ctx, monkeypatch):
    """Troisième issue : le compte existe, la citation est correcte, et
    pourtant rien ne part. La confondre avec un succès serait mentir."""
    _spy_emit(monkeypatch)
    r = ctx["client"].post(f"/api/alerts/{ctx['alerte'].id}/comment",
                           json={"comment": f"@paul_{UID} ton avis ?"})
    assert r.json()["mentions_sans_adresse"] == [f"paul_{UID}"]
    assert r.json()["mentions_notifiees"] == []


def test_la_citation_est_insensible_a_la_casse_et_ignore_l_auteur(ctx, monkeypatch):
    calls = _spy_emit(monkeypatch)
    r = ctx["client"].post(
        f"/api/alerts/{ctx['alerte'].id}/comment",
        json={"comment": f"@MARIE_{UID} et @auteur_{UID} : je note."})
    assert r.json()["mentions_notifiees"] == [f"marie_{UID}"], \
        "@MARIE doit porter, et se citer soi-même ne se notifie pas"
    assert len([c for c in calls if c[0] == "alert_mentioned"]) == 1


def test_un_commentaire_sans_citation_n_emet_rien(ctx, monkeypatch):
    calls = _spy_emit(monkeypatch)
    ctx["client"].post(f"/api/alerts/{ctx['alerte'].id}/comment",
                       json={"comment": "Instruction en cours, rien à signaler."})
    assert not [c for c in calls if c[0] == "alert_mentioned"]


def test_l_evenement_de_citation_ne_replie_pas_sur_tout_le_monde(ctx):
    from fiskr.notifier import resolve_recipients
    ev = EVENT_CATALOG["alert_mentioned"]
    assert ev.audience == (AUDIENCE_MENTIONED,)
    assert resolve_recipients(ctx["db"], "alert_mentioned", {}) == [], \
        "sans cité résoluble, « personne » doit rester « personne »"


# ----------------------------------------------------------------- les notes

def test_une_note_s_ecrit_se_relit_et_porte_son_auteur(ctx):
    c, eid = ctx["client"], ctx["entity_id"]
    r = c.post(f"/api/watchlist/notes/{eid}",
               json={"note": "Homonymie établie avec le client 12345."})
    assert r.status_code == 201, r.text
    lecture = c.get(f"/api/watchlist/notes/{eid}").json()
    assert lecture["items"][0]["note"] == "Homonymie établie avec le client 12345."
    assert lecture["items"][0]["created_by"] == f"auteur_{UID}"
    assert lecture["sans_effet_sur_le_criblage"] is True, \
        "l'API doit le dire aussi, pas seulement l'écran"


def test_les_notes_sont_bornees_et_jamais_vides(ctx):
    c, eid = ctx["client"], ctx["entity_id"]
    assert c.post(f"/api/watchlist/notes/{eid}", json={"note": "   "}).status_code == 400
    trop = c.post(f"/api/watchlist/notes/{eid}", json={"note": "x" * 4001})
    assert trop.status_code == 400 and "4 000" in trop.json()["detail"]


def test_la_note_survit_au_remplacement_de_l_instantane(ctx):
    """LE choix structurant : la note est ancrée sur l'identifiant métier. Sur
    la ligne de l'instantané, elle mourrait à la synchronisation de mardi —
    exactement au moment où l'on a besoin de se souvenir."""
    db, c, eid = ctx["db"], ctx["client"], ctx["entity_id"]
    snap = f"snap-nc-{UID.lower()}"
    db.add(Snapshot(snapshot_id=snap, file_type="WATCHLIST_OFAC", file_name="x.csv",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    db.add(WatchlistEntity(snapshot_id=snap, entity_id=eid, entity_type="I",
                           primary_name="Listé NC", entity_checksum="chk-nc"))
    db.commit()
    c.post(f"/api/watchlist/notes/{eid}", json={"note": "Note d'avant la synchro."})
    # La synchronisation remplace la ligne de l'instantané
    db.query(WatchlistEntity).filter(WatchlistEntity.snapshot_id == snap).delete(synchronize_session=False)
    db.query(Snapshot).filter(Snapshot.snapshot_id == snap).delete(synchronize_session=False)
    db.commit()
    apres = c.get(f"/api/watchlist/notes/{eid}").json()["items"]
    assert any(n["note"] == "Note d'avant la synchro." for n in apres)


def test_le_dossier_d_alerte_porte_les_notes_de_la_fiche(ctx):
    """L'analyste rencontre le listé ICI : une note qu'il faudrait aller
    chercher ailleurs ne serait jamais lue."""
    c = ctx["client"]
    c.post(f"/api/watchlist/notes/{ctx['entity_id']}",
           json={"note": "Même personne que EU-1234."})
    detail = c.get(f"/api/alerts/{ctx['alerte'].id}").json()
    assert [n["note"] for n in detail["watchlist_notes"]] == ["Même personne que EU-1234."]


def test_les_notes_se_lisent_de_la_plus_recente_a_la_plus_ancienne(ctx):
    c, eid = ctx["client"], ctx["entity_id"]
    for texte in ("Première.", "Deuxième.", "Troisième."):
        c.post(f"/api/watchlist/notes/{eid}", json={"note": texte})
    items = c.get(f"/api/watchlist/notes/{eid}").json()["items"]
    assert [n["note"] for n in items][:3] == ["Troisième.", "Deuxième.", "Première."]


# ------------------------------------------------------- le frontal, statique

def test_l_ecran_dit_que_la_note_n_agit_pas_sur_le_criblage(ctx):
    """Sans cette phrase, quelqu'un écrira une note en croyant que l'alerte ne
    reviendra pas : le réglage qu'on enregistre et qui ne fait rien."""
    src = _lire("app.js")
    assert src.count("Sans effet sur le criblage") >= 2, \
        "à la lecture ET au moment d'écrire la note"
    dico = _lire("i18n.js")
    assert '"Sans effet sur le criblage : seule la liste blanche supprime des alertes."' in dico


def test_la_citation_est_decoree_apres_neutralisation(ctx):
    """Décorer d'abord, échapper ensuite, injecterait le balisage de la
    décoration — et rien ne le signalerait."""
    src = _lire("app.js")
    fn = src[src.index("function marquerLesCitations"):]
    fn = fn[:fn.index("\n}")]
    assert "escapeHtml(texte).replace" in fn, "la neutralisation passe AVANT la décoration"
    assert '<span class="citation">' in fn
    assert ".citation" in _lire("styles.css")


def test_le_compte_rendu_des_citations_est_montre_a_l_auteur(ctx):
    src = _lire("app.js")
    fn = src[src.index("function rendreCompteDesCitations"):]
    fn = fn[:fn.index("\n}")]
    for cle in ("mentions_notifiees", "mentions_inconnues", "mentions_sans_adresse"):
        assert cle in fn, cle
    assert fn.count("showToast") == 3, "les trois issues se disent séparément"


def test_les_notes_indisponibles_ne_se_lisent_pas_comme_aucune_note(ctx):
    src = _lire("app.js")
    fn = src[src.index("async function chargerSectionNotes"):]
    fn = fn[:fn.index("\n}")]
    assert "items === null" in fn and "indisponibles" in fn

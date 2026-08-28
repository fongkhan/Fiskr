"""
« Par quoi je commence » — les panneaux personnels de l'accueil.

L'accueil est déjà une grille riche et personnalisable, mais tous ses
panneaux sont COLLECTIFS : « alertes ouvertes » les compte toutes, « charge
par analyste » montre celle de tout le monde. Rien ne répondait à la
première question d'une matinée : par quoi je commence ?

Un chiffre change de sens au passage, et c'est le cœur de ce lot. La tuile
« 4 yeux » collective compte les décisions en attente de validation — y
compris celles que J'AI proposées, que la règle des quatre yeux m'interdit
précisément de valider. Annoncer « 3 à valider » à quelqu'un qui n'en peut
valider aucune, c'est promettre du travail qui n'existe pas. Le compte
personnel ne retient que ce sur quoi ce lecteur peut réellement agir.
"""
import os
import uuid

import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (get_db, Alert, AlertEvent, AlertFollower, AuditTrail,
                            Snapshot)

UID = uuid.uuid4().hex[:8].upper()
MOI, AUTRE = f"moi_{UID}", f"autre_{UID}"
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _connecter(username, roles=("admin", "reviewer")):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": username, "full_name": username,
        "role": ",".join(roles), "roles": list(roles),
    }


@pytest.fixture()
def ctx():
    db = next(get_db())
    _connecter(MOI)

    def creer(nom, **champs):
        audit = AuditTrail(client_id=f"test_mj_{UID}_{nom}", client_name=f"{nom} MJ-{UID}",
                           client_type="PP", watchlist_id=f"MJ-{UID}",
                           watchlist_name=f"Listé MJ-{UID}", base_score=91.0,
                           final_score=91.0, status="ALERT", decision_tree={},
                           config_state={}, watchlist_version="t", watchlist_hash="h")
        db.add(audit)
        db.commit()
        champs = {"status": "OPEN", "channel": "SCREENING", **champs}
        a = Alert(audit_id=audit.id, client_id=audit.client_id, client_name=audit.client_name,
                  watchlist_entity_id=f"MJ-{UID}", watchlist_name=audit.watchlist_name,
                  final_score=91.0, **champs)
        db.add(a)
        db.commit()
        return a

    yield {"db": db, "client": TestClient(app), "creer": creer}

    app.dependency_overrides.pop(get_current_user, None)
    try:
        ids = [a.id for a in db.query(Alert).filter(Alert.client_id.like(f"test_mj_{UID}%")).all()]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(AlertFollower).filter(AlertFollower.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like(f"test_mj_{UID}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _journee(ctx):
    r = ctx["client"].get("/api/me/journee")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------- ce qui est à moi

def test_mes_alertes_ne_comptent_que_les_miennes(ctx):
    ctx["creer"]("Mienne", assigned_to=MOI)
    ctx["creer"]("Sienne", assigned_to=AUTRE)
    ctx["creer"]("Personne")
    j = _journee(ctx)
    assert j["mes_alertes"]["total"] == 1
    assert j["mes_alertes"]["items"][0]["client_name"].startswith("Mienne")


def test_mes_retards_sont_comptes_a_part(ctx):
    ctx["creer"]("EnRetard", assigned_to=MOI, due_at=datetime.utcnow() - timedelta(hours=3))
    ctx["creer"]("DansLesTemps", assigned_to=MOI, due_at=datetime.utcnow() + timedelta(days=2))
    j = _journee(ctx)
    assert j["mes_alertes"]["total"] == 2 and j["mes_alertes"]["en_retard"] == 1


def test_mes_alertes_arrivent_dans_l_ordre_de_la_file(ctx):
    ctx["creer"]("Basse", assigned_to=MOI, priority="LOW")
    ctx["creer"]("Critique", assigned_to=MOI, priority="CRITICAL")
    ctx["creer"]("Haute", assigned_to=MOI, priority="HIGH")
    noms = [i["client_name"].split()[0] for i in _journee(ctx)["mes_alertes"]["items"]]
    assert noms == ["Critique", "Haute", "Basse"], noms


def test_une_alerte_close_ne_m_attend_plus(ctx):
    ctx["creer"]("Close", assigned_to=MOI, status="CLOSED_FALSE_POSITIVE")
    assert _journee(ctx)["mes_alertes"]["total"] == 0


# ----------------------------------- le chiffre qui change de sens : 4 yeux

def test_a_valider_exclut_mes_propres_propositions(ctx):
    """LE point du lot. La règle des quatre yeux interdit de valider sa propre
    proposition : la compter serait promettre du travail impossible."""
    ctx["creer"]("ProposeeParMoi", status="PENDING_VALIDATION", proposed_by=MOI,
                 proposed_at=datetime.utcnow())
    ctx["creer"]("ProposeeParAutre", status="PENDING_VALIDATION", proposed_by=AUTRE,
                 proposed_at=datetime.utcnow())
    j = _journee(ctx)
    assert j["a_valider"]["total"] == 1
    assert j["a_valider"]["items"][0]["client_name"].startswith("ProposeeParAutre")
    assert j["a_valider"]["items"][0]["proposed_by"] == AUTRE


def test_le_refus_du_serveur_confirme_la_regle_que_le_compte_applique(ctx):
    """Le compte et la règle doivent dire la même chose : sans ce test, ils
    pourraient diverger sans que rien ne le signale."""
    a = ctx["creer"]("Mienne", status="PENDING_VALIDATION", proposed_by=MOI,
                     proposed_at=datetime.utcnow(), proposed_decision="FALSE_POSITIVE")
    r = ctx["client"].post(f"/api/alerts/{a.id}/validate", json={"approve": True, "comment": "ok"})
    assert r.status_code == 403, "le serveur doit refuser de valider sa propre proposition"
    assert _journee(ctx)["a_valider"]["total"] == 0, "et le compte ne doit pas la promettre"


def test_sans_le_role_rien_n_est_a_valider(ctx):
    ctx["creer"]("Attente", status="PENDING_VALIDATION", proposed_by=AUTRE,
                 proposed_at=datetime.utcnow())
    _connecter(MOI, roles=("user",))
    j = _journee(ctx)
    assert j["a_valider"]["peut_valider"] is False
    assert j["a_valider"]["total"] == 0 and j["a_valider"]["items"] == []
    assert j["lots_a_homologuer"] == 0, "ni lots à homologuer sans le rôle"


# ------------------------------------------------------ ce qui me revient

def test_mes_mises_en_attente_qui_reviennent_sont_annoncees(ctx):
    ctx["creer"]("RevientBientot", assigned_to=MOI,
                 snoozed_until=datetime.utcnow() + timedelta(hours=3))
    ctx["creer"]("RevientDansUnMois", assigned_to=MOI,
                 snoozed_until=datetime.utcnow() + timedelta(days=30))
    assert _journee(ctx)["reveils_du_jour"] == 1


def test_mes_dossiers_suivis_encore_ouverts_sont_comptes(ctx):
    a = ctx["creer"]("Suivie")
    b = ctx["creer"]("SuivieClose", status="CLOSED_CONFIRMED")
    db = ctx["db"]
    db.add(AlertFollower(alert_id=a.id, username=MOI))
    db.add(AlertFollower(alert_id=b.id, username=MOI))
    db.commit()
    assert _journee(ctx)["dossiers_suivis"] == 1


# ------------------------------------------------------------- le frontal

def test_les_panneaux_personnels_rejoignent_le_catalogue_existant():
    """Un écran « Ma journée » séparé aurait doublé une grille déjà
    personnalisable : les panneaux entrent dans le catalogue, l'utilisateur
    les place où il veut."""
    src = _lire("app.js")
    for pan in ("tile-mes-alertes", "tile-a-valider", "table-ma-journee"):
        assert f'"{pan}":' in src, pan
        assert f'{{ id: "{pan}"' in src, f"{pan} absent de la disposition par défaut"


def test_les_trois_panneaux_ne_font_qu_une_requete():
    src = _lire("app.js")
    fn = src[src.index("async function chargerMaJournee"):]
    fn = fn[:fn.index("\nasync function ")]
    assert "_maJourneePromesse" in fn, "sans promesse partagée, trois panneaux = trois requêtes"
    assert "_maJournee = null;" in src[src.index("async function loadDashboardLayout"):
                                       src.index("async function loadDashboardLayout") + 400], \
        "un retour sur l'accueil doit relire la journée, pas resservir celle d'il y a une heure"


def test_journee_indisponible_ne_se_lit_pas_comme_journee_vide():
    src = _lire("app.js")
    fn = src[src.index("async function renderMaJourneeWidget"):]
    fn = fn[:fn.index("\nfunction ")]
    assert "indisponible" in fn
    assert "Rien ne vous attend" in fn
    assert fn.index("indisponible") < fn.index("Rien ne vous attend"), \
        "l'indisponibilité se traite avant, et séparément, du vide"


def test_un_panneau_asynchrone_qui_echoue_ne_casse_pas_l_accueil():
    """Le try/catch synchrone du rendu ne voit pas le rejet d'une promesse :
    sans cette rustine, un panneau asynchrone en erreur laisserait l'accueil
    à moitié dessiné."""
    src = _lire("app.js")
    assert "Promise.resolve(def.render(body, d))" in src


def test_les_libelles_personnels_sont_traduits():
    dico = _lire("i18n.js")
    for cle in ("Mes alertes", "À valider par moi", "Ma journée",
                "hors mes propres propositions",
                "Rien ne vous attend : votre journée est à jour."):
        assert f'"{cle}"' in dico, cle

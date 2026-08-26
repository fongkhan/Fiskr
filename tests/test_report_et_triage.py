"""
Report d'une alerte (mise en attente) et triage clavier des files.

Le report — « attente de pièces, revoir dans 3 jours » — obéit à trois règles
qui ne se négocient pas sur un produit de conformité :

1. **Jamais masqué.** Une alerte reportée descend en bas de la file, elle
   n'en sort pas : cacher du travail est la définition du défaut que ce
   produit passe son temps à chasser.
2. **Le SLA continue de courir.** Reporter son travail ne reporte pas
   l'obligation : `due_at` ne bouge pas, et si l'échéance réglementaire tombe
   pendant l'attente, le serveur le DIT au moment du report.
3. **Tracé au journal.** Motif obligatoire, inscrit dans l'historique
   immuable de l'alerte — un dossier qui dort doit pouvoir dire pourquoi.

Le triage clavier (j/k, o, r) est le motif des clients mail, vérifié ici en
statique et dans un navigateur réel avant livraison.
"""
import os
import re
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AuditTrail

UID = uuid.uuid4().hex[:8].upper()
TAG = f"ReportTriage-{UID}"
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


def _iso(delta):
    return (datetime.utcnow() + delta).isoformat()


@pytest.fixture()
def ctx():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "test_rt_admin", "full_name": "test_rt_admin",
        "role": "admin", "roles": ["admin", "reviewer"],
    }
    db = next(get_db())

    def creer(nom, **champs):
        audit = AuditTrail(client_id=f"test_rt_{UID}_{nom}", client_name=f"{nom} {TAG}",
                           client_type="PP", watchlist_id=f"RT-{UID}",
                           watchlist_name=f"Liste {TAG}", base_score=91.0,
                           final_score=91.0, status="ALERT", decision_tree={},
                           config_state={}, watchlist_version="test", watchlist_hash="test")
        db.add(audit)
        db.commit()
        champs = {"status": "OPEN", "channel": "SCREENING", **champs}
        alerte = Alert(audit_id=audit.id, client_id=audit.client_id, client_name=audit.client_name,
                       watchlist_entity_id=f"RT-{UID}", watchlist_name=audit.watchlist_name,
                       final_score=91.0, **champs)
        db.add(alerte)
        db.commit()
        return alerte

    yield {"db": db, "client": TestClient(app), "creer": creer}
    app.dependency_overrides.pop(get_current_user, None)
    try:
        ids = [a.id for a in db.query(Alert).filter(Alert.client_id.like(f"test_rt_{UID}%")).all()]
        if ids:
            db.query(AlertEvent).filter(AlertEvent.alert_id.in_(ids)).delete(synchronize_session=False)
            db.query(Alert).filter(Alert.id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditTrail).filter(AuditTrail.client_id.like(f"test_rt_{UID}%")).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------ le report

def test_reporter_exige_un_motif(ctx):
    a = ctx["creer"]("SansMotif")
    r = ctx["client"].post(f"/api/alerts/{a.id}/snooze", json={"until": _iso(timedelta(days=3))})
    assert r.status_code == 400
    assert "motif" in r.json()["detail"].lower()


def test_reporter_ecrit_l_echeance_et_le_journal(ctx):
    a = ctx["creer"]("Journalise")
    r = ctx["client"].post(f"/api/alerts/{a.id}/snooze",
                           json={"until": _iso(timedelta(days=3)),
                                 "reason": "Attente de pièces du client."})
    assert r.status_code == 200, r.text
    corps = r.json()
    assert corps["snoozed"] is True and corps["snoozed_until"]
    detail = ctx["client"].get(f"/api/alerts/{a.id}").json()
    reports = [e for e in detail["events"] if e["action"] == "SNOOZED"]
    assert len(reports) == 1, "le report doit s'inscrire au journal de l'alerte"
    assert "Attente de pièces du client." in reports[0]["detail"], \
        "le motif doit figurer dans l'entrée du journal — un dossier qui dort dit pourquoi"


def test_les_bornes_du_report(ctx):
    a = ctx["creer"]("Bornes")
    passe = ctx["client"].post(f"/api/alerts/{a.id}/snooze",
                               json={"until": _iso(timedelta(hours=-1)), "reason": "x"})
    assert passe.status_code == 400 and "futur" in passe.json()["detail"]
    trop = ctx["client"].post(f"/api/alerts/{a.id}/snooze",
                              json={"until": _iso(timedelta(days=31)), "reason": "x"})
    assert trop.status_code == 400
    # Le message est un littéral (traduisible) : il doit rester accordé à la
    # constante, sinon l'API annonce une borne qu'elle n'applique pas.
    assert f"{api._SNOOZE_MAX_JOURS} jours" in trop.json()["detail"]
    illisible = ctx["client"].post(f"/api/alerts/{a.id}/snooze",
                                   json={"until": "pas-une-date", "reason": "x"})
    assert illisible.status_code == 400


def test_le_sla_continue_de_courir(ctx):
    echeance = datetime.utcnow() + timedelta(days=1)
    a = ctx["creer"]("SlaCourt", due_at=echeance)
    r = ctx["client"].post(f"/api/alerts/{a.id}/snooze",
                           json={"until": _iso(timedelta(days=3)), "reason": "Attente longue."})
    assert r.status_code == 200
    corps = r.json()
    assert corps["warning"] and "SLA" in corps["warning"], \
        "reporter au-delà de l'échéance réglementaire doit se dire au moment du report"
    assert corps["due_at"] == echeance.isoformat(), "due_at ne bouge JAMAIS : le report ne suspend pas le SLA"
    court = ctx["creer"]("SlaLoin", due_at=datetime.utcnow() + timedelta(days=10))
    r2 = ctx["client"].post(f"/api/alerts/{court.id}/snooze",
                            json={"until": _iso(timedelta(days=3)), "reason": "Attente courte."})
    assert r2.status_code == 200 and r2.json()["warning"] is None


def test_reveil_manuel_et_double_reveil(ctx):
    a = ctx["creer"]("Reveil")
    ctx["client"].post(f"/api/alerts/{a.id}/snooze",
                       json={"until": _iso(timedelta(days=3)), "reason": "x"})
    r = ctx["client"].post(f"/api/alerts/{a.id}/snooze", json={"until": None})
    assert r.status_code == 200
    assert r.json()["snoozed_until"] is None
    detail = ctx["client"].get(f"/api/alerts/{a.id}").json()
    assert any(e["action"] == "WOKEN" for e in detail["events"]), "le réveil aussi se journalise"
    encore = ctx["client"].post(f"/api/alerts/{a.id}/snooze", json={"until": None})
    assert encore.status_code == 400


def test_report_refuse_sur_alerte_close(ctx):
    a = ctx["creer"]("Close", status="CLOSED_FALSE_POSITIVE")
    r = ctx["client"].post(f"/api/alerts/{a.id}/snooze",
                           json={"until": _iso(timedelta(days=3)), "reason": "x"})
    assert r.status_code == 409


def test_la_file_classe_les_reportees_en_bas_sans_les_masquer(ctx):
    """Une CRITICAL reportée passe APRÈS une LOW active — mais elle reste dans
    la réponse : descendre n'est pas disparaître."""
    ctx["creer"]("Critique", priority="CRITICAL",
                 snoozed_until=datetime.utcnow() + timedelta(days=2))
    ctx["creer"]("Basse", priority="LOW")
    ctx["creer"]("Haute", priority="HIGH")
    r = ctx["client"].get(f"/api/alerts?search={TAG}&page_size=50")
    noms = [i["client_name"].split()[0] for i in r.json()["items"]]
    assert noms == ["Haute", "Basse", "Critique"], noms
    assert r.json()["items"][2]["snoozed"] is True


def test_un_report_expire_s_eteint_sans_geste(ctx):
    """L'échéance passée, l'alerte redevient active toute seule : ni action,
    ni écriture en base — le drapeau et le classement se calculent à la
    lecture."""
    ctx["creer"]("Expiree", priority="CRITICAL",
                 snoozed_until=datetime.utcnow() - timedelta(hours=1))
    ctx["creer"]("Active", priority="LOW")
    r = ctx["client"].get(f"/api/alerts?search={TAG}&page_size=50")
    items = r.json()["items"]
    noms = [i["client_name"].split()[0] for i in items]
    assert noms[0] == "Expiree", "un report expiré rend sa place de CRITICAL en tête de file"
    assert items[0]["snoozed"] is False


def test_proposer_une_decision_efface_le_report(ctx):
    a = ctx["creer"]("Proposee")
    ctx["client"].post(f"/api/alerts/{a.id}/snooze",
                       json={"until": _iso(timedelta(days=3)), "reason": "x"})
    r = ctx["client"].post(f"/api/alerts/{a.id}/propose",
                           json={"decision": "FALSE_POSITIVE", "comment": "Homonyme avéré."})
    assert r.status_code == 200, r.text
    detail = ctx["client"].get(f"/api/alerts/{a.id}").json()
    assert detail["snoozed_until"] is None, \
        "proposer une décision, c'est instruire : l'attente n'a plus de sens"


# ------------------------------------------------------- le frontal, en statique

def test_les_lignes_portent_leur_identifiant_et_la_pastille(ctx):
    src = _lire("app.js")
    assert '<tr data-alert-id="${a.id}">' in src, \
        "sans identifiant sur la ligne, ni visée clavier ni restauration après re-rendu"
    assert "${alertStatusBadge(a.status)}${alertSnoozeChip(a)}" in src
    chip = src[src.index("function alertSnoozeChip"):]
    chip = chip[:chip.index("\n}")]
    assert "if (!a.snoozed) return \"\";" in chip, \
        "la pastille ne s'affiche que si le serveur dit « en attente » — pas de calcul local divergent"
    assert '"EN ATTENTE"' in _lire("i18n.js")


def test_le_triage_clavier_respecte_le_reste_de_la_page(ctx):
    src = _lire("app.js")
    triage = src[src.index("function initTriageClavier"):]
    triage = triage[:triage.index("\n}")]
    for garde in ('cible.tagName === "INPUT"', 'cible.tagName === "TEXTAREA"',
                  'cible.tagName === "SELECT"', "cible.isContentEditable",
                  '".modal:not(.hidden)"'):
        assert garde in triage, f"garde absente : {garde}"
    assert 'e.key === "Enter" && cible !== document.body' in triage, \
        "Entrée appartient déjà aux éléments focalisés (tri, boutons) : ne la prendre qu'au corps de page"
    assert re.search(r"^ initTriageClavier\(\);", src, re.M)
    assert "_viseAlerteId" in src[src.index("function renderAlertsTable"):
                                  src.index("function renderAlertsTable") + 4000], \
        "la visée doit survivre au re-rendu de la file"


def test_l_aide_annonce_les_touches_du_triage(ctx):
    page = _lire("index.html")
    aide = page[page.index('id="raccourcis-modal"'):]
    aide = aide[:aide.index("</table>")]
    assert "<kbd>j</kbd> / <kbd>k</kbd>" in aide
    assert "<kbd>o</kbd> / <kbd>Entrée</kbd>" in aide
    assert "<kbd>r</kbd>" in aide
    assert ".ligne-visee" in _lire("styles.css")


def test_le_dialogue_de_report_borne_et_traduit(ctx):
    src = _lire("app.js")
    fn = src[src.index("async function reporterAlerte"):]
    fn = fn[:fn.index("\n}")]
    assert "n < 1 || n > 30" in fn, "la borne du dialogue doit refléter celle du serveur"
    dico = _lire("i18n.js")
    m = re.search(r"const _MOTIFS_DE_REPORT = \[(.*?)\];", src, re.S)
    assert m, "suggestions de motif absentes"
    motifs = re.findall(r'"((?:[^"\\]|\\.)+)"', m.group(1))
    assert len(motifs) >= 3
    absents = [x for x in motifs if f'"{x}"' not in dico]
    assert not absents, f"motifs de report sans traduction : {absents}"

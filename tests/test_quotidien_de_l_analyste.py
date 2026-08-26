"""
Lot 2 du programme qualité de vie : le quotidien de l'analyste.

* **Échéance visible pendant qu'il est temps** (n° 9) — le retard s'affichait
  déjà (« EN RETARD ») ; l'approche, non : on apprenait l'échéance en la
  dépassant. Sous douze heures, une chip ambre l'annonce. La file était déjà
  triée par priorité puis échéance — le tri n'était pas le manque, la
  lisibilité l'était.
* **Motifs de clôture** (n° 10) — chaque décision exigeait un commentaire
  libre, et les analystes retapaient les mêmes phrases. Bibliothèque
  paramétrable (réglages, un motif par ligne), proposée au moment de la
  décision : le clic REMPLIT le commentaire, qui reste éditable — une
  bibliothèque, pas un carcan. La liste vide est un choix valable (« pas de
  suggestions »), distinct de « non fourni », qui laisse le défaut.
* **Récemment consultés** (n° 13) — les dix derniers dossiers ouverts vivent
  dans la palette Ctrl+K, avant la première frappe.
* **Re-cribler un client** (n° 16) — le geste manquait : les seuls chemins
  étaient la mise à jour de liste ou le lookback entier. Un client corrigé se
  recrible maintenant, avec les mêmes garanties que le temps réel.
"""
import os
import re
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import AppSetting, ClientEntity, Snapshot, get_db
from fiskr.settings import (DEFAULT_ALERT_CLOSE_REASONS, SETTING_ALERT_CLOSE_REASONS,
                            alert_close_reasons)

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "fiskr", "static")


def _lire(nom):
    with open(os.path.join(STATIC, nom), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def app_js():
    return _lire("app.js")


def _connecte(role="admin"):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": f"c_{role}", "full_name": role, "role": role, "roles": [role],
    }


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    db = next(get_db())
    try:
        db.query(AppSetting).filter(
            AppSetting.key == SETTING_ALERT_CLOSE_REASONS).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------- motifs de clôture

def test_la_bibliotheque_par_defaut_existe_et_reste_editable():
    assert len(DEFAULT_ALERT_CLOSE_REASONS) >= 3
    assert all(len(m) <= 300 for m in DEFAULT_ALERT_CLOSE_REASONS)


def test_les_motifs_se_reglent_et_se_lisent(client):
    _connecte("admin")
    reponse = client.put("/api/settings/ingestion", json={
        "alert_close_reasons": ["  Motif A  ", "", "Motif B"]})
    assert reponse.status_code == 200, reponse.text
    lu = client.get("/api/settings/ingestion").json()
    assert lu["alert_close_reasons"] == ["Motif A", "Motif B"]


def test_un_analyste_lit_les_motifs_sans_etre_admin(client):
    """Les motifs servent à la décision, pas au réglage : un analyste doit
    pouvoir les lire — c'est l'écriture qui reste administrateur."""
    _connecte("user")
    lu = client.get("/api/settings/ingestion")
    assert lu.status_code == 200
    assert lu.json()["alert_close_reasons"] == list(DEFAULT_ALERT_CLOSE_REASONS)


def test_la_liste_vide_est_un_choix_pas_un_oubli(client):
    """[] = « pas de suggestions ». Si l'écriture de [] laissait le défaut en
    place, l'admin ne pourrait jamais les retirer."""
    _connecte("admin")
    client.put("/api/settings/ingestion", json={"alert_close_reasons": []})
    assert client.get("/api/settings/ingestion").json()["alert_close_reasons"] == []


def test_les_bornes_refusent_l_excessif(client):
    _connecte("admin")
    trop = client.put("/api/settings/ingestion", json={"alert_close_reasons": ["m"] * 51})
    assert trop.status_code == 400
    long = client.put("/api/settings/ingestion", json={"alert_close_reasons": ["x" * 301]})
    assert long.status_code == 400


def test_helper_ne_leve_jamais_sur_une_valeur_corrompue():
    class _Db:
        pass
    # une valeur non-liste retombe sur le defaut, sans erreur
    db = next(get_db())
    try:
        from fiskr.settings import set_setting
        set_setting(db, SETTING_ALERT_CLOSE_REASONS, {"pas": "une liste"})
        assert alert_close_reasons(db) == list(DEFAULT_ALERT_CLOSE_REASONS)
    finally:
        db.query(AppSetting).filter(
            AppSetting.key == SETTING_ALERT_CLOSE_REASONS).delete(synchronize_session=False)
        db.commit()
        db.close()


def test_le_dialogue_propose_sans_imposer(app_js):
    """Le clic REMPLIT le champ ; le texte reste éditable ; un champ déjà
    entamé reçoit le motif À LA SUITE, jamais à la place."""
    debut = app_js.index("suggestions && suggestions.length")
    bloc = app_js[debut:debut + 900]
    assert "inputEl.value.trim() ? inputEl.value.trim()" in bloc
    assert "inputEl.focus()" in bloc


def test_les_motifs_se_chargent_sans_ouvrir_les_reglages(app_js):
    """Un analyste n'ouvre jamais l'écran Paramètres : les motifs se chargent
    à la première décision, silencieusement."""
    debut = app_js.index("async function chargerMotifsDeCloture()")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert '"/api/settings/ingestion", { silent: true }' in corps


# ------------------------------------------------------- re-criblage unitaire

@pytest.fixture
def client_en_production():
    db = next(get_db())
    snap_id = f"cb-{uuid.uuid4().hex[:8]}"
    ref = f"RECRIB-{uuid.uuid4().hex[:6]}"
    db.add(Snapshot(snapshot_id=snap_id, file_type="CLIENT_BASE", file_name="c.csv",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY",
                    uploaded_at=datetime.utcnow()))
    db.add(ClientEntity(snapshot_id=snap_id, client_id=ref, client_type="PP",
                        client_first_name="Jean", client_last_name="Testeur",
                        entity_checksum=f"ck-{ref}"))
    db.commit()
    yield ref
    db.query(ClientEntity).filter(ClientEntity.snapshot_id == snap_id).delete(synchronize_session=False)
    db.query(Snapshot).filter(Snapshot.snapshot_id == snap_id).delete(synchronize_session=False)
    db.commit()
    db.close()


def test_un_client_du_referentiel_se_recrible_a_la_demande(client, client_en_production):
    _connecte("user")
    reponse = client.post(f"/api/clients/{client_en_production}/screen")
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    assert "status" in corps or "best_match" in corps


def test_un_client_inconnu_rend_un_404_clair(client):
    _connecte("user")
    reponse = client.post("/api/clients/INEXISTANT-999/screen")
    assert reponse.status_code == 404
    assert "référentiel" in reponse.json()["detail"]


def test_le_bouton_existe_dans_la_vue_client(app_js):
    assert "onclick=\"recriblerClient('${escapeHtml(clientId)}')\"" in app_js, (
        "l'identifiant doit passer en guillemets SIMPLES échappés : "
        "JSON.stringify produirait des guillemets doubles DANS un attribut à "
        "guillemets doubles — HTML cassé à l'exécution")
    assert '`/api/clients/${encodeURIComponent(clientId)}/screen`' in app_js


# ------------------------------------------------------- échéance qui approche

def test_l_approche_de_l_echeance_s_affiche_avant_le_retard(app_js):
    debut = app_js.index("function alertPriorityBadge(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "12 * 3600 * 1000" in corps
    assert "ÉCHÉANCE" in corps
    assert 'startsWith("CLOSED")' in corps, "une alerte clôturée n'a plus d'échéance à tenir"


# ------------------------------------------------------- récemment consultés

def test_les_recents_vivent_dans_la_palette_avant_la_frappe(app_js):
    debut = app_js.index("async function runPaletteSearch(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "_recentsMemorises()" in corps
    assert '"Récents"' in corps


def test_ouvrir_un_dossier_le_memorise(app_js):
    assert '_memoriserRecent("alerte", a.id' in app_js
    assert '_memoriserRecent("client", clientId' in app_js


def test_la_memoire_des_recents_est_bornee_et_dedupliquee(app_js):
    debut = app_js.index("function _memoriserRecent(")
    corps = app_js[debut:app_js.index("\n}", debut)]
    assert "_RECENTS_MAX" in corps
    assert "filter(r => !(r.type === type && r.id === id))" in corps, (
        "rouvrir le même dossier dix fois ne doit pas remplir la liste de doublons")

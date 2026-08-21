"""
Le scénario complet, de bout en bout : « Mohammed Ali » contre « Vladimir Tyurin ».

C'est la demande à laquelle la rareté des noms répond, vérifiée sur la chaîne
entière plutôt que module par module : cache → table de rareté → criblage →
lignes d'audit → alertes → règle anti-faux positifs → clôture tracée.

Deux clients, un seul paramétrage :

* **Mohammed Ali**, sans pays ni date de naissance, face à quarante fiches PEP
  homonymes. Les quarante correspondances existent et sont écrites — l'exigence
  d'audit — puis clôturées par la règle, avec son nom en clair. Aucune n'est
  supprimée en silence.
* **Vladimir Tyurin**, même absence de contexte, face à une fiche de sanctions.
  « TYURIN » n'est porté que par une fiche : l'alerte **reste ouverte**. Un seul
  mot rare partagé suffit.

C'est exactement l'arbitrage demandé : agressif hors sanctions, où manquer un
PEP ne se constate pas de la même façon ; intact sur les sanctions, où manquer
un gel d'avoirs est constatable à l'audit et sanctionnable.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api_module
from fiskr import rarete
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AuditTrail, FpRule
from fiskr.fprules import rule_templates

TAG = uuid.uuid4().hex[:6].upper()
NB_HOMONYMES = 40


def _fiche(n, nom, list_type):
    return {"id": 80000 + n, "entity_id": f"MA-{TAG}-{n:03d}", "entity_type": "I",
            "primary_name": nom,
            "aliases": {"high_priority": [], "low_priority": []},
            "dates_of_birth": [], "date_of_death": None, "is_deceased": False,
            "gender": "U",
            "countries": {"citizenship": [], "residence": [], "birth_country": [],
                          "jurisdiction_country": []},
            "_list_type": list_type}


@pytest.fixture()
def univers(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "ma", "full_name": "ma", "role": "admin",
        "roles": ["admin"]}

    fiches = [_fiche(i, "MOHAMMED ALI", "WATCHLIST_PEP") for i in range(NB_HOMONYMES)]
    fiches.append(_fiche(999, "VLADIMIR TYURIN", "WATCHLIST_OFAC"))

    class _IndexUnique(dict):
        def get(self, cle, defaut=None):
            return fiches

    monkeypatch.setattr(api_module, "watchlist_index", _IndexUnique())
    monkeypatch.setattr(api_module, "watchlist_store", fiches)
    monkeypatch.setattr(api_module, "watchlist_hash", f"h-{TAG}")
    monkeypatch.setattr(api_module, "_ensure_watchlist_cache", lambda db: None)

    # La table de rareté que le chargement du cache poserait sur cet univers.
    precedente = rarete.table_courante()
    rarete.installer(rarete.construire(fiches, "SCREENING", f"h-{TAG}"))

    db = next(get_db())
    yield db, TestClient(app), fiches

    rarete.installer(precedente)
    ids = [f["entity_id"] for f in fiches]
    alertes = db.query(Alert).filter(Alert.watchlist_entity_id.in_(ids)).all()
    if alertes:
        db.query(AlertEvent).filter(
            AlertEvent.alert_id.in_([a.id for a in alertes])).delete(synchronize_session=False)
    db.query(Alert).filter(Alert.watchlist_entity_id.in_(ids)).delete(synchronize_session=False)
    db.query(AuditTrail).filter(AuditTrail.watchlist_id.in_(ids)).delete(synchronize_session=False)
    db.query(FpRule).filter(FpRule.name.like(f"%{TAG}%")).delete(synchronize_session=False)
    db.commit()
    db.close()
    app.dependency_overrides.pop(get_current_user, None)


def _pose_la_regle_de_rarete(db):
    modele = next(m for m in rule_templates("SCREENING")
                  if m["key"] == "common_name_tokens")
    regle = FpRule(name=f"Rareté {TAG}", channel="SCREENING", code=modele["code"],
                   status="ACTIVE", enabled=True, version=1, run_order=1,
                   created_by="test", perimeters=modele["perimeters"])
    db.add(regle)
    db.commit()
    return regle


def _crible(client, prenom, nom):
    reponse = client.post("/api/screen", json={
        "client_id": f"C-{TAG}-{nom}", "client_type": "PP",
        "client_first_name": prenom, "client_last_name": nom})
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


# ------------------ LA MESURE QUI FONDE LE SCÉNARIO ------------------

def test_l_univers_rend_bien_un_nom_repandu_et_un_nom_rare(univers):
    table = rarete.table_courante()
    repandu = table.profil("Mohammed Ali", "MOHAMMED ALI")
    rare = table.profil("Vladimir Tyurin", "VLADIMIR TYURIN")
    assert repandu["nom_repandu"] is True
    assert repandu["df_min"] == NB_HOMONYMES
    assert rare["nom_repandu"] is False
    assert rare["df_max"] < table.seuil_repandu


# ------------------ SANS RÈGLE : TOUT EXISTE ET TOUT RESTE ------------------

def test_sans_regle_les_quarante_correspondances_restent_ouvertes(univers):
    db, client, fiches = univers
    corps = _crible(client, "MOHAMMED", "ALI")
    assert corps["hits"]["hits"] == NB_HOMONYMES
    assert corps["hits"]["opened"] == NB_HOMONYMES
    assert corps["hits"]["closed_by_rule"] == 0


# ------------------ AVEC LA RÈGLE : CRÉÉES PUIS CLÔTURÉES ------------------

def test_les_homonymes_sont_crees_puis_clotures_par_la_regle(univers):
    db, client, fiches = univers
    regle = _pose_la_regle_de_rarete(db)

    corps = _crible(client, "MOHAMMED", "ALI")
    assert corps["hits"]["hits"] == NB_HOMONYMES
    assert corps["hits"]["closed_by_rule"] == NB_HOMONYMES
    assert corps["hits"]["opened"] == 0

    ids = [f["entity_id"] for f in fiches if f["primary_name"] == "MOHAMMED ALI"]
    alertes = db.query(Alert).filter(Alert.watchlist_entity_id.in_(ids)).all()
    assert len(alertes) == NB_HOMONYMES, "des correspondances ont disparu"
    assert all(a.status == "CLOSED_BY_RULE" for a in alertes)

    # Chacune garde sa ligne d'audit : la piste reste complète.
    lignes = db.query(AuditTrail).filter(AuditTrail.watchlist_id.in_(ids)).all()
    assert len(lignes) == NB_HOMONYMES


def test_la_regle_qui_a_cloture_est_nommee_en_clair(univers):
    db, client, fiches = univers
    regle = _pose_la_regle_de_rarete(db)
    _crible(client, "MOHAMMED", "ALI")

    ids = [f["entity_id"] for f in fiches if f["primary_name"] == "MOHAMMED ALI"]
    alerte = db.query(Alert).filter(Alert.watchlist_entity_id.in_(ids)).first()
    evenements = db.query(AlertEvent).filter(AlertEvent.alert_id == alerte.id).all()
    detail = " ".join(e.detail or "" for e in evenements)
    assert regle.name in detail, "un contrôleur ne peut pas savoir QUI a clôturé"


def test_le_compte_de_la_regle_reflete_ce_qu_elle_a_tranche(univers):
    db, client, _ = univers
    regle = _pose_la_regle_de_rarete(db)
    _crible(client, "MOHAMMED", "ALI")
    db.refresh(regle)
    assert regle.hit_count == NB_HOMONYMES


# ------------------ LE NOM RARE, LUI, RESTE OUVERT ------------------

def test_le_nom_rare_reste_ouvert_sous_la_meme_regle(univers):
    """Un seul mot rare partagé suffit à conserver l'alerte — et la fiche est
    de surcroît sur le périmètre SANCTION, hors de portée de la règle."""
    db, client, fiches = univers
    _pose_la_regle_de_rarete(db)

    corps = _crible(client, "VLADIMIR", "TYURIN")
    identifiant = next(f["entity_id"] for f in fiches
                       if f["primary_name"] == "VLADIMIR TYURIN")
    alerte = db.query(Alert).filter(
        Alert.watchlist_entity_id == identifiant).one()
    assert alerte.status == "OPEN"
    assert corps["hits"]["closed_by_rule"] == 0


# ------------------ LA RARETÉ EST ÉCRITE, PAS SEULEMENT AFFICHÉE ------------------

def test_la_rarete_est_dans_l_arbre_de_decision_de_chaque_ligne(univers):
    """Une rareté se relit des mois plus tard, en contrôle, avec le corpus qui
    l'a produite : elle doit être DANS le journal immuable."""
    db, client, fiches = univers
    _crible(client, "MOHAMMED", "ALI")
    ids = [f["entity_id"] for f in fiches if f["primary_name"] == "MOHAMMED ALI"]
    lignes = db.query(AuditTrail).filter(AuditTrail.watchlist_id.in_(ids)).all()
    assert lignes
    for ligne in lignes:
        arbre = ligne.decision_tree or {}
        assert arbre.get("name_rarity", {}).get("nom_repandu") is True
        assert arbre["name_rarity"]["corpus"] == NB_HOMONYMES + 1


def test_la_reponse_rend_la_rarete_en_entier_sur_la_meilleure(univers):
    db, client, _ = univers
    corps = _crible(client, "MOHAMMED", "ALI")
    rarete_meilleure = corps["best_match"]["name_rarity"]
    assert {t["token"] for t in rarete_meilleure["tokens"]} == {"MOHAMMED", "ALI"}
    # …et pas sur chaque ligne de la liste bornée (la réponse ne doit pas
    # grossir avec le périmètre).
    assert all("name_rarity" not in m for m in corps["all_matches"])

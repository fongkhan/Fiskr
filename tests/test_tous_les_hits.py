"""
Criblage : TOUTES les correspondances au-dessus du seuil sont conservées.

Le moteur ne persistait que la **meilleure**. Mesuré sur la production via
`GET /api/screen/preview` (lecture seule, même moteur) :

| Profil criblé | Candidats | Hits ≥ seuil | Traces écrites |
|---|---:|---:|---:|
| Mohammed Ali, sans pays | 17 649 | 2 976 | **1** |
| Ivan Ivanov, sans pays | 28 940 | 538 | **1** |
| Ivan Ivanov + RU | 1 223 | 453 | **1** |

Et les douze meilleurs de « Mohammed Ali » sont tous à **100,00** : des
homonymes réels (« ALI MUHAMMED », « MOHAMMAD ALI »…), pas du bruit de score.
2 975 correspondances réglementaires disparaissaient sans laisser d'écrit —
sur les quatre canaux : criblage unitaire, batch, re-criblage post-delta et
filtrage transactionnel.

Elles sont désormais toutes écrites. Celles qu'une règle anti-faux positifs
tranche sont **créées puis clôturées** `CLOSED_BY_RULE`, avec le nom et la
version de la règle en clair — jamais supprimées en silence.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.engine import Engine

import fiskr.api as api_module
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import (get_db, Alert, AlertEvent, AuditTrail, FpRule,
                            WhitelistPair)

TAG = uuid.uuid4().hex[:6].upper()
NB_HOMONYMES = 12


def _fiche(n: int) -> dict:
    return {"id": 70000 + n, "entity_id": f"TH-{TAG}-{n:03d}", "entity_type": "I",
            "primary_name": "MOHAMMED ALI",
            "aliases": {"high_priority": [], "low_priority": []},
            "dates_of_birth": [], "date_of_death": None, "is_deceased": False,
            "gender": "U",
            "countries": {"citizenship": [], "residence": [], "birth_country": [],
                          "jurisdiction_country": []},
            "_list_type": "WATCHLIST_OFAC"}


@pytest.fixture()
def contexte(monkeypatch):
    """Douze fiches listées portant le MÊME nom que le client — le cas réel
    de l'homonymie sur un nom très courant."""
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "th", "full_name": "th", "role": "admin",
        "roles": ["admin"]}
    fiches = [_fiche(i) for i in range(NB_HOMONYMES)]

    class _IndexUnique(dict):
        def get(self, cle, defaut=None):
            return fiches

    monkeypatch.setattr(api_module, "watchlist_index", _IndexUnique())
    monkeypatch.setattr(api_module, "watchlist_store", fiches)
    monkeypatch.setattr(api_module, "watchlist_hash", f"h-{TAG}")
    monkeypatch.setattr(api_module, "_ensure_watchlist_cache", lambda db: None)

    db = next(get_db())
    yield db, TestClient(app), fiches

    ids = [f["entity_id"] for f in fiches]
    alertes = db.query(Alert).filter(Alert.watchlist_entity_id.in_(ids)).all()
    if alertes:
        db.query(AlertEvent).filter(
            AlertEvent.alert_id.in_([a.id for a in alertes])).delete(
            synchronize_session=False)
    db.query(Alert).filter(Alert.watchlist_entity_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(AuditTrail).filter(AuditTrail.watchlist_id.in_(ids)).delete(
        synchronize_session=False)
    db.query(WhitelistPair).filter(
        WhitelistPair.watchlist_entity_id.in_(ids)).delete(synchronize_session=False)
    db.query(FpRule).filter(FpRule.name.like(f"%{TAG}%")).delete(
        synchronize_session=False)
    db.commit()
    db.close()
    app.dependency_overrides.pop(get_current_user, None)


def _crible(client, client_id=None):
    reponse = client.post("/api/screen", json={
        "client_id": client_id or f"C-{TAG}", "client_type": "PP",
        "client_first_name": "MOHAMMED", "client_last_name": "ALI"})
    assert reponse.status_code == 200, reponse.text
    return reponse.json()


def _ids(fiches):
    return [f["entity_id"] for f in fiches]


# ------------------------- toutes les traces existent -------------------------

def test_chaque_correspondance_laisse_une_ligne_d_audit(contexte):
    db, client, fiches = contexte
    corps = _crible(client)
    assert corps["hits"]["hits"] == NB_HOMONYMES
    lignes = db.query(AuditTrail).filter(AuditTrail.watchlist_id.in_(_ids(fiches))).all()
    assert len(lignes) == NB_HOMONYMES
    assert {l.watchlist_id for l in lignes} == set(_ids(fiches))
    assert all(l.status == "ALERT" for l in lignes)


def test_chaque_correspondance_ouvre_une_alerte(contexte):
    db, client, fiches = contexte
    corps = _crible(client)
    assert corps["hits"]["opened"] == NB_HOMONYMES
    alertes = db.query(Alert).filter(Alert.watchlist_entity_id.in_(_ids(fiches))).all()
    assert len(alertes) == NB_HOMONYMES
    assert all(a.status == "OPEN" for a in alertes)


def test_chaque_alerte_pointe_sa_propre_ligne_d_audit(contexte):
    """Une alerte qui pointerait la ligne d'audit d'une autre fiche rendrait
    le dossier illisible pour un contrôleur."""
    db, client, fiches = contexte
    _crible(client)
    alertes = db.query(Alert).filter(Alert.watchlist_entity_id.in_(_ids(fiches))).all()
    assert len({a.audit_id for a in alertes}) == NB_HOMONYMES
    for alerte in alertes:
        ligne = db.query(AuditTrail).filter(AuditTrail.id == alerte.audit_id).one()
        assert ligne.watchlist_id == alerte.watchlist_entity_id


def test_le_meilleur_reste_celui_que_la_reponse_designe(contexte):
    """Contrat historique : `alert_id` et `audit_trail_id` sont ceux de la
    meilleure correspondance."""
    db, client, fiches = contexte
    corps = _crible(client)
    assert corps["best_match"] is not None
    alerte = db.query(Alert).filter(Alert.id == corps["alert_id"]).one()
    assert alerte.watchlist_entity_id == \
        corps["best_match"]["watchlist_entity"]["entity_id"]
    assert alerte.audit_id == corps["audit_trail_id"]


# --------------------- une règle clôture, en clair ---------------------

def _pose_regle(db, code, nom=None):
    regle = FpRule(name=nom or f"Corroboration {TAG}", channel="SCREENING",
                   code=code, status="ACTIVE", enabled=True, version=1,
                   run_order=1, created_by="test")
    db.add(regle)
    db.commit()
    return regle


def test_les_hits_tranches_par_une_regle_sont_crees_puis_clotures(contexte):
    """L'exigence : jamais de suppression silencieuse. Le hit existe, il est
    clôturé, et la règle qui l'a clôturé est nommée."""
    db, client, fiches = contexte
    regle = _pose_regle(db, "def rule(ctx):\n    return ctx['corroboration']['name_only']\n")

    corps = _crible(client)
    assert corps["hits"]["hits"] == NB_HOMONYMES
    assert corps["hits"]["closed_by_rule"] == NB_HOMONYMES
    assert corps["hits"]["opened"] == 0

    alertes = db.query(Alert).filter(Alert.watchlist_entity_id.in_(_ids(fiches))).all()
    assert len(alertes) == NB_HOMONYMES, "les hits doivent EXISTER, pas disparaître"
    for alerte in alertes:
        assert alerte.status == "CLOSED_BY_RULE"
        assert alerte.decided_by == "fp-rule"
        assert regle.name in (alerte.decision_comment or "")
        assert f"v{regle.version}" in (alerte.decision_comment or "")

    evenements = db.query(AlertEvent).filter(
        AlertEvent.alert_id.in_([a.id for a in alertes])).all()
    creations = [e for e in evenements if e.action == "CREATED"]
    suppressions = [e for e in evenements if e.action == "RULE_SUPPRESSED"]
    assert len(creations) == NB_HOMONYMES
    assert len(suppressions) == NB_HOMONYMES
    assert all(regle.name in (e.detail or "") for e in suppressions)


def test_la_ligne_d_audit_porte_la_regle_appliquee(contexte):
    db, client, fiches = contexte
    _pose_regle(db, "def rule(ctx):\n    return True\n")
    _crible(client)
    lignes = db.query(AuditTrail).filter(AuditTrail.watchlist_id.in_(_ids(fiches))).all()
    assert lignes
    for ligne in lignes:
        assert (ligne.decision_tree or {}).get("fp_rule_applied"), ligne.decision_tree


# --------------------- ce que la règle peut lire ---------------------

def test_la_regle_voit_la_volumetrie_et_le_rang(contexte):
    """Sans ça, une règle ne peut pas distinguer « une correspondance isolée »
    de « 2 976 homonymes »."""
    db, client, fiches = contexte
    _pose_regle(db, "def rule(ctx):\n"
                    "    return ctx['hits_count'] >= 10 and ctx['hit_rank'] > 3\n")
    corps = _crible(client)
    assert corps["hits"]["closed_by_rule"] == NB_HOMONYMES - 3
    assert corps["hits"]["opened"] == 3


def test_la_regle_voit_l_absence_d_element_corroborant(contexte):
    """Le cœur du sujet : le score est à 100 parce que les noms sont
    identiques ; ce qui manque, c'est l'identification."""
    db, client, fiches = contexte
    vus = {}
    _pose_regle(db, "def rule(ctx):\n"
                    "    c = ctx['corroboration']\n"
                    "    return c['name_only'] and not c['has_dob'] and not c['has_country']\n")
    corps = _crible(client)
    assert corps["hits"]["closed_by_rule"] == NB_HOMONYMES

    # Le même profil AVEC une date de naissance n'est plus « nom seul »
    reponse = client.post("/api/screen", json={
        "client_id": f"C-{TAG}-dob", "client_type": "PP",
        "client_first_name": "MOHAMMED", "client_last_name": "ALI",
        "client_dob": "1980-05-04"})
    assert reponse.status_code == 200, reponse.text
    assert reponse.json()["hits"]["closed_by_rule"] == 0


def test_le_contexte_de_corroboration_est_complet():
    from fiskr.fprules import corroboration_context
    ctx = corroboration_context(
        {"client_dob": "1980-01-01", "client_countries": {"nationality": ["RU"]}},
        {"adjustments": {"dob": {"score": 15.0}, "geography": {"score": 5.0},
                         "gender": {"score": 0.0}}})
    assert ctx["has_dob"] is True and ctx["has_country"] is True
    assert ctx["name_only"] is False
    assert ctx["corroborated"] is True
    vide = corroboration_context({}, {})
    assert vide["name_only"] is True and vide["corroborated"] is False


# ----------------------------- liste blanche -----------------------------

def test_une_paire_en_liste_blanche_est_journalisee_sans_alerte(contexte):
    db, client, fiches = contexte
    cible = fiches[0]["entity_id"]
    db.add(WhitelistPair(client_id=f"C-{TAG}", watchlist_entity_id=cible,
                         created_by="test", justification="test"))
    db.commit()

    corps = _crible(client)
    assert corps["hits"]["whitelisted"] == 1
    assert corps["hits"]["opened"] == NB_HOMONYMES - 1
    ligne = db.query(AuditTrail).filter(AuditTrail.watchlist_id == cible).one()
    assert ligne.status == "WHITELISTED"
    assert db.query(Alert).filter(Alert.watchlist_entity_id == cible).count() == 0


# ----------------------------- volumétrie -----------------------------

def test_aucune_LECTURE_ne_croit_avec_le_nombre_de_hits(contexte, monkeypatch):
    """Écrire N hits demande forcément N insertions — c'est la fonctionnalité.
    Ce qui ne doit PAS croître, ce sont les LECTURES : liste blanche, dédup
    d'alerte, règles anti-FP, réglage SLA. Une lecture par hit aurait remis un
    N+1 sur le chemin le plus chaud de l'application, 2 976 fois.

    Le piège rencontré ici : `commit()` expire les objets de la session, donc
    relire `ligne.id` après coup déclenchait un SELECT par ligne — le N+1
    revenait par la porte de derrière, après le regroupement des écritures."""
    db, client, fiches = contexte

    def _compte(perimetre, cid):
        class _Index(dict):
            def get(self, cle, defaut=None):
                return perimetre
        monkeypatch.setattr(api_module, "watchlist_index", _Index())
        lectures = []

        def _ecoute(conn, cursor, statement, params, context, executemany):
            if " ".join(statement.split()).lower().startswith("select"):
                lectures.append(1)

        event.listen(Engine, "before_cursor_execute", _ecoute)
        try:
            _crible(client, client_id=cid)
        finally:
            event.remove(Engine, "before_cursor_execute", _ecoute)
        return len(lectures)

    # Comparaison au-dessus du plafond de notification (10) : en dessous, le
    # nombre de notifications individuelles fait bouger le compte pour une
    # raison qui n'a rien d'un N+1 de criblage.
    avec_11 = _compte(fiches[:11], f"C-{TAG}-a")
    avec_12 = _compte(fiches, f"C-{TAG}-b")
    assert avec_12 <= avec_11, (
        f"{avec_11} lectures pour 11 hits, {avec_12} pour 12 : "
        "une lecture croît encore avec le nombre de correspondances")


def test_un_re_criblage_ne_recree_pas_les_alertes(contexte):
    db, client, fiches = contexte
    premier = _crible(client)
    assert premier["hits"]["opened"] == NB_HOMONYMES
    second = _crible(client)
    assert second["hits"]["opened"] == 0
    assert second["hits"]["redetected"] == NB_HOMONYMES
    assert db.query(Alert).filter(
        Alert.watchlist_entity_id.in_(_ids(fiches))).count() == NB_HOMONYMES


def test_une_alerte_deja_tranchee_par_regle_ne_reecrit_pas_un_evenement(contexte):
    """Le re-criblage repasse toute la base après chaque mise en production.
    Une ligne d'événement par alerte close-par-règle et par passage ferait
    grossir le journal sans rien apprendre — la ligne d'audit du criblage,
    elle, est bien écrite à chaque fois."""
    db, client, fiches = contexte
    _pose_regle(db, "def rule(ctx):\n    return True\n")
    _crible(client)
    alertes = db.query(Alert).filter(Alert.watchlist_entity_id.in_(_ids(fiches))).all()
    avant = db.query(AlertEvent).filter(
        AlertEvent.alert_id.in_([a.id for a in alertes])).count()

    _crible(client)
    apres = db.query(AlertEvent).filter(
        AlertEvent.alert_id.in_([a.id for a in alertes])).count()
    assert apres == avant, "un événement de re-détection par passage"
    # ... mais la trace d'audit du second criblage existe bien
    assert db.query(AuditTrail).filter(
        AuditTrail.watchlist_id.in_(_ids(fiches))).count() == NB_HOMONYMES * 2


def test_le_journal_est_valide_meme_sans_alerte_a_ouvrir(contexte, monkeypatch):
    """Piège rencontré en écrivant ce lot : le commit passait par la création
    des alertes. Quand il n'y en a aucune à ouvrir — tout en liste blanche, ou
    aucune correspondance au-dessus du seuil — les lignes d'audit restaient
    écrites mais jamais validées, donc perdues à la fermeture de la session.
    Un criblage sans alerte n'aurait plus laissé AUCUNE trace."""
    db, client, fiches = contexte
    for fiche in fiches:
        db.add(WhitelistPair(client_id=f"C-{TAG}-wl", justification="test",
                             watchlist_entity_id=fiche["entity_id"],
                             created_by="test"))
    db.commit()

    corps = _crible(client, client_id=f"C-{TAG}-wl")
    assert corps["hits"]["whitelisted"] == NB_HOMONYMES
    assert corps["hits"]["opened"] == 0
    assert corps["audit_trail_id"] is not None

    # Relu dans une AUTRE session : c'est ce que voit un contrôleur
    autre = next(get_db())
    try:
        lignes = autre.query(AuditTrail).filter(
            AuditTrail.watchlist_id.in_(_ids(fiches)),
            AuditTrail.client_id == f"C-{TAG}-wl").all()
        assert len(lignes) == NB_HOMONYMES
        assert all(l.status == "WHITELISTED" for l in lignes)
    finally:
        autre.query(AuditTrail).filter(
            AuditTrail.client_id == f"C-{TAG}-wl").delete(synchronize_session=False)
        autre.commit()
        autre.close()


def test_un_criblage_sans_correspondance_laisse_toujours_sa_trace(contexte, monkeypatch):
    """L'autre bout du même piège : sous le seuil, le journal doit porter
    « ce client a bien été criblé, meilleur score X »."""
    db, client, fiches = contexte

    class _IndexVide(dict):
        def get(self, cle, defaut=None):
            return [{"id": 1, "entity_id": f"TH-{TAG}-zzz", "entity_type": "I",
                     "primary_name": "ZZZZZZ QQQQQQ",
                     "aliases": {"high_priority": [], "low_priority": []},
                     "dates_of_birth": [], "gender": "U",
                     "countries": {"citizenship": [], "residence": [],
                                   "birth_country": [], "jurisdiction_country": []},
                     "_list_type": "WATCHLIST_OFAC"}]

    monkeypatch.setattr(api_module, "watchlist_index", _IndexVide())
    corps = _crible(client, client_id=f"C-{TAG}-nomatch")
    assert corps["hits"]["hits"] == 0
    assert corps["audit_trail_id"] is not None

    autre = next(get_db())
    try:
        ligne = autre.query(AuditTrail).filter(
            AuditTrail.id == corps["audit_trail_id"]).one()
        assert ligne.status != "ALERT"
    finally:
        autre.query(AuditTrail).filter(
            AuditTrail.client_id == f"C-{TAG}-nomatch").delete(synchronize_session=False)
        autre.commit()
        autre.close()


# --------------------- modèles de règles proposés ---------------------

def test_les_modeles_de_regles_compilent_et_disent_ce_qu_ils_coutent():
    """Un modèle qui ne compile pas serait proposé puis refusé à l'installation.
    Et un arbitrage de conformité sans son coût écrit noir sur blanc n'est pas
    proposable."""
    from fiskr.fprules import RULE_TEMPLATES, compile_rule, FP_RULE_CHANNELS
    assert RULE_TEMPLATES
    for modele in RULE_TEMPLATES:
        assert modele["channel"] in FP_RULE_CHANNELS, modele["key"]
        assert compile_rule(modele["code"]) is not None
        assert len(modele["loss"]) > 60, f"{modele['key']} : perte non explicitée"
        assert modele["summary"] and modele["name"]


def test_aucun_modele_ne_cloture_un_hard_match():
    """Un identifiant officiel identique est une identification, pas une
    homonymie : aucun modèle proposé ne doit le clôturer."""
    from fiskr.fprules import RULE_TEMPLATES, run_rule
    ctx = {"hits_count": 5000, "hit_rank": 900, "hard_match": True,
           "final_score": 100.0, "base_score": 100.0,
           "corroboration": {"name_only": True, "corroborated": False,
                             "has_dob": False, "has_country": False,
                             "has_identity_document": False,
                             "dob_score": 0.0, "gender_score": 0.0,
                             "geography_score": 0.0}}
    for modele in RULE_TEMPLATES:
        resultat, erreur = run_rule(modele["code"], ctx)
        assert erreur is None, f"{modele['key']} : {erreur}"
        assert resultat is False, f"{modele['key']} clôture un hard match"


def test_le_modele_de_volumetrie_ne_touche_pas_les_petits_criblages():
    """Une correspondance isolée reste à traiter par un analyste, quel que
    soit le contexte manquant."""
    from fiskr.fprules import RULE_TEMPLATES, run_rule
    modele = next(m for m in RULE_TEMPLATES if m["key"] == "name_only_volume")
    sans_contexte = {"name_only": True, "corroborated": False, "has_dob": False,
                     "has_country": False, "has_identity_document": False,
                     "dob_score": 0.0, "gender_score": 0.0, "geography_score": 0.0}
    isole = {"hits_count": 2, "hit_rank": 1, "hard_match": False,
             "corroboration": sans_contexte}
    masse = {"hits_count": 2976, "hit_rank": 900, "hard_match": False,
             "corroboration": sans_contexte}
    assert run_rule(modele["code"], isole)[0] is False
    assert run_rule(modele["code"], masse)[0] is True


def test_les_modeles_sont_servis_par_l_api(contexte):
    _, client, _ = contexte
    reponse = client.get("/api/fprules/templates?channel=SCREENING")
    assert reponse.status_code == 200, reponse.text
    items = reponse.json()["items"]
    assert items and all(m["channel"] == "SCREENING" for m in items)
    assert all({"key", "name", "summary", "loss", "code"} <= set(m) for m in items)


def test_aucun_modele_n_est_actif_par_defaut(contexte):
    """Ce sont des arbitrages de conformité : ils s'installent sur décision,
    ils ne s'appliquent pas parce qu'ils existent."""
    db, _, _ = contexte
    from fiskr.fprules import RULE_TEMPLATES, active_rules
    noms = {m["name"] for m in RULE_TEMPLATES}
    for canal in ("SCREENING", "FILTERING"):
        assert not [r for r in active_rules(db, canal) if r.name in noms]

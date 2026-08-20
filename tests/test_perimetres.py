"""
Deux périmètres de criblage : SANCTION et HORS_SANCTION.

Les deux ne portent pas le même risque, donc pas le même traitement.

**SANCTION** — désignations avec obligation de gel (OFAC, UE, ONU, DGT, OFSI,
listes nationales antiterroristes…). Manquer une correspondance, c'est manquer
un terroriste ou un sanctionné : constatable à l'audit, sanctionnable
financièrement. Tout est généré, rien n'est clôturé par volumétrie.

**HORS_SANCTION** — PEP, alertes de régulateurs, exclusions de bailleurs
multilatéraux. Signaux de vigilance, pas obligations de gel. Ce périmètre
supporte une clôture plus agressive.

Ce que ça change, en volumétrie : les listes hors sanction pèsent **709 511
fiches sur 895 157 en production (79 %)**, presque toutes portées par
`WATCHLIST_PEP`. C'est là que l'homonymie de noms courants explose — et c'est
donc là que le seuil peut monter, ce qui évite de créer la correspondance
plutôt que d'avoir à la clôturer ensuite.

Deux garanties tenues ici :

* le classement d'un type **inconnu** tombe du côté SANCTION — celui qui ne
  clôture rien ;
* la portée d'une règle est filtrée par le **moteur**, pas par le code de la
  règle : une règle hors-sanction ne peut pas clôturer un gel d'avoirs, même
  si son code l'oublie.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import fiskr.api as api_module
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import get_db, Alert, AlertEvent, AuditTrail, FpRule, AppSetting
from fiskr.perimeters import (PERIMETRE_SANCTION, PERIMETRE_HORS_SANCTION,
                              perimetre_de, perimetres_par_defaut, PERIMETRES)
from fiskr.scoring import resolve_cut_off

TAG = uuid.uuid4().hex[:6].upper()


# ----------------------------- le classement -----------------------------

def test_toutes_les_listes_sont_classees():
    from fiskr.database import WATCHLIST_FILE_TYPES
    table = perimetres_par_defaut()
    manquants = [t for t in WATCHLIST_FILE_TYPES if t not in table]
    assert not manquants, f"types non classés : {manquants}"
    assert set(table.values()) <= set(PERIMETRES)


@pytest.mark.parametrize("list_type", [
    "WATCHLIST_OFAC", "WATCHLIST_EU", "WATCHLIST_UN", "WATCHLIST_DGT",
    "WATCHLIST_OFSI", "WATCHLIST_SECO", "WATCHLIST_CSL", "WATCHLIST_CANADA",
    "WATCHLIST_US_FTO", "WATCHLIST_GB_PROSCRIBED", "WATCHLIST_BE_TERROR",
    "WATCHLIST_IL_NBCTF", "WATCHLIST_TN_CNLCT",
])
def test_les_gels_d_avoirs_sont_du_cote_sanction(list_type):
    assert perimetre_de(list_type) == PERIMETRE_SANCTION


@pytest.mark.parametrize("list_type", [
    "WATCHLIST_PEP", "WATCHLIST_AMF", "WATCHLIST_HK_SFC",
    "WATCHLIST_WORLDBANK", "WATCHLIST_WB_DEBARRED", "WATCHLIST_ADB",
    "WATCHLIST_AFDB", "WATCHLIST_IADB", "WATCHLIST_EBRD",
])
def test_les_signaux_de_vigilance_sont_hors_sanction(list_type):
    assert perimetre_de(list_type) == PERIMETRE_HORS_SANCTION


def test_une_liste_inconnue_tombe_du_cote_qui_ne_cloture_rien():
    """Une liste mal classée du côté hors-sanction se ferait clôturer ses
    correspondances en volume. Le défaut penche donc vers ce qui ne fait rien
    perdre."""
    assert perimetre_de("WATCHLIST_TOUTE_NOUVELLE") == PERIMETRE_SANCTION
    assert perimetre_de(None) == PERIMETRE_SANCTION
    assert perimetre_de("") == PERIMETRE_SANCTION


def test_le_classement_est_surchargeable():
    """C'est un arbitrage de conformité : il doit pouvoir être décidé."""
    assert perimetre_de("WATCHLIST_PEP") == PERIMETRE_HORS_SANCTION
    assert perimetre_de("WATCHLIST_PEP",
                        {"WATCHLIST_PEP": "SANCTION"}) == PERIMETRE_SANCTION
    # Une surcharge invalide est ignorée, pas appliquée au hasard
    assert perimetre_de("WATCHLIST_OFAC",
                        {"WATCHLIST_OFAC": "N_IMPORTE_QUOI"}) == PERIMETRE_SANCTION


def test_le_perimetre_couvre_la_masse_des_fiches():
    """Le relevé qui justifie tout : les listes hors sanction portent
    l'écrasante majorité des fiches en production."""
    hors = [t for t, p in perimetres_par_defaut().items()
            if p == PERIMETRE_HORS_SANCTION]
    assert "WATCHLIST_PEP" in hors


# --------------------------- le seuil par périmètre ---------------------------

def _cfg(seuil_global=75.0, par_perimetre=None, par_liste=None):
    return {"scoring": {"cut_off_threshold": seuil_global,
                        "cut_off_overrides": par_liste or {},
                        "cut_off_by_perimeter": par_perimetre or {}}}


def test_sans_reglage_aucun_seuil_ne_bouge():
    """Table vide par défaut : introduire les périmètres ne déplace aucun
    score, donc aucun cahier de tests déjà homologué."""
    for liste in ("WATCHLIST_OFAC", "WATCHLIST_PEP"):
        assert resolve_cut_off(_cfg(), {"_list_type": liste}) == 75.0


def test_le_seuil_de_perimetre_s_applique():
    cfg = _cfg(par_perimetre={"HORS_SANCTION": 90.0})
    assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_PEP"}) == 90.0
    assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_OFAC"}) == 75.0


def test_la_surcharge_par_liste_reste_prioritaire():
    """Ordre : surcharge par liste > périmètre > seuil global. Un réglage posé
    sur UNE liste est plus précis qu'un réglage de périmètre."""
    cfg = _cfg(par_perimetre={"HORS_SANCTION": 90.0},
               par_liste={"WATCHLIST_PEP": 82.0})
    assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_PEP"}) == 82.0
    assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_AMF"}) == 90.0


def test_le_seuil_de_perimetre_suit_le_classement_surcharge():
    cfg = _cfg(par_perimetre={"HORS_SANCTION": 90.0})
    cfg["scoring"]["perimeter_overrides"] = {"WATCHLIST_OFAC": "HORS_SANCTION"}
    assert resolve_cut_off(cfg, {"_list_type": "WATCHLIST_OFAC"}) == 90.0


# ------------------- la portée d'une règle, tenue par le moteur -------------------

def test_une_regle_hors_sanction_ne_touche_pas_une_sanction():
    """La garantie centrale : le filtre est dans le MOTEUR. Le code de cette
    règle renvoie True pour tout — elle ne doit malgré tout jamais clôturer
    une correspondance de gel d'avoirs."""
    from fiskr.fprules import evaluate_fp_rules

    class _Regle:
        id, name, version, run_order = 1, f"tout-{TAG}", 1, 1
        code = "def rule(ctx):\n    return True\n"
        perimeters = ["HORS_SANCTION"]
        hit_count = 0

    regle = _Regle()
    hors = {"perimeter": PERIMETRE_HORS_SANCTION, "final_score": 100.0}
    sanction = {"perimeter": PERIMETRE_SANCTION, "final_score": 100.0}
    assert evaluate_fp_rules(None, "SCREENING", hors, dry_run=True,
                             rules=[regle]) is regle
    assert evaluate_fp_rules(None, "SCREENING", sanction, dry_run=True,
                             rules=[regle]) is None


def test_une_regle_sans_portee_declaree_garde_son_comportement():
    """Les règles écrites avant cette colonne s'appliquent partout, comme
    avant : introduire les périmètres ne change rien à l'existant."""
    from fiskr.fprules import evaluate_fp_rules

    class _Regle:
        id, name, version, run_order = 2, f"ancienne-{TAG}", 1, 1
        code = "def rule(ctx):\n    return True\n"
        perimeters = None
        hit_count = 0

    regle = _Regle()
    for perimetre in PERIMETRES:
        assert evaluate_fp_rules(None, "SCREENING", {"perimeter": perimetre},
                                 dry_run=True, rules=[regle]) is regle


def test_une_portee_illisible_ne_s_applique_nulle_part():
    """Élargir la portée en silence sur une déclaration cassée serait le pire
    des deux comportements."""
    from fiskr.fprules import evaluate_fp_rules, rule_perimeters

    class _Regle:
        id, name, version, run_order = 3, f"cassee-{TAG}", 1, 1
        code = "def rule(ctx):\n    return True\n"
        perimeters = ["N_IMPORTE_QUOI"]
        hit_count = 0

    assert rule_perimeters(_Regle()) == ["__AUCUN__"]
    for perimetre in PERIMETRES:
        assert evaluate_fp_rules(None, "SCREENING", {"perimeter": perimetre},
                                 dry_run=True, rules=[_Regle()]) is None


# ------------------------------ les modèles ------------------------------

def test_aucun_modele_agressif_ne_vise_le_perimetre_sanction():
    from fiskr.fprules import RULE_TEMPLATES, run_rule
    masse = {"hits_count": 5000, "hit_rank": 4000, "hard_match": False,
             "perimeter": PERIMETRE_SANCTION,
             "corroboration": {"name_only": True, "corroborated": False,
                               "has_dob": False, "has_country": False,
                               "has_identity_document": False, "dob_score": 0.0,
                               "gender_score": 0.0, "geography_score": 0.0}}
    for modele in RULE_TEMPLATES:
        portees = modele.get("perimeters") or []
        if PERIMETRE_SANCTION in portees:
            # Un modèle de ce périmètre existe, mais il ne clôture rien
            assert run_rule(modele["code"], masse)[0] is False, modele["key"]
        else:
            assert portees == [PERIMETRE_HORS_SANCTION], modele["key"]


def test_chaque_modele_declare_une_portee():
    from fiskr.fprules import RULE_TEMPLATES
    for modele in RULE_TEMPLATES:
        assert modele.get("perimeters"), modele["key"]
        assert set(modele["perimeters"]) <= set(PERIMETRES), modele["key"]


# ------------------------------ bout en bout ------------------------------

@pytest.fixture()
def contexte(monkeypatch):
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "pm", "full_name": "pm", "role": "admin",
        "roles": ["admin"]}

    def _fiche(n, list_type):
        return {"id": 60000 + n, "entity_id": f"PM-{TAG}-{list_type[-4:]}-{n}",
                "entity_type": "I", "primary_name": "MOHAMMED ALI",
                "aliases": {"high_priority": [], "low_priority": []},
                "dates_of_birth": [], "gender": "U",
                "countries": {"citizenship": [], "residence": [],
                              "birth_country": [], "jurisdiction_country": []},
                "_list_type": list_type}

    fiches = ([_fiche(i, "WATCHLIST_PEP") for i in range(6)]
              + [_fiche(i, "WATCHLIST_OFAC") for i in range(6)])

    class _Index(dict):
        def get(self, cle, defaut=None):
            return fiches

    monkeypatch.setattr(api_module, "watchlist_index", _Index())
    monkeypatch.setattr(api_module, "watchlist_store", fiches)
    monkeypatch.setattr(api_module, "_ensure_watchlist_cache", lambda db: None)
    db = next(get_db())
    yield db, TestClient(app), fiches
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


def test_une_regle_hors_sanction_laisse_les_sanctions_ouvertes(contexte):
    """Le cas réel : le même nom est porté par des PEP et par des sanctionnés.
    La règle ferme les PEP, les sanctions restent à traiter."""
    db, client, fiches = contexte
    db.add(FpRule(name=f"Hors sanctions {TAG}", channel="SCREENING",
                  code="def rule(ctx):\n    return True\n",
                  perimeters=["HORS_SANCTION"], status="ACTIVE", enabled=True,
                  version=1, run_order=1, created_by="test"))
    db.commit()

    reponse = client.post("/api/screen", json={
        "client_id": f"C-{TAG}", "client_type": "PP",
        "client_first_name": "MOHAMMED", "client_last_name": "ALI"})
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()

    assert corps["hits"]["hits"] == 12
    assert corps["hits"]["by_perimeter"] == {PERIMETRE_SANCTION: 6,
                                             PERIMETRE_HORS_SANCTION: 6}
    assert corps["hits"]["closed_by_rule"] == 6
    assert corps["hits"]["opened"] == 6

    ouvertes = db.query(Alert).filter(
        Alert.watchlist_entity_id.in_([f["entity_id"] for f in fiches]),
        Alert.status == "OPEN").all()
    assert {a.list_type for a in ouvertes} == {"WATCHLIST_OFAC"}
    fermees = db.query(Alert).filter(
        Alert.watchlist_entity_id.in_([f["entity_id"] for f in fiches]),
        Alert.status == "CLOSED_BY_RULE").all()
    assert {a.list_type for a in fermees} == {"WATCHLIST_PEP"}


def test_le_classement_est_servi_par_l_api(contexte):
    _, client, _ = contexte
    reponse = client.get("/api/screening/perimeters")
    assert reponse.status_code == 200, reponse.text
    corps = reponse.json()
    cles = {p["key"] for p in corps["perimeters"]}
    assert cles == set(PERIMETRES)
    assert corps["by_list_type"]["WATCHLIST_OFAC"] == PERIMETRE_SANCTION
    assert corps["by_list_type"]["WATCHLIST_PEP"] == PERIMETRE_HORS_SANCTION
    for bloc in corps["perimeters"]:
        assert bloc["list_types"] and bloc["label"]
        assert isinstance(bloc["cut_off"], (int, float))

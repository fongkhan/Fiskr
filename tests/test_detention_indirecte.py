"""
La détention indirecte : au-delà du premier niveau de la chaîne capitalistique.

Constat : le lot « parties liées » réputait ACME détenue quand ses parties
désignées confirmées cumulaient 50 % — au PREMIER niveau seulement. ACME ← 60 %
HOLDCO ← 100 % d'une personne désignée n'était pas réputée détenue : HOLDCO
n'est pas désignée, et la personne désignée n'était pas une partie d'ACME.

La règle, LUE (fiskr/parties.py, « LA CHAÎNE DE DÉTENTION ») :
  - lignes directrices conjointes DGT/ACPR sur le gel des avoirs, 18 mars 2026,
    §131 : « identifier toute la chaîne capitalistique et déterminer si la
    personne [...] désignée détient [50 % ou plus] une ou plusieurs personnes,
    groupes ou entités » ; propriété agrégée (30 % + 25 %) ;
  - meilleures pratiques de l'UE (doc. du Conseil 11623/24, 3 juillet 2024),
    §62-65 ; Commission européenne, FAQ gel des avoirs (règlement 269/2014),
    n° 8 (agrégation) et n° 9 (le contrôle d'une entité « would impact its
    own subsidiaries ») ;
  - d'où la CASCADE : une société réputée détenue compte pour sa participation
    entière. La multiplication le long d'une chaîne n'est fixée par aucun texte
    lu : montrée, nommée comme un doute quand elle seule franchirait le seuil.
    L'OFAC (FAQ 401, comparaison, non applicable en France) écrit la même
    cascade.
"""
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import gels, parties
from fiskr.database import (Base, Alert, AlertEvent, ClientEntity, MesureDeGel, PartieLiee,
                            Snapshot, WatchlistEntity)
from fiskr.rescreen import rescreen_lookback


# ================================================================ base isolée

@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'detention.sqlite3'}")
    Base.metadata.create_all(engine)
    # Comme la session de l'application : pas de vidage implicite.
    session = sessionmaker(bind=engine, autoflush=False)()
    yield session
    session.close()


def _client(db, client_id, nom, pays="FR"):
    snap = Snapshot(snapshot_id=f"clients-{uuid.uuid4().hex[:6]}", file_type="CLIENT_BASE",
                    file_name="clients.csv", file_hash=uuid.uuid4().hex,
                    record_count=1, status="READY")
    db.add(snap)
    db.add(ClientEntity(
        snapshot_id=snap.snapshot_id, client_id=client_id, client_type="PM",
        client_company_name=nom,
        client_countries={"nationality": [], "residence": [], "birth_country": [],
                          "registration_country": [pays]},
        entity_checksum=uuid.uuid4().hex))
    db.commit()


def _chaine(de, client):
    """Les deux portes de la chaîne, passées seulement quand elles servent : un
    test du premier niveau s'exécute alors aussi sur le code d'avant."""
    return {**({"partie_de": de} if de else {}),
            **({"partie_client_id": client} if client else {})}


def _societe(partie_id, nom, pct, de=None, client=None, engage=None):
    return parties.lire_une_partie(
        {"client_type": "PM", "client_company_name": nom,
         "client_countries": {"nationality": [], "residence": [], "birth_country": [],
                              "registration_country": ["LU"]}},
        roles=["ASSOCIE"], pourcentage=pct, peut_engager=engage, partie_id=partie_id,
        **_chaine(de, client))


VIKTOR = ("Viktor", "ZOLOTAREV", "1954-01-27")


def _personne(partie_id, pct, de=None, engage=None, roles=("ASSOCIE",), qui=VIKTOR):
    prenom, nom, dob = qui
    return parties.lire_une_partie(
        {"client_type": "PP", "client_first_name": prenom, "client_last_name": nom,
         "client_dob": dob, "client_gender": "M",
         "client_countries": {"nationality": ["RU"], "residence": [], "birth_country": [],
                              "registration_country": []}},
        roles=list(roles), pourcentage=pct, peut_engager=engage, partie_id=partie_id,
        **_chaine(de, None))


def _declarer(db, client_id, lues):
    bilan = parties.remplacer_les_parties(db, client_id, lues, source=parties.SOURCE_IMPORT,
                                          source_ref="test", username="test")
    db.commit()
    return bilan


def _fiche(snap_id, entity_id, qui=VIKTOR):
    prenom, nom, dob = qui
    return WatchlistEntity(
        snapshot_id=snap_id, entity_id=entity_id, entity_type="I",
        primary_name=f"{prenom} {nom}",
        individual_name_parsed={"first_name": prenom, "last_name": nom, "maiden_name": ""},
        aliases={"high_priority": [], "low_priority": []},
        dates_of_birth=[dob], is_deceased=False, gender="M",
        countries={"citizenship": ["RU"], "residence": [], "birth_country": [],
                   "jurisdiction_country": []},
        entity_checksum=f"chk-{entity_id}")


def _liste(db, snap_id, fiches, file_type="WATCHLIST_DGT"):
    db.add(Snapshot(snapshot_id=snap_id, file_type=file_type, file_name=f"{snap_id}.json",
                    file_hash=uuid.uuid4().hex, record_count=len(fiches), status="READY"))
    for f in fiches:
        db.add(f)
    db.commit()


def _cribler(db, file_type="WATCHLIST_DGT"):
    return rescreen_lookback(db, file_type)


def _confirmer(db, alerte):
    """Ce que fait `_close_alert` (fiskr/api.py) à toute confirmation."""
    alerte.status = "CLOSED_CONFIRMED"
    alerte.decided_by = "reviseur"
    alerte.decided_at = datetime.utcnow()
    mesure = gels.ouvrir_si_due(db, alerte, "reviseur")
    db.commit()
    return mesure


def _evenements(db, alerte, action):
    return [e.detail for e in db.query(AlertEvent).filter(
        AlertEvent.alert_id == alerte.id, AlertEvent.action == action).all()]


def _signalements(evaluation, code):
    return [s for s in evaluation["signalements"] if s["type"] == code]


# ------------------------------------------------- la chaîne compte

def test_acme_a_60_pct_d_une_holding_detenue_a_100_pct_par_un_designe_est_reputee_detenue(db):
    """ACME ← 60 % HOLDCO ← 100 % personne désignée. Premier niveau seul : 0 %
    désigné (HOLDCO ne l'est pas) — le lot précédent n'ouvrait rien. LD §131 :
    HOLDCO est réputée détenue (100 % ≥ 50 %) et ses 60 % d'ACME comptent :
    ACME est réputée détenue, GEL au registre, au même point de décision."""
    _client(db, "ACME-1", "ACME HOLDING SAS")
    _declarer(db, "ACME-1", [_societe("HOLDCO", "HOLDCO SAS", 60),
                             _personne("UBO-X", 100, de="HOLDCO")])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL")])
    r = _cribler(db)
    assert r["parties_screened"] == 2 and r["new_alerts"] == 1, r
    alerte = db.query(Alert).one()
    # L'alerte est celle d'ACME, et dit où la personne se tient dans la chaîne
    assert alerte.client_id == "ACME-1"
    assert alerte.via_partie == ("via l'associé Viktor ZOLOTAREV (100 %) de HOLDCO SAS, "
                                 "associé (60 %) du client ACME HOLDING SAS")

    mesure = _confirmer(db, alerte)
    assert mesure is not None, "la chaîne au-delà du premier niveau n'a rien ouvert"
    assert mesure.client_id == "ACME-1" and mesure.type_action == "GEL"
    assert mesure.fondement == parties.FONDEMENT_DETENTION
    calcul = mesure.detention
    assert calcul["cumul_direct_pct"] == 0.0, "le premier niveau seul ne trouve rien"
    assert calcul["cumul_indirect_pct"] == 60.0 and calcul["cumul_pct"] == 60.0
    [chemin] = calcul["chaines"]
    assert chemin["texte"] == ("ACME HOLDING SAS ← 60 % HOLDCO SAS ← 100 % Viktor ZOLOTAREV "
                               "(personne désignée)")
    assert chemin["compte"] is True
    assert [e["qualification"] for e in chemin["etapes"]] == [parties.QUALIFICATION_DETENUE,
                                                              parties.QUALIFICATION_DESIGNEE]
    assert any("dont 60 % par la chaîne de détention" in d
               for d in _evenements(db, alerte, "DETENTION_REPUTEE"))


def test_le_chemin_est_conserve_et_la_multiplication_montree_sans_etre_retenue(db):
    """ACME ← 60 % HOLDCO ← 70 % X. Par multiplication, X aurait 42 % d'ACME ;
    la cascade lue (LD §131, FAQ Commission n° 9) retient 60 % : HOLDCO est
    réputée détenue (70 %), sa participation entière compte. Les deux chiffres
    sont dans la mesure et dans l'historique : un réviseur refait le calcul."""
    _client(db, "ACME-1", "ACME HOLDING SAS")
    _declarer(db, "ACME-1", [_societe("HOLDCO", "HOLDCO SAS", 60),
                             _personne("UBO-X", 70, de="HOLDCO")])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL")])
    _cribler(db)
    alerte = db.query(Alert).one()
    mesure = _confirmer(db, alerte)
    assert mesure is not None and mesure.fondement == parties.FONDEMENT_DETENTION
    assert mesure.detention["cumul_pct"] == 60.0
    assert mesure.detention["multiplication_pct"] == 42.0
    [chemin] = mesure.detention["chaines"]
    assert chemin["produit_pct"] == 42.0
    assert chemin["texte"] == ("ACME HOLDING SAS ← 60 % HOLDCO SAS ← 70 % Viktor ZOLOTAREV "
                               "(personne désignée)")
    [phrase] = _evenements(db, alerte, "DETENTION_REPUTEE")
    assert "ACME HOLDING SAS ← 60 % HOLDCO SAS ← 70 % Viktor ZOLOTAREV (personne désignée)" in phrase
    assert "42 % par multiplication" in phrase
    # La partie retenue dit de QUELLE société elle détient 70 %
    [retenue] = mesure.detention["parties"]
    assert retenue["partie_de"] == "HOLDCO SAS" and retenue["niveau"] == 2


def test_deux_chaines_de_30_pct_vers_le_meme_designe_s_additionnent(db):
    """Agrégation : LD §131 (« il convient également de tenir compte de la
    propriété agrégée ») ; meilleures pratiques de l'UE, doc. 11623/24, §63 ;
    Commission, FAQ gel des avoirs n° 8. Deux holdings, chacune détenue à
    100 % par la même personne désignée, détiennent chacune 30 % d'ACME : deux
    participations distinctes, toutes deux portées par des sociétés réputées
    détenues — 60 %, ACME est réputée détenue (OFAC, FAQ 401, exemple 2 :
    même résultat). Ce n'est pas la règle « une part par personne » : celle-ci
    vaut pour la MÊME personne déclarée deux fois dans la MÊME société, qui ne
    compte qu'une fois (contre-épreuve sur BETA)."""
    _client(db, "ACME-1", "ACME HOLDING SAS")
    _declarer(db, "ACME-1", [_societe("H1", "ALPHA PARTICIPATIONS SARL", 30),
                             _societe("H2", "OMEGA INVEST SA", 30),
                             _personne("X-H1", 100, de="H1"),
                             _personne("X-H2", 100, de="H2")])
    _client(db, "BETA-1", "BETA INDUSTRIE SAS")
    _declarer(db, "BETA-1", [_personne("X-A", 30, roles=("BENEFICIAIRE_EFFECTIF",)),
                             _personne("X-B", 30, roles=("ASSOCIE",))])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL")])
    assert _cribler(db)["new_alerts"] == 4
    for alerte in db.query(Alert).filter(Alert.client_id == "BETA-1").all():
        assert _confirmer(db, alerte) is None
    beta = parties.evaluer_la_detention(db, "BETA-1")
    assert beta["cumul_pct"] == 30.0 and beta["reputee"] is False, \
        "la même personne déclarée deux fois dans la même société s'est additionnée à elle-même"

    acme = db.query(Alert).filter(Alert.client_id == "ACME-1").order_by(Alert.id).all()
    assert _confirmer(db, acme[0]) is None, "une seule chaîne : 30 %, sous le seuil"
    mesure = _confirmer(db, acme[1])
    assert mesure is not None and mesure.client_id == "ACME-1"
    assert mesure.fondement == parties.FONDEMENT_DETENTION
    assert mesure.detention["cumul_pct"] == 60.0 and mesure.detention["cumul_indirect_pct"] == 60.0
    assert sorted(c["texte"] for c in mesure.detention["chaines"]) == [
        "ACME HOLDING SAS ← 30 % ALPHA PARTICIPATIONS SARL ← 100 % Viktor ZOLOTAREV (personne désignée)",
        "ACME HOLDING SAS ← 30 % OMEGA INVEST SA ← 100 % Viktor ZOLOTAREV (personne désignée)"]


def test_un_beneficiaire_confirme_sur_deux_listes_ne_compte_qu_une_fois(db):
    """Défaut du premier lot, corrigé : la part s'indexait sur la FICHE listée.
    Un bénéficiaire effectif à 30 %, confirmé sur la liste nationale ET sur la
    liste de l'UE (deux fiches, la même personne), comptait 30 % + 30 % = 60 % :
    ACME était « réputée détenue » et gelée sur 30 %. Une personne désignée,
    une part (§131 additionne des personnes, pas des inscriptions)."""
    _client(db, "ACME-1", "ACME HOLDING SAS")
    _declarer(db, "ACME-1", [_personne("UBO-X", 30, roles=("BENEFICIAIRE_EFFECTIF",))])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL")])
    _liste(db, "ue-v1", [_fiche("ue-v1", "EU-ZOL")], file_type="WATCHLIST_EU")
    _cribler(db, "WATCHLIST_DGT")
    _cribler(db, "WATCHLIST_EU")
    alertes = db.query(Alert).order_by(Alert.id).all()
    assert len(alertes) == 2 and {a.watchlist_entity_id for a in alertes} == {"DGT-ZOL", "EU-ZOL"}
    for alerte in alertes:
        assert _confirmer(db, alerte) is None
    evaluation = parties.evaluer_la_detention(db, "ACME-1")
    assert evaluation["cumul_pct"] == 30.0 and evaluation["reputee"] is False
    assert db.query(MesureDeGel).count() == 0
    [retenue] = evaluation["parties"]
    assert sorted(retenue["alertes"]) == [a.id for a in alertes]


def test_une_societe_controlee_par_un_designe_fait_suspendre_le_client_qu_elle_detient(db):
    """Le contrôle se propage (FAQ Commission n° 9 ; meilleures pratiques §64 c,
    majorité des droits de vote) : X, désigné, dirige HOLDCO et peut engager ses
    fonds (§133, §144) avec 10 % seulement — HOLDCO est réputée CONTRÔLÉE, pas
    détenue ; ses 60 % d'ACME mettent ACME sous contrôle. La suite est celle du
    contrôle : suspendre et interroger la DG Trésor, pas geler."""
    _client(db, "ACME-1", "ACME HOLDING SAS")
    _declarer(db, "ACME-1", [_societe("HOLDCO", "HOLDCO SAS", 60),
                             _personne("DIR-X", 10, de="HOLDCO", engage=True,
                                       roles=("DIRIGEANT", "ASSOCIE"))])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL")])
    _cribler(db)
    mesure = _confirmer(db, db.query(Alert).one())
    assert mesure is not None
    assert mesure.fondement == parties.FONDEMENT_CONTROLE
    assert mesure.type_action == "SUSPENSION_MISE_A_DISPOSITION"
    assert mesure.detention["cumul_pct"] == 0.0
    assert mesure.detention["cumul_avec_controle_pct"] == 60.0
    [chemin] = mesure.detention["chaines"]
    assert chemin["etapes"][0]["qualification"] == parties.QUALIFICATION_CONTROLEE


def test_la_multiplication_qui_seule_franchirait_le_seuil_est_nommee_comme_un_doute(db):
    """Là où les textes se taisent. X détient 30 % d'ACME en direct, et 40 % de
    HOLDCO qui détient 60 % d'ACME. Cascade : HOLDCO n'est pas détenue (40 %),
    sa participation ne compte pas — 30 %, pas de gel. Par multiplication :
    30 % + 60 % × 40 % = 54 %. Aucun texte lu ne tranche : la décision le
    NOMME (« en cas de doute, il s'abstient [...] et interroge la DG Trésor »,
    §144) au lieu de choisir en silence."""
    _client(db, "ACME-1", "ACME HOLDING SAS")
    _declarer(db, "ACME-1", [_personne("X-DIRECT", 30, roles=("BENEFICIAIRE_EFFECTIF",)),
                             _societe("HOLDCO", "HOLDCO SAS", 60),
                             _personne("X-HOLDCO", 40, de="HOLDCO")])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL")])
    _cribler(db)
    alertes = db.query(Alert).order_by(Alert.id).all()
    assert len(alertes) == 2
    for alerte in alertes:
        assert _confirmer(db, alerte) is None
    evaluation = parties.evaluer_la_detention(db, "ACME-1")
    assert evaluation["reputee"] is False and evaluation["cumul_pct"] == 30.0
    assert evaluation["multiplication_pct"] == 54.0
    assert len(_signalements(evaluation, parties.SIGNAL_MULTIPLICATION)) == 1
    [_, derniere] = alertes
    [phrase] = _evenements(db, derniere, "DETENTION_NON_ETABLIE")
    assert "54 % par multiplication" in phrase and "en cas de doute" in phrase
    assert "HOLDCO SAS : ni réputée détenue ni réputée contrôlée" in phrase


# ------------------------------------------------- la chaîne qui ne finit pas

def test_un_cycle_holdco_acme_ne_boucle_pas_et_se_signale(db):
    """Participation croisée : HOLDCO (cliente) détient 60 % d'ACME, ACME
    (cliente) 20 % de HOLDCO ; X, désigné, 70 % de HOLDCO. Le parcours suit
    ACME → HOLDCO → ACME : il s'arrête au cycle, le DIT, et conclut sur le
    reste (HOLDCO détenue à 70 %, donc ACME à 60 %). La confirmation de X,
    alerte de HOLDCO, décide aussi d'ACME — même point de décision : la mesure
    d'ACME porte son nom et l'alerte qui a établi la personne désignée."""
    _client(db, "ACME-1", "ACME HOLDING SAS")
    _client(db, "HOLDCO-7", "HOLDCO SAS")
    _declarer(db, "ACME-1", [_societe("P-HOLDCO", "HOLDCO SAS", 60, client="HOLDCO-7")])
    _declarer(db, "HOLDCO-7", [_societe("P-ACME", "ACME HOLDING SAS", 20, client="ACME-1"),
                               _personne("UBO-X", 70)])
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL")])
    _cribler(db)
    alerte = db.query(Alert).one()
    assert alerte.client_id == "HOLDCO-7", "X est la partie de HOLDCO, criblée avec elle"

    mesure = _confirmer(db, alerte)
    assert mesure is not None and mesure.client_id == "HOLDCO-7"
    mesures = {m.client_id: m for m in db.query(MesureDeGel).all()}
    assert set(mesures) == {"HOLDCO-7", "ACME-1"}, "la confirmation n'a pas décidé d'ACME"
    acme = mesures["ACME-1"]
    assert acme.alert_id == alerte.id and acme.client_name == "ACME HOLDING SAS"
    assert acme.fondement == parties.FONDEMENT_DETENTION and acme.detention["cumul_pct"] == 60.0
    [cycle] = _signalements(acme.detention, parties.SIGNAL_CYCLE)
    assert cycle["texte"] == "ACME HOLDING SAS ← 60 % HOLDCO SAS ← 20 % ACME HOLDING SAS"
    assert any("Participation circulaire" in d and "ACME HOLDING SAS ← 60 % HOLDCO SAS" in d
               for d in _evenements(db, alerte, "DETENTION_REPUTEE"))
    # Le projet de déclaration nomme ACME — dont les avoirs sont en cause —,
    # pas HOLDCO, le client de l'alerte ; et il porte le chemin.
    from fiskr.api import _build_gel_draft
    projet = _build_gel_draft(db, acme, "reviseur")
    assert projet["personne_concernee"]["reference_interne"] == "ACME-1"
    assert projet["personne_concernee"]["raison_sociale"] == "ACME HOLDING SAS"
    assert projet["detention_ou_controle"]["chaines"] == [
        "ACME HOLDING SAS ← 60 % HOLDCO SAS ← 70 % Viktor ZOLOTAREV (personne désignée)"]
    # Une nouvelle évaluation termine encore, et dit encore le cycle
    assert _signalements(parties.evaluer_la_detention(db, "HOLDCO-7"), parties.SIGNAL_CYCLE)


def test_une_chaine_plus_profonde_que_la_borne_est_signalee_pas_tronquee_en_silence(db):
    """Les textes ne bornent pas la profondeur ; le parcours, si
    (`PROFONDEUR_MAXIMALE`). À la borne, la personne désignée compte ; un
    niveau plus loin, elle n'est pas atteinte — et le calcul DIT qu'il est
    incomplet au lieu de conclure « ni détenu ni contrôlé » en silence."""
    borne = parties.PROFONDEUR_MAXIMALE

    def chaine(client_id, nom, niveaux):
        # client ← 100 % S1 ← 100 % S2 ← … ← 100 % S(niveaux-1) ← 100 % X
        lues = [_societe("S1", f"{nom} NIVEAU 1 SA", 100)]
        lues += [_societe(f"S{i}", f"{nom} NIVEAU {i} SA", 100, de=f"S{i - 1}")
                 for i in range(2, niveaux)]
        lues.append(_personne("UBO-X", 100, de=f"S{niveaux - 1}"))
        _client(db, client_id, f"{nom} TETE SA")
        _declarer(db, client_id, lues)

    chaine("A-BORNE", "ALDEBARAN", borne)
    chaine("B-AU-DELA", "BETELGEUSE", borne + 1)
    _liste(db, "dgt-v1", [_fiche("dgt-v1", "DGT-ZOL")])
    _cribler(db)
    a = db.query(Alert).filter(Alert.client_id == "A-BORNE").one()
    b = db.query(Alert).filter(Alert.client_id == "B-AU-DELA").one()

    mesure = _confirmer(db, a)
    assert mesure is not None and mesure.fondement == parties.FONDEMENT_DETENTION
    assert mesure.detention["chaines"][0]["niveau"] == borne
    assert not _signalements(mesure.detention, parties.SIGNAL_PROFONDEUR)

    assert _confirmer(db, b) is None
    evaluation = parties.evaluer_la_detention(db, "B-AU-DELA")
    assert evaluation["reputee"] is False
    [profondeur] = _signalements(evaluation, parties.SIGNAL_PROFONDEUR)
    assert profondeur["chemin"][0] == "BETELGEUSE TETE SA"
    assert profondeur["chemin"][-1] == f"BETELGEUSE NIVEAU {borne} SA"
    [phrase] = _evenements(db, b, "DETENTION_NON_ETABLIE")
    assert "calcul incomplet" in phrase and f"au-delà de {borne} niveaux" in phrase


# ------------------------------------------------- une partie qui EST un client

def test_un_client_designe_rend_repute_detenu_le_client_dont_il_est_la_partie(db):
    """Les deux mondes reliés là où c'est sain : la partie HOLDCO d'ACME EST le
    client HOLDCO-7 du référentiel (déclaré par la source, jamais deviné par le
    nom). HOLDCO-7 est elle-même listée : la confirmation de SA propre alerte
    ouvre son gel ET rend ACME réputée détenue (60 %) — même point de décision
    (`ouvrir_si_due`). Avant : aucune mesure pour ACME, faute de confirmation
    sur une partie d'ACME."""
    _client(db, "ACME-1", "ACME HOLDING SAS")
    _client(db, "HOLDCO-7", "SEVASTOPOL HOLDING SA", pays="LU")
    _declarer(db, "ACME-1", [_societe("P-HOLDCO", "SEVASTOPOL HOLDING SA", 60, client="HOLDCO-7")])
    db.add(Snapshot(snapshot_id="dgt-v1", file_type="WATCHLIST_DGT", file_name="d.json",
                    file_hash=uuid.uuid4().hex, record_count=1, status="READY"))
    db.add(WatchlistEntity(
        snapshot_id="dgt-v1", entity_id="DGT-SEV", entity_type="E",
        primary_name="SEVASTOPOL HOLDING SA", aliases={"high_priority": [], "low_priority": []},
        countries={"citizenship": [], "residence": [], "birth_country": [],
                   "jurisdiction_country": ["LU"]},
        entity_checksum="chk-sev"))
    db.commit()
    _cribler(db)
    directe = db.query(Alert).filter(Alert.client_id == "HOLDCO-7",
                                     Alert.partie_liee_id.is_(None)).one()
    gel_direct = _confirmer(db, directe)
    assert gel_direct is not None and gel_direct.client_id == "HOLDCO-7"
    acme = db.query(MesureDeGel).filter(MesureDeGel.client_id == "ACME-1").one_or_none()
    assert acme is not None, "le client désigné n'a pas rendu ACME réputée détenue"
    assert acme.fondement == parties.FONDEMENT_DETENTION and acme.alert_id == directe.id
    assert acme.detention["cumul_direct_pct"] == 60.0
    assert any("pour le client ACME HOLDING SAS" in d for d in _evenements(db, directe, "GEL_OUVERT"))


# ------------------------------------------------- une chaîne incohérente est refusée

def test_une_chaine_incoherente_est_refusee_et_rien_n_est_ecrit(db):
    """Une chaîne à moitié lue serait un calcul faux qui se présente comme juste :
    refus franc, en nommant pourquoi, et le jeu en base reste celui d'avant."""
    _declarer(db, "ACME-1", [_societe("HOLDCO", "HOLDCO SAS", 60)])
    cas = {
        "absente du jeu": [_personne("X", 70, de="HOLDCO-INCONNUE")],
        "personne physique": [_personne("X", 70), _personne("Y", 10, de="X",
                                                              qui=("Igor", "SOKOLINE", "1958-08-08"))],
        "détentions circulaires": [_societe("A", "ALPHA SA", 50, de="B"),
                                   _societe("B", "BRAVO SA", 50, de="A")],
        "compterait deux fois": [_societe("HOLDCO", "HOLDCO SAS", 60, client="HOLDCO-7"),
                                 _personne("X", 70, de="HOLDCO")],
        "le client ACME-1 lui-même": [_societe("SOI", "ACME HOLDING SAS", 10, client="ACME-1")],
        "même client": [_societe("H1", "HOLDCO SAS", 30, client="HOLDCO-7"),
                        _societe("H2", "HOLDCO SA", 30, client="HOLDCO-7")],
    }
    for motif, lues in cas.items():
        with pytest.raises(parties.RefusDePartie, match=motif):
            parties.remplacer_les_parties(db, "ACME-1", lues, source=parties.SOURCE_IMPORT,
                                          source_ref="test", username="test")
        db.rollback()
    restantes = db.query(PartieLiee).filter(PartieLiee.client_id == "ACME-1").all()
    assert [(p.partie_id, p.retiree_le) for p in restantes] == [("HOLDCO", None)]


# ================================================================ par l'API

ADMIN = {"id": 1, "username": "admin_detention", "full_name": "Admin",
         "role": "admin,reviewer", "roles": ["admin", "reviewer"]}
_LISTES: list = []


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from fiskr.api import app
    from fiskr.auth import get_current_user
    from fiskr.database import AppSetting, get_db
    from fiskr.settings import SETTING_ALERT_FOUR_EYES, set_setting
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    session = next(get_db())
    avant = session.query(AppSetting).filter(AppSetting.key == SETTING_ALERT_FOUR_EYES).first()
    valeur = avant.value if avant else None
    set_setting(session, SETTING_ALERT_FOUR_EYES, False)
    _LISTES.clear()
    try:
        with TestClient(app) as c:
            yield c
    finally:
        ligne = session.query(AppSetting).filter(AppSetting.key == SETTING_ALERT_FOUR_EYES).first()
        if avant is None and ligne is not None:
            session.delete(ligne)
        elif ligne is not None:
            ligne.value = valeur
        # Les listes de ce test quittent la production : un homonyme d'un
        # autre module les rencontrerait sinon.
        if _LISTES:
            from fiskr.settings import bump_watchlist_epoch
            session.query(WatchlistEntity).filter(
                WatchlistEntity.snapshot_id.in_(_LISTES)).delete(synchronize_session=False)
            session.query(Snapshot).filter(
                Snapshot.snapshot_id.in_(_LISTES)).delete(synchronize_session=False)
            bump_watchlist_epoch(session)
        session.commit()
        session.close()
        app.dependency_overrides.pop(get_current_user, None)


def _ingest(client, file_type, corps, nom):
    r = client.post("/api/ingest", data={"file_type": file_type},
                    files={"file": (f"{nom}_{uuid.uuid4().hex[:6]}.csv", corps, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _alertes_de(client_id):
    from fiskr.database import get_db
    session = next(get_db())
    try:
        return session.query(Alert).filter(Alert.client_id == client_id).order_by(Alert.id).all()
    finally:
        session.close()


def test_le_fichier_des_parties_decrit_la_chaine_et_l_ecran_la_montre(client):
    """De bout en bout, par les portes de l'exploitant : la colonne `partie_de`
    du fichier des parties liées (aperçu compris) place la détention de X dans
    HOLDCO ; l'alerte d'ACME le dit, la confirmation ouvre le GEL d'ACME, la vue
    360 et l'alerte montrent la chaîne. Le webhook refuse une chaîne qui ne
    tient pas debout (422) et n'écrit rien."""
    tag = uuid.uuid4().hex[:6].upper()
    acme = f"test_detention_ACME_{tag}"
    corps = ("entity_id,entity_type,primary_name,nationality,dob\n"
             f"DI-ZOL-{tag},I,Vsevolod Zolotarev{tag},RU,1954-01-27\n")
    _LISTES.append(_ingest(client, "WATCHLIST_EU", corps, "test_detention_wl")["snapshot_id"])
    _ingest(client, "CLIENT_BASE", "client_id,client_type,client_company_name\n"
            f"{acme},PM,ACME CHAINE {tag}\n", "test_detention_cb")
    fichier = ("client_id,partie_id,role,client_type,client_company_name,client_first_name,"
               "client_last_name,client_dob,nationality,pourcentage_detention,partie_de\n"
               f"{acme},HOLDCO,associe,PM,HOLDCO {tag} SARL,,,,,60,\n"
               f"{acme},UBO-X,associe,PP,,Vsevolod,Zolotarev{tag},1954-01-27,RU,70,HOLDCO\n")
    apercu = client.post("/api/ingest/preview", data={"file_type": "CLIENT_PARTIES"},
                         files={"file": ("parties.csv", fichier, "text/csv")}).json()
    assert apercu["acceptees"] == 2, apercu
    assert "partie_de" in apercu["champs_attendus"]
    assert apercu["lignes"][1]["compris"]["partie_de"] == "HOLDCO"
    _ingest(client, "CLIENT_PARTIES", fichier, "test_detention_pl")

    [alerte] = _alertes_de(acme)
    assert alerte.via_partie == (f"via l'associé Vsevolod Zolotarev{tag} (70 %) de HOLDCO {tag} "
                                 f"SARL, associé (60 %) du client ACME CHAINE {tag}")
    r = client.post(f"/api/alerts/{alerte.id}/propose",
                    json={"decision": "CONFIRMED", "comment": "Identité alignée, pièces au dossier."})
    assert r.status_code == 200, r.text
    [mesure] = client.get(f"/api/gels?q={acme}").json()["items"]
    assert mesure["type_action"] == "GEL" and mesure["fondement"] == parties.FONDEMENT_DETENTION
    assert mesure["detention"]["chaines"][0]["texte"] == (
        f"ACME CHAINE {tag} ← 60 % HOLDCO {tag} SARL ← 70 % Vsevolod Zolotarev{tag} "
        f"(personne désignée)")

    vue = client.get(f"/api/clients/{acme}/overview").json()
    assert vue["detention"]["reputee"] is True and vue["detention"]["cumul_indirect_pct"] == 60.0
    assert vue["detention"]["mesure_en_vigueur"] == mesure["id"]
    assert [(s["niveau"], s["nom"], s["de"]) for s in vue["detention"]["structure"]] == [
        (1, f"HOLDCO {tag} SARL", f"ACME CHAINE {tag}"),
        (2, f"Vsevolod Zolotarev{tag}", f"HOLDCO {tag} SARL")]
    assert {p["partie_id"]: p["partie_de"] for p in vue["parties"]} == {"HOLDCO": None, "UBO-X": "HOLDCO"}
    detail = client.get(f"/api/alerts/{alerte.id}").json()
    assert detail["detention"]["chaines"][0]["compte"] is True
    assert detail["gel_doute_possible"] is False

    # Webhook : une chaîne suspendue à une société absente du jeu → 422, rien d'écrit
    from fiskr.api import app
    from fiskr.auth import get_current_user
    cle = client.post("/api/apikeys", json={"name": f"test_detention_{tag}", "role": "user"}).json()["api_key"]
    sauve = app.dependency_overrides.pop(get_current_user, None)
    try:
        r = client.post("/api/hooks/client-upsert", json={
            "client_id": acme, "client_type": "PM", "client_company_name": f"ACME CHAINE {tag}",
            "parties": [{"partie_id": "UBO-X", "roles": ["ASSOCIE"], "pourcentage_detention": 70,
                         "partie_de": "HOLDCO-ABSENTE",
                         "identite": {"client_type": "PP", "client_first_name": "Vsevolod",
                                      "client_last_name": f"Zolotarev{tag}"}}]},
            headers={"X-API-Key": cle})
    finally:
        if sauve is not None:
            app.dependency_overrides[get_current_user] = sauve
    assert r.status_code == 422 and "absente du jeu" in r.text, r.text
    vue = client.get(f"/api/clients/{acme}/overview").json()
    assert {p["partie_id"] for p in vue["parties"]} == {"HOLDCO", "UBO-X"}

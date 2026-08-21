"""
Le mode delta doit rendre EXACTEMENT ce que rendrait une passe complète.

Le cahier de tests crible de deux façons. En mode **complet**, une passe sur
l'univers de production, une sur l'univers candidat. En mode **delta** (le
défaut), une passe sur l'univers *partagé* — tout ce qui ne bouge pas — puis
deux passes minuscules sur les fiches retirées et ajoutées. Le second est
beaucoup plus rapide, et c'est celui qui tourne en production.

Les deux doivent donner les mêmes chiffres, sinon l'écart qui décide de
l'homologation dépend du chemin de calcul plutôt que des listes.

Ils ne les donnaient pas. Un client touché **à la fois** par une fiche
inchangée et par une fiche du delta recevait un sort dans chaque passe, et les
passes s'additionnaient : le client était compté deux fois. Sur un panel d'un
seul client, le rapport annonçait **200 % de taux d'interception** — et
`gap_pct`, la porte d'homologation, se calculait sur ce compte-là.

La correction tient en une phrase : **un client, un sort** — celui de sa
meilleure correspondance, exactement ce qu'une passe unique aurait retenu.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.backtest import (_dry_run_screen, _universe_snapshot_ids,
                            reset_shared_pass_memo, run_backtest)
from fiskr.database import (get_db, AppSetting, ClientEntity, Snapshot,
                            WatchlistEntity)
from fiskr.rescreen import _entity_dicts
from fiskr.settings import SETTING_REQUIRE_APPROVAL

TAG = uuid.uuid4().hex[:6].upper()

ENTETE_LISTE = "entity_id,entity_type,primary_name,nationality,dob\n"
ENTETE_PANEL = ("client_id,client_type,client_first_name,client_last_name,"
                "client_dob,client_gender,nationality\n")
# Les listes OpenSanctions (EBRD, ADB, IADB...) ont leur propre en-tete.
ENTETE_OS = "id,schema,name,aliases,birth_date,countries\n"


@pytest.fixture()
def contexte():
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "testeur", "full_name": "Testeur",
        "role": "admin", "roles": ["admin"],
    }
    reset_shared_pass_memo()
    session = next(get_db())
    yield session, TestClient(app)
    app.dependency_overrides.clear()
    reset_shared_pass_memo()
    for modele, colonne in ((WatchlistEntity, WatchlistEntity.entity_id),
                            (ClientEntity, ClientEntity.client_id)):
        session.query(modele).filter(colonne.like(f"%{TAG}%")).delete(
            synchronize_session=False)
    session.query(Snapshot).filter(
        Snapshot.file_name.like(f"%{TAG}%")).delete(synchronize_session=False)
    session.query(AppSetting).filter(
        AppSetting.key == SETTING_REQUIRE_APPROVAL).delete(synchronize_session=False)
    session.commit()
    session.close()


def _ingest(client, file_type, corps, nom):
    r = client.post("/api/ingest", data={"file_type": file_type},
                    files={"file": (nom, corps, "text/csv")})
    assert r.status_code == 200, r.text
    return r.json()


def _scenario_doublon(session, c):
    """
    Le cas réel : une liste officielle porte le même individu deux fois (deux
    programmes, deux identifiants). La production en connaît une, la version
    candidate ajoute l'autre. Le client est touché par les deux — l'une venant
    de la passe partagée, l'autre de la passe des fiches ajoutées.
    """
    c.put("/api/settings/ingestion", json={"require_approval": False})
    _ingest(c, "WATCHLIST_EU",
            ENTETE_LISTE + f"EU-{TAG}-1,I,Ivan Petrov{TAG},RU,1960-05-05\n",
            f"eu_prod_{TAG}.csv")

    c.put("/api/settings/ingestion", json={"require_approval": True})
    candidat = _ingest(c, "WATCHLIST_EU",
                       ENTETE_LISTE
                       + f"EU-{TAG}-1,I,Ivan Petrov{TAG},RU,1960-05-05\n"
                       + f"EU-{TAG}-2,I,Ivan Petrov{TAG},RU,1960-05-05\n",
                       f"eu_cand_{TAG}.csv")
    panel = _ingest(c, "CLIENT_BASE",
                    ENTETE_PANEL
                    + f"CLI-{TAG}-A,PP,Ivan,Petrov{TAG},1960-05-05,M,RU\n",
                    f"clients_{TAG}.csv")
    snap = session.query(Snapshot).filter(
        Snapshot.snapshot_id == candidat["snapshot_id"]).one()
    return snap, panel["snapshot_id"]


def test_le_taux_d_interception_ne_depasse_jamais_cent_pour_cent(contexte):
    """Le symptôme visible, et le plus difficile à défendre devant un
    contrôleur : plus de clients interceptés qu'il n'y a de clients."""
    session, c = contexte
    with c:
        snap, panel_id = _scenario_doublon(session, c)
        rapport = run_backtest(session, [snap], panel_id,
                               threshold_pct=100.0, executed_by="testeur")

    assert rapport["mode"] == "delta", "ce test doit porter sur le mode delta"
    assert rapport["panel_size"] == 1
    for cote in ("current", "candidate"):
        assert rapport[cote]["alerts"] <= rapport["panel_size"], (
            f"{cote} : {rapport[cote]['alerts']} client(s) intercepté(s) pour "
            f"{rapport['panel_size']} au panel")
        assert rapport[cote]["interception_rate_pct"] <= 100.0, rapport[cote]


def test_le_mode_delta_rend_les_memes_chiffres_qu_une_passe_unique(contexte):
    """
    L'égalité de fond. L'univers candidat criblé EN UNE PASSE est la référence :
    c'est ce que fait le mode complet, et c'est ce que fera la production.
    """
    session, c = contexte
    with c:
        snap, panel_id = _scenario_doublon(session, c)
        rapport = run_backtest(session, [snap], panel_id,
                               threshold_pct=100.0, executed_by="testeur")
        _, candidate_ids = _universe_snapshot_ids(session, [snap])
        unique = _dry_run_screen(session, None, _entity_dicts(session, candidate_ids),
                                 rule_set=[], panel_snapshot_id=panel_id)

    cote = rapport["candidate"]
    assert cote["alerts"] == unique["alerts"], (
        f"clients interceptés : delta={cote['alerts']} passe unique={unique['alerts']}")
    assert cote["hits"] == unique["hits"], (
        f"correspondances : delta={cote['hits']} passe unique={unique['hits']}")
    assert cote["alerts_before_rules"] == unique["alerts_before_rules"]
    assert cote["whitelisted_suppressed"] == unique["whitelisted_suppressed"]
    assert cote["rule_suppressed"] == unique["rule_suppressed"]


def test_un_client_deja_intercepte_n_est_pas_une_interception_nouvelle(contexte):
    """
    Le doublon ajouté n'intercepte AUCUN client de plus — celui-là l'était
    déjà. Il ajoute en revanche une correspondance, donc une alerte de plus à
    traiter. Confondre les deux, c'est annoncer au réviseur une couverture
    nouvelle là où il n'y a que du volume.
    """
    session, c = contexte
    with c:
        snap, panel_id = _scenario_doublon(session, c)
        rapport = run_backtest(session, [snap], panel_id,
                               threshold_pct=100.0, executed_by="testeur")

    ligne = next(s for s in rapport["snapshots"] if s["file_type"] == "WATCHLIST_EU")
    assert ligne["new_pairs_count"] == 0, "aucun client nouvellement intercepté"
    assert ligne["hits_delta"] == 1, "une correspondance de plus, donc une alerte de plus"
    assert rapport["candidate"]["hits"] == rapport["current"]["hits"] + 1


def test_les_correspondances_sont_ventilees_par_liste(contexte):
    """
    Un cahier consolidé couvre plusieurs deltas. Le volume d'alertes que chaque
    liste va ouvrir doit se lire liste par liste — c'est la question que pose
    un réviseur, et un total global n'y répond pas.
    """
    session, c = contexte
    with c:
        c.put("/api/settings/ingestion", json={"require_approval": False})
        _ingest(c, "WATCHLIST_EU",
                ENTETE_LISTE + f"EU-{TAG}-S,I,Socle Eu{TAG},RU,1960-05-05\n",
                f"eu_prod_{TAG}.csv")
        _ingest(c, "WATCHLIST_EBRD",
                ENTETE_OS + f"eb-{TAG}-S,person,Socle Ebrd{TAG},,1962-06-06,fr\n",
                f"eb_prod_{TAG}.csv")

        c.put("/api/settings/ingestion", json={"require_approval": True})
        eu = _ingest(c, "WATCHLIST_EU",
                     ENTETE_LISTE
                     + f"EU-{TAG}-S,I,Socle Eu{TAG},RU,1960-05-05\n"
                     + f"EU-{TAG}-N,I,Igor Neufeu{TAG},RU,1971-02-02\n",
                     f"eu_cand_{TAG}.csv")
        gb = _ingest(c, "WATCHLIST_EBRD",
                     ENTETE_OS
                     + f"eb-{TAG}-S,person,Socle Ebrd{TAG},,1962-06-06,fr\n"
                     + f"eb-{TAG}-N,person,Marc Neufebrd{TAG},,1975-03-03,fr\n",
                     f"eb_cand_{TAG}.csv")
        panel = _ingest(c, "CLIENT_BASE",
                        ENTETE_PANEL
                        + f"CLI-{TAG}-A,PP,Igor,Neufeu{TAG},1971-02-02,M,RU\n"
                        + f"CLI-{TAG}-B,PP,Marc,Neufebrd{TAG},1975-03-03,M,FR\n",
                        f"clients_{TAG}.csv")
        snaps = session.query(Snapshot).filter(Snapshot.snapshot_id.in_(
            [eu["snapshot_id"], gb["snapshot_id"]])).all()
        rapport = run_backtest(session, snaps, panel["snapshot_id"],
                               threshold_pct=100.0, executed_by="testeur")

    par_liste = {s["file_type"]: s for s in rapport["snapshots"]}
    assert set(par_liste) == {"WATCHLIST_EU", "WATCHLIST_EBRD"}
    for file_type in par_liste:
        assert par_liste[file_type]["hits_delta"] == 1, (
            f"{file_type} ajoute une correspondance et une seule", rapport["snapshots"])

    # La ventilation redonne le total : rien n'est perdu, rien n'est double.
    for cote in ("current", "candidate"):
        assert sum(rapport[cote]["hits_by_list"].values()) == rapport[cote]["hits"], (
            cote, rapport[cote])


def test_la_fusion_ne_modifie_pas_les_passes_qu_elle_reunit():
    """
    La passe partagée est MÉMORISÉE d'un cahier à l'autre : si la fusion la
    modifiait, le deuxième cahier d'une vague partirait d'un état pollué par le
    premier — et personne ne le verrait, les chiffres restant plausibles.
    """
    from fiskr import screenpool

    def _passe(client_id, entity_id, score, categorie="alert"):
        agg = screenpool.new_partial()
        screenpool.apply_outcome(agg, (categorie, {
            "client_id": client_id, "entity_id": entity_id, "score": score,
            "client_name": client_id, "entity_name": entity_id,
            "list_type": "WATCHLIST_EU", "hits": 1,
            "hits_by_list": {"WATCHLIST_EU": 1}}))
        return screenpool.finalize(agg)

    partagee = _passe("C-1", "E-partagee", 88.0)
    delta = _passe("C-1", "E-delta", 95.0)
    avant = (dict(partagee["par_client"]), partagee["hits"], partagee["alerts"])

    fusion = screenpool.merge_partials([partagee, delta])

    assert (dict(partagee["par_client"]), partagee["hits"], partagee["alerts"]) == avant
    assert fusion["alerts"] == 1, "un client, un sort"
    assert fusion["hits"] == 2, "deux correspondances distinctes"
    retenu = next(iter(fusion["pairs"].values()))
    assert retenu["entity_id"] == "E-delta", "la meilleure correspondance l'emporte"


def test_la_meilleure_correspondance_l_emporte_quel_que_soit_l_ordre():
    """
    Une passe unique retient le meilleur score. La fusion doit faire de même
    sans dépendre de l'ordre dans lequel les passes lui arrivent — sinon deux
    exécutions identiques rendraient deux rapports différents.
    """
    from fiskr import screenpool

    def _sort(entity_id, score, categorie="alert"):
        agg = screenpool.new_partial()
        screenpool.apply_outcome(agg, (categorie, {
            "client_id": "C-1", "entity_id": entity_id, "score": score,
            "client_name": "C-1", "entity_name": entity_id,
            "list_type": "WATCHLIST_EU", "hits": 1,
            "hits_by_list": {"WATCHLIST_EU": 1}}))
        return screenpool.finalize(agg)

    faible, fort = _sort("E-faible", 71.0), _sort("E-fort", 93.0)
    for passes in ((faible, fort), (fort, faible)):
        fusion = screenpool.merge_partials(list(passes))
        assert fusion["alerts"] == 1
        assert next(iter(fusion["pairs"].values()))["entity_id"] == "E-fort"


def test_un_sort_de_liste_blanche_porte_l_identite_de_son_client():
    """
    Sans `client_id`, une mise en liste blanche ne peut pas être rapprochée du
    sort que l'autre passe a donné au même client : elle s'ajoutait au total au
    lieu de le disputer. C'est la garde qui empêche ce retour en arrière.
    """
    import inspect

    from fiskr import screenpool

    code = inspect.getsource(screenpool.screen_one)
    blanche = code[code.index("whitelist_keys:"):]
    assert '"client_id"' in blanche and '"score"' in blanche, (
        "le sort « liste blanche » doit porter le client et son score")

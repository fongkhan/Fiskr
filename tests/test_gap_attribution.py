"""
Attribution de l'écart, liste par liste, dans un cahier consolidé.

Un cahier consolidé rend UN écart pour toutes les listes testées. Constaté en
production après la première vague : verdict WARN, écart 13,69 %, dix-neuf
listes couvertes — et rien pour dire laquelle en était responsable. Un cahier
par liste l'attribuait de lui-même ; la consolidation a fait perdre cette
information.

Chaque paire (client × listé) porte le type de liste de l'entité qui l'a
déclenchée : le compte est donc EXACT, jamais une estimation. Ces tests
verrouillent cette exactitude, et le fait que les mouvements imputables à
aucune liste testée soient rendus à part plutôt que tus.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.backtest import reset_shared_pass_memo, run_backtest
from fiskr.database import (get_db, AppSetting, ClientEntity, Snapshot,
                            WatchlistEntity)
from fiskr.settings import SETTING_REQUIRE_APPROVAL

TAG = uuid.uuid4().hex[:6].upper()


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


def test_each_list_carries_its_own_share_of_the_gap(contexte):
    """Deux listes ajoutent chacune UN listé qui touche un client du panel.
    Le rapport consolidé doit attribuer une paire nouvelle à CHACUNE, et non
    deux à l'ensemble sans dire d'où elles viennent."""
    session, c = contexte
    with c:
        def ingest(file_type, corps, nom):
            r = c.post("/api/ingest", data={"file_type": file_type},
                       files={"file": (nom, corps, "text/csv")})
            assert r.status_code == 200, r.text
            return r.json()

        entete = "entity_id,entity_type,primary_name,nationality,dob\n"
        entete_os = "id,schema,name,aliases,birth_date,countries\n"

        assert c.put("/api/settings/ingestion",
                     json={"require_approval": False}).status_code == 200
        ingest("WATCHLIST_EU", entete + f"EU-{TAG}-1,I,Socle Eu{TAG},RU,1960-05-05\n",
               f"eu_prod_{TAG}.csv")
        ingest("WATCHLIST_EBRD",
               entete_os + f"eb-{TAG}-1,person,Socle Ebrd{TAG},,1962-06-06,fr\n",
               f"eb_prod_{TAG}.csv")

        assert c.put("/api/settings/ingestion",
                     json={"require_approval": True}).status_code == 200
        eu = ingest("WATCHLIST_EU",
                    entete
                    + f"EU-{TAG}-1,I,Socle Eu{TAG},RU,1960-05-05\n"
                    + f"EU-{TAG}-2,I,Igor Neufeu{TAG},RU,1971-02-02\n",
                    f"eu_cand_{TAG}.csv")
        eb = ingest("WATCHLIST_EBRD",
                    entete_os
                    + f"eb-{TAG}-1,person,Socle Ebrd{TAG},,1962-06-06,fr\n"
                    + f"eb-{TAG}-2,person,Marc Neufebrd{TAG},,1975-03-03,fr\n",
                    f"eb_cand_{TAG}.csv")

        panel = ingest(
            "CLIENT_BASE",
            "client_id,client_type,client_first_name,client_last_name,"
            "client_dob,client_gender,nationality\n"
            f"CLI-{TAG}-A,PP,Igor,Neufeu{TAG},1971-02-02,M,RU\n"
            f"CLI-{TAG}-B,PP,Marc,Neufebrd{TAG},1975-03-03,M,FR\n",
            f"clients_{TAG}.csv")

        snaps = session.query(Snapshot).filter(Snapshot.snapshot_id.in_(
            [eu["snapshot_id"], eb["snapshot_id"]])).all()
        rapport = run_backtest(session, snaps, panel["snapshot_id"],
                               threshold_pct=100.0, executed_by="testeur")

    par_liste = {s["file_type"]: s for s in rapport["snapshots"]}
    assert set(par_liste) == {"WATCHLIST_EU", "WATCHLIST_EBRD"}
    # Chaque liste porte SA paire nouvelle
    assert par_liste["WATCHLIST_EU"]["new_pairs_count"] == 1, rapport["snapshots"]
    assert par_liste["WATCHLIST_EBRD"]["new_pairs_count"] == 1, rapport["snapshots"]
    # Et la somme des attributions redonne le total global : rien n'est perdu
    total_attribue = sum(s["new_pairs_count"] for s in rapport["snapshots"])
    total_attribue += rapport["unattributed_pairs"]["new_pairs_count"]
    assert total_attribue == rapport["new_pairs_count"], rapport


def test_attribution_sums_back_to_the_global_counts(contexte):
    """Garde-fou d'exactitude : la somme des parts, y compris les mouvements
    hors périmètre, doit égaler les totaux du rapport — sinon l'attribution
    ment par omission."""
    session, c = contexte
    with c:
        entete = "entity_id,entity_type,primary_name,nationality,dob\n"
        assert c.put("/api/settings/ingestion",
                     json={"require_approval": False}).status_code == 200
        r = c.post("/api/ingest", data={"file_type": "WATCHLIST_EU"},
                   files={"file": (f"solo_prod_{TAG}.csv",
                                   entete + f"EU-{TAG}-9,I,Solo Base{TAG},RU,1950-01-01\n",
                                   "text/csv")})
        assert r.status_code == 200
        assert c.put("/api/settings/ingestion",
                     json={"require_approval": True}).status_code == 200
        cand = c.post("/api/ingest", data={"file_type": "WATCHLIST_EU"},
                      files={"file": (f"solo_cand_{TAG}.csv",
                                      entete
                                      + f"EU-{TAG}-9,I,Solo Base{TAG},RU,1950-01-01\n"
                                      + f"EU-{TAG}-8,I,Ajout Solo{TAG},RU,1980-08-08\n",
                                      "text/csv")}).json()
        panel = c.post("/api/ingest", data={"file_type": "CLIENT_BASE"},
                       files={"file": (f"solo_cli_{TAG}.csv",
                                       "client_id,client_type,client_first_name,"
                                       "client_last_name,client_dob,client_gender,nationality\n"
                                       f"CLI-{TAG}-S,PP,Ajout,Solo{TAG},1980-08-08,M,RU\n",
                                       "text/csv")}).json()

        snap = session.query(Snapshot).filter(
            Snapshot.snapshot_id == cand["snapshot_id"]).first()
        rapport = run_backtest(session, snap, panel["snapshot_id"],
                               threshold_pct=100.0, executed_by="testeur")

    for cle in ("new_pairs_count", "resolved_pairs_count"):
        somme = sum(s[cle] for s in rapport["snapshots"]) \
            + rapport["unattributed_pairs"][cle]
        assert somme == rapport[cle], f"{cle} : {somme} attribué(s) pour {rapport[cle]}"


def test_single_list_backtest_still_attributes(contexte):
    """Un cahier sur UNE liste attribue tout à cette liste : l'attribution
    n'est pas réservée au mode consolidé."""
    session, c = contexte
    with c:
        entete = "entity_id,entity_type,primary_name,nationality,dob\n"
        assert c.put("/api/settings/ingestion",
                     json={"require_approval": True}).status_code == 200
        cand = c.post("/api/ingest", data={"file_type": "WATCHLIST_EU"},
                      files={"file": (f"uni_{TAG}.csv",
                                      entete + f"EU-{TAG}-7,I,Premier Import{TAG},RU,1955-05-05\n",
                                      "text/csv")}).json()
        panel = c.post("/api/ingest", data={"file_type": "CLIENT_BASE"},
                       files={"file": (f"uni_cli_{TAG}.csv",
                                       "client_id,client_type,client_first_name,"
                                       "client_last_name,client_dob,client_gender,nationality\n"
                                       f"CLI-{TAG}-U,PP,Premier,Import{TAG},1955-05-05,M,RU\n",
                                       "text/csv")}).json()
        snap = session.query(Snapshot).filter(
            Snapshot.snapshot_id == cand["snapshot_id"]).first()
        rapport = run_backtest(session, snap, panel["snapshot_id"],
                               threshold_pct=100.0, executed_by="testeur")

    assert len(rapport["snapshots"]) == 1
    ligne = rapport["snapshots"][0]
    assert ligne["file_type"] == "WATCHLIST_EU"
    assert ligne["new_pairs_count"] == rapport["new_pairs_count"]
    assert rapport["unattributed_pairs"]["new_pairs_count"] == 0

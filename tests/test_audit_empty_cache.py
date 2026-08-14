"""
Recensement des décisions rendues sur un cache moteur VIDE.

Sous Passenger, un processus web n'a jamais chargé son cache (le `lifespan`
ASGI n'y tourne pas) : `watchlist_index` restait vide, aucun candidat n'en
sortait, et le criblage rendait NO_MATCH — un listé déclaré non listé.

Le journal d'audit a tout gardé. Une telle décision porte une signature nette :
`watchlist_hash = 'N/A'` (la valeur initiale du module, jamais remplacée par le
hash du snapshot en production). Ces tests verrouillent la détection : ni faux
positif sur les décisions saines, ni silence sur celles à re-cribler.
"""
import importlib
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text


def _outil():
    return importlib.import_module("tools.audit_empty_cache_decisions")


@pytest.fixture
def journal(tmp_path):
    """Un journal d'audit minimal : 2 décisions saines, 3 sur cache vide."""
    eng = create_engine(f"sqlite:///{tmp_path / 'audit.sqlite3'}")
    base = datetime(2026, 8, 12, 9, 0, 0)
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE compliance_audit_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                client_id VARCHAR(100),
                client_name VARCHAR(1000) NOT NULL,
                client_type VARCHAR(10) NOT NULL,
                watchlist_id VARCHAR(100) NOT NULL,
                watchlist_name VARCHAR(1000) NOT NULL,
                base_score FLOAT NOT NULL,
                final_score FLOAT NOT NULL,
                status VARCHAR(20) NOT NULL,
                decision_tree JSON NOT NULL,
                config_state JSON NOT NULL,
                watchlist_version VARCHAR(50) NOT NULL,
                watchlist_hash VARCHAR(64) NOT NULL
            )"""))

        def ligne(n, client, wl_id, statut, hash_, minutes):
            conn.execute(text(
                "INSERT INTO compliance_audit_trail (timestamp, client_id,"
                " client_name, client_type, watchlist_id, watchlist_name,"
                " base_score, final_score, status, decision_tree, config_state,"
                " watchlist_version, watchlist_hash) VALUES (:t,:ci,:cn,'PP',"
                " :wi,'x',0,0,:st,'{}','{}','v',:h)"),
                {"t": base + timedelta(minutes=minutes), "ci": client,
                 "cn": f"Client {n}", "wi": wl_id, "st": statut, "h": hash_})

        # Saines : cache chargé, hash réel du snapshot
        ligne(1, "C1", "OFAC-1", "ALERT", "a" * 64, 0)
        ligne(2, "C2", "NONE", "NO_MATCH", "a" * 64, 5)
        # Rendues sur un cache vide : le défaut à recenser
        ligne(3, "C3", "NONE", "NO_MATCH", "N/A", 10)
        ligne(4, "C4", "NONE", "NO_MATCH", "N/A", 15)
        ligne(5, "C3", "NONE", "NO_MATCH", "N/A", 20)
    yield eng, base
    eng.dispose()


def test_isolates_only_the_decisions_made_without_lists(journal):
    """Les décisions saines ne doivent JAMAIS être signalées : elles portent le
    hash réel du snapshot en production."""
    eng, _ = journal
    rapport = _outil().scan(eng)
    assert rapport["total"] == 3
    assert rapport["total_global"] == 5
    hashes = {r.watchlist_id for r in rapport["rows"]}
    assert hashes == {"NONE"}
    assert {r.client_id for r in rapport["rows"]} == {"C3", "C4"}


def test_reports_the_period_to_investigate(journal):
    """La période encadre l'incident : c'est elle qui dit quels criblages
    rejouer."""
    eng, base = journal
    debut, fin = _outil().scan(eng)["bornes"]
    assert str(base + timedelta(minutes=10)) in str(debut)
    assert str(base + timedelta(minutes=20)) in str(fin)


def test_healthy_journal_reports_nothing(tmp_path):
    """Un journal sain ne déclenche aucune alerte (pas de faux positif)."""
    eng = create_engine(f"sqlite:///{tmp_path / 'sain.sqlite3'}")
    with eng.begin() as conn:
        conn.execute(text("""
            CREATE TABLE compliance_audit_trail (
                id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME,
                client_id VARCHAR(100), client_name VARCHAR(1000) NOT NULL,
                client_type VARCHAR(10) NOT NULL, watchlist_id VARCHAR(100) NOT NULL,
                watchlist_name VARCHAR(1000) NOT NULL, base_score FLOAT NOT NULL,
                final_score FLOAT NOT NULL, status VARCHAR(20) NOT NULL,
                decision_tree JSON NOT NULL, config_state JSON NOT NULL,
                watchlist_version VARCHAR(50) NOT NULL, watchlist_hash VARCHAR(64) NOT NULL
            )"""))
        conn.execute(text(
            "INSERT INTO compliance_audit_trail (timestamp, client_id, client_name,"
            " client_type, watchlist_id, watchlist_name, base_score, final_score,"
            " status, decision_tree, config_state, watchlist_version, watchlist_hash)"
            " VALUES (:t,'C1','Client 1','PP','OFAC-1','x',0,0,'ALERT','{}','{}','v',:h)"),
            {"t": datetime(2026, 8, 12, 9, 0, 0), "h": "b" * 64})
    rapport = _outil().scan(eng)
    assert rapport["total"] == 0 and rapport["rows"] == []
    eng.dispose()


def test_sample_is_bounded(journal):
    """L'échantillon détaillé est borné : sur une production volumineuse, on ne
    rapatrie pas le journal entier."""
    eng, _ = journal
    rapport = _outil().scan(eng, limit=2)
    assert rapport["total"] == 3, "le TOTAL reste exact"
    assert len(rapport["rows"]) == 2, "seul le détail est borné"


def test_tool_never_writes_and_never_migrates():
    """Garde-fou : un outil d'audit lit, point. Aucun appel destructeur ni
    migration — contrôle sur l'arbre syntaxique, pas sur la prose."""
    import ast
    from pathlib import Path
    arbre = ast.parse(Path("tools/audit_empty_cache_decisions.py").read_text(encoding="utf-8"))
    appels = {n.func.id for n in ast.walk(arbre)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attributs = {n.func.attr for n in ast.walk(arbre)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    interdits = {"init_db", "drop_all", "create_all"}
    assert not (appels & interdits) and not (attributs & interdits)
    # Aucune instruction d'écriture dans les chaînes SQL de l'outil
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Constant) and isinstance(noeud.value, str):
            majuscule = noeud.value.upper()
            if "COMPLIANCE_AUDIT_TRAIL" in majuscule:
                assert majuscule.lstrip().startswith("SELECT"), noeud.value

"""
Mise à niveau du schéma : on AJOUTE, on ne détruit jamais.

Le démarrage portait ceci :

    if "place_of_birth" not in columns:
        Base.metadata.drop_all(bind=engine)      # TOUTES les tables

Une seule colonne NULLABLE absente — l'écart qu'une migration additive règle
en une seconde — et l'application effaçait l'intégralité de la base au
démarrage : listes homologuées, alertes, journal d'audit immuable. Constaté en
conditions réelles : 2,79 millions de fiches perdues sur un simple démarrage.

Ces tests verrouillent la garantie inverse : **aucune donnée n'est jamais
détruite par une mise à niveau de schéma.**
"""
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text

from fiskr import database
from fiskr.database import Base, _add_missing_nullable_columns


def _table_avec_colonnes_manquantes(engine, retirees):
    """Crée `watchlist_entities` SANS les colonnes données, avec une ligne."""
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE snapshots (
                snapshot_id VARCHAR(50) PRIMARY KEY,
                file_type VARCHAR(50) NOT NULL,
                file_name VARCHAR(255) NOT NULL,
                file_hash VARCHAR(64) NOT NULL,
                status VARCHAR(20)
            )"""))
        conn.execute(text("""
            CREATE TABLE watchlist_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id VARCHAR(50) NOT NULL,
                entity_id VARCHAR(100) NOT NULL,
                entity_type VARCHAR(10) NOT NULL,
                primary_name VARCHAR(1000) NOT NULL,
                entity_checksum VARCHAR(64) NOT NULL
            )"""))
        conn.execute(text(
            "INSERT INTO snapshots VALUES ('s1','WATCHLIST_EU','f.csv','h1','READY')"))
        conn.execute(text(
            "INSERT INTO watchlist_entities (snapshot_id, entity_id, entity_type,"
            " primary_name, entity_checksum) VALUES ('s1','E1','I','Igor PETROV','c1')"))
    assert retirees  # le scénario n'a de sens qu'avec des colonnes absentes


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'migr.sqlite3'}")
    yield eng
    eng.dispose()


def test_missing_nullable_column_is_added_not_dropped(engine):
    """Le cas EXACT qui déclenchait le drop_all : `place_of_birth` absente.
    La colonne doit être ajoutée et la ligne existante survivre."""
    _table_avec_colonnes_manquantes(engine, ["place_of_birth"])
    with engine.connect() as conn:
        avant = conn.execute(text("SELECT count(*) FROM watchlist_entities")).scalar()
    assert avant == 1

    ajoutees = _add_missing_nullable_columns(engine, inspect(engine))

    colonnes = {c["name"] for c in inspect(engine).get_columns("watchlist_entities")}
    assert "place_of_birth" in colonnes
    assert any("place_of_birth" in a for a in ajoutees)
    # LA garantie : la donnée est toujours là
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM watchlist_entities")).scalar() == 1
        nom = conn.execute(text("SELECT primary_name FROM watchlist_entities")).scalar()
    assert nom == "Igor PETROV"


def test_every_nullable_model_column_is_restored(engine):
    """Une table réduite à ses colonnes d'identité récupère TOUTES les colonnes
    nullables du modèle — sans liste tenue à la main."""
    _table_avec_colonnes_manquantes(engine, ["toutes"])
    _add_missing_nullable_columns(engine, inspect(engine))

    presentes = {c["name"] for c in inspect(engine).get_columns("watchlist_entities")}
    attendues = {c.name for c in Base.metadata.tables["watchlist_entities"].columns
                 if c.nullable or c.default is not None or c.server_default is not None}
    manquantes = attendues - presentes
    assert not manquantes, f"colonnes non restaurées : {sorted(manquantes)}"


def test_not_null_column_is_reported_never_destroyed(engine, caplog):
    """Une colonne NOT NULL sans défaut ne peut pas être ajoutée à une table
    peuplée : elle doit être SIGNALÉE, jamais traitée par une destruction."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE fp_rules (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " name VARCHAR(150) NOT NULL, code TEXT NOT NULL)"))
        conn.execute(text("INSERT INTO fp_rules (name, code) VALUES ('r','pass')"))

    with caplog.at_level("WARNING"):
        _add_missing_nullable_columns(engine, inspect(engine))

    # La table existe toujours, avec sa ligne
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM fp_rules")).scalar() == 1
    assert any("NON NULL" in r.message or "NON NULL" in str(r.msg)
               for r in caplog.records), "l'écart doit être signalé"


def test_startup_never_calls_drop_all():
    """Garde-fou contre la régression : `init_db` ne doit plus contenir aucun
    appel destructeur. Contrôle sur l'arbre syntaxique, pas sur la prose."""
    import ast
    import inspect as _inspect
    source = _inspect.getsource(database.init_db)
    arbre = ast.parse(source.lstrip())
    appels = {n.func.attr for n in ast.walk(arbre)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "drop_all" not in appels
    assert "drop" not in appels


def test_sweep_is_idempotent(engine):
    """Relancé, le balayage n'ajoute plus rien (et ne casse rien)."""
    _table_avec_colonnes_manquantes(engine, ["place_of_birth"])
    premier = _add_missing_nullable_columns(engine, inspect(engine))
    second = _add_missing_nullable_columns(engine, inspect(engine))
    assert premier and not second
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM watchlist_entities")).scalar() == 1

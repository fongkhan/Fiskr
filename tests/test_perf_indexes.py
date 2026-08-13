"""
Index de performance de la consultation des listes.

Constat de production : afficher UNE page de 50 fiches prenait 18 s. Le
périmètre « production » se définit par des colonnes de `snapshots` (statut,
type) et par `excluded`, dont aucune n'était indexée — PostgreSQL lisait donc
la table entière (11,2 M de lignes, 9 Go) pour rendre 50 lignes.

Ces tests verrouillent :
- la déclaration des index (ils ne doivent pas disparaître d'une refonte) ;
- le GARDE-FOU de démarrage : sur une grosse table, un `CREATE INDEX` ordinaire
  verrouille plusieurs minutes — il ne doit JAMAIS partir au démarrage ;
- l'outil sans interruption : `CONCURRENTLY`, idempotent, et surtout il ne
  migre rien (il ne doit pas pouvoir toucher au schéma).
"""
from pathlib import Path

from fiskr import database


def test_browse_indexes_are_declared():
    """Les trois index qui rendent la consultation utilisable sont déclarés."""
    noms = {ix.name for ix in database._PERFORMANCE_INDEXES}
    assert "ix_snapshots_status_type" in noms
    assert "ix_snapshots_uploaded_at" in noms
    assert "ix_wl_entities_production" in noms


def test_production_index_is_partial_on_non_excluded():
    """L'index de production est PARTIEL : il n'indexe que les fiches non
    exclues, c'est-à-dire exactement le périmètre lu — sinon il pèserait pour
    rien les 92 % de fiches hors production."""
    ix = next(i for i in database._PERFORMANCE_INDEXES
              if i.name == "ix_wl_entities_production")
    assert ix.dialect_options["postgresql"]["where"] is not None
    colonnes = [c.name for c in ix.columns]
    assert colonnes == ["snapshot_id", "id"]


def test_row_estimate_never_counts_on_sqlite():
    """L'estimation ne doit jamais faire de COUNT(*) : sur une grosse table il
    coûterait lui-même des secondes AU DÉMARRAGE. Hors PostgreSQL elle rend 0,
    donc le garde-fou ne bloque pas les petites installations."""
    assert database._table_row_estimate(database.engine, "watchlist_entities") == 0


def test_index_exists_helper_is_never_blocking():
    """L'inspection ne doit jamais lever, même sur un moteur absent."""
    assert database._index_exists(None, "ix_inexistant") is False
    assert database._index_exists(database.engine, "ix_inexistant") is False


def test_small_installs_still_get_their_indexes():
    """Une base de développement (SQLite) reste sous le garde-fou : ses index
    sont bien créés au démarrage — c'est le cas de cette suite de tests."""
    assert database.LARGE_TABLE_ROW_GUARD > 0
    estimate = database._table_row_estimate(database.engine, "watchlist_entities")
    assert estimate <= database.LARGE_TABLE_ROW_GUARD


# ------------------ OUTIL SANS INTERRUPTION ------------------

def _tool_source() -> str:
    return Path("tools/create_perf_indexes.py").read_text(encoding="utf-8")


def _tool_sql_literals() -> list:
    """Les chaînes SQL RÉELLES de l'outil — pas la prose des commentaires."""
    import ast
    arbre = ast.parse(_tool_source())
    return [n.value for n in ast.walk(arbre)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "CREATE INDEX" in n.value]


def test_tool_builds_indexes_without_locking_the_table():
    """Toute création passe par CONCURRENTLY (pas de verrou exclusif) et par
    IF NOT EXISTS (relançable sans risque). Vérifié sur les chaînes SQL du
    code, pas sur les commentaires."""
    sqls = _tool_sql_literals()
    assert sqls, "aucune instruction CREATE INDEX trouvée dans l'outil"
    for sql in sqls:
        assert "CONCURRENTLY" in sql, sql
        assert "IF NOT EXISTS" in sql, sql


def test_tool_never_migrates_the_schema():
    """Garde-fou : un outil de performance ne doit pas pouvoir toucher au
    schéma. `init_db()` porte des migrations et peut recréer des tables — il
    ne doit être ni importé ni APPELÉ ici. Contrôle sur l'arbre syntaxique :
    une simple mention en commentaire est légitime, un appel ne l'est pas."""
    import ast
    arbre = ast.parse(_tool_source())
    appels = {n.func.id for n in ast.walk(arbre)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attributs = {n.func.attr for n in ast.walk(arbre)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    interdits = {"init_db", "drop_all", "create_all"}
    assert not (appels & interdits), f"appel interdit : {appels & interdits}"
    assert not (attributs & interdits), f"appel interdit : {attributs & interdits}"
    # …et il ne doit pas non plus l'importer
    importes = {a.name for n in ast.walk(arbre) if isinstance(n, ast.ImportFrom)
                for a in n.names}
    assert "init_db" not in importes


def test_trigram_search_indexes_are_opt_in():
    """Les index trigramme accélèrent la recherche (x73 mesuré) mais coûtent
    ~78 % d'écriture en plus à l'ingestion : ils restent explicitement
    optionnels, jamais créés au démarrage."""
    noms_startup = {ix.name for ix in database._PERFORMANCE_INDEXES}
    for nom, _table, _col in database.TRIGRAM_SEARCH_INDEXES:
        assert nom not in noms_startup, f"{nom} ne doit pas partir au démarrage"
    assert "--search" in _tool_source()

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


def test_les_dates_des_alertes_sont_indexees():
    """
    Depuis qu'un criblage ouvre une alerte PAR CORRESPONDANCE, cette table
    grossit du nombre d'homonymes : un seul « Mohammed Ali » sans pays en
    ajoute 2 976. L'accueil, les exports, les indicateurs et la courbe
    journalière filtrent et trient tous sur `created_at` et `decided_at` —
    aucune des deux n'était indexée, donc chaque chargement de l'accueil
    parcourait la table entière deux fois.
    """
    noms = {ix.name for ix in database._PERFORMANCE_INDEXES}
    assert "ix_alerts_created_at" in noms
    assert "ix_alerts_decided_at" in noms


def test_la_cle_etrangere_vers_le_journal_est_indexee():
    """
    PostgreSQL n'indexe PAS automatiquement le côté RÉFÉRENÇANT d'une clé
    étrangère. Sans index sur `alerts.audit_id`, chaque ligne de
    `compliance_audit_trail` supprimée par la rétention déclenche un parcours
    SÉQUENTIEL de `alerts` pour vérifier l'intégrité référentielle : purger
    100 000 lignes d'audit valait 100 000 parcours d'une table qui se compte
    désormais en millions.
    """
    par_nom = {ix.name: ix for ix in database._PERFORMANCE_INDEXES}
    assert "ix_alerts_audit_id" in par_nom
    assert [c.name for c in par_nom["ix_alerts_audit_id"].columns] == ["audit_id"]
    # La colonne EST bien une clé étrangère : c'est ce qui motive l'index.
    assert database.Alert.__table__.c.audit_id.foreign_keys


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


def _tool_sql_statements() -> list:
    """Les instructions RÉELLEMENT produites par l'outil — plus fiable que
    lire des littéraux, depuis qu'elles sont dérivées du modèle."""
    outil = _outil()
    sqls = [ddl for _nom, ddl in outil.browse_indexes()]
    sqls += [ddl for _nom, ddl in outil._trigram_statements("installed")]
    return sqls


def test_tool_builds_indexes_without_locking_the_table():
    """Toute création passe par CONCURRENTLY (pas de verrou exclusif) et par
    IF NOT EXISTS (relançable sans risque)."""
    sqls = _tool_sql_statements()
    assert sqls, "l'outil ne produit aucune instruction"
    for sql in sqls:
        assert "CONCURRENTLY" in sql, sql
        assert "IF NOT EXISTS" in sql, sql


def test_tool_covers_every_declared_index():
    """LE garde-fou : l'outil doit couvrir TOUS les index du modèle.

    La liste était codée en dur et n'en couvrait que trois sur quinze. Or le
    démarrage diffère tout index manquant sur une table volumineuse et renvoie
    vers cet outil : un index déclaré mais inconnu de l'outil n'aurait JAMAIS
    été créé en production, alors que le journal affirmait le contraire."""
    couverts = {nom for nom, _ in _outil().browse_indexes()}
    declares = {ix.name for ix in database._PERFORMANCE_INDEXES}
    manquants = declares - couverts
    assert not manquants, (
        f"index déclarés que l'outil ne créerait jamais : {sorted(manquants)}")


def test_partial_index_keeps_its_where_clause():
    """L'index partiel perd tout son intérêt sans son WHERE : il porterait
    alors les 92 % de fiches hors production qu'il est censé éviter."""
    ddl = next(d for n, d in _outil().browse_indexes()
               if n == "ix_wl_entities_production")
    # SQLAlchemy compile le littéral en minuscules (« IS NOT true ») : c'est
    # du SQL valide, la casse n'a pas à faire échouer le garde-fou.
    assert "WHERE EXCLUDED IS NOT TRUE" in ddl.upper(), ddl


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


# ------------------ EXTENSION pg_trgm ABSENTE DU SERVEUR ------------------
# Constaté en production (o2switch) : l'extension n'est pas fournie par
# l'hébergeur. L'outil enchaînait alors TROIS échecs SQL bruts, ce qui donne
# l'impression d'une manipulation ratée alors que l'exploitant n'y peut rien.

class _FauxResultat:
    def __init__(self, valeur):
        self._valeur = valeur

    def first(self):
        return self._valeur


class _FausseConnexion:
    def __init__(self, installee, disponible):
        self._reponses = [installee, disponible]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, _stmt):
        return _FauxResultat(self._reponses.pop(0) if self._reponses else None)


class _FauxMoteur:
    def __init__(self, installee=None, disponible=None):
        self._installee, self._disponible = installee, disponible

    def connect(self):
        return _FausseConnexion(self._installee, self._disponible)


def _outil():
    import importlib
    return importlib.import_module("tools.create_perf_indexes")


def test_absent_trigram_extension_is_detected_before_any_attempt():
    """Serveur qui ne fournit pas pg_trgm : constaté AVANT d'agir."""
    outil = _outil()
    assert outil.trigram_support(_FauxMoteur(None, None)) == "absent"
    assert outil.trigram_support(_FauxMoteur(None, 1)) == "available"
    assert outil.trigram_support(_FauxMoteur(1, None)) == "installed"


def test_absent_extension_message_blames_the_host_not_the_operator():
    """Le message doit dire que rien n'a été tenté, que ce n'est pas un réglage
    de l'exploitant, et que les index de consultation sont bien posés."""
    message = _outil().TRIGRAM_ABSENT_MESSAGE
    assert "n'est pas fournie par cet hébergeur" in message
    assert "Ce n'est pas un" in message and "votre côté" in message
    assert "Rien n'a été tenté" in message
    assert "consultation" in message  # rassure sur ce qui, lui, a marché


def test_extension_creation_is_skipped_when_already_installed():
    """Extension déjà installée : on ne réémet pas le CREATE EXTENSION."""
    outil = _outil()
    noms_installee = [n for n, _ in outil._trigram_statements("installed")]
    noms_dispo = [n for n, _ in outil._trigram_statements("available")]
    assert "pg_trgm" not in noms_installee
    assert "pg_trgm" in noms_dispo
    # Les index, eux, sont proposés dans les deux cas
    assert len(noms_installee) == len(outil.TRIGRAM_SEARCH_INDEXES)


def test_trigram_search_indexes_are_opt_in():
    """Les index trigramme accélèrent la recherche (x73 mesuré) mais coûtent
    ~78 % d'écriture en plus à l'ingestion : ils restent explicitement
    optionnels, jamais créés au démarrage."""
    noms_startup = {ix.name for ix in database._PERFORMANCE_INDEXES}
    for nom, _table, _col in database.TRIGRAM_SEARCH_INDEXES:
        assert nom not in noms_startup, f"{nom} ne doit pas partir au démarrage"
    assert "--search" in _tool_source()

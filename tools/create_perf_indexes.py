"""
Crée les index de performance SANS INTERROMPRE LE SERVICE.

Pourquoi cet outil existe
-------------------------
Un `CREATE INDEX` ordinaire prend un verrou EXCLUSIF sur la table pendant
toute sa construction : sur les 11 millions de fiches constatées en
production (9 Go), cela fige lectures ET écritures plusieurs minutes.
PostgreSQL sait construire un index `CONCURRENTLY`, sans ce verrou — mais
c'est incompatible avec une transaction, donc impossible depuis le démarrage
de l'application. D'où cet outil, à lancer **service allumé**.

Ce qu'il crée
-------------
1. Les index de consultation des listes (btree). Sans eux, afficher UNE page
   de 50 fiches lit la table entière : mesuré à 18 s en production.
2. Optionnellement (`--search`) les index trigramme GIN qui rendent la
   recherche plein texte utilisable (54 s → moins d'une seconde). Ils ont un
   coût : ~78 % d'écriture en plus à l'ingestion, et de l'espace disque.
   L'arbitrage vous revient, d'où l'option explicite.

Usage
-----
    python tools/create_perf_indexes.py            # index de consultation
    python tools/create_perf_indexes.py --search   # + recherche plein texte
    python tools/create_perf_indexes.py --dry-run  # montre sans rien faire

Idempotent (`IF NOT EXISTS`) : relançable sans risque. PostgreSQL uniquement —
sur SQLite les index sont créés au démarrage, la question ne se pose pas.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fiskr.database import engine, TRIGRAM_SEARCH_INDEXES  # noqa: E402

# (nom, instruction). L'ordre compte peu : chaque création est indépendante.
BROWSE_INDEXES = [
    ("ix_snapshots_status_type",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_snapshots_status_type "
     "ON snapshots (status, file_type)"),
    ("ix_snapshots_uploaded_at",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_snapshots_uploaded_at "
     "ON snapshots (uploaded_at)"),
    # Index partiel : uniquement les fiches non exclues — le périmètre lu.
    ("ix_wl_entities_production",
     "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_wl_entities_production "
     "ON watchlist_entities (snapshot_id, id) WHERE excluded IS NOT TRUE"),
]


def _open_engine():
    """
    Ouvre une connexion à la base de l'application SANS toucher au schéma.

    L'application ne construit son moteur que dans `init_db()`, qui porte aussi
    les migrations : on ne peut donc pas l'appeler ici. On relit simplement la
    même configuration (`database.url`, surchargeable par DATABASE_URL).
    """
    from sqlalchemy import create_engine
    from fiskr.config import config
    url = ((config.get("database") or {}).get("url") or "").strip()
    if not url.startswith("postgresql"):
        return None
    try:
        return create_engine(url, connect_args={"connect_timeout": 5})
    except Exception as e:
        print(f"Connexion impossible : {e}")
        return None


def trigram_support(live_engine):
    """
    État de l'extension `pg_trgm` : ("installed" | "available" | "absent").

    Elle n'est pas fournie par tous les hébergeurs — sur un mutualisé, le
    fichier de contrôle de l'extension peut tout simplement ne pas exister sur
    le serveur. Sans cette vérification préalable, l'outil enchaînait trois
    échecs SQL bruts (« n'a pas pu ouvrir le fichier de contrôle », puis deux
    fois « la classe d'opérateur gin_trgm_ops n'existe pas »), ce qui donne
    l'impression d'une manipulation ratée alors que rien, côté exploitant, ne
    peut y changer quoi que ce soit. On le constate donc AVANT, et on
    l'explique en une phrase.
    """
    from sqlalchemy import text
    try:
        with live_engine.connect() as conn:
            if conn.execute(text(
                    "SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")).first():
                return "installed"
            if conn.execute(text(
                    "SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm'")).first():
                return "available"
    except Exception:
        # Inspection impossible : on ne bloque pas, la tentative dira la vérité.
        return "available"
    return "absent"


def _trigram_statements(state: str):
    if state != "installed":
        yield ("pg_trgm", "CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for name, table, column in TRIGRAM_SEARCH_INDEXES:
        yield (name,
               f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {name} ON {table} "
               f"USING gin ({column} gin_trgm_ops) WHERE excluded IS NOT TRUE")


TRIGRAM_ABSENT_MESSAGE = """
La recherche plein texte ne peut pas être accélérée sur ce serveur.

  L'extension PostgreSQL « pg_trgm » n'est pas fournie par cet hébergeur :
  son fichier de contrôle est absent de l'installation. Ce n'est pas un
  réglage de votre côté — seul l'hébergeur peut l'ajouter.

  Rien n'a été tenté : les index de consultation ci-dessus, eux, sont bien
  en place et c'est ce qui règle la lenteur d'affichage des listes.

  Options, si la recherche vous gêne :
    • demander l'activation de pg_trgm à l'hébergeur (contrib PostgreSQL) ;
    • sinon, la recherche restera un balayage — aucun index SQL ne peut
      accélérer un « ILIKE %terme% » sans cette extension.
""".rstrip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--search", action="store_true",
                        help="crée aussi les index trigramme de recherche "
                             "(coût : ~78 %% d'écriture en plus à l'ingestion)")
    parser.add_argument("--dry-run", action="store_true",
                        help="affiche les instructions sans les exécuter")
    args = parser.parse_args()

    # Cet outil ne MIGRE RIEN et n'appelle JAMAIS init_db() : il ne fait que
    # creer des index. C'est deliberé — init_db() porte des migrations de
    # schema (et peut recreer des tables) ; un outil de performance n'a rien a
    # y faire. On ouvre donc notre propre connexion, depuis la meme
    # configuration que l'application.
    live_engine = engine if engine is not None else _open_engine()
    if live_engine is None:
        print("Base indisponible : vérifiez `database.url` dans config.yaml "
              "ou la variable DATABASE_URL.")
        return 1
    if live_engine.dialect.name != "postgresql":
        print(f"Moteur « {live_engine.dialect.name} » : les index sont créés au "
              f"démarrage, cet outil ne sert que pour PostgreSQL.")
        return 0

    statements = list(BROWSE_INDEXES)
    trigram_state = None
    if args.search:
        # Constate AVANT d'agir : un hébergeur mutualisé peut simplement ne pas
        # fournir pg_trgm, et l'exploitant n'y peut rien. Mieux vaut une phrase
        # claire que trois échecs SQL en cascade.
        trigram_state = trigram_support(live_engine)
        if trigram_state == "absent":
            args.search = False
        else:
            statements += list(_trigram_statements(trigram_state))

    from sqlalchemy import text
    print(f"{len(statements)} instruction(s) — construction CONCURRENTLY, "
          f"sans verrou exclusif.\n")
    failures = 0
    for name, sql in statements:
        if args.dry_run:
            print(f"  [dry-run] {sql}")
            continue
        print(f"  → {name} …", end="", flush=True)
        started = time.monotonic()
        try:
            # AUTOCOMMIT obligatoire : CONCURRENTLY refuse de tourner dans une
            # transaction. Chaque instruction est donc autonome.
            with live_engine.connect().execution_options(
                    isolation_level="AUTOCOMMIT") as conn:
                conn.execute(text(sql))
            print(f" fait en {time.monotonic() - started:.1f} s")
        except Exception as e:
            failures += 1
            print(f" ÉCHEC : {e}")

    if args.dry_run:
        return 0
    if failures:
        print(f"\n{failures} échec(s). Un index laissé « invalide » par une "
              f"construction interrompue se supprime avec "
              f"DROP INDEX CONCURRENTLY <nom>, puis se relance.")
        return 1
    print("\nTerminé. La consultation des listes doit repasser sous la seconde.")
    if trigram_state == "absent":
        # L'extension manque sur CE serveur : ce n'est pas un échec de
        # l'exploitant, et aucune commande de sa part n'y changera rien.
        print(TRIGRAM_ABSENT_MESSAGE)
    elif not args.search:
        print("La recherche plein texte reste lente : relancez avec --search "
              "si vous acceptez ~78 % d'écriture en plus à l'ingestion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Migration UNIQUE des données SQLite → PostgreSQL, à l'identique (ids compris).

Contexte : quand PostgreSQL est injoignable, Fiskr retombe silencieusement sur
SQLite (`database.fallback_to_sqlite: true`) — toutes les données vivent alors
dans `fiskr.sqlite3`. Le jour où la connexion PostgreSQL est réparée, la base
Pg est VIERGE (juste l'admin re-seedé au premier démarrage) : rien n'est migré
tout seul, et l'API et le démon peuvent se retrouver chacun sur une base
différente. Ce script copie TOUT le contenu SQLite vers PostgreSQL :
toutes les tables du schéma, dans l'ordre des dépendances, lots de 5000,
séquences d'auto-incrément recalées, comptes vérifiés table par table.
Le fichier SQLite n'est JAMAIS modifié.

AVANT DE LANCER :
  1. arrêtez le démon        : pkill -u "$(id -un)" -f 'python[^ ]* -m fiskr\\.worker'
  2. arrêtez l'application   : cPanel → Setup Python App → Stop
  3. sauvegardez le SQLite   : cp fiskr.sqlite3 fiskr.sqlite3.avant-migration

Usage :
  python tools/migrate_sqlite_to_postgres.py                # refuse si la cible n'est pas vide
  python tools/migrate_sqlite_to_postgres.py --wipe-target  # vide d'abord la cible (Pg SEULEMENT)
Options :
  --sqlite CHEMIN   fichier source (défaut : database.sqlite_path de config.yaml)
  --pg-url URL      cible (défaut : database.url de config.yaml, .env interpolé)
  --batch N         taille des lots d'insertion (défaut 5000)

APRÈS MIGRATION (affiché aussi en fin d'exécution) :
  - config.yaml → database.fallback_to_sqlite: false  (plus jamais de repli silencieux)
  - renommez fiskr.sqlite3 (ex. fiskr.sqlite3.migré) pour interdire toute reprise
  - redémarrez l'application ET le démon, puis vérifiez GET /api/diagnostic/jobs :
    system.db_engine doit valoir "postgresql"
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Integer, create_engine, func, select, text  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Migration SQLite -> PostgreSQL (copie fidèle)")
    parser.add_argument("--sqlite", default=None, help="fichier SQLite source")
    parser.add_argument("--pg-url", default=None, help="URL PostgreSQL cible")
    parser.add_argument("--wipe-target", action="store_true",
                        help="vide la cible PostgreSQL avant copie (TRUNCATE)")
    parser.add_argument("--batch", type=int, default=5000)
    args = parser.parse_args()

    from fiskr import database as dbmod

    src_path = Path(args.sqlite or dbmod.sqlite_path)
    if not src_path.is_absolute():
        src_path = Path(__file__).resolve().parents[1] / src_path
    if not src_path.exists():
        sys.exit(f"ERREUR : fichier SQLite introuvable : {src_path}")

    pg_url = args.pg_url or dbmod.pg_url
    if not pg_url.startswith("postgresql"):
        sys.exit("ERREUR : la cible n'est pas une URL PostgreSQL "
                 "(database.url dans config.yaml, variables .env comprises).")

    src = create_engine(f"sqlite:///{src_path}")
    dst = create_engine(pg_url, connect_args={"connect_timeout": 5})
    try:
        with dst.connect():
            pass
    except Exception as exc:
        sys.exit(f"ERreur : PostgreSQL injoignable : {exc}")

    print(f"Source : {src_path}")
    print(f"Cible  : {dst.url.render_as_string(hide_password=True)}")

    # Schema complet cote cible (idempotent)
    dbmod.Base.metadata.create_all(dst)
    tables = dbmod.Base.metadata.sorted_tables  # ordre des dependances (FK)

    # -- La cible doit etre vide (le seed du premier demarrage compte !) --
    with dst.connect() as conn:
        non_empty = [(t.name, conn.execute(select(func.count()).select_from(t)).scalar())
                     for t in tables]
        non_empty = [(n, c) for n, c in non_empty if c]
    if non_empty and not args.wipe_target:
        print("\nLa cible PostgreSQL n'est PAS vide :")
        for name, count in non_empty:
            print(f"  - {name}: {count} ligne(s)")
        sys.exit("\nRelancez avec --wipe-target pour la vider d'abord "
                 "(le fichier SQLite n'est jamais modifié).")
    if non_empty:
        with dst.begin() as conn:
            names = ", ".join(f'"{t.name}"' for t in tables)
            conn.execute(text(f"TRUNCATE TABLE {names} RESTART IDENTITY CASCADE"))
        print("Cible vidée (TRUNCATE … RESTART IDENTITY CASCADE).")

    # -- Copie fidele, table par table, en lots --
    print()
    total = 0
    for t in tables:
        copied = 0
        with src.connect() as sconn, dst.begin() as dconn:
            batch = []
            for row in sconn.execute(select(t)).mappings():
                batch.append(dict(row))
                if len(batch) >= args.batch:
                    dconn.execute(t.insert(), batch)
                    copied += len(batch)
                    batch = []
            if batch:
                dconn.execute(t.insert(), batch)
                copied += len(batch)
        with src.connect() as sconn:
            n_src = sconn.execute(select(func.count()).select_from(t)).scalar()
        with dst.connect() as dconn:
            n_dst = dconn.execute(select(func.count()).select_from(t)).scalar()
        status = "OK " if n_src == n_dst else "ÉCART"
        print(f"  [{status}] {t.name}: {n_dst}/{n_src}")
        if n_src != n_dst:
            sys.exit(f"ERREUR : {t.name} — {n_src} ligne(s) source, {n_dst} copiée(s). "
                     "Migration interrompue, la cible est incomplète.")
        total += n_dst

    # -- Recaler les sequences d'auto-increment (ids preserves) --
    with dst.begin() as conn:
        for t in tables:
            pk = list(t.primary_key.columns)
            if len(pk) != 1 or not isinstance(pk[0].type, Integer):
                continue
            col = pk[0].name
            seq = conn.execute(text("SELECT pg_get_serial_sequence(:t, :c)"),
                               {"t": t.name, "c": col}).scalar()
            if seq:
                conn.execute(text(
                    f'SELECT setval(:s, GREATEST((SELECT COALESCE(MAX("{col}"), 0) '
                    f'FROM "{t.name}"), 1))'), {"s": seq})

    print(f"\nMigration terminée : {total} ligne(s) copiée(s), séquences recalées.")
    print("""
Prochaines étapes :
  1. config.yaml → database.fallback_to_sqlite: false   (échec franc, plus de repli)
  2. mv fiskr.sqlite3 fiskr.sqlite3.migré               (interdit toute reprise SQLite)
  3. redémarrez l'application (cPanel → Restart) ET le démon (bash tools/refresh_prod.sh)
  4. vérifiez GET /api/diagnostic/jobs : system.db_engine == "postgresql"
""")


if __name__ == "__main__":
    main()

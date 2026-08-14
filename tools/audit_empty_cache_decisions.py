"""
Recense les décisions de criblage rendues sur un cache moteur VIDE.

Pourquoi cet outil existe
-------------------------
Sous Passenger, le `lifespan` de FastAPI ne s'exécute pas (a2wsgi.ASGIMiddleware
n'implémente pas ce protocole ASGI). Un processus web n'a donc jamais chargé son
cache moteur : `watchlist_index` restait vide, aucun candidat n'en sortait, et
le criblage rendait NO_MATCH — un listé déclaré non listé, sans erreur.

Le journal d'audit, lui, a tout gardé. Une décision rendue dans un tel processus
porte une signature nette :

    watchlist_hash = 'N/A'          <- valeur initiale du module, jamais chargée
    watchlist_id   = 'NONE'         <- aucun candidat
    status         = 'NO_MATCH'

Un processus dont le cache est chargé écrit toujours le hash réel du snapshot en
production. Le critère est donc `watchlist_hash = 'N/A'` : il isole exactement
les décisions à re-cribler.

Ce que l'outil NE fait PAS
--------------------------
Il ne modifie rien. Ni re-criblage, ni correction du journal — celui-ci est
immuable par construction et le rester est le principe même de la piste
d'audit. Il ne touche pas au schéma et n'appelle jamais `init_db()`.

Usage
-----
    python tools/audit_empty_cache_decisions.py
    python tools/audit_empty_cache_decisions.py --csv clients_a_recribler.csv
    python tools/audit_empty_cache_decisions.py --limit 5000
"""
import argparse
import csv
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fiskr.database import engine  # noqa: E402

# Valeur initiale du module `fiskr.api` : elle ne peut apparaître dans le
# journal que si le cache n'a jamais été chargé dans le processus qui a écrit.
EMPTY_CACHE_HASH = "N/A"


def _open_engine():
    """
    Ouvre une connexion SANS toucher au schéma (même précaution que
    tools/create_perf_indexes.py : `init_db()` porte des migrations, un outil
    de lecture n'a rien à y faire).
    """
    from sqlalchemy import create_engine
    from fiskr.config import config
    url = ((config.get("database") or {}).get("url") or "").strip()
    if not url:
        return None
    # Lecture seule : contrairement aux index CONCURRENTLY, rien ici n'est
    # propre a PostgreSQL — une installation SQLite se relit aussi bien.
    kwargs = {"connect_args": {"connect_timeout": 5}} \
        if url.startswith("postgresql") else {}
    try:
        return create_engine(url, **kwargs)
    except Exception as e:
        print(f"Connexion impossible : {e}")
        return None


def scan(live_engine, limit: int = 2000) -> dict:
    """
    Relit le journal d'audit et isole les décisions rendues sur un cache vide.

    Lecture pure : aucun SELECT n'écrit, et le journal reste immuable. Rend le
    volume total, la période couverte et un échantillon détaillé borné.
    """
    from sqlalchemy import text
    with live_engine.connect() as conn:
        total = conn.execute(text(
            "SELECT count(*) FROM compliance_audit_trail "
            "WHERE watchlist_hash = :h"), {"h": EMPTY_CACHE_HASH}).scalar() or 0
        total_global = conn.execute(text(
            "SELECT count(*) FROM compliance_audit_trail")).scalar() or 0
        if not total:
            return {"total": 0, "total_global": total_global,
                    "bornes": (None, None), "rows": []}
        bornes = conn.execute(text(
            "SELECT min(timestamp), max(timestamp) FROM compliance_audit_trail "
            "WHERE watchlist_hash = :h"), {"h": EMPTY_CACHE_HASH}).first()
        rows = conn.execute(text(
            "SELECT id, timestamp, client_id, client_name, client_type, "
            "       watchlist_id, status "
            "FROM compliance_audit_trail WHERE watchlist_hash = :h "
            "ORDER BY timestamp DESC LIMIT :n"),
            {"h": EMPTY_CACHE_HASH, "n": limit}).fetchall()
    return {"total": total, "total_global": total_global,
            "bornes": tuple(bornes), "rows": list(rows)}


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", metavar="FICHIER",
                        help="exporte les décisions concernées (à re-cribler)")
    parser.add_argument("--limit", type=int, default=2000,
                        help="nombre de lignes détaillées lues (défaut : 2000)")
    args = parser.parse_args()

    live_engine = engine if engine is not None else _open_engine()
    if live_engine is None:
        print("Base indisponible : vérifiez `database.url` dans config.yaml "
              "ou la variable DATABASE_URL.")
        return 1

    # `create_engine` ne se connecte pas : la vraie panne (serveur eteint,
    # identifiants) n'apparait qu'ici. Un message vaut mieux qu'une trace.
    try:
        rapport = scan(live_engine, args.limit)
    except Exception as e:
        print(f"Lecture du journal impossible : "
              f"{str(e).strip().splitlines()[0]}")
        return 1

    total, total_global = rapport["total"], rapport["total_global"]
    rows, bornes = rapport["rows"], rapport["bornes"]
    if not total:
        print(f"Aucune décision rendue sur un cache vide "
              f"({total_global} décisions au journal).")
        print("Rien à re-cribler.")
        return 0

    part = 100.0 * total / total_global if total_global else 0.0
    print(f"{total} décision(s) rendue(s) sur un cache moteur vide "
          f"({part:.1f} % des {total_global} du journal).")
    print(f"Période : du {bornes[0]} au {bornes[1]}.\n")

    statuts = Counter(r.status for r in rows)
    sans_candidat = sum(1 for r in rows if r.watchlist_id == "NONE")
    clients = {r.client_id for r in rows if r.client_id}
    print(f"Sur les {len(rows)} plus récentes :")
    print(f"  statuts          : {dict(statuts)}")
    print(f"  sans candidat    : {sans_candidat}/{len(rows)}")
    print(f"  clients distincts: {len(clients)}")

    # Contrôle de cohérence : sur un index vide, AUCUNE décision ne peut avoir
    # trouvé de candidat. Une exception invaliderait le critère de recherche.
    anomalies = [r for r in rows if r.watchlist_id != "NONE" or r.status != "NO_MATCH"]
    if anomalies:
        print(f"\n  {len(anomalies)} ligne(s) ne suivent pas la signature attendue "
              f"(candidat trouvé malgré un hash « N/A ») — à examiner :")
        for r in anomalies[:5]:
            print(f"    #{r.id} {r.timestamp} {r.client_name} "
                  f"→ {r.watchlist_id} ({r.status})")

    print("\nCes clients doivent être re-criblés : leur décision a été rendue "
          "sans qu'aucune liste ne soit chargée en mémoire.")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["audit_id", "timestamp", "client_id", "client_name",
                             "client_type", "status"])
            for r in rows:
                writer.writerow([r.id, r.timestamp, r.client_id, r.client_name,
                                 r.client_type, r.status])
        print(f"\n{len(rows)} ligne(s) exportée(s) dans {args.csv}.")
        if total > len(rows):
            print(f"ATTENTION : {total - len(rows)} décision(s) au-delà de "
                  f"--limit={args.limit} ne sont pas dans l'export.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

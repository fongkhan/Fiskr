# Performance de la base de données

Diagnostic mené sur la production (9 Go) et correctifs. Tout ce qui suit a été
**mesuré**, jamais supposé.

## Le constat

Chaque endpoint de lecture répondait en ~1 s, sauf un :

| Endpoint | Temps |
|---|---|
| `/api/counters` | 1,1 s |
| `/api/snapshots` | 1,8 s |
| `/api/history` | 0,8 s |
| **`/api/watchlist/db`** (écran « Watchlist Active ») | **18,2 s** |
| `/api/watchlist/db?search=…` | **54,5 s** |

La signature excluait la sérialisation : demander **10** lignes coûtait autant
que 50, et filtrer par type de liste répondait en 1,2 s. Le coût était donc
**fixe et proportionnel au périmètre balayé**, pas à la page rendue.

## La cause

Le périmètre « production » se définit par des colonnes qui n'étaient
**indexées nulle part** :

- `snapshots.status = 'READY'`
- `snapshots.file_type IN (…)`
- `watchlist_entities.excluded IS NOT TRUE`

PostgreSQL lisait donc la table entière à chaque affichage de page. Deux faits
rendaient l'addition brutale :

1. **92 % de la table n'est pas de la production.** Relevé en production :

   | Statut | Snapshots | Fiches |
   |---|---:|---:|
   | SUPERSEDED | 287 | 9 433 081 |
   | READY | 42 | 898 807 |
   | PENDING_REVIEW | 10 | 831 536 |
   | **Total** | **339** | **11 163 424** |

   Les lots remplacés conservent toutes leurs fiches : c'est l'essentiel des
   9 Go, et c'est balayé à chaque lecture.

2. **Le tri par défaut porte sur la table jointe** (`snapshots.uploaded_at`) :
   aucun index d'une seule table ne peut le servir, PostgreSQL doit donc
   matérialiser puis trier tout le périmètre avant de garder 50 lignes.

Plan mesuré avant correction (reproduction locale à 2,79 M lignes) :
`Parallel Seq Scan on watchlist_entities … rows=931000 loops=3` — soit la
table entière, pour rendre 50 lignes.

## Le correctif

Trois index, **sans modifier une seule requête** :

```sql
CREATE INDEX ix_snapshots_status_type   ON snapshots (status, file_type);
CREATE INDEX ix_snapshots_uploaded_at   ON snapshots (uploaded_at);
CREATE INDEX ix_wl_entities_production  ON watchlist_entities (snapshot_id, id)
       WHERE excluded IS NOT TRUE;      -- PARTIEL : exactement le périmètre lu
```

L'index partiel est le cœur du correctif : il n'indexe que les fiches non
exclues, donc il ne porte pas les 92 % de poids mort, et il place
`(snapshot_id, id)` dans l'ordre utile au tri.

Mesure sur reproduction locale (PostgreSQL 16, 1,4 M lignes aux proportions
réelles) :

| Requête | Avant | Après | Gain |
|---|---:|---:|---:|
| Comptage du périmètre | 431 ms | **26,7 ms** | ×16 |
| Page de 50 fiches | 173 ms | **1,6 ms** | ×106 |

Les deux passent d'un `Seq Scan` à un `Index Only Scan`. La production est
8 fois plus grosse, où le coût du balayage croît linéairement.

## Comment les poser SANS interruption

Un `CREATE INDEX` ordinaire prend un **verrou exclusif pendant toute sa
construction** : sur 11 M de lignes, cela fige lectures et écritures plusieurs
minutes. C'est pourquoi :

- **le démarrage ne les crée jamais sur une grosse table.** `init_db` n'agit
  qu'en dessous d'un garde-fou de volume (installations neuves, développement)
  et journalise sinon la commande à lancer. L'estimation de volume lit les
  statistiques du planificateur — jamais un `COUNT(*)`, qui coûterait
  lui-même des secondes au démarrage ;
- **l'outil dédié construit en `CONCURRENTLY`**, service allumé :

```bash
python tools/create_perf_indexes.py            # index de consultation
python tools/create_perf_indexes.py --search   # + recherche plein texte
python tools/create_perf_indexes.py --dry-run  # montre sans rien faire
```

Idempotent (`IF NOT EXISTS`), relançable. Il **ne migre rien** et n'appelle
jamais `init_db()` — un outil de performance n'a pas à toucher au schéma.

## Recherche plein texte : un compromis à arbitrer

La recherche `ILIKE '%terme%'` ne peut utiliser aucun index btree. Des index
**trigramme GIN** la rendent utilisable, à un coût réel :

| | Sans | Avec |
|---|---:|---:|
| Recherche | 1 270 ms | **17 ms** (×73) |
| Insertion de 50 000 fiches | 1,58 s | **2,82 s** (+78 %) |

L'ingestion PEP porte 773 000 fiches : +78 % sur cette phase n'est pas
anodin. L'arbitrage revient à l'exploitation — l'ingestion est une tâche de
fond, la recherche est interactive. Ces index sont donc **explicitement
optionnels** (`--search`) et jamais créés automatiquement.

## Ce qui reste à décider (hors correctif)

**Les 9,4 M de fiches en snapshots SUPERSEDED.** C'est la cause première du
volume. Les purger diviserait la table par plus de dix et accélérerait tout,
mais c'est une **suppression de données** : un dossier d'alerte référence une
fiche listée et son lot, et la piste d'audit doit rester relisible des années
après. Cette décision appartient à la conformité, pas à une optimisation —
elle n'a donc pas été prise ici. Piste à instruire : archiver hors ligne
plutôt que supprimer, et ne purger que les lots sans alerte rattachée.

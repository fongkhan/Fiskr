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

## Recherche plein texte : d'abord, l'hébergeur doit la permettre

**Constaté en production (o2switch) : l'extension `pg_trgm` n'y est pas
fournie.** Son fichier de contrôle est absent de l'installation PostgreSQL —
ce n'est pas un réglage d'exploitation, seul l'hébergeur peut l'ajouter.

Conséquence directe : sur ce serveur, **aucun index SQL ne peut accélérer un
`ILIKE '%terme%'`**. Le compromis décrit ci-dessous ne se pose donc même pas ;
la recherche restera un balayage tant que l'extension n'est pas activée.

L'outil le constate désormais **avant d'agir** et l'explique en une phrase. Il
n'essaie plus : auparavant il enchaînait trois échecs SQL bruts (« n'a pas pu
ouvrir le fichier de contrôle d'extension », puis deux fois « la classe
d'opérateur `gin_trgm_ops` n'existe pas »), ce qui laissait croire à une
manipulation ratée alors que rien, côté exploitant, ne pouvait y changer quoi
que ce soit.

Si la recherche devient gênante sans `pg_trgm`, la voie n'est pas la base mais
l'application : le moteur tient un index mémoire (celui qui sert le Ctrl+K),
qui pourrait servir aussi la recherche de l'écran des listes.

**Réserve importante, constatée depuis :** cet index n'existe pas dans les
processus web. Passenger sert l'application via `a2wsgi.ASGIMiddleware`, qui
n'implémente pas le protocole ASGI `lifespan` — le démarrage FastAPI, donc le
chargement du cache moteur, n'y tourne jamais. S'appuyer sur cet index depuis
un endpoint web suppose donc de le charger d'abord, et ce chargement coûte
plusieurs minutes en pleine requête (mesuré : la palette Ctrl+K ne répondait
plus du tout). Voir « Le cache moteur dans un processus web » ci-dessous.

## Le cache moteur dans un processus web

Le cache moteur (référentiel en mémoire + index de blocking + index de
recherche) est bâti par `load_watchlist_cache()`, appelée depuis le `lifespan`
de FastAPI. Sous Passenger, **ce `lifespan` ne s'exécute pas**. Rien ne le
signale : `get_db()` appelle `init_db()` paresseusement, donc tous les
endpoints de base de données fonctionnent et masquent le trou.

Règles qui en découlent, et qui sont désormais vérifiées par les tests :

1. **Un endpoint qui crible garantit son cache** (`_ensure_watchlist_cache`).
   Sans cette garantie, l'index de blocking est vide, aucun candidat n'en sort
   et le criblage rend `NO_MATCH` : un listé déclaré non listé, silencieusement.
2. **Un endpoint de consultation ne bâtit jamais le cache.** Le coût est de
   plusieurs minutes ; il n'a rien à faire dans une requête d'affichage. Le
   badge « Hash Actif » lit la base ; la palette Ctrl+K utilise l'index s'il est
   déjà là, et se replie sinon sur une requête bornée.
3. **Le démarrage précharge**, pour que personne ne paie ce coût en requête.

### Le préchargement, et pourquoi il est en thread

Mesures relevées sur la production après le correctif de justesse :

| | Mesuré |
|---|---:|
| Premier criblage après redémarrage | **64 s** |
| Le même, cache chaud | 5,6 s |
| Palette Ctrl+K (repli SQL) | 30,9 s |

Le coût n'était pas de trop, il était **mal placé** : il tombait sur le premier
utilisateur. `fiskr/wsgi.py` chauffe donc le cache au démarrage du processus.

Trois choix, tous délibérés :

- **en thread de fond, pas en ligne.** Bloquer le démarrage une minute
  risquerait `passenger_start_timeout` (90 s par défaut), et un processus tué
  au démarrage met le site à terre — bien pire que la lenteur corrigée.
  L'application répond immédiatement pendant que le cache chauffe ;
- **sous verrou.** Sans lui, la chauffe et un criblage arrivé entre-temps
  liraient le référentiel **deux fois en parallèle** : deux fois le temps et la
  mémoire pour un résultat identique. Le verrou est remis à neuf après `fork()`
  — le pool de criblage forke, et un verrou tenu au moment du fork resterait
  tenu pour toujours dans l'enfant ;
- **jamais fatal.** Une base momentanément indisponible ne doit pas empêcher un
  processus de démarrer. En cas d'échec, `_ensure_watchlist_cache` reste le
  filet.

### …et pourquoi il est désactivé par défaut

La suite des mesures a renversé la conclusion. Sur un mutualisé, **le vrai
levier n'est pas le préchargement mais la survie du processus** :

| | Palette Ctrl+K | Criblage à chaud | Survie du processus |
|---|---:|---:|---:|
| Sans `passenger_min_instances` | 31 s | — | recyclé aussitôt |
| Avec `passenger_min_instances 1` | **1,4 s** | **5,2 s** | > 12 min |

Sans ce réglage, Passenger recycle les processus dès qu'ils sont inactifs — un
processus naissait **à la seconde même** de la requête. Le thread de chauffe se
disputait alors le CPU avec la requête : le premier criblage passait de 64 s à
118 s. Le préchargement coûtait plus qu'il ne rapportait, parce que le
processus mourait avant d'en profiter.

Avec `passenger_min_instances 1`, le processus survit et le cache chargé une
fois sert toutes les requêtes suivantes. Le préchargement n'épargne alors plus
que **le tout premier criblage après un redémarrage**, au prix de 60 s de CPU
et de l'empreinte du référentiel dans chaque processus qui naît — y compris
ceux que Passenger crée en renfort sous charge.

Le bon réglage sur un mutualisé est donc `passenger_min_instances 1` **sans**
préchargement. Celui-ci reste activable par `FISKR_PRELOAD_CACHE=1`, pour un
hébergement dédié où le démarrage n'est pas contraint et la mémoire pas
partagée.

**Réserve sur les chiffres à froid.** Les valeurs 64 s / 118 s ont été relevées
à une heure d'intervalle ; une mesure ultérieure a dépassé 280 s pour la même
opération. L'explication la plus probable est le throttling CPU du compte
(CloudLinux) après une dizaine de chargements complets en une demi-heure. Les
temps *à chaud* (5,2 s, 1,4 s), eux, sont stables et reproductibles.

## Recherche plein texte : un compromis à arbitrer (là où pg_trgm existe)

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

## Deuxième passe (production, 11,2 M lignes)

Les index avaient ramené `/api/watchlist/db` de 18,2 s à ~4 s. Nouvelle mesure,
en variant la taille de page :

| `page_size` | Réponse |
|---:|---:|
| 1 | 4,00 s |
| 10 | 3,73 s |
| 50 | 3,84 s |
| 200 | 4,13 s |

**Le coût ne dépend pas de la page** : 2 ms par ligne au-delà de la première.
Ce qui reste, c'est le `COUNT` du périmètre — 895 157 fiches en production —
refait à chaque changement de page.

Et par périmètre :

| `scope` | Lignes | Réponse |
|---|---:|---:|
| `production` | 895 157 | 3,6 – 3,9 s |
| `production` + `list_type=OFAC` | 19 199 | 0,8 – 0,9 s |
| `production` + `list_type=AMF` | 3 396 | 0,6 – 1,0 s |
| `PENDING_REVIEW` | 1 588 840 | 5,3 – 6,5 s |
| **`EXCLUDED`** | **0** | **21 – 35 s** |

### L'index partiel ne servait que la négation

`ix_wl_entities_production` est partiel sur `excluded IS NOT TRUE`. Un index
partiel ne sert **que** les requêtes dont la clause est impliquée par la
sienne : `WHERE excluded IS TRUE` n'était couverte par rien et parcourait les
11,2 M lignes — pour rendre zéro ligne, puisque rien n'est exclu aujourd'hui.
D'où les 21 à 35 s.

Le symétrique `ix_wl_entities_excluded` (même colonnes, `WHERE excluded IS
TRUE`) comble le trou. Il n'indexe que les fiches effectivement exclues : une
poignée, là où l'autre en indexe des centaines de milliers.

### Le compte de production, mémorisé par signature

Le `COUNT` du périmètre `production` n'est plus refait à chaque page. Il est
mémorisé sous une **signature de la production** : l'époque de la watchlist
(bougée par tout ce qui change l'univers criblé) plus un relevé direct des
snapshots `READY` — nombre, dernier téléversement, somme des compteurs.

Ce relevé porte sur 42 lignes et sert un but précis : la mise en production
commit le passage en `READY` puis **délègue** le rechargement du cache (donc la
remontée d'époque) à un travail de fond. Sur l'époque seule, le compte serait
resté en retard d'une homologation le temps que ce travail passe. Le relevé,
lui, capte la bascule au commit.

Seul le périmètre `production` est mémorisé. Les exclusions se posent et se
retirent sur des snapshots **en attente d'homologation**, sans remontée
d'époque : les comptes de `EXCLUDED` et `PENDING_REVIEW` restent donc calculés
à chaque appel. C'est aussi pourquoi la signature ne compte pas les fiches
exclues — cette requête est précisément celle qui mettait 21 à 35 s.

### Le badge « Hash actif » et les badges de la barre latérale

`GET /api/watchlist/summary` alimente le badge chargé à **chaque ouverture de
page**, et son compte de fiches est le même `COUNT` sur les mêmes 895 157
fiches : ~1,3 s de travail serveur, à chaque fois. Il partage désormais le
total mémorisé — les deux comptent exactement le même univers.

`GET /api/counters` émettait **six** requêtes, dont cinq `COUNT` sur `alerts`
au même périmètre. La barre latérale interroge cet endpoint en boucle : les
cinq sont lus en une passe d'agrégats conditionnels.

**Réserve de méthode sur les chiffres bruts.** Les temps totaux relevés depuis
l'extérieur incluent ~0,45 s fixes (poignée de main TLS et surcoût Passenger,
mesurés sur `/api/health` et `/api/version` qui ne font aucun travail). Les
comparaisons ci-dessus — taille de page, périmètre — restent valables :
ce surcoût est constant et s'élimine dans la différence.

### La ligne servie n'hydrate plus l'entité complète

La consultation chargeait des entités ORM à 70 colonnes pour en rendre 16,
obligeant PostgreSQL à détoaster ligne par ligne les blocs JSON (alias, motifs
de désignation, adresses, documents) que la sérialisation jetait ensuite. Elle
ne demande plus que les colonnes rendues — la leçon que le balayage fuzzy avait
déjà tirée (25 000 fiches ORM complètes : ~2,5 s par tranche ; en tuples
légers : moins d'une demi-seconde).

## Troisième passe : les index de la table `alerts`

Depuis qu'un criblage ouvre une alerte **par correspondance**, cette table
grossit du nombre d'homonymes : un seul « Mohammed Ali » sans pays en ajoute
2 976. Trois index qui ne coûtaient rien sur quelques milliers de lignes en
valent la peine sur une table qui se compte en millions.

| Index | Ce qu'il sert |
|---|---|
| `ix_alerts_created_at` | l'accueil (« alertes créées 24 h »), les exports, la courbe journalière, `ORDER BY created_at DESC LIMIT 50` |
| `ix_alerts_decided_at` | l'accueil (« décidées 24 h »), les indicateurs par analyste, la sélection de rétention |
| `ix_alerts_audit_id` | l'intégrité référentielle vers le journal d'audit |

Le dernier mérite une explication. PostgreSQL n'indexe **pas** automatiquement
le côté *référençant* d'une clé étrangère. Sans index sur `alerts.audit_id`,
chaque ligne de `compliance_audit_trail` supprimée par la rétention déclenche
un **parcours séquentiel** de `alerts` pour la vérification d'intégrité :
purger 100 000 lignes d'audit valait 100 000 parcours d'une table qui se compte
désormais en millions.

Ces index sont déclarés dans le modèle, donc `tools/create_perf_indexes.py` les
crée `CONCURRENTLY`, sans interruption de service, comme les autres.

## Le calcul de delta : deux instantanés entiers en mémoire

Le moteur de comparaison recevait deux **listes complètes de dictionnaires
d'entités** — soixante-dix colonnes chacune — pour ne publier au plus que
**cent lignes par catégorie** (`MAX_REPORT_DETAILS`). Mesuré sur 40 000 fiches
contre 40 000, dont la moitié modifiées :

| | Temps | Pic mémoire |
|---|---:|---:|
| chargement des deux instantanés + `calculate_delta` | 5,90 s | +94 Mo |
| `calculate_delta_db` | **0,22 s** | **+0 Mo** |

Extrapolé à la plus grosse liste de la production — WATCHLIST_PEP,
**709 511 fiches** — l'ancien chemin demande **~1,66 Go et ~105 s**, dans une
requête HTTP synchrone, sur un hébergement mutualisé. L'écran d'examen d'un
import manuel de cette liste ne pouvait pas s'ouvrir.

Le nouveau chemin fait le travail en SQL : anti-jointure pour les ajouts et les
retraits, jointure sur les **empreintes** pour les modifications, un `COUNT` et
un `LIMIT` pour chacun. Seules les fiches effectivement publiées en détail sont
ensuite chargées en entier, pour en tirer le champ-à-champ.

L'équivalence des empreintes n'est pas une approximation : `compute_checksum`
et `find_differences` excluent **exactement** les mêmes trois clés (`id`,
`snapshot_id`, `entity_checksum`), donc « empreintes différentes » et « au
moins un champ comparé diffère » sont la même affirmation — et un test vérifie
que les deux implémentations rendent les mêmes lignes sur un jeu mêlant ajouts,
retraits, modifications et fiches inchangées.

L'index `ix_wl_entities_snapshot_entity` porte le couple
`(snapshot_id, entity_id)`, qui est la clé de rapprochement : les deux index
séparés ne servaient chacun qu'une moitié de la condition.

## Ce qui reste à décider (hors correctif)

**Les 9,4 M de fiches en snapshots SUPERSEDED.** C'est la cause première du
volume. Les purger diviserait la table par plus de dix et accélérerait tout,
mais c'est une **suppression de données** : un dossier d'alerte référence une
fiche listée et son lot, et la piste d'audit doit rester relisible des années
après. Cette décision appartient à la conformité, pas à une optimisation —
elle n'a donc pas été prise ici. Piste à instruire : archiver hors ligne
plutôt que supprimer, et ne purger que les lots sans alerte rattachée.

# Un autre langage que Python aiderait-il Fiskr ?

Analyse demandée : Python reste-t-il le bon choix, ou un autre langage
(Node.js, Ruby, Go, Rust…) apporterait-il un gain réel ? Réponse courte :
**ne pas réécrire l'application**. Le seul endroit où un autre langage
pourrait payer est le **noyau de comparaison du criblage**, et seulement si
un profilage le désigne — pas avant.

> **Mise à jour — le profilage a été fait, et un premier palier de gain a été
> pris EN PYTHON.** `tools/bench_screening.py` profile `screen_one` sur un
> univers synthétique. Il a confirmé que le temps part presque entièrement
> dans le noyau de matching : **Damerau-Levenshtein** (de loin le premier
> poste — il bâtissait sa matrice dans un dict de tuples), le parse des dates,
> les vérifications de capacités moteur et la normalisation des noms. Quatre
> optimisations **à résultat identique** (suite verte, DL vérifié au bit près
> sur 200 000 paires) ont fait passer le banc de **17,9 s à 8,9 s (~2×)** :
> réécriture de DL en rangées glissantes, et mémoïsation du parse de dates, de
> la résolution des capacités et de la normalisation. Ce gain profite à TOUS
> les chemins (temps réel, batch, cahier de tests, re-criblage). Il valide la
> démarche ci-dessous : **on optimise d'abord le Python**, et le levier
> langage ne se pose que pour le palier SUIVANT.
>
> **Deux leviers restants, dans l'ordre :**
> 1. **Paralléliser le re-criblage post-delta.** Il tourne aujourd'hui en
>    boucle **séquentielle** (`rescreen.py`) alors qu'il repasse toute la base
>    clients après chaque mise à jour de liste — c'est le criblage de
>    production le plus fréquent. Le cahier de tests, lui, forke déjà un pool.
>    Le paralléliser demande de gérer l'écriture concurrente des alertes en
>    base (le pool actuel n'agrège que des résultats, sans écrire) : chantier
>    réel mais **en Python**, sans changer de langage.
> 2. **Accélérer le noyau de métrique** (DL + Jaro, désormais ~la moitié du
>    temps restant). C'est ici, et seulement ici, qu'un noyau **Rust via
>    PyO3** (in-process, sans sérialisation) prendrait le palier suivant —
>    voir plus bas.

## 1. Où le temps passe réellement

Le seul chemin coûteux de Fiskr est le **criblage d'un univers** (cahier de
tests, re-criblage, campagne batch) : ~770 000 fiches. Tout le reste (API,
ingestion, homologation, alertes) est borné par l'I/O ou par la taille des
données, pas par le CPU — un autre langage n'y changerait rien.

Le criblage se décompose en :

1. **Chargement de l'univers** depuis la base (déjà en *projection* : seuls
   les champs lus, ~3,8 Ko/fiche). Coût = I/O base + création d'objets Python.
2. **Génération des clés de blocage** par fiche (normalisation, translittération).
3. **Scoring flou** de chaque client contre ses candidats (le cœur CPU).

Les points 2 et 3 sont **CPU-bound** et **parallélisables**. Fiskr les
parallélise déjà via un **pool de processus forkés** (`screenpool`), qui
contourne le GIL au prix d'une duplication mémoire (d'où le travail sur la
projection et la sérialisation des passes).

**Avant toute décision de langage : profiler.** Si le temps part dans le
point 1 (chargement/objets), aucun langage ne sauve — le correctif est dans
la façon de charger (colonnes, curseur serveur, cache). Si le temps part dans
le point 3 (le noyau de scoring), alors et seulement alors un langage compilé
sur *cette fonction* est le bon levier.

## 2. Langage par langage

### Node.js — disponible sur l'hébergement actuel
- **Pour** : V8 est un très bon JIT ; les opérations sur chaînes y sont
  souvent plus rapides que CPython ; `worker_threads` donne du vrai
  parallélisme.
- **Contre** : réécrire le moteur en JS = perdre l'écosystème Python qui
  *fait* le produit (`anyascii` pour la translittération, `pypdf`, les
  parseurs XML/CSV, SQLAlchemy). Un pont Python↔Node paierait une
  sérialisation de 770 000 fiches à chaque passe. Le front est déjà en JS ;
  le back n'a rien à y gagner.
- **Verdict** : non. Aucun gain qui justifie le risque.

### Ruby — disponible sur l'hébergement actuel
- MRI a un GIL comme CPython et un débit généralement **inférieur** à Python
  sur ce type de charge. Aucun avantage. **Verdict : non.**

### Go — nécessite un hébergeur qui accepte les binaires
- **Pour** : compilé, **vrai parallélisme** (goroutines, pas de GIL),
  excellent pour un **service de matching** séparé. Binaire statique, déploiement
  trivial (un fichier). Rapport effort/gain le meilleur des langages compilés.
- **Contre** : un service séparé impose une **frontière réseau** — il faut lui
  passer l'univers (ou l'y charger lui-même depuis la base). Si l'API Python
  lui envoie 770 000 fiches par socket, la **sérialisation** peut manger le
  gain de calcul. Le modèle viable est un service Go qui **lit lui-même la
  base** et rend les hits — c'est-à-dire ré-implémenter en Go le chargement,
  la projection, le blocage et le scoring : un second moteur à maintenir en
  parallèle du Python (risque de divergence des verdicts, inacceptable en
  conformité).
- **Verdict** : envisageable pour un **microservice de criblage** autonome,
  mais c'est un second moteur, pas une optimisation ponctuelle.

### Rust — nécessite un hébergeur qui accepte les binaires
- **Pour** : le plus rapide, contrôle mémoire fin, SIMD possible. Surtout, via
  **PyO3/maturin**, on expose une fonction Rust comme un module Python
  **in-process** : pas de frontière réseau, pas de sérialisation, l'orchestration
  (DB, API, audit) reste en Python. On remplace *uniquement* le noyau chaud —
  « normaliser + scorer un client contre N candidats » — par une fonction Rust,
  appelée exactement là où `match_entities` l'est aujourd'hui.
- **Contre** : itération plus lente, et surtout **build par architecture** :
  il faut compiler une *wheel* pour la plateforme cible. Sur mutualisé
  (o2switch/cPanel) sans toolchain, c'est le point bloquant — d'où « je
  trouverai un hébergeur qui accepterait ». Une wheel pré-compilée pour l'archi
  du serveur lève l'obstacle.
- **Verdict** : **le meilleur candidat** si le profilage désigne le scoring —
  parce qu'il reste in-process et chirurgical (une fonction), sans dupliquer le
  moteur ni payer de sérialisation.

## 3. Recommandation

1. **Profiler d'abord** un criblage de 770 000 fiches (`cProfile`/`py-spy` sur
   un worker) pour répartir le temps entre chargement, blocage et scoring.
2. **Si le chargement domine** : rester en Python, optimiser la lecture (curseur
   serveur, colonnes strictes, éventuellement `arrow`/`numpy` pour les tableaux
   de fiches). Aucun changement de langage.
3. **Si le scoring domine** : extraire le **seul** noyau
   (`quality.strip_accents` + `blocking.lookup_blocking_keys` + `scoring.match_entities`)
   en **Rust via PyO3**, exposé comme `fiskr._kernel`, avec un repli Python pur
   identique (au bit près) gardé comme oracle de test. Le reste de l'application
   ne bouge pas. Gain attendu : un facteur 5–20 sur la partie CPU, sans toucher
   à l'orchestration ni aux verdicts.
4. **Ne pas** réécrire en Node/Ruby (aucun gain), ni bâtir un microservice Go
   qui deviendrait un second moteur à tenir synchronisé avec le Python.

En clair : le langage n'est pas le levier tant qu'on n'a pas mesuré ; et si
levier il y a, c'est **une fonction en Rust in-process**, pas une réécriture.
Côté hébergement, cela demande seulement de pouvoir déposer une *wheel*
compilée pour l'architecture du serveur — le reste de Fiskr reste du Python
standard, portable partout où tourne CPython 3.12.

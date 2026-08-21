# Criblage, Filtrage, Blocking Keys & Règles Anti-Faux Positifs

Ce guide couvre la gestion des alertes séparée par canal, le paramétrage des
blocking keys et le moteur de règles Python anti-faux positifs (mode DEV).

## 1. Deux canaux d'alertes distincts

Les alertes sont désormais séparées en deux files, dans l'onglet **Alertes** :

| Canal | Origine | Sous-onglet |
|---|---|---|
| **SCREENING** (Criblage Clients) | criblage du référentiel clients contre les listes | 🧍 Criblage Clients |
| **FILTERING** (Filtrage Transactionnel) | filtrage des parties des messages ISO 20022 (`pain.001`/`pacs.008`) | 💸 Filtrage Transactionnel |

Chaque file a son propre cycle de vie 4-yeux, ses filtres de statut, son
compteur de badge, et — c'est le point clé — son **blocking key** et son
**jeu de règles** propres. La colonne `channel` de la table `alerts` porte le
canal (les alertes de filtrage étaient déjà reconnaissables à leur `client_id`
préfixé `TXN:` ; elles sont rétro-classées automatiquement).

`GET /api/alerts?channel=SCREENING|FILTERING` filtre par canal ;
`GET /api/counters` expose `open_alerts_screening` et `open_alerts_filtering`.

## 2. Blocking Keys paramétrables par canal

La **blocking key** sélectionne les candidats à scorer (réduit la combinatoire).
Elle est composée de composantes ordonnées :

**Trois composantes de base** :

| Composante | Rôle |
|---|---|
| `COUNTRY_ISO` | pays rattachés (nationalité, résidence, naissance, juridiction) |
| `ENTITY_TYPE` | type PP (personne physique) / PM (personne morale) |
| `PHONETIC_FIRST` | code phonétique (Double Metaphone) du nom |

**Dix composantes de champ**, ajoutables à la clé : `DOB_YEAR` (année de
naissance), `GENDER`, `PLACE_OF_BIRTH`, `CITY`, `TAX_ID`, `LEI`, `BIC`,
`IBAN`, `IMO`, `NATIONAL_REGISTRY`. Une fiche listée qui ne renseigne pas le
champ porte un **joker** sur cette composante, et l'interrogation teste toutes
les combinaisons de jokers : ajouter « Année de naissance » ne fait donc pas
perdre les fiches sans date — c'est-à-dire l'essentiel des listes officielles.

Le nombre de composantes de champ est **plafonné** (`MAX_BLOCKING_FIELDS`,
3 aujourd'hui) : chaque champ ajouté double le nombre de variantes jokerisées
à interroger, soit 2^N sondes par criblage. Un layout qui dépasse le plafond est
**refusé à l'enregistrement** (400, avec le compte et les composantes en
cause) — il était auparavant accepté puis silencieusement ignoré, l'exploitant
croyant avoir changé le criblage alors que le moteur retombait sur le layout
par défaut.

Accès : **Criblage → Moteur** (rôle `blocking` ou `admin`), avec simulation
d'impact avant application.

- **Criblage** (défaut `COUNTRY_ISO, ENTITY_TYPE, PHONETIC_FIRST`) : toute
  modification **recharge immédiatement le cache de production** — l'index en
  mémoire et la sonde du criblage utilisent toujours le même layout (cohérence
  garantie).
- **Filtrage** (défaut `PHONETIC_FIRST` seul) : les données d'un message de
  paiement sont pauvres (souvent juste un nom), donc filtrer sur le pays ou le
  type ferait manquer des hits. Le type PP/PM est de toute façon testé dans les
  deux variantes côté partie de paiement.

Endpoints : `GET/PUT /api/settings/blocking` (rôle `blocking`).

## 3. Règles Anti-Faux Positifs (Python, mode DEV)

Une fois les alertes générées, des **règles Python** suppriment les faux
positifs récurrents — l'objectif est d'en filtrer le maximum **sans supprimer
un seul vrai positif**. Les jeux de règles criblage et filtrage sont
**indépendants** (les contextes d'alerte sont différents).

Accès : **onglet Alertes → ⚖️ Règles Faux Positifs** (rôle `rules` ou `admin`).

### Contrat d'une règle

```python
def rule(ctx):
    # True  = SUPPRIMER l'alerte (auto-clôture CLOSED_BY_RULE, tracée à l'audit)
    # False = CONSERVER l'alerte
    return ctx["final_score"] < 80 and not ctx["hard_match"]
```

Le dictionnaire `ctx` contient :

| Clé | Contenu |
|---|---|
| `channel` | `SCREENING` ou `FILTERING` |
| `client_id`, `client_name`, `entity_id`, `entity_name`, `list_type` | identité des deux côtés du rapprochement |
| `final_score`, `base_score`, `hard_match`, `adjustments` | le score et sa décomposition (dob / genre / géographie) |
| `client`, `entity` | le profil criblé et la fiche listée, complets |
| `party`, `message` | en filtrage : la partie du paiement et l'en-tête du message |
| **`perimeter`** | `SANCTION` ou `HORS_SANCTION` (cf. § 3.7) |
| **`hits_count`** | nombre de correspondances au-dessus du seuil produites par **ce** criblage |
| **`hit_rank`** | rang de celle-ci par score décroissant (1 = la meilleure) |
| **`corroboration`** | `has_dob`, `has_country`, `has_identity_document`, `name_only`, `corroborated`, plus les trois scores d'ajustement |
| **`rarity`** | fréquence des mots du nom dans le corpus listé (cf. plus bas) |

Les cinq dernières existent parce qu'un criblage rend **toutes** ses
correspondances au-dessus du seuil : une règle doit pouvoir raisonner sur le
lot, et distinguer « une correspondance isolée » de « 2 976 homonymes ».

#### `ctx["rarity"]` — ce que vaut le nom rapproché

Rapprocher deux noms sur « MOHAMMED » et « ALI » n'identifie personne : des
milliers de fiches les portent. Le faire sur « TYURIN » identifie presque
sûrement. Le moteur compte, sur l'univers **réellement criblé**, dans combien
de fiches apparaît chaque mot de nom (fréquence *documentaire* : une fiche
comptée une fois par mot, nom principal **et** alias haute priorité).

| Clé | Contenu |
|---|---|
| `disponible` | `False` quand aucune table n'est construite dans ce processus — **tous les autres drapeaux sont alors au repos** |
| `corpus` | nombre de fiches mesurées |
| `tokens` | `[{token, df, part, repandu}]` pour chaque mot **partagé** par les deux noms, du plus répandu au moins |
| `df_min`, `df_max` | fréquence du mot partagé le plus, et le moins, discriminant |
| `seuil_repandu` | au-delà de ce nombre de fiches, un mot est dit « répandu » |
| `plancher` | en dessous de ce nombre, la table ne compte plus : un `df` égal à `plancher - 1` signifie « au plus autant », pas « exactement autant » |
| `information`, `information_nom_liste` | information portée par les mots partagés, et par le nom listé entier (en nats) |
| `couverture` | part de l'identité du nom listé effectivement rapprochée, 0 à 1 |
| `rarete` | rareté du mot partagé le plus discriminant, ramenée sur 0-100 |
| `nom_repandu` | **vrai seulement si TOUS les mots partagés sont répandus** — un seul mot rare le met à faux |
| `sans_token_commun` | rapprochement purement flou, aucun mot exactement commun : la rareté ne dit rien de ce cas |

Deux garde-fous à connaître avant d'écrire une règle dessus :

- un **mot inconnu de la table** compte comme rare (`df = plancher - 1`), jamais
  comme absent : une borne *supérieure*, pour qu'un mot inconnu ne fasse jamais
  clôturer ;
- **sans table**, `disponible` vaut `False` et `nom_repandu` vaut `False` : une
  règle fondée sur la rareté ne clôture rien, elle ne plante pas.

La rareté **ne déplace aucun score**. Elle est jointe à l'alerte, écrite dans
l'arbre de décision (donc relisible en contrôle avec le corpus qui l'a
produite) et lisible ici. Pour la calibrer avant d'écrire un seuil :
`GET /api/screening/name-rarity` (sans paramètre : les mots les plus répandus
de *votre* univers ; avec `name=` : le profil d'un nom), ou l'écran **Moteur**.

Le modèle de règle livré « le nom ne partage que des mots très répandus » s'en
sert, limité au périmètre `HORS_SANCTION`.

Modules disponibles dans la règle : `re`, `math`, `datetime`, `date`,
`timedelta`, `unicodedata`.

### Pourquoi du Python et pas un DSL

C'est un choix assumé : coder directement en Python évite d'avoir à prévoir
chaque cas particulier dans un langage de règles limité, et supprime les zones
d'ombre. En contrepartie, le dispositif est **strictement gouverné** :

- accès réservé au rôle `rules` (ou admin) ;
- **toutes** les modifications sont journalisées de façon immuable
  (`fp_rule_changes` : qui, quand, quel code) ;
- une règle ne s'applique en production qu'après le cycle DEV ci-dessous ;
- **fail-open conformité** : une règle qui lève une exception en production est
  ignorée (l'alerte est CONSERVÉE) et l'erreur loggée — jamais de suppression
  par accident.

### Cycle de vie « branche → tests → 4-yeux → merge » (mode DEV)

Chaque règle vit comme une branche de la production :

```
BROUILLON ──(tests unitaires 100% verts)──▶ EN VALIDATION ──(4-yeux)──▶ ACTIVE
   ▲                                              │
   └──────────── Renvoyer en brouillon ◀──────────┘
```

- **Brouillon (DRAFT)** : modifiable, **jamais appliqué à la production**.
  Doté d'un banc d'essai (voir plus bas).
- **Soumission** : refusée tant que la règle n'a pas **au moins un test
  unitaire enregistré et 100 % de tests verts**.
- **Validation 4-yeux** : par un utilisateur habilité **différent du
  soumetteur** ; les tests sont rejoués (garde-fou). La règle devient `ACTIVE`.
- **Versionnage** : modifier une règle `ACTIVE` ne la touche pas — cela crée
  une **nouvelle version brouillon** (branche). À sa validation, elle devient
  `ACTIVE` et l'ancienne passe `SUPERSEDED` (le « merge »). La production n'est
  jamais modifiée sans repasser par le cycle.
- **Interrupteur** : une règle `ACTIVE` peut être activée/désactivée sans la
  supprimer (journalisé).

### Banc d'essai du mode DEV (sans toucher la production)

Trois sources d'alertes de test :

1. **Tests unitaires enregistrés** : cas nommés (contexte `ctx` JSON + résultat
   attendu supprimer/conserver). C'est la définition exécutable du
   comportement attendu, exigée pour la soumission.
2. **Rejeu de l'historique réel** (`bench source=history`) : les N dernières
   alertes du canal, avec **garde-fou vrais positifs** — les alertes
   `CLOSED_CONFIRMED` qui seraient supprimées sont affichées en rouge.
3. **Alertes générées depuis un panel** (`bench source=panel`, criblage
   uniquement) : criblage à blanc d'un panel de pseudo-clients (réutilise les
   panels du cahier de tests d'homologation).

### Que deviennent les alertes supprimées

Elles ne disparaissent **jamais** (exigence ACPR/FED) :

- l'alerte est **créée puis immédiatement auto-clôturée** au statut
  `CLOSED_BY_RULE` (visible dans la file via le filtre « Clôturées par règle »),
  avec `decided_by = fp-rule` et un événement `RULE_SUPPRESSED` dans son
  historique ;
- la ligne du **journal d'audit immuable** porte `fp_rule_applied {id, name,
  version}` dans son `decision_tree` ;
- le compteur `hit_count` de la règle est incrémenté.

Pour maîtriser les volumes, une alerte déjà `CLOSED_BY_RULE` pour la même paire
client × listé est re-détectée (événement) plutôt que recréée à chaque
re-criblage.

### 3.7 Portée par périmètre

Une règle **déclare le périmètre où elle s'applique** (`FpRule.perimeters` :
`SANCTION`, `HORS_SANCTION`, ou les deux). `NULL` = tous les périmètres : les
règles écrites avant cette colonne se comportent exactement comme avant.

C'est le **moteur** qui filtre, pas le code de la règle. Une règle limitée au
hors-sanction ne peut pas clôturer une correspondance de gel d'avoirs, même si
son code oublie de tester `ctx["perimeter"]`. Deux conséquences voulues :

* un contrôleur lit la portée **sur** la règle, sans avoir à en relire le code ;
* une déclaration illisible ne s'applique **nulle part** plutôt que partout —
  élargir la portée en silence serait le pire des deux comportements.

**Modèles prêts à installer** (`GET /api/fprules/templates`) : quatre règles
bâties sur `perimeter`, `hits_count`, `hit_rank` et `corroboration`. Les trois
volumétriques déclarent `HORS_SANCTION` ; la quatrième, de portée `SANCTION`,
est volontairement **inerte** — elle ne clôture rien, et sert de point de
départ à une règle visant une famille de faux positifs identifiée, jamais un
tri par le nombre. Chacune porte un champ `loss` qui dit ce qu'elle coûte, et
**aucune n'est active par défaut** : ce sont des arbitrages de conformité.

### Points d'application en production

Les règles `ACTIVE` et activées s'appliquent, **après** la décision ALERT et le
contrôle de liste blanche, dans : le criblage temps réel (`/api/screen`), le
re-criblage automatique post-delta, le filtrage transactionnel, et le cahier de
tests d'homologation (en dry-run, compteur dédié). La **première** règle qui
matche (ordre `run_order`) supprime l'alerte.

## 4. Droits d'accès (rôles empilables)

| Rôle | Accès |
|---|---|
| `blocking` | paramétrage des blocking keys (2 canaux) |
| `rules` | gestion des règles anti-faux positifs (2 canaux) |
| `admin` | tout, y compris les deux ci-dessus |

Les rôles sont cumulables (ex. `rules,user` pour un membre de l'équipe
criblage). L'administration des comptes propose des combinaisons prêtes à
l'emploi.

## 5. Distinction avec la liste blanche

- **Liste blanche** (« Good Guys ») : supprime une **paire précise** client ×
  listé (statut `WHITELISTED`). Idéale pour un homonyme avéré ponctuel.
- **Règle anti-FP** : logique **générale** applicable à toutes les alertes d'un
  canal (statut `CLOSED_BY_RULE`). Idéale pour un motif structurel de faux
  positif (ex. « score faible sans hard match sur une liste PEP »).

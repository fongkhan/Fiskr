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

| Composante | Rôle |
|---|---|
| `COUNTRY_ISO` | pays rattachés (nationalité, résidence, naissance, juridiction) |
| `ENTITY_TYPE` | type PP (personne physique) / PM (personne morale) |
| `PHONETIC_FIRST` | code phonétique (Double Metaphone) du nom |

Accès : **onglet Alertes → 🔑 Blocking Keys** (rôle `blocking` ou `admin`).

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

Le dictionnaire `ctx` contient : `channel`, `client_id`, `client_name`,
`entity_id`, `entity_name`, `list_type`, `final_score`, `base_score`,
`hard_match`, `adjustments` (dob/genre/géographie), `client` (profil complet,
criblage), `entity` (fiche listée complète), et en filtrage `party`
(name/roles/country/bic/is_agent) + `message` (type/msg_id). Modules
disponibles dans la règle : `re`, `math`, `datetime`, `date`, `timedelta`,
`unicodedata`.

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

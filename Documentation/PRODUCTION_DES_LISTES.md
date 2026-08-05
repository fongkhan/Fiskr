# Production des Listes — Parcours Guidé d'Homologation

Ce guide décrit le processus métier de mise en production d'une liste de
sanctions/PEP dans Fiskr : de l'import à la promotion, avec vérification du
delta, cahier de tests sur pseudo-clients et traitement des faux positifs
(« Good Guys »).

## Vue d'ensemble

```
1. IMPORT            2. DELTA               3. CAHIER DE TESTS         4. GOOD GUYS            5. PRODUCTION
Synchro auto ou      Ajouts / Modifs /      Criblage À BLANC d'un      Liste blanche en        Approbation → READY,
upload manuel        Suppressions           panel de pseudo-clients    masse sur les faux      re-criblage automatique
→ PENDING_REVIEW     (avant → après)        actuel vs candidat         positifs, re-test       du référentiel clients
```

Le parcours est porté par l'onglet **Gestion des Watchlists → Homologation**,
qui présente chaque snapshot candidat en 4 étapes numérotées : **Delta →
Exclusions → Cahier de Tests → Décision**.

> **Prérequis** : activer « Homologation obligatoire » dans **⚙️ Paramètres**
> (admin). Sans ce réglage, les imports passent directement en production et
> le parcours est court-circuité (un encart le rappelle dans l'onglet).

## Étape 1 — Importer une liste

Deux voies, toutes deux aboutissant au même parcours :

- **Sources automatiques** (OFAC SDN et Non-SDN, UE FSF, DGT, ONU, PEP, OFSI,
  SECO, US CSL, Canada, Australie, HK SFC, AMF, Banque mondiale) :
  synchronisation quotidienne planifiée ou manuelle (*Sources Automatiques*).
- **Import manuel** : fichier XML / CSV / JSON / PDF (*Import de Fichiers*).

> **EUR-Lex n'est pas une source de désignations.** Dans son mode par défaut
> (`sync.eurlex.mode: alert`), la surveillance du Journal Officiel **signale**
> qu'un acte de mesures restrictives est paru et en archive le PDF officiel —
> elle n'inscrit aucune fiche. Pour l'Union européenne, c'est la **liste
> consolidée UE FSF** qui fait autorité : elle est structurée et **porte les
> radiations**, que le Journal Officiel n'appliquait pas. Le signal EUR-Lex
> sert à savoir qu'une mise à jour arrive ; la liste, elle, vient du FSF.

> **La synchronisation travaille en tâche de fond.** `POST /api/sync/run`
> répond **202** avec un jeton et rend la main aussitôt : le cycle complet
> (téléchargement, analyse, ingestion, delta, rechargement du cache,
> re-criblage) dure plusieurs minutes, et l'application doit rester utilisable
> pendant ce temps. La progression se suit dans le bouton, dans la pastille et
> par `GET /api/progress?id=<jeton>` ; le rapport final est publié sur ce même
> jeton et archivé dans *Historique des synchronisations*. Les refus restent
> immédiats : source inconnue ou date malformée (**400**), source déjà en
> cours de synchronisation (**409** — deux ingestions concurrentes de la même
> liste se marcheraient dessus).

> **Une republication au contenu identique n'entre pas en homologation.**
> Certains fournisseurs (OpenSanctions notamment) republient chaque jour des
> fichiers aux métadonnées nouvelles (horodatages, ordre des lignes) : le hash
> change, aucune fiche ne diffère. Après calcul du delta, un snapshot
> strictement identique à la liste en production est archivé (`SUPERSEDED`) et
> le rapport conclut `NO_CHANGE` — pas de pointage humain ni de cahier de
> tests pour un non-événement. Un **premier import** (aucune base de
> comparaison) passe, lui, toujours par l'homologation.

En mode homologation, le snapshot arrive en `PENDING_REVIEW` : il est archivé,
comparé, testable — mais **invisible du moteur de criblage** tant qu'il n'est
pas approuvé. Après un import ou une synchro, l'application propose d'ouvrir
directement le parcours d'homologation du snapshot créé.

## Étape 2 — Vérifier le delta

L'étape **1 · Delta** affiche, par rapport à la liste actuellement en
production (calcul à la volée, toujours à jour) :

- les compteurs **Ajouts / Suppressions / Modifications** ;
- le **détail** : liste des entités ajoutées, supprimées, et pour chaque
  modification les champs concernés avec les valeurs **avant → après**
  (détails plafonnés à 100 par catégorie, compteurs exacts).

Le comparateur libre de deux snapshots reste disponible dans
*Snapshots & Comparateur*.

## Étape 3 — Exclusions (facultatif)

L'étape **2 · Exclusions** permet d'écarter de la production des fiches non
pertinentes (périmètre hors activité, faux positifs structurels), avec
justification et pièce jointe selon les réglages de gouvernance. Les fiches
exclues restent archivées en base.

## Étape 4 — Cahier de tests (taux d'interception)

L'étape **3 · Cahier de Tests** exécute un **criblage à blanc** (dry-run
strict : aucune alerte réelle, aucune ligne d'audit) d'un panel de
pseudo-clients contre **deux univers** :

- **Actuel** : les listes en production aujourd'hui ;
- **Candidat** : le même univers où les listes du même type sont remplacées
  par le snapshot en attente (exclusions déduites, ajouts manuels préservés) —
  le miroir exact de ce que produirait l'approbation.

Le panel provient au choix :

- d'une **base clients importée** (`CLIENT_BASE`) — vos vrais dossiers ou un
  fichier de test maison ;
- d'un **panel généré** (bouton « ⚙️ Générer un panel », 50 à 5000
  pseudo-clients) : ~10 % de copies exactes de listés (hits attendus), ~10 %
  de variantes (typos, inversion prénom/nom), ~10 % de quasi-collisions (même
  nom, date de naissance différente) et ~70 % de clients neutres. Les panels
  générés sont stockés en `CLIENT_TEST_PANEL` : ils ne sont **jamais** repris
  par le re-criblage du référentiel clients réel.

Le rapport restitue :

| Indicateur | Signification |
|---|---|
| Taux d'interception actuel vs candidat | alertes / taille du panel, pour chaque univers |
| **Écart (%)** | variation relative du nombre d'alertes ; comparé au **seuil toléré** (réglage, défaut 20 %) |
| Verdict | `OK` (écart dans le seuil) ou `WARN` (écart élevé) |
| Nouvelles alertes | paires client × listé qui n'alertaient pas avec la liste actuelle |
| Alertes résolues | paires qui disparaissent avec la candidate |

Le rapport est **archivé avec le snapshot** (auditable après promotion) via
`POST /api/review/snapshots/{id}/backtest`. Le même criblage (seuils par
liste, liste blanche) que la production est appliqué : le taux mesuré prédit
le comportement réel.

### Exécution, mémoire et progression

Le cahier de tests s'exécute dans le **démon travailleur** (processus séparé
de l'API) : l'application reste pleinement réactive pendant tout le criblage,
même sur un univers de 750 000 fiches, et le calcul est **parallélisé** sur
plusieurs processus (tranches de pseudo-clients, résultats identiques au
séquentiel).

**Mémoire maîtrisée, par construction** : les univers sont chargés en
*projection* (seuls les champs lus par le moteur, ~3,8 Ko/fiche au lieu de
~8,4), jamais deux univers ne sont en mémoire simultanément (passes
séquentielles, mémoire rendue entre les passes), et les jobs lourds —
cahiers de tests **et** simulations moteur — forment un **groupe sérialisé
exclusif** : un seul univers en RAM à la fois, quel que soit le mélange.

**Progression continue** : une seule barre 0 → 100 % couvre tout le cahier
(le total cumule toutes les passes — 2 en mode complet, 3 en mode delta),
chaque passe est nommée (« passe 1/3 — univers partagé », « passe 2/3 —
fiches retirées »…) et les chargements d'univers, longs sur une grosse
base, sont annoncés au lieu de laisser la barre muette. L'affichage se met
à jour en douceur (barre animée, sans clignotement) et survit à un
rechargement de page.

### Pannes visibles, reprise et retour arrière

Trois garde-fous rendent tout incident **visible et réparable** — jamais
silencieux, jamais bloquant :

- **Chien de garde du criblage parallèle** : un processus enfant tué (OOM)
  ou figé est détecté (aucune progression pendant
  `jobs.screen_stall_timeout_s`, 15 min par défaut) ; le cahier de tests
  **repart alors automatiquement en séquentiel** (mémoire minimale) et
  aboutit au lieu de bloquer la file.
- **Réparation continue des zombies** : un job laissé RUNNING par un démon
  mort est remis en file en ~60 s (2 tentatives au plus) ; au-delà, il
  apparaît **en échec** — dans la section « Travaux » du centre de
  notifications ET directement dans l'**étape 3 du stepper
  d'homologation**, avec sa cause et un bouton **↻ Relancer**.
- **Retour à l'état précédent** : un cahier de tests encore **en file
  d'attente** n'a rien exécuté — il s'annule d'un clic (✕ Annuler, depuis
  le panneau des opérations ou le stepper). Et le rapport archivé n'est
  écrit **qu'en fin de succès, en un seul commit** : un échec de
  ré-exécution n'écrase jamais le dernier rapport valide.

## Étape 5 — Good Guys (liste blanche) si écart élevé

Si le verdict est `WARN`, examinez les **nouvelles alertes** : pour chaque
homonyme avéré, cochez la paire et cliquez **« 🕊️ Good Guy (liste blanche) »**.
Une justification commune est demandée (`POST /api/whitelist/bulk`, paires
déjà actives sautées). **Relancez ensuite le cahier de tests** : les paires en
liste blanche sont supprimées du comptage (`whitelisted_suppressed`) et
l'écart doit revenir dans le seuil.

Pour un faux positif structurel (fiche entière non pertinente), préférez une
**exclusion** (étape 2) à la liste blanche (qui n'agit que sur une paire
client × listé).

## Étape 6 — Décision et mise en production

L'étape **4 · Décision** rappelle le dernier verdict du cahier de tests
(avertissement si aucun test n'a été exécuté ou si l'écart est élevé), puis :

- **Approuver** : le snapshot passe `READY`, les versions antérieures du même
  type passent `SUPERSEDED`, le cache de criblage est rechargé et — si le
  re-criblage automatique est actif — le référentiel clients réel est
  re-criblé contre les entités nouvelles/modifiées (les nouveaux hits ouvrent
  de vraies alertes) ;
- **Rejeter** (commentaire obligatoire) : le snapshot n'entrera jamais en
  production, la liste actuelle continue de servir.

## Homologation groupée (plusieurs listes en un geste)

Au lendemain des synchronisations planifiées, plusieurs listes attendent
souvent ensemble. La file d'attente est une **table de décision** : chaque
ligne porte le **delta** (ajouts / modifications / suppressions) à côté du
**verdict du cahier de tests** et de son écart. Cochez les listes à
promouvoir, puis **« Homologuer la sélection »**.

Le lot n'assouplit **rien** : chaque liste franchit exactement les mêmes
contrôles qu'une approbation unitaire (justifications d'exclusion, cahier de
tests obligatoire au verdict `OK` si le réglage est actif). Une liste refusée
**n'interrompt pas le lot** — elle est rendue avec son motif, les autres
passent. L'opération est tracée au journal d'administration, et deux versions
d'une même liste suivent la règle habituelle : la dernière approuvée
supersede l'autre.

> Le delta affiché dans la file est celui **mémorisé à la synchronisation**
> (instantané). Quand il ne s'applique pas — import manuel, production changée
> depuis — la ligne l'indique (« à l'examen », « premier import ») plutôt que
> d'afficher un 0/0/0 trompeur. Le détail, lui, recalcule toujours exactement.

## Réglages de gouvernance (⚙️ Paramètres, admin)

| Réglage | Défaut | Effet |
|---|---|---|
| Homologation obligatoire | inactif | tout snapshot watchlist attend un pointage humain |
| Seuil d'écart toléré (`review.backtest_max_gap_pct`) | 20 % | au-delà, le verdict du cahier de tests est `WARN` |
| Cahier de tests obligatoire (`review.backtest_required`) | inactif | blocage dur : impossible d'approuver sans un rapport au verdict `OK` |

Avec le blocage inactif, le verdict reste indicatif : la décision appartient
au réviseur (avertissement visible à l'étape Décision).

## Bonnes pratiques

- **Panel représentatif** : le taux d'interception n'a de sens que si le panel
  ressemble à votre base réelle. Idéalement, utilisez un extrait anonymisé de
  votre base clients ; le panel généré sert de filet quand aucune base n'est
  disponible.
- **Re-tester après chaque action corrective** (Good Guy, exclusion, seuil) :
  le rapport archivé est celui de la **dernière** exécution — c'est lui que le
  blocage évalue.
- **Écart en baisse ≠ anodin** : un écart négatif (moins d'alertes) peut
  signaler des radiations légitimes… ou une liste tronquée. Le delta
  (étape 1) et les « alertes résolues » du rapport permettent de trancher.

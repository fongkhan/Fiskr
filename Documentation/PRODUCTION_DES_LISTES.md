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

- **Sources automatiques** (OFAC, EUR-Lex, UE FSF, DGT, ONU, PEP, OFSI) :
  synchronisation quotidienne planifiée ou manuelle (*Sources Automatiques*).
- **Import manuel** : fichier XML / CSV / JSON / PDF (*Import de Fichiers*).

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

# Fiskr — Traitement des Alertes & Surveillance Continue

Ce document décrit le **flux de travail post-criblage** de Fiskr : ce qui se passe une fois qu'une correspondance est détectée, et comment le dispositif reste à jour en continu. Il complète le [README](../README.md) (architecture, moteur de matching, connecteurs) et le [Benchmark Concurrentiel](BENCHMARK_CONCURRENTS.md) (justification marché/réglementaire de chaque capacité).

Sommaire :
1. [Cycle de vie des alertes & validation 4-yeux](#1-cycle-de-vie-des-alertes--validation-4-yeux)
2. [Liste blanche client×listé (« Good Guys »)](#2-liste-blanche-clientlisté--good-guys-)
3. [Re-criblage automatique post-delta & lookback](#3-re-criblage-automatique-post-delta--lookback)
4. [Narratifs d'alertes (human-in-the-loop)](#4-narratifs-dalertes-human-in-the-loop)
5. [Adverse media](#5-adverse-media)
6. [Filtrage transactionnel ISO 20022](#6-filtrage-transactionnel-iso-20022)
7. [Pilotage (KPI conformité)](#7-pilotage-kpi-conformité)
8. [Récapitulatif des réglages à chaud](#8-récapitulatif-des-réglages-à-chaud)

---

## 1. Cycle de vie des alertes & validation 4-yeux

Chaque décision de criblage temps réel en statut `ALERT` ouvre un **objet de travail** dans la table `alerts`, dédupliqué par paire client×listé : un re-criblage de la même paire ajoute un événement `REDETECTED` au lieu de créer un doublon.

**Cycle de vie** :

```
OPEN → IN_PROGRESS (assignée) → PENDING_VALIDATION (décision proposée)
     → CLOSED_CONFIRMED | CLOSED_FALSE_POSITIVE
ESCALATED (chemin latéral, motif obligatoire)
```

* **Proposition** : un analyste propose une décision (vrai/faux positif) avec commentaire obligatoire.
* **Validation 4-yeux** : la clôture exige un validateur de rôle `reviewer` ou `admin` **différent du proposeur** (HTTP 403 en cas d'auto-validation). Un refus renvoie l'alerte en analyse avec motif obligatoire. Exigence désactivable à chaud (`review.alert_four_eyes_required`, défaut activé) : désactivée, une proposition clôture directement.
* **Traçabilité** : chaque action (assignation, commentaire, escalade, proposition, validation, re-détection, narratif) est enregistrée dans l'historique **append-only** `alert_events`. Le journal d'audit immuable (`compliance_audit_trail`) n'est jamais modifié — l'alerte y est simplement liée (`audit_id`).
* **Dashboard** : onglet **Alertes** avec badge du nombre d'alertes ouvertes, filtres par statut, et modale d'investigation affichant l'explication du score (ajustements du decision tree), la chronologie des actions et des boutons adaptés au rôle.

**Endpoints** : `GET /api/alerts` (file de travail triée par risque, filtres statut/assigné), `GET /api/alerts/{id}` (détail + decision_tree + historique), `POST /api/alerts/{id}/assign|comment|escalate|propose|validate`.

> Les alertes sont ouvertes par le criblage temps réel (et le re-criblage automatique) ; le moteur batch Spark optionnel ne crée pas encore d'objets de travail.

## 2. Liste blanche client×listé (« Good Guys »)

Recommandation explicite de la guidance Wolfsberg : un client homonyme d'un listé, dont le faux positif a été avéré, ne doit pas regénérer la même alerte à chaque criblage.

* **Création gouvernée** (rôle `reviewer` ou `admin`) : justification texte et pièce jointe justificative, chacune à caractère obligatoire **modulaire** (`review.whitelist_justification_required` / `review.whitelist_file_required`) ; pièces archivées sous `whitelist_evidence/` et retéléchargeables. Depuis une alerte close en faux positif, un bouton « Mettre en liste blanche » pré-remplit la paire.
* **Suppression jamais silencieuse** : chaque hit d'une paire whitelistée est **quand même tracé** dans le journal d'audit avec ses scores complets, sous le statut explicite `WHITELISTED`. Aucune alerte n'est ouverte.
* **Revue périodique** : expiration optionnelle (`expires_at`) — passé la date, les alertes reprennent.
* **Révocation douce uniquement** (commentaire obligatoire) : jamais de suppression physique, les alertes reprennent immédiatement.

**Endpoints** : `POST /api/whitelist` (multipart), `GET /api/whitelist`, `POST /api/whitelist/{id}/revoke`, `GET /api/whitelist/evidence/{id}`.

## 3. Re-criblage automatique post-delta & lookback

Le passage en **surveillance continue** : dès qu'un snapshot de liste entre en production — synchronisation manuelle ou planifiée, upload manuel, ou approbation d'homologation — le référentiel clients (`CLIENT_BASE`) est automatiquement re-criblé contre les **seules entités nouvelles ou modifiées** (comparaison des checksums avec le snapshot remplacé), via un index de blocking local.

* Les nouveaux hits ouvrent des alertes par le cycle de vie standard (dédupliquées ; événements signés `rescreen-auto`).
* Les paires en liste blanche sont supprimées de façon tracée (compteur `whitelisted_suppressed`).
* Désactivable à chaud (`ingestion.auto_rescreen`, défaut activé) ; les compteurs (`changed_entities`, `clients_screened`, `new_alerts`, `whitelisted_suppressed`) sont retournés dans les réponses de sync/upload/approbation.
* **Lookback manuel** (capacité Wolfsberg) : `POST /api/rescreen/run` (admin, body `{file_type?}`) re-crible tout le référentiel clients contre toutes les listes en production, ou un seul type.

## 4. Narratifs d'alertes (human-in-the-loop)

La modale d'alerte peut générer un **projet de narratif d'investigation** en français, composé **exclusivement depuis les données tracées** : decision_tree du journal d'audit lié (hard match ou score fuzzy, ajustements date de naissance/genre/géographie, seuil appliqué), identités, version des listes, re-détections et historique de décision. Chaque phrase est justifiable par un champ en base — l'approche déterministe répond à l'exigence d'explicabilité (EU AI Act).

* **Reformulation LLM optionnelle** (`narrative.llm_enabled`, désactivée par défaut ; nécessite `ANTHROPIC_API_KEY` et `pip install anthropic`) : le brouillon est reformulé en prose par l'API Claude avec interdiction stricte d'ajouter le moindre fait, et **repli déterministe silencieux** en cas d'erreur ou d'absence de configuration.
* **Jamais de décision automatique** : le narratif est un brouillon éditable/copiable ; la proposition et la validation 4-yeux restent des actes humains. Chaque génération est tracée (événement `NARRATIVE`).

**Endpoint** : `POST /api/alerts/{id}/narrative` → `{narrative, llm_used}`.

## 5. Adverse media

Revue de presse négative au moment de l'adjudication : le nom (client ou listé) est recherché conjointement avec des mots-clés LCB-FT (blanchiment, sanctions, fraude, corruption, terrorisme... configurables via `adverse_media.keywords`) sur le **flux RSS public de Google News** — gratuit, sans partenariat de données, et remplaçable (`adverse_media.provider`).

* **Strictement informatif** : les résultats ne modifient jamais un score ni un statut de criblage ; la décision reste à l'analyste.
* Boutons « Presse : client » / « Presse : listé » dans la modale d'alerte (titres, sources, dates, liens).
* Les solutions à base de données presse propriétaires (Dow Jones/Factiva) restent supérieures en couverture — le connecteur est conçu pour être remplacé le jour où un partenariat existe.

**Endpoint** : `GET /api/adverse-media?name=...`.

## 6. Filtrage transactionnel ISO 20022

Filtrage de paiements façon Fircosoft : soumission d'un message **pain.001** (ordre de virement client) ou **pacs.008** (virement interbancaire), toute version mineure (correspondance par nom local de balise).

* **Extraction de toutes les parties** : donneur d'ordre, bénéficiaire, ultimes (`UltmtDbtr`/`UltmtCdtr`), partie initiante, et agents financiers (BICFI/BIC, pays déduit du BIC si absent, date/pays de naissance depuis `PrvtId`).
* **Criblage adapté aux données pauvres d'un paiement** : la recherche de candidats ignore volontairement le pays de blocking et compare la phonétique sur **tous les mots** du nom libre ; chaque candidat est scoré avec la variante de profil (PP/PM) correspondant à son type.
* **Verdict global `PASS` / `HIT`** : chaque partie criblée laisse une ligne dans le journal d'audit immuable (identifiants `TXN:{msg_id}:{n}`), et chaque hit ouvre une alerte de travail adjudicable dans l'onglet Alertes.
* **Dashboard** : sous-onglet **Criblage → Filtrage Transactionnel** (upload du XML, bandeau de verdict, tableau par partie avec lien direct vers les alertes ouvertes).

**Endpoint** : `POST /api/transactions/screen` (multipart XML ; message invalide → 400).

> Limite actuelle : la liste blanche client×listé ne s'applique pas aux parties de paiement (qui ne sont pas des clients du référentiel).

## 7. Pilotage (KPI conformité)

Onglet **Pilotage** alimenté par `GET /api/kpi` :

* encours d'alertes par statut (ouvertes, en cours, en attente de validation, closes) ;
* **taux de faux positifs** (calculé sur l'ensemble des alertes closes) et délai moyen de décision (calculé sur les 500 dernières alertes closes) ;
* paires en liste blanche actives ;
* volumétrie des listes en production par type, snapshots par statut ;
* répartition des décisions de criblage (journal d'audit) et 15 dernières synchronisations.

## 8. Récapitulatif des réglages à chaud

Tous ces réglages sont modifiables **sans redémarrage** par un admin (carte réglages du dashboard ou `PUT /api/settings/ingestion`) ; les valeurs sont stockées en base (`app_settings`) et `config.yaml` ne fournit que les défauts.

| Réglage | Clé config | Défaut | Effet |
|---|---|---|---|
| Homologation obligatoire | `ingestion.require_approval` | `false` | Tout snapshot watchlist entrant attend un pointage humain (`PENDING_REVIEW`) |
| Re-criblage automatique | `ingestion.auto_rescreen` | `true` | Re-criblage du référentiel clients après chaque mise à jour de liste |
| Justification d'exclusion obligatoire | `review.exclusion_justification_required` | `true` | Texte requis pour exclure un listé en revue |
| Pièce d'exclusion obligatoire | `review.exclusion_file_required` | `false` | Pièce jointe requise pour exclure un listé |
| Validation 4-yeux | `review.alert_four_eyes_required` | `true` | Validateur ≠ proposeur pour clore une alerte |
| Justification de liste blanche obligatoire | `review.whitelist_justification_required` | `true` | Texte requis pour whitelister une paire |
| Pièce de liste blanche obligatoire | `review.whitelist_file_required` | `false` | Pièce jointe requise pour whitelister une paire |

Réglages fichier uniquement (redémarrage requis) : `scoring.cut_off_overrides` (seuils par liste), `adverse_media.*`, `narrative.*`, sections `sync.*`.

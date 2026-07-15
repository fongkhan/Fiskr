# Benchmark Concurrentiel & Feuille de Route d'Amélioration — Fiskr

*Version 1.0 — Juillet 2026. Analyse du marché du criblage sanctions/PEP (solutions commerciales et open source), du cadre réglementaire (Wolfsberg, ACPR/DGT) et des écarts de Fiskr par rapport à l'état de l'art, avec feuille de route priorisée.*

---

## 1. Résumé exécutif

Fiskr dispose d'un socle différenciant : moteur de scoring hybride **explicable** (`decision_tree` persisté dans la piste d'audit), hard match par identifiants, blocking phonétique Double Metaphone, quality gate réglementaire, delta engine, **mode homologation avec pointage humain** (rare, y compris chez les leaders), et archivage probant des actes officiels. Les écarts se situent principalement sur la **couverture des sources**, le **traitement des alertes** et l'**automatisation du re-criblage**.

Les 5 améliorations au meilleur ratio impact/effort :

| # | Amélioration | Impact | Effort |
|---|---|---|---|
| 1 | **Connecteur registre national des gels (DGT)** — API publique JSON officielle | Conformité ACPR : le gel des avoirs national est une obligation autonome pour les établissements français | S |
| 2 | **Remplacer le scraping EUR-Lex par le XML consolidé officiel FSF** | Fiabilité totale + détection des **radiations** (invisibles au scraping du JO) | M |
| 3 | **Liste ONU consolidée (XML officiel)** | Couverture réglementaire de base attendue de tout moteur du marché | S |
| 4 | **Liste blanche client×listé (« Good Guys » Wolfsberg)** avec justification | Réduction massive des faux positifs récurrents — 1er levier d'efficacité analyste du marché | M |
| 5 | **Re-criblage automatique post-delta** du référentiel clients | Passage d'un criblage à la demande à une **surveillance continue**, standard du marché 2026 | M |

---

## 2. Panorama concurrentiel

### 2.1 Solutions commerciales

| Acteur | Positionnement | Capacités clés | À retenir pour Fiskr |
|---|---|---|---|
| **LSEG World-Check One** | Référentiel de données de référence (millions de profils sanctions/PEP/adverse media) | Données exhaustives et curées ; souvent couplé à un moteur tiers (Actimize, Alessa) pour le workflow | La donnée et le moteur sont des métiers distincts : Fiskr (moteur) doit exceller sur l'**ingestion de n'importe quelle source** — le SSIE va déjà dans ce sens |
| **Dow Jones Risk & Compliance** | Qualité de données + adverse media inégalé (Factiva, milliers de sources presse) | Sanctions, PEP/SIP, due diligence, monitoring continu | L'adverse media est le prochain territoire de différenciation (P3) |
| **ComplyAdvantage** | Plateforme IA « temps réel » | Fraîcheur des données propriétaires, screening transactionnel, **case management intégré**, paramètres de risque configurables | Le case management intégré au moteur est devenu le standard — c'est l'écart n°1 de Fiskr côté produit |
| **SymphonyAI Sensa** | Next-gen investigation | **Copilote IA** d'investigation, narratifs d'alertes générés, cohérence des décisions | Cible P3 : narratifs human-in-the-loop sur la base du `decision_tree` existant |
| **Fircosoft (LexisNexis)** | Standard de fait du **filtrage transactionnel** (paiements SWIFT/ISO 20022) | Filtrage temps réel des messages de paiement | Hors périmètre actuel de Fiskr (criblage de référentiel) — horizon P3 |
| **Napier AI** | Screening + monitoring, approche « compliance-first » | Sandbox de calibration des règles, explicabilité | L'idée de **sandbox de calibration** des seuils rejoint le mode homologation de Fiskr |
| **sanctions.io / Sanction Scanner** | API de screening légères pour fintechs | Intégration rapide, prix bas, listes standards | Concurrents directs sur le segment de Fiskr : la profondeur réglementaire française (DGT, ACPR) est le différenciant à creuser |

### 2.2 Moteurs open source (comparables directs)

| Acteur | Capacités clés | À retenir pour Fiskr |
|---|---|---|
| **OpenSanctions / yente** | Matcher `logic-v2` **déterministe** : matching de noms multi-écritures et multi-langues par dictionnaires culturels, règles sur identifiants (IMO, LEI, ISIN, INN, OGRN), dégradation du score quand les informations secondaires divergent, **explications textuelles par sous-système** dans la réponse API, pondérations réglables, seuil par défaut 0,7 (0,8-0,85 recommandé en tolérance basse) | Trois idées directement transposables : (1) restitution **lisible** des explications de matching (le `decision_tree` de Fiskr contient déjà la matière), (2) translittération multi-écritures, (3) seuils différenciés par contexte. Le dataset OpenSanctions (agrégat de ~toutes les listes + PEP) est aussi une **source candidate** |
| **Moov Watchman** | Jaro-Winkler calqué sur l'OFAC Search officiel ; sources OFAC + CSL US/UK/EU + ONU + datasets OpenSanctions traités comme sources de première classe ; API HTTP + lib Go ; rafraîchissement automatique | La **couverture multi-listes native** (OFAC + EU + UK + ONU) est le socle minimal attendu, même en open source — Fiskr n'a aujourd'hui que OFAC + EU (scrapée) |

### 2.3 Tableau comparatif de capacités

| Capacité | Fiskr | World-Check One | ComplyAdvantage | yente | Watchman |
|---|---|---|---|---|---|
| Sources sanctions natives | OFAC + EU (FSF consolidé + JO) + ONU + UK OFSI + SSIE générique | Référentiel propriétaire mondial | Référentiel propriétaire temps réel | ~Toutes (agrégat OpenSanctions) | OFAC, CSL US/UK/EU, ONU, OpenSanctions |
| Registre gel des avoirs FR (DGT) | ✅ (API publique, natif) | ✅ (agrégé) | ✅ (agrégé) | ✅ (dataset) | ❌ |
| PEP | ✅ (dataset OpenSanctions, opt-in licence) | ✅ | ✅ | ✅ | via OpenSanctions |
| Matching fuzzy explicable | ✅ (`decision_tree`) | n/a (donnée) | ✅ | ✅ (explications API) | partiel |
| Hard match identifiants | ✅ (LEI, passeport, IMO...) | n/a | ✅ | ✅ | partiel |
| Multi-écritures (cyrillique/arabe/CJK) | ✅ (translittération `anyascii`) | ✅ | ✅ | ✅ | partiel |
| Cycle de vie des alertes / case management | ✅ (4-yeux, historique append-only) | via partenaires | ✅ | ❌ (moteur pur) | ❌ |
| Liste blanche / suppression FP | ✅ (gouvernée, jamais silencieuse) | via partenaires | ✅ | ❌ | ❌ |
| Re-criblage continu post-delta | ✅ (+ lookback manuel) | ✅ | ✅ | ❌ (à la charge du client) | ❌ |
| **Homologation humaine des listes avant prod** | ✅✅ (différenciant) | ❌ | ❌ | ❌ | ❌ |
| Piste d'audit immuable + config figée par décision | ✅ | n/a | ✅ | ❌ | ❌ |
| Archivage probant des actes officiels | ✅ (PDF EUR-Lex + SHA-256) | ❌ | ❌ | ❌ | ❌ |
| Narratifs IA / copilote | ❌ | ❌ | ✅ | ❌ (explications structurées) | ❌ |

---

## 3. Cadre réglementaire et bonnes pratiques

### 3.1 Guidance Wolfsberg sur le criblage de sanctions

La guidance du Wolfsberg Group structure un programme de criblage autour de : l'approche par les risques, la **génération et le traitement des alertes**, la gestion des listes (list management), et les **lookbacks**. Points directement actionnables pour Fiskr :

- **Triage des alertes** : priorisation par matérialité du risque (match exact vs partiel, nature des informations concordantes — DOB, nationalité, lieu de naissance servent à départager). Fiskr calcule déjà ces signaux (bonus/malus contextuels) mais ne les exploite pas pour **prioriser une file de travail**.
- **Règles de suppression / listes « Good Guys »** : la guidance recommande explicitement la suppression pilotée par règles des faux positifs récurrents, avec gouvernance (justification, revue périodique). C'est l'écart n°3 de Fiskr — et le motif « exclusion + justification modulaire » du mode homologation est directement réutilisable.
- **Adjudication en 4-yeux** : décision d'alerte documentée, avec séparation des rôles. Le système de rôles empilables de Fiskr (`reviewer`) fournit déjà l'infrastructure.
- **Lookbacks** : capacité à re-cribler rétroactivement le stock à la suite d'un changement de liste ou de paramétrage — découle naturellement du re-criblage post-delta (écart n°4).

### 3.2 Exigences françaises (ACPR / DGT)

- Les **lignes directrices conjointes ACPR/DGT sur le gel des avoirs** (nouvelle version publiée en mars 2026) rappellent que la mise en œuvre des mesures de gel nationales est une obligation **autonome** du dispositif LCB-FT : le criblage contre le **registre national des gels** est attendu à l'entrée en relation et en continu. L'ACPR a relevé des carences récurrentes sur ce point lors de ses contrôles.
- La DGT publie le registre national sur **gels-avoirs.dgtresor.gouv.fr avec une API publique ouverte** (JSON, référencée sur data.gouv.fr) — l'intégration est techniquement simple et constitue le quick win de conformité n°1 pour un outil destiné aux assujettis français.
- Côté UE, la Commission publie la **liste consolidée des sanctions financières (fichiers FSF)** en XML sur le webgate FSD — la source officielle machine-readable que le scraping actuel du Journal Officiel tente de reconstituer heuristiquement.

### 3.3 Tendances 2026

- **Re-criblage continu** : rafraîchir le criblage quand les listes **ou** les données client changent, plutôt qu'un criblage ponctuel — standard du marché.
- **Réduction des faux positifs par apprentissage** : les décisions des analystes (vrai/faux positif) alimentent la calibration. Prérequis : disposer d'un cycle de vie d'alertes qui capture ces décisions (écart n°2).
- **Narratifs d'alertes générés par IA** avec analyste human-in-the-loop ; **explicabilité** exigée (EU AI Act pour les usages à haut risque) — l'approche déterministe et tracée de Fiskr est un atout, pas un retard.

---

## 4. Analyse d'écart détaillée

### Écart 1 — Couverture des sources officielles (effort : S à M par source)

**Marché** : tout moteur comparable couvre nativement OFAC + UE + ONU + UK, et pour la France le registre DGT ; les leaders y ajoutent PEP et adverse media.
**Fiskr aujourd'hui** : OFAC SDN_ADVANCED (`fiskr/ingest.py::parse_ofac_advanced_xml`, robuste depuis le correctif 2.7.1), UE par **scraping heuristique du JO** (`fiskr/sync.py::run_eurlex_sync` — ne détecte pas les radiations, dépend de la structure HTML), SSIE générique pour tout XML à références croisées (`fiskr/ssie.py`).
**Améliorations** :
- **DGT gel des avoirs** (S) : nouveau `run_dgt_sync` calqué sur `run_ofac_sync` (téléchargement JSON, hash, delta, supersede, rapport, compatible homologation). Champs du registre déjà couverts par le schéma pivot 26 champs.
- **EU FSF XML consolidé** (M) : nouveau parseur (ou profil SSIE dédié) sur le XML officiel ; le scraping JO actuel devient un complément « fraîcheur J+0 » optionnel. Bénéfice majeur : les **REMOVED** deviennent fiables.
- **ONU consolidée** (S) : XML officiel simple, parseur direct.
- **UK OFSI** (S, P2) et **dataset OpenSanctions** comme source PEP (M, P2).

### Écart 2 — Cycle de vie des alertes / case management (effort : L)

**Marché** : alerte = objet de travail avec statuts (ouverte → en cours → escaladée → close vrai/faux positif), assignation, 4-yeux, commentaires, SLA — intégré chez ComplyAdvantage, Sensa, etc.
**Fiskr aujourd'hui** : `AuditTrail` (`fiskr/database.py`) est un journal immuable en écriture seule — parfait pour l'audit, insuffisant pour le travail quotidien : aucune trace de la **décision humaine** sur une alerte.
**Amélioration** : table `Alert` (FK vers AuditTrail, statut, assigné, décision, commentaire, décidé par/quand) + endpoints de file de travail + onglet dashboard. Réutiliser le motif homologation : rôles (`require_reviewer`), 4-yeux (décision proposée par un analyste, validée par un reviewer), justifications obligatoires modulaires (réglages `app_settings`). Prérequis de toute calibration ML future (les décisions deviennent des données d'entraînement).

### Écart 3 — Liste blanche / suppression des faux positifs (effort : M)

**Marché** : recommandation Wolfsberg explicite (« Good Guys » lists) ; présent chez tous les leaders.
**Fiskr aujourd'hui** : rien au niveau alerte — un client homonyme d'un listé regénère la même alerte à chaque criblage. (Les exclusions du mode homologation opèrent au niveau de la liste, pas de la paire client×listé.)
**Amélioration** : table `WhitelistPair` (client_id × entity_id, justification + pièce jointe **obligatoires modulaires** — motif exclusions réutilisable tel quel, y compris `exclusion_evidence/`), consultée par `/api/screen` avant émission d'alerte (l'audit trail note « supprimée par liste blanche », jamais silencieux), avec date d'expiration/revue périodique conformément à la gouvernance Wolfsberg.

### Écart 4 — Re-criblage automatique post-delta (effort : M)

**Marché** : surveillance continue standard — chaque mise à jour de liste re-crible le portefeuille, seuls les nouveaux hits alertent.
**Fiskr aujourd'hui** : `calculate_delta` (`fiskr/delta.py`) produit ADDED/MODIFIED/REMOVED à chaque sync… stocké dans le rapport et jamais exploité pour re-cribler ; le criblage reste à la demande (`/api/screen`, batch Spark).
**Amélioration** : après promotion d'un snapshot (sync directe ou approbation d'homologation — point d'accroche unique : le bloc « supersede + reload cache »), re-cribler les `ClientEntity` contre les seules entités ADDED/MODIFIED (le blocking limite déjà le produit cartésien), générer les alertes des nouveaux hits, notifier. Donne aussi la capacité de **lookback** Wolfsberg (rejouer sur une période).

### Écart 5 — Matching multi-écritures et seuils contextuels (effort : M)

**Marché** : yente `logic-v2` fait du matching multi-écritures par dictionnaires culturels ; les listes OFAC/ONU portent des alias en cyrillique, arabe, chinois.
**Fiskr aujourd'hui** : `fiskr/quality.py` aplatit les diacritiques latins (Müller → MULLER) mais un alias « Владимир Путин » ne matche jamais « Vladimir Putin » ; seuil unique 75 % (`config.yaml::scoring.cut_off_threshold`) quel que soit le contexte.
**Améliorations** : translittération non-latine (ICU/`unidecode` étendu) appliquée aux alias à l'ingestion **et** aux données client au criblage ; seuils par type d'entité et par liste (les PEP tolèrent un seuil plus haut que le gel des avoirs) via `config.yaml` + réglages à chaud ; restitution **lisible** du `decision_tree` dans le dashboard (à la manière du champ `explanations` de yente) — la donnée existe déjà, seul le rendu manque.

### Écart 6 — Scoring de risque client & pilotage (effort : M)

**Marché** : le niveau de risque client (juridiction, produit, historique) contextualise la priorité des alertes ; tableaux de bord conformité (volumes, taux de FP, délais).
**Fiskr aujourd'hui** : pas de champ risque sur `ClientEntity` ; les rapports se limitent aux syncs.
**Amélioration** : champ `risk_level` sur ClientEntity (alimenté à l'ingestion CSV), priorisation de la file d'alertes par risque × score, page KPI (volumes par statut, taux de faux positifs — disponible dès que l'écart 2 est comblé, délais moyens de traitement, historique des campagnes de lookback).

### Écart 7 — Horizon (effort : L chacun)

- **Filtrage transactionnel** (Fircosoft-like, ISO 20022) : autre métier, n'ouvrir qu'avec un besoin client avéré.
- **Adverse media** : nécessite une source de données presse (le différenciant de Dow Jones/Factiva) — non réaliste sans partenariat données.
- **Narratifs IA** : générer un projet de narratif d'alerte depuis le `decision_tree` (déterministe, donc contexte fiable), décision humaine obligatoire — cohérent avec l'exigence d'explicabilité (EU AI Act).

---

## 5. Feuille de route priorisée

### P0 — Conformité & quick wins (différenciant réglementaire français) — ✅ livré

| Item | Bénéfice | Effort | Dépendances | Fichiers principaux |
|---|---|---|---|---|
| Connecteur **DGT gel des avoirs** (API JSON publique) | Obligation ACPR autonome couverte nativement | S | — | `sync.py` (nouveau `run_dgt_sync`), `config.yaml`, dashboard sync |
| **EU FSF XML consolidé** en remplacement du scraping JO | Radiations fiables, robustesse | M | — | `ingest.py` ou profil SSIE, `sync.py::run_eurlex_sync` |
| **ONU consolidée XML** | Couverture socle du marché | S | — | `ingest.py`/SSIE, `sync.py` |

### P1 — Efficacité analyste (cœur produit) — ✅ livré

| Item | Bénéfice | Effort | Dépendances | Fichiers principaux |
|---|---|---|---|---|
| **Cycle de vie des alertes + 4-yeux** | Standard marché ; capture les décisions (base de toute calibration future) | L | rôles existants | `database.py` (table Alert), `api.py`, dashboard |
| **Liste blanche client×listé** avec justification modulaire | 1er levier de réduction des FP (Wolfsberg) | M | motif exclusions existant | `database.py`, `api.py::screen`, dashboard |
| **Re-criblage automatique post-delta** + lookback | Surveillance continue | M | delta engine existant | `sync.py`, `api.py` (hook post-promotion), scheduler |

### P2 — Différenciation technique — ✅ livré

| Item | Bénéfice | Effort | Statut |
|---|---|---|---|
| Translittération multi-écritures (cyrillique/arabe/CJK) | Rappel sur les alias non latins des listes | M | ✅ `anyascii` dans `quality.strip_accents` |
| Seuils par liste / type d'entité (réglages à chaud) | Calibration fine façon Napier/yente | S | ✅ `scoring.cut_off_overrides` par type de liste (config) |
| Restitution lisible du `decision_tree` dans le dashboard | Explicabilité analyste façon yente `explanations` | S | ✅ livré avec P1-1 (modale d'alerte) + badge WHITELISTED/tooltip seuil |
| Source PEP (dataset OpenSanctions) | Ouvre le criblage PEP annoncé dans le README | M | ✅ `run_pep_sync` (opt-in, contrainte de licence) |
| UK OFSI | Couverture internationale complète | S | ✅ `run_ofsi_sync` (opt-in) |
| Page KPI conformité | Pilotage du dispositif | M | ✅ onglet Pilotage + `GET /api/kpi` |

### P3 — Horizon

Filtrage transactionnel ISO 20022 · adverse media (partenariat données requis) · narratifs d'alertes IA human-in-the-loop fondés sur le `decision_tree`.

---

## 6. Sources

**Marché & comparatifs** : [LSEG — best sanctions screening software 2026](https://www.lseg.com/en/insights/risk-intelligence/the-best-sanctions-screening-software-and-companies-in-2026) · [ComplyAdvantage — vendor comparison](https://complyadvantage.com/vendor/best-sanctions-screening-software/) · [Alessa — Top 10 sanctions screening](https://alessa.com/blog/top-10-sanctions-screening-solutions/) · [SymphonyAI — Top 10 for banks](https://www.symphonyai.com/resources/blog/financial-services/top-10-sanctions-screening-software/)

**Open source** : [OpenSanctions — matching API](https://www.opensanctions.org/docs/api/matching/) · [OpenSanctions — logic-v2](https://www.opensanctions.org/articles/2025-09-11-logic-v2/) · [OpenSanctions — scoring](https://www.opensanctions.org/docs/api/scoring/) · [yente (GitHub)](https://github.com/opensanctions/yente) · [Moov Watchman (GitHub)](https://github.com/moov-io/watchman)

**Réglementaire** : [Wolfsberg Group — Sanctions Screening Guidance](https://wolfsberg-group.org/resources/168/53) · [Lignes directrices ACPR/DGT gel des avoirs](https://acpr.banque-france.fr/system/files/2025-01/20210616_lignes_directrices_gel_des_avoirs.pdf) · [LCB-FT.fr — lignes directrices 2026](https://www.lcb-ft.fr/news/apcr-lignes-directrices-gel-des-avoirs-2026) · [Registre national des gels (DGT)](https://gels-avoirs.dgtresor.gouv.fr/) · [API Gels des avoirs (data.gouv.fr)](https://www.data.gouv.fr/dataservices/api-gels-des-avoirs) · [EU FSF — liste consolidée (data.europa.eu)](https://data.europa.eu/data/datasets/financialsanctions?locale=en) · [EU FSF sur OpenSanctions](https://www.opensanctions.org/datasets/eu_fsf/)

**Tendances 2026** : [sanctions.io — 2026 trends](https://www.sanctions.io/blog/sanctions-and-compliance-2026-trends-to-watch-out-for) · [Genpact — GenAI in sanctions screening](https://www.genpact.com/case-studies/a-leap-in-sanctions-screening-accuracy-and-efficiency) · [Sumsub — AI in sanctions/PEP screening](https://sumsub.com/blog/ai-in-sanctions-pep-screening/) · [Moody's — AML automation](https://www.moodys.com/web/en/us/kyc/solutions/aml-automation.html) · [Hawk — unified case manager](https://hawk.ai/platform/unified-case-manager)

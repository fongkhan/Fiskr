# Fiskr - Moteur de Criblage LBA-CFT & Personnes Politiquement Exposées (PEP)

[![CI](https://github.com/fongkhan/Fiskr/actions/workflows/ci.yml/badge.svg)](https://github.com/fongkhan/Fiskr/actions/workflows/ci.yml)
[![Licence: Sustainable Use](https://img.shields.io/badge/licence-Sustainable%20Use%20(fair--code)-blue)](LICENSE.md)
[![Sponsor](https://img.shields.io/badge/%E2%9D%A4-Sponsoriser-ff69b4)](https://github.com/sponsors/fongkhan)

Fiskr est un moteur de criblage (Screening Engine) de nouvelle génération destiné aux institutions financières. Il permet de confronter le référentiel tiers (clients, mandataires, bénéficiaires effectifs) aux listes de sanctions et de Personnes Politiquement Exposées (PEP) fournies par les éditeurs officiels (OFAC, UE, ONU, Dow Jones, World-Check) conformément aux exigences réglementaires ACPR/AMF.

Le projet propose une API temps réel asynchrone, un script de traitement de masse (Batch) sous Apache Spark, un comparateur de snapshots historiques (Delta Engine), et un tableau de bord interactif pour les agents de conformité.

---

## 🛠️ Architecture et Modules

Le système est structuré autour des modules définis dans le Document d'Architecture Technique (DAT) :

1. **Module 1 : Data Quality Gate & Nettoyage (`fiskr/quality.py`)**
   * **Niveau 1 (Bloquant/Rejet)** : Vérification des champs vides (`Rule_B01`), types d'entités invalides (`Rule_B02`), structure individu invalide (`Rule_B04` - prénom/nom absents après parsing), et longueur de nom insuffisante (`Rule_B05` - moins de 2 caractères).
   * **Niveau 2 (Alerte/Dégradé)** : Absence de pays rattaché (`Rule_M01`), absence de DOB pour les individus vivants (`Rule_M02`), caractères non translittérés (`Rule_M03`), contradiction de statut vital (`Rule_M04` - décès avec date mais booléen à faux), formats de date invalides (`Rule_M05`), numéro de passeport suspect (`Rule_M06`), structure LEI invalide (`Rule_M07`), et score d'extraction PDF faible (`Rule_M08`).
   * **Nettoyage Automatique & Niveau 3** : Normalisation de la casse, aplatissement ASCII (diacritiques/accents Müller -> MULLER), gestion d'incohérence de genre multi-valuée (`Rule_I03` - repli sur `U`), et suppression des suffixes légaux corporatifs (SA, SARL, LLC, GMBH, LTD, SOCIETE) pour les personnes morales via expressions régulières.


2. **Module 2 : Custom Blocking Engine (`fiskr/blocking.py`)**
   * Partitionnement par clé configurable (`config.yaml`) pour éviter le produit cartésien.
   * Utilisation de l'algorithme phonétique **Double Metaphone** sur le premier mot du nom (ex: *Müller* ou *Meller* -> *MLR*).
   * Gestion automatique des valeurs manquantes avec des clés de secours (`XX`).
   * Produit cartésien des clés en cas d'alias multiples ou pays multiples pour garantir un criblage sans omission.

3. **Module 3 : Moteur de Scoring, Hard Match & Ajustements (`fiskr/scoring.py`)**
   * **Priorité Absolue (Hard Match)** : Raccourci exact sur identifiants par ordre de priorité :
     1. Numéro LEI (Personnes Morales - 20 caractères structurels).
     2. Numéro de Passeport + pays émetteur (Personnes Physiques).
     3. Registres Nationaux d'Entreprises (SIREN, TVA, Tax ID) + pays.
     4. Cartes Nationales d'Identité + pays.
     5. Moyens de Transport (Vessel IMO à 7 chiffres, Aircraft Tail registration).
     6. Autres documents d'identité et codes (SWIFT, SWIFT-BIC, etc.).
     * Si l'un des contrôles correspond, le score est verrouillé à `100.0%` avec statut `ALERT`.
   * **Translittération multi-écritures** : les noms en cyrillique, arabe, chinois, grec... sont automatiquement translittérés en latin (bibliothèque `anyascii`) avant normalisation, de sorte que *Владимир Путин* et *VLADIMIR PUTIN* obtiennent un score de 100%.
   * **Score Textuel de Base (Fuzzy)** : Moyenne pondérée hybride : $S_{base} = (0.4 \times JW) + (0.4 \times DL) + (0.2 \times TS)$
     * *Jaro-Winkler (JW)* : Fautes d'orthographe en début de chaîne.
     * *Damerau-Levenshtein (DL)* : Inversions, omissions et insertions.
     * *Token Sort (TS)* : Inversions de mots (ex: *PUTIN Vladimir* vs *Vladimir PUTIN*).
   * **Alias Risk Categorization** : Ingestion dynamique séparant les alias en `high_priority` (inclus dans le fuzzy scoring) et `low_priority` (exclus du scoring, stockés pour consultation humaine).
   * **Ajustements Contextuels (Bonus/Malus)** :
     * Date de Naissance (DOB) : Match exact (`+15`), dans la fenêtre de tolérance (`+5`), hors tolérance (`-15`).
     * Genre : Contradiction homme/femme (`-20`).
     * Géographie : Match sur pays (`+10`), aucun contact trouvé (`-10`).
   * **Seuil Réglementaire (Cut-off)** : Alertes générées si le Score Final $\ge 75\%$. Le seuil est **surchargeable par type de liste** (`scoring.cut_off_overrides`, ex. seuil plus tolérant sur les PEP que sur le gel des avoirs) ; le seuil effectivement appliqué est restitué dans chaque résultat (`cut_off_applied`).

4. **Module 4 & 6 : API Temps Réel & Piste d'Audit (`fiskr/api.py`, `fiskr/database.py`)**
   * Service API asynchrone écrit en **FastAPI**.
   * Indexation et mise en cache des watchlists en mémoire vive à l'initialisation pour des performances optimales (latence $\le 200\text{ms}$).
   * Persistance immuable (SQLAlchemy) avec connexion PostgreSQL cible et **failover automatique sur base SQLite locale** (`fiskr.sqlite3`).

5. **Module 5 : PySpark Batch Engine (`fiskr/batch.py`)**
   * Algorithme Spark de traitement de masse optimisé par **Broadcast Join** pour éliminer le produit cartésien sur le réseau de clusters.

6. **Module 8 : Versioning & Delta Engine (`fiskr/delta.py`)**
   * Tableaux d'historiques d'instantanés (Snapshots) immuables.
   * Analyse différentielle calculant les états `ADDED`, `REMOVED` et `MODIFIED` par comparaison de hashs de lignes (`entity_checksum`).
   * Détection récursive des différences colonnes/nœuds imbriqués ramenée sous forme de dot-path (ex: `countries.residence`) avec affichage d'état *before* et *after*.

---

## 🏃 Ingestion & Connecteurs d'Entrée (`fiskr/ingest.py`, `fiskr/ssie.py`)

L'outil intègre quatre familles de connecteurs génériques pour charger les listes sources :
* **OFAC XML Connector** : Lecture et traitement séquentiels d'un flux XML via `ElementTree.iterparse` pour éviter la saturation de la mémoire vive.
* **CSV Connector** : Parseur de fichiers délimités personnalisables (délimiteur et dictionnaire de colonnes).
* **PDF Connector** : Extracteur textuel via `pypdf` avec analyseur heuritique NER (Named Entity Recognition) pour isoler les navires, identifiants et caractéristiques.
* **Smart Sanctions Ingestion Engine (SSIE)** : Connecteur XML générique et structurellement agnostique (`fiskr/ssie.py`) pour les flux à références croisées par ID (OFAC Advanced, SWIFT SLD, etc.).

S'y ajoutent des **parseurs dédiés aux formats des sources officielles** (voir la section « Synchronisation Automatique des Sources » ci-dessous) : registre DGT des gels des avoirs (JSON), liste consolidée UE FSF (XML), liste consolidée ONU (XML), dataset PEP OpenSanctions (CSV) et liste UK OFSI `ConList` (CSV) — tous utilisables aussi bien par les synchronisations que par l'upload manuel du dashboard.

### Moteur de Détection des Noms d'Individus (`fiskr/names.py`)

Tous les connecteurs partagent un moteur de découpage des noms complets en **prénom(s) / nom de famille**, appliqué lorsque la source ne fournit pas de structure (EUR-Lex, SSIE, CSV, PDF, ajout manuel) — un découpage fourni par la source (parties de noms OFAC XML, colonnes CSV explicites) n'est jamais écrasé. Règles par priorité :

1. **Format « NOM, Prénoms »** : la virgule sépare famille et prénoms.
2. **Signal typographique** : les listes officielles écrivent le nom de famille en CAPITALES et les prénoms en casse mixte — les prénoms multiples sont ainsi préservés quel que soit l'ordre des blocs (*Aleksandr Vladimirovich GUTSAN* → prénoms « Aleksandr Vladimirovich », famille « GUTSAN »), avec rattachement des particules adjacentes (*bin LADIN*, *Le PEN*, *van der...*).
3. **Repli** : sans signal de casse, premier mot = prénom, reste = nom.

### Le Moteur SSIE (Smart Sanctions Ingestion Engine)

Intégré à l'import de listes du dashboard (type de fichier **Smart Sanctions — XML générique**), le pipeline SSIE s'exécute en 3 phases séquentielles à consommation mémoire constante (`iterparse` + `elem.clear()`) :

1. **Étape de Découverte (Phase 1)** : Extraction en continu des ID et Libellés des types de caractéristiques pour alimenter le dictionnaire de référence.
2. **Étape de Résolution (Phase 2)** : Lecture des listés (entités) et jointure dynamique de leurs caractéristiques (Features) avec le dictionnaire de référence — sans codage en dur des types.
3. **Étape de Restitution (Phase 3)** : Pivot dynamique des caractéristiques résolues vers le schéma de criblage Fiskr (26 champs réglementaires) ; les caractéristiques découvertes mais non pivotables sont conservées dans `additional_informations`.

L'**adaptabilité (Change Management)** est assurée par des sélecteurs de balises pivots externes, définis dans la section `ssie` de `config.yaml` et surchargables à chaque import depuis le formulaire (JSON) :

```yaml
ssie:
  source_format: "OFAC_ADVANCED_v1"
  selectors:
    reference_root_tag: ".//ReferenceValueList"
    reference_item_tag: "ReferenceValue"
    entity_root_tag: ".//DistinctParty"
    entity_feature_tag: "Feature"
    mapping_id_attr: "ID"
    mapping_link_attr: "FeatureTypeID"
```

Ainsi, un changement de nomenclature de l'émetteur (ex: `<DistinctParty>` devenant `<EntitiesList>`) se gère par simple reconfiguration des sélecteurs, sans modification de code. Les snapshots SSIE bénéficient des mêmes services que les autres listes : Data Quality Gate, checksums d'entités, Delta Engine et criblage temps réel.

---

## 🛰️ Synchronisation Automatique des Sources (`fiskr/sync.py`)

L'onglet **Gestion des Watchlists → Sources Automatiques** permet de récupérer les listes directement auprès des émetteurs officiels, manuellement ou automatiquement chaque matin :

* **🇺🇸 OFAC — SDN Advanced** : Téléchargement du fichier officiel [`SDN_ADVANCED.XML`](https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML), ingestion en snapshot, **delta** (ADDED / MODIFIED / REMOVED) par rapport à la liste OFAC active, puis application : le nouveau snapshot remplace l'ancien (statut `SUPERSEDED`) dans le cache de criblage. Si le hash du fichier est inchangé, le rapport indique `NO_CHANGE` sans retraitement.
* **🇫🇷 DGT — Registre national des gels des avoirs** : Téléchargement du registre officiel de la Direction générale du Trésor via son **API publique** ([gels-avoirs.dgtresor.gouv.fr](https://gels-avoirs.dgtresor.gouv.fr/)), ingestion en snapshot (personnes physiques → I, personnes morales → E, navires → V, avec normalisation ISO2 des nationalités françaises pour le blocking), **delta** et remplacement de la liste DGT active. La mise en œuvre des mesures de gel nationales étant une **obligation autonome** des établissements assujettis (lignes directrices ACPR/DGT), ce connecteur couvre nativement l'exigence française. Compatible mode homologation et planification quotidienne.
* **🇺🇳 ONU — Liste consolidée du Conseil de sécurité** : Téléchargement du XML officiel public ([scsanctions.un.org](https://scsanctions.un.org/resources/xml/en/consolidated.xml)), ingestion (individus → I, entités → E, alias Good/Low → priorités haute/basse, pays anglais normalisés en ISO2), **delta** et remplacement de la liste ONU active.
* **🇪🇺 UE — Liste consolidée officielle (fichiers FSF)** : Téléchargement du XML consolidé des sanctions financières publié par la Commission (webgate FSD) — la source qui **fait autorité** sur le scraping du JO, avec des **radiations fiables**. Nécessite un token gratuit : créez un compte sur le webgate FSD puis renseignez `sync.eu_fsf.token` dans `config.yaml` et passez `sync.eu_fsf.enabled` à `true`. Partage le type `WATCHLIST_EU` : le snapshot FSF remplace la liste scrapée, et le scraping quotidien du JO (ci-dessous) reste un complément « fraîcheur J+0 » optionnel qui fusionne par-dessus.
* **🇬🇧 UK OFSI — Liste consolidée HM Treasury** *(opt-in)* : Téléchargement du fichier officiel `ConList.csv` (format 2022) publié par l'Office of Financial Sanctions Implementation, regroupement des lignes par Group ID (nom principal + alias), typage Individual → I / Ship → V / autres → E, conversion des dates `jj/mm/aaaa` et normalisation ISO2 des nationalités. À activer (`sync.ofsi.enabled`) selon l'exposition UK de l'établissement.
* **🌐 PEP — OpenSanctions** *(opt-in)* : Téléchargement du dataset consolidé des Personnes Politiquement Exposées d'OpenSanctions (`targets.simple.csv`), ingestion en liste `WATCHLIST_PEP` (individus et organisations liées, alias, dates de naissance partielles normalisées, pays ISO2). ⚠️ **Licence** : l'usage commercial des données OpenSanctions requiert une licence payante ([opensanctions.org/licensing](https://www.opensanctions.org/licensing/)) — le connecteur est désactivé par défaut (`sync.pep.enabled`).
* **🇪🇺 EUR-Lex — Journal Officiel du jour (édition anglaise)** : Lecture de la page du Journal Officiel (série L, **version anglaise, qui fait référence pour la réglementation européenne**) de la date choisie, détection des actes dont le titre mentionne **« restrictive measures »**, puis scraping heuristique des annexes (tableaux et listes numérotées) pour en extraire les listés — Individus (avec date de naissance), Entités, Navires (IMO) et Aéronefs. Le type du listé est déduit de toute la ligne d'annexe, **motifs de la désignation compris** (les indices personnels — pronoms, fonctions, données de naissance — priment sur les mots-clés d'entités cités dans les motifs). Les fiches extraites sont **fusionnées de manière incrémentale** avec la liste EU active (le JO amende la liste, il ne la remplace pas) et le delta est calculé. En l'absence d'acte pertinent, le rapport indique `NO_PUBLICATION`.
* **Archivage probant** : le **PDF officiel** de chaque acte retenu — la version qui **fait foi lors des audits** — est téléchargé dans `eurlex_archives/` avec son empreinte SHA-256 d'intégrité, référencé dans le rapport de synchronisation et téléchargeable depuis l'application (`GET /api/sync/evidence/{fichier}`).

Dans les deux cas, les **ajouts manuels à la volée sont préservés** (le snapshot `manual-watchlist` n'est jamais remplacé), et chaque exécution génère un **rapport de suivi** consultable dans l'application (table `sync_reports`, avec le détail du delta) et envoyé **par email** si un serveur SMTP est configuré dans `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SYNC_EMAIL_TO`).

**Fiabilité réseau** (`sync.network` dans `config.yaml`) : toutes les récupérations HTTP reprennent automatiquement sur les **erreurs de connexion/timeout** (httpx transport) ET sur les statuts transitoires (202 anti-robot, 408/429/5xx) avec backoff linéaire — les 403/404 échouent immédiatement ; les téléchargements de fichiers envoient un **User-Agent navigateur** (les portails officiels filtrent l'UA par défaut) avec un timeout de lecture **par bloc**, et un client HTTP keep-alive partagé évite un handshake TLS par requête. Les **échecs partiels sont visibles** : actes EUR-Lex inaccessibles restitués dans le rapport (`fetch_failures`/`pdf_failures`, badge ⚠ dans l'application, repris au prochain run) et **panne réseau totale → rapport `ERROR`** (jamais un faux `NO_CHANGE`). La **progression** des synchronisations et des imports volumineux est suivie en direct (`GET /api/progress?id=`, jeton d'ingestion, `sync:<source>` ou snapshot_id ; barre de progression pendant l'import, phase vivante dans la table des snapshots).

La planification quotidienne se configure dans `config.yaml` :

```yaml
sync:
  auto_enabled: true         # exécution automatique chaque matin
  schedule_time: "06:00"     # heure locale de déclenchement (HH:MM)
  ofac:
    enabled: true
    url: "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML"
  eurlex:
    enabled: true
    daily_journal_url: "https://eur-lex.europa.eu/oj/daily-view/L-series/default.html?ojDate={date}&locale=en"
    keyword: "restrictive measures"
  dgt:
    enabled: true
    url: "https://gels-avoirs.dgtresor.gouv.fr/ApiPublic/api/v1/publication/derniere-publication-fichier-json"
  eu_fsf:
    enabled: false            # true apres inscription au webgate FSD
    token: ""                 # nom d'utilisateur du webgate
  un:
    enabled: true
    url: "https://scsanctions.un.org/resources/xml/en/consolidated.xml"
  pep:
    enabled: false            # attention a la licence OpenSanctions (usage commercial payant)
    url: "https://data.opensanctions.org/datasets/latest/peps/targets.simple.csv"
  ofsi:
    enabled: false            # liste UK : opt-in selon l'exposition
    url: "https://ofsistorage.blob.core.windows.net/publishlive/2022format/ConList.csv"
```

Les endpoints associés : `POST /api/sync/run` (déclenchement manuel, réservé aux administrateurs), `GET /api/sync/reports` (historique des rapports) et `GET /api/sync/config` (configuration active).

---

## ✅ Mode Homologation — Environnement de Validation avant Production

Certaines banques exigent un **pointage humain** avant qu'une nouvelle liste ne serve au criblage. Le **mode homologation** répond à ce besoin : lorsqu'il est actif, **tout snapshot watchlist entrant** — upload manuel ou synchronisation (manuelle comme planifiée) de n'importe quelle source : OFAC, EUR-Lex, DGT, ONU, UE FSF, PEP, OFSI — prend le statut `PENDING_REVIEW` au lieu d'entrer directement en production. Il est alors **invisible du moteur de criblage** — la liste `READY` précédente reste active — jusqu'à la décision d'un réviseur.

Cycle de vie des snapshots : `PROCESSING → PENDING_REVIEW → READY | REJECTED → SUPERSEDED` (mode inactif : `PROCESSING → READY`, comportement historique inchangé).

* **Activation / désactivation à chaud** : réglage `Homologation obligatoire` modifiable par un admin depuis l'onglet **Gestion des Watchlists → Homologation** (ou `PUT /api/settings/ingestion`), stocké en base (table `app_settings`) avec repli sur les défauts de `config.yaml` (section `ingestion.require_approval`). Aucun redémarrage nécessaire ; désactiver le mode laisse les snapshots déjà en attente approuvables.
* **Revue** : le réviseur consulte le **delta calculé en direct par rapport à la production** (ajouts / modifications / suppressions), parcourt les entités du snapshot, puis **approuve** (promotion `READY`, remplacement des listes antérieures du même type, rechargement du cache) ou **rejette** (commentaire obligatoire, le snapshot n'entre jamais en production mais est conservé pour l'audit). L'identité du réviseur, la date et le commentaire sont tracés sur le snapshot.
* **Exclusions d'entités justifiées** : avant approbation, des listés individuels peuvent être **exclus de la mise en production** (conservés en base pour l'audit, jamais chargés dans le cache ni reconduits par la fusion EUR-Lex). Chaque exclusion s'accompagne d'une **justification texte** et d'une **pièce jointe justificative** (archivée sous `exclusion_evidence/`, retéléchargeable) ; le caractère **obligatoire de chacun des deux champs est modulaire** (`review.exclusion_justification_required`, `review.exclusion_file_required`).
* **Rôle `reviewer` et rôles empilables** : un nouveau rôle dédié à la validation, cumulable avec les autres (ex. `user,reviewer`). L'approbation, le rejet et les exclusions exigent le rôle `reviewer` ou `admin` ; la gestion des réglages reste réservée aux admins.
* **Déduplication consciente de l'attente** : une synchronisation quotidienne dont le fichier correspond à un snapshot déjà en attente d'homologation rend `NO_CHANGE` (pas de doublon chaque matin), et les JO EUR-Lex de jours successifs s'enchaînent sur le snapshot en attente le plus récent sans perte d'amendements.

Endpoints associés : `GET/PUT /api/settings/ingestion`, `GET /api/review/pending`, `GET /api/review/snapshots/{id}` (+ `/entities`), `POST /api/review/snapshots/{id}/exclusions` (+ `/remove`), `GET /api/review/exclusion-evidence/{id}`, `POST /api/review/snapshots/{id}/approve|reject`.

### 🧭 Parcours guidé de production de listes (delta → tests → Good Guys → production)

L'homologation est présentée comme un **parcours en 4 étapes numérotées** (guide complet : **[Documentation/PRODUCTION_DES_LISTES.md](Documentation/PRODUCTION_DES_LISTES.md)**) ; après un import ou une synchro en attente, l'application propose d'ouvrir directement le parcours :

1. **Delta** : compteurs ET détail complet des ajouts / suppressions / modifications (champs modifiés avec valeurs avant → après), calculé en direct contre la production.
2. **Exclusions** : mise à l'écart justifiée des fiches non pertinentes (existant).
3. **Cahier de tests** (`POST /api/review/snapshots/{id}/backtest`) : **criblage à blanc** d'un panel de pseudo-clients contre la liste actuelle ET la liste candidate — mêmes seuils par liste, même liste blanche et **mêmes règles anti-faux positifs actives** que la production, mais **aucune alerte réelle créée**. Restitue les **taux d'interception** des deux univers, l'**écart (%)** comparé au seuil toléré (réglage, défaut 20 %), le verdict `OK`/`WARN`, les **nouvelles alertes** et les alertes résolues ; le rapport est **archivé avec le snapshot** (auditable après promotion). Le panel provient d'une **base clients importée** ou d'un **panel généré** (`POST /api/testpanels/generate`, 50–5000 pseudo-clients : copies exactes, typos, inversions, quasi-collisions, clients neutres — stocké en `CLIENT_TEST_PANEL`, **jamais** repris par le re-criblage réel). **Règle candidate** (`candidate_rule_id`) : une règle anti-FP en brouillon/validation peut être injectée côté candidat uniquement — le rapport chiffre alors l'effet de la règle (suppressions par côté, delta, écart avant/après règles, échantillon des paires supprimées) : liste trop bruyante → coder la règle → relancer le cahier de tests → mesurer l'écart, avant toute validation 4-yeux.
4. **Décision** : approbation/rejet avec rappel du verdict. Si un écart élevé révèle des homonymes (« **Good Guys** »), la sélection multiple des nouvelles alertes alimente `POST /api/whitelist/bulk` (justification commune) avant de relancer le test. Deux réglages à chaud : `review.backtest_max_gap_pct` (seuil d'écart) et `review.backtest_required` (blocage dur : aucun passage en production sans cahier de tests au verdict `OK`).

---

## 🚨 Traitement des Alertes & Surveillance Continue

Le flux de travail post-criblage est documenté en détail dans **[Documentation/ALERTES_ET_SURVEILLANCE_CONTINUE.md](Documentation/ALERTES_ET_SURVEILLANCE_CONTINUE.md)** ; la séparation criblage/filtrage, les blocking keys et les règles anti-faux positifs dans **[Documentation/REGLES_ET_BLOCKING.md](Documentation/REGLES_ET_BLOCKING.md)**. En synthèse :

* **Deux canaux d'alertes distincts** : le **Criblage Clients** (`SCREENING`, référentiel clients × listes) et le **Filtrage Transactionnel** (`FILTERING`, parties des messages `pain.001`/`pacs.008`) sont désormais deux files séparées (`GET /api/alerts?channel=`, compteurs par canal), chacune avec son blocking key et son jeu de règles propres.
* **Cycle de vie des alertes & 4-yeux** : chaque décision `ALERT` ouvre un objet de travail dédupliqué (`OPEN → IN_PROGRESS → PENDING_VALIDATION → CLOSED_CONFIRMED | CLOSED_FALSE_POSITIVE`, escalade possible) ; la clôture exige un validateur **différent du proposeur** (rôle `reviewer`/`admin`, désactivable à chaud), avec historique append-only de chaque action.
* **Blocking keys paramétrables par canal** (`GET/PUT /api/settings/blocking`, rôle `blocking`/admin) : composantes ordonnées (`COUNTRY_ISO`, `ENTITY_TYPE`, `PHONETIC_FIRST`) réglables séparément pour le criblage (rechargement immédiat du cache) et le filtrage (phonétique seule par défaut, données de paiement pauvres).
* **Règles anti-faux positifs en Python** (`/api/fprules`, rôle `rules`/admin) : du code `def rule(ctx) -> bool` supprime les faux positifs (auto-clôture `CLOSED_BY_RULE`, **jamais silencieuse** — `fp_rule_applied` tracé au journal d'audit immuable, conservé pour ACPR/FED). Jeux de règles indépendants par canal. **Mode DEV façon branche/merge** : brouillon → tests unitaires 100 % verts → soumission → validation 4-yeux (validateur ≠ soumetteur) → production ; modifier une règle active crée une nouvelle version brouillon qui remplace l'ancienne à sa validation. Banc d'essai (tests unitaires, rejeu de l'historique réel avec garde-fou vrais positifs, panel de pseudo-clients) ; **fail-open** (une règle en erreur conserve l'alerte). **Atelier d'édition** (zéro lib externe) : palette de clés `ctx` cliquables, snippets, **validation syntaxique serveur** en continu (`POST /api/fprules/validate`, ligne d'erreur cliquable), **autocomplétion** sur `ctx["` et `.get("`, cas de test pré-rempli **depuis une alerte réelle** (`GET /api/fprules/context-from-alert/{id}`). **Création en langage naturel** : formulaire structuré sans IA (conditions typées ET/OU → Python déterministe) ou génération par l'API Claude (`POST /api/fprules/generate`, opt-in `fprules.llm_enabled` + `ANTHROPIC_API_KEY`, erreurs explicites) — dans les deux cas le résultat n'est qu'un **brouillon dans l'éditeur**, le circuit de gouvernance reste inchangé.
* **Liste blanche client×listé** (« Good Guys », Wolfsberg) : suppression gouvernée des faux positifs récurrents — justification et pièce jointe modulaires, expiration de revue, révocation douce, et suppression **jamais silencieuse** (statut `WHITELISTED` tracé dans l'audit).
* **Re-criblage automatique post-delta** : à chaque mise en production d'une liste, le référentiel clients est re-criblé contre les seules entités nouvelles/modifiées ; **lookback manuel** admin (`POST /api/rescreen/run`).
* **Narratifs d'alertes** : projet de narratif d'investigation composé exclusivement depuis les données tracées (decision_tree, seuil, historique), reformulation Claude optionnelle — la décision reste humaine.
* **Adverse media** : revue de presse négative par mots-clés LCB-FT (Google News RSS, fournisseur remplaçable), strictement informative.
* **Filtrage transactionnel ISO 20022** : criblage de toutes les parties d'un message `pain.001` / `pacs.008`, verdict `PASS`/`HIT`, audit + alertes.
* **Pilotage** : KPI conformité (`GET /api/kpi`) — taux de faux positifs, délais de décision, volumétrie, synchronisations, **séries temporelles 30 jours**, ventilation par analyste et par liste, efficacité des règles anti-faux positifs.

* **Case management** : priorité explicite par alerte (CRITIQUE sur hard match, modifiable et journalisée), **échéances SLA** par priorité (réglage à chaud, badge « ⏰ En retard »), pièces jointes justificatives, **rapport d'alerte imprimable** (`GET /api/alerts/{id}/report`, prêt ACPR/FED).
* **Exports CSV** (Excel FR : `;` + BOM) : alertes, journal d'audit et vue base des listes, avec les filtres de l'écran (`/api/export/*.csv`).
* **Journal des actions d'administration** (`admin_audit_log`, append-only) : comptes, réglages (avant → après), purges, révocations — sous-onglet dédié de l'Audit.
* **Notifications métier** : email (SMTP) + webhooks génériques (`notifications.webhooks`), par événement (nouvelle alerte, 4-yeux en attente, snapshot à homologuer, échec de sync), fire-and-forget — jamais bloquant.
* **Graphe de relations & règle des 50 % (OFAC)** : les `ProfileRelationships` du SDN_ADVANCED sont extraits (détenu par, agit pour, associé, famille, dirigeant, soutien) et rafraîchis à chaque sync ; relations manuelles avec % de détention (reviewer/admin). Le **risque hérité par détention majoritaire** (≥ 50 %, transitif, présomption sur les liens OFAC sans %) est affiché dans la fiche et annoté dans le decision tree de chaque criblage. **Visualisation réseau** : modale « 🕸 Graphe » (SVG natif, rendu radial, flèches rouges = détention majoritaire, clic sur un nœud pour recentrer, profondeur 1-3).
* **Planification cron par source** (`fiskr/cron.py`, sans dépendance) : chaque source de synchronisation suit sa propre expression cron 5 champs, modifiable à chaud (`PUT /api/settings/sync`, admin) avec repli sur `config.yaml` puis sur l'horaire quotidien global ; prochaine exécution affichée par source, aucun chevauchement d'une même source.
* **Campagnes de criblage batch persistées** : un CSV de clients (upload ou **dépôt CFT dans l'inbox surveillée** `batch.inbox_dir`) est criblé côté serveur en tâche de fond avec les mêmes garanties que le temps réel (quality gate, liste blanche, règles, audit immuable, alertes) — progression en direct, résultats filtrables, export CSV, rejets quality gate conservés avec motif.
* **Vue client 360°** (`GET /api/clients/{id}/overview`, bouton 👤 de la modale d'alerte) : fiche KYC du dernier référentiel en production, historique de criblage, alertes et paires de liste blanche du client — tout au même endroit pendant l'instruction.
* **Sécurité des accès** : verrouillage de compte après échecs répétés (423, durée et seuil dans `config.yaml security`), politique de mots de passe (12+ caractères, minuscule/majuscule/chiffre), sessions tracées au journal admin (`LOGIN`/`LOGIN_FAILED`/`ACCOUNT_LOCKED`/`LOGOUT` avec IP), cookies durcis et en-têtes HTTP de sécurité sur chaque réponse.
* **Double authentification optionnelle (TOTP, RFC 6238, sans dépendance)** : enrôlement par compte depuis les Paramètres (secret montré une seule fois, activation après un premier code valide), login en 2 temps (code absent → champ redemandé sans compter d'échec ; code faux → compte dans l'anti-brute-force), désactivation protégée par mot de passe, réinitialisation admin en cas de téléphone perdu — le tout tracé au journal d'administration.
* **Actions en masse sur les alertes** (`POST /api/alerts/bulk`, ≤ 200) : sélection multiple dans les files criblage/filtrage, assignation ou changement de priorité en un geste — mêmes règles que les actions unitaires et un `AlertEvent` par alerte (jamais silencieux).
* **Digest conformité planifié** : synthèse KPI envoyée par email/webhooks à heure fixe (cron 5 champs à chaud, défaut 8h00 en semaine) — files ouvertes, retards SLA, 4-yeux, homologations, volumétrie 24 h et santé des synchronisations.
* **Rétention des données (RGPD / archivage)** : durée de conservation à chaud par famille (décisions de criblage, alertes clôturées, rapports de sync, campagnes batch ; 0 = illimité), purge quotidienne planifiée + purge manuelle, prévisualisation des volumes. Garde-fous : minimum 30 jours, **journal admin jamais purgé**, décisions de criblage encore liées à une alerte conservée jamais supprimées ; chaque purge tracée `RETENTION_PURGE`.
* **Vues sauvegardées** : chaque analyste mémorise ses combinaisons de filtres des files d'alertes sous un nom et les restaure en un clic (par utilisateur, mise à jour au même nom).
* **Rapport d'activité sur période** (`GET /api/reports/activity`) : synthèse réglementaire (criblages, alertes créées/décidées, délais, escalades, liste blanche, syncs, batch) avec export CSV et rapport HTML imprimable — carte dédiée dans Pilotage.
* **Archivage avant purge** : chaque purge de rétention peut d'abord vider les enregistrements condamnés en JSON Lines dans `retention_archive/<horodatage>/` (activé par défaut, chemin tracé dans `RETENTION_PURGE`) — purge réversible hors ligne, dossier à externaliser par l'exploitation.
* **Charge de travail des analystes** (`GET /api/alerts/workload`) : alertes ouvertes par assigné et par priorité, retards SLA, prochaine échéance, 4-yeux en attente et file non assignée — carte dédiée dans Pilotage pour répartir le travail.
* **Portabilité de la configuration** : export/import JSON des réglages à chaud entre environnements (recette → production), sans aucun secret, clés inconnues ignorées, delta journalisé `SETTINGS_IMPORTED`.
* **Interface en 6 langues** (français, anglais, allemand, espagnol, chinois, arabe) : moteur i18n maison sans dépendance (`i18n.js`), sélecteur de langue dans le header et sur la page de connexion, persistance locale, traduction du contenu dynamique en continu (MutationObserver) et **passage complet en RTL pour l'arabe**. Couverture : libellés, tableaux, formulaires, **tous les paragraphes descriptifs**, chaînes composées (pagination, compteurs) et **dates/nombres localisés** selon la langue active. Toute chaîne non traduite retombe sur le français.
* **Délégation d'absence** : pendant une absence déclarée (carte 🌴 des Paramètres ou admin), les assignations d'alertes sont redirigées vers le délégué et les alertes ouvertes peuvent lui être réassignées immédiatement — chaque mouvement tracé.
* **Seuils de score à chaud** (`PUT /api/settings/scoring`, admin) : cut-off global et surcharges par liste modifiables sans redémarrage, appliqués au criblage et au filtrage transactionnel.
* **Rôle auditeur lecture seule** (`auditor`, exclusif) : accès intégral en consultation, toute écriture refusée (403) — pour un contrôleur externe, en session comme par clé d'API.
* **Messages d'API multilingues** : les champs `detail`/`message` des réponses JSON sont traduits selon l'en-tête `Accept-Language` (EN/DE/ES/ZH/AR, catalogue + gabarits pour les messages à variables, repli français) — les toasts d'erreur suivent la langue de l'interface de bout en bout.
* **Dossier d'investigation** (`GET /api/alerts/{id}/casefile`, bouton 📁 de la modale d'alerte) : alerte, arbre de décision, historique, pièces jointes, contexte client, relations et règle des 50 % — avec une **checklist d'instruction paramétrable** (chaque coche tracée dans l'historique append-only) et un **dossier imprimable** (→ PDF) à remettre au régulateur.
* **Simulation d'impact des seuils** (`POST /api/settings/scoring/simulate`) : rejeu du journal d'audit des N derniers jours avec les seuils candidats — alertes en plus/en moins par liste, sans aucune écriture — le réglage des cut-offs devient piloté par les données.
* **Clés d'API techniques** (`fsk_…`, carte admin des Paramètres) : comptes de service pour les intégrations (CFT, supervision) — clé montrée une seule fois, hash SHA-256 stocké, authentification `X-API-Key`, révocation immédiate, rôle admin interdit (moindre privilège).
* **Healthcheck** `GET /api/health` non authentifié (statut/base/cache, volontairement minimal) pour load-balancers et supervision.
* **Projet de déclaration de soupçon TRACFIN** (`GET /api/alerts/{id}/str-draft` + `/print`, rôle reviewer/admin, bouton « 🇫🇷 Projet de déclaration » de la modale dossier 📁) : projet **pré-rempli** aux rubriques d'une télédéclaration — déclarant (section `institution` de `config.yaml` : nom, SIREN, correspondant), personne concernée (KYC du référentiel en production), personne listée (programmes, motifs de désignation, référence officielle), motifs tracés (scores, seuil appliqué, ajustements, règle des 50 %) et chronologie append-only. **Aucune transmission automatique** (ERMES est un portail humain) : bandeau « projet à valider par le correspondant TRACFIN », génération tracée `STR_DRAFT_GENERATED` dans l'historique de l'alerte.
* **Qualité des données clients** (`GET /api/quality/clients`, carte « 🧪 Qualité des Données Clients » de Pilotage) : complétude des champs KYC du référentiel en production (barres vert ≥ 95 % / orange ≥ 80 % / rouge), ventilation par segment, **fiches à risque pour le criblage** (PP sans date de naissance, fiches sans pays, PP sans prénom) et score global — un dossier incomplet dégrade la précision du criblage.

### 🔗 Intégration SI amont (webhooks entrants)

Deux endpoints permettent au SI amont (core banking, CRM) de pousser des demandes vers Fiskr, **authentifiés par clé d'API `fsk_`** (en-tête `X-API-Key`, comptes de service ci-dessus — les sessions humaines sont refusées) :

* **`POST /api/hooks/screening`** — criblage temps réel : même charge utile et même réponse que `POST /api/screen` (même cœur de criblage : quality gate, blocking, scoring, liste blanche, règles anti-FP, audit immuable, alerte).
* **`POST /api/hooks/client-upsert`** — création/mise à jour d'une fiche client unitaire dans le dernier référentiel `CLIENT_BASE` en production (tracée `CLIENT_UPSERT_HOOK` au journal des actions d'administration).

Garanties d'intégration :

* **Signature HMAC facultative** : si `hooks.secret` est renseigné dans `config.yaml`, l'en-tête `X-Fiskr-Signature` (HMAC-SHA256 hexadécimal du corps brut) devient obligatoire sur ces endpoints.
* **Idempotence** : l'en-tête `X-Idempotency-Key` (recommandé) garantit qu'une retransmission (retry réseau de l'appelant) **rejoue la réponse d'origine** sans recribler ni dupliquer (`X-Idempotency-Replayed: true` sur la réponse rejouée) ; les livraisons sont conservées 90 jours (table `hook_deliveries`, auto-nettoyée).

```bash
# Criblage temps réel signé + idempotent
BODY='{"client_id":"CUST-001","client_type":"PP","client_first_name":"Vladimir","client_last_name":"Putin","client_dob":"1952-10-07","client_countries":{"nationality":["RU"]}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$FISKR_HOOKS_SECRET" -hex | awk '{print $NF}')
curl -X POST https://fiskr.example/api/hooks/screening \
  -H "X-API-Key: $FISKR_API_KEY" -H "Content-Type: application/json" \
  -H "X-Fiskr-Signature: $SIG" -H "X-Idempotency-Key: req-2026-07-24-0001" \
  -d "$BODY"

# Upsert d'une fiche client dans le référentiel en production
curl -X POST https://fiskr.example/api/hooks/client-upsert \
  -H "X-API-Key: $FISKR_API_KEY" -H "Content-Type: application/json" \
  -H "X-Idempotency-Key: crm-evt-88412" \
  -d '{"client_id":"CUST-001","client_type":"PP","client_first_name":"Jean","client_last_name":"Dupont","client_dob":"1980-01-01","client_email":"jean@exemple.fr"}'
```

### 🖥️ Interface (dashboard)

* **Vue d'ensemble** (onglet d'accueil) : tuiles cliquables (alertes ouvertes par canal, 4-yeux en attente, homologation, taux de FP, délai moyen), graphiques SVG natifs sans dépendance (courbe 30 jours créées/clôturées, barres fiches par liste, donut des statuts), alertes les plus anciennes à traiter et dernière synchronisation.
* **Thème clair / sombre** commutable (bouton 🌙/☀️ du header, persisté), design system 100 % piloté par tokens CSS.
* **Responsive** : sidebar rétractable (hamburger + overlay) sous 1024 px, formulaires en une colonne sur mobile.
* **Tri des colonnes** sur toutes les tables (tri API validé pour la vue base paginée), squelettes de chargement, états vides homogènes, fermeture des modales à Échap, statuts affichés en français.
* **Recherche globale Ctrl+K** : palette de commande (listés — y compris fuzzy —, alertes, navigation), entièrement au clavier.
* **Liens profonds** : chaque onglet/sous-onglet est adressable par l'URL (`#alerts/subtab-filtering-queue`, …) — écran restauré au chargement, navigation arrière/avant du navigateur respectée, liens partageables entre analystes.
* **Cloche de notifications** : badge du nombre d'éléments à traiter et panneau déroulant (alertes ouvertes par canal, 4-yeux en attente, alertes en retard SLA, snapshots à homologuer) avec accès direct en un clic.
* **Pagination serveur** des files d'alertes et de la liste blanche (100 par page) ; **glisser-déposer** des fichiers sur les zones d'import (listes, batch, transactions).

---

## 📋 Référentiel des 26 Champs Réglementaires de Criblage

Le moteur intègre 26 champs obligatoires de conformité AML/CFT, tous exploitables lors de l'ingestion de fichiers ou du screening temps réel :

1. **ID** (`entity_id` / `client_id`) : Identifiant unique de l'enregistrement.
2. **Type** (`entity_type` / `client_type`) : Catégorie d'entité (PP: Individu, PM: Personne Morale, V: Navire, O: Autre).
3. **Gender** (`gender` / `client_gender`) : Genre (M, F, U).
4. **Last Name** (`client_last_name` / `last_name`) : Nom de famille de l'individu.
5. **First Name** (`client_first_name` / `first_name`) : Prénom de l'individu.
6. **Maiden Name** (`client_maiden_name` / `maiden_name`) : Nom de jeune fille.
7. **Nationality** (`countries.citizenship` / `client_countries.nationality`) : Codes pays de nationalité — portés par le champ structuré `countries`, qui regroupe aussi résidence, pays de naissance et juridiction.
8. **Place of Birth** (`place_of_birth` / `client_place_of_birth`) : Lieu de naissance (Ville/Pays).
9. **Date of Birth** (`dates_of_birth` / `client_dob`) : Dates de naissance multiples (sanctions) ou unitaire (client).
10. **Adress** (`address` / `client_address`) : Adresse postale principale.
11. **City** (`city` / `client_city`) : Ville de résidence.
12. **State** (`state` / `client_state`) : Région / État.
13. **Country** (`country` / `client_country`) : Pays associé.
14. **Date of Death** (`date_of_death` / `client_date_of_death`) : Date de décès de l'individu.
15. **Origin** (`origin` / `client_origin`) : Origine / Source de la fiche.
16. **Designation** (`designation` / `client_designation`) : Fonction de la personne (ex: Chef d'État, Diplomate).
17. **Additional Informations** (`additional_informations` / `client_additional_informations`) : Notes réglementaires et métadonnées.
18. **Alternatives Adresses** (`alternative_addresses` / `client_alternative_addresses`) : Adresses secondaires.
19. **Aliases** (`aliases`) : Liste d'alias qualifiés.
20. **Jurisdiction Country** (`jurisdiction_country`) : Pays de juridiction ou d'immatriculation.
21. **IMO Code** (`imo_number` / `transaction_vessel_imo`) : Code d'identification des navires.
22. **Passport ID** (`passport_documents` / `client_passport_documents`) : Numéro et pays de passeport.
23. **National ID** (`national_id_documents` / `client_national_id_documents`) : Numéro et pays de carte nationale d'identité.
24. **Tail Number** (`aircraft_tail_number` / `transaction_aircraft_registration`) : Immatriculation d'aéronef.
25. **Legal Entity Identifier** (`lei_number` / `client_lei_number`) : Identifiant d'entité juridique à 20 caractères.
26. **Designation Reasons** (`designation_reasons`) : Motifs de la désignation / de l'inscription sur liste (extraits de la colonne « Motifs » des annexes EUR-Lex, des libellés SSIE « motif / reason / grounds », ou saisis manuellement).

### Champs étendus (extraction structurée des sources)

Au-delà du référentiel réglementaire, chaque fiche listée porte **26 colonnes étendues** extraites automatiquement des sources officielles (OFAC SDN_ADVANCED, ONU, UE FSF, DGT, UK OFSI, PEP OpenSanctions) — auparavant fondues dans le texte libre `additional_informations` :

| Groupe | Champs | Usage |
|---|---|---|
| **Matching (hard match)** | `crypto_wallets` (`[{currency, address}]`), `bic_swift`, `tax_id`, `vessel_mmsi`, `vessel_call_sign` | Nouvelles clés de correspondance exacte (score 100) : adresse crypto, BIC/SWIFT (8/11, comparaison banque sur 8), identifiant fiscal, MMSI et indicatif radio navire |
| **Identifiants** | `duns_number` | Consultatif (pas de miroir client fiable) |
| **Navires / Aéronefs** | `vessel_flag`, `vessel_type`, `vessel_tonnage`, `vessel_owner`, `aircraft_model`, `aircraft_operator`, `aircraft_construction_number` | Enrichissement des fiches V / A |
| **Détection & tri** | `sanction_programs` (liste), `listed_on`, `delisted_on`, `name_original_script`, `title`, `pep_role`, `secondary_sanctions_risk`, `designating_state` | Programmes structurés, dates d'inscription, script d'origine (aussi conservé en alias de matching), fonction PEP |
| **Personnes morales** | `organization_established_date`, `organization_type` | Date de création et forme juridique |
| **Contacts** | `phone_numbers`, `email_addresses`, `websites` (listes) | Investigations |

Tous ces champs sont **cherchables** (recherche par champ de l'onglet Watchlist Active, groupes Références / Identifiants / Contact), **éditables** (PATCH journalisé, modale de détails) et acceptés dans les **CSV d'import** (colonnes du même nom ; les champs liste se découpent sur `;`).

Côté **clients**, 14 colonnes KYC miroirs sont acceptées à l'ingestion `CLIENT_BASE` : `client_iban`, `client_bic`, `client_tax_id`, `client_phone`, `client_email`, `client_website`, `client_crypto_wallets` (`;`), `client_risk_rating`, `client_pep_flag`, `client_segment`, `client_activity_sector`, `client_activity_countries` (`,`), `client_relationship_start`, `client_status`. Les miroirs de matching (`client_bic`, `client_tax_id`, `client_crypto_wallets`, `transaction_vessel_mmsi`, `transaction_vessel_call_sign`) sont aussi acceptés par `POST /api/screen`, et le **filtrage ISO 20022** croise désormais le BIC des agents bancaires (`DbtrAgt`/`CdtrAgt`) avec le `bic_swift` des institutions sanctionnées.

### Configuration de Sécurité & Fichier `.env`

Les secrets de l'application et la chaîne de connexion à la base de données ne sont plus stockés en clair dans `config.yaml`. Ils sont configurables via les variables d'environnement ou le fichier `.env` à la racine du projet (un modèle est fourni dans [`.env.example`](file:///e:/Program%20Files/git/Fiskr/.env.example)) :

```env
# Connexion PostgreSQL / Base de données
DB_USER=postgres
DB_PASSWORD=votre_mot_de_passe_securise
DB_HOST=localhost
DB_PORT=5438
DB_NAME=fiskr

# Clé Secrète JWT & Compte Administrateur Initial
SECRET_KEY=votre_cle_secrete_jwt_32_caracteres
ADMIN_USERNAME=admin
ADMIN_PASSWORD=adminpassword
```

---

## 🚀 Installation & Lancement

### Prérequis
* Python 3.10 ou supérieur (développé et validé sous Python 3.13.1)
* Dépendances principales : `fastapi`, `uvicorn`, `sqlalchemy`, `pydantic`, `pyyaml`, `python-dotenv`, `pyjwt`, `python-multipart`, `pypdf`, `anyascii`, `faker`, `pytest`. Optionnel : `anthropic` (reformulation LLM des narratifs d'alertes, voir `narrative.llm_enabled` ; génération de règles anti-FP en langage naturel, voir `fprules.llm_enabled`).

### Déploiement local
1. Installez les dépendances :
   ```bash
   pip install -r requirements.txt
   ```
2. Créez votre fichier `.env` à partir du modèle :
   ```bash
   cp .env.example .env
   ```

### 1. Démarrer le Serveur et Accéder à l'Interface Sécurisée
Lancez le serveur web avec Uvicorn :
```bash
python -m uvicorn fiskr.api:app --host 127.0.0.1 --port 8000 --reload
```
Ouvrez votre navigateur sur : **`http://127.0.0.1:8000/`**

1. Vous serez automatiquement redirigé vers la page de connexion **`/login`**.
2. Connectez-vous avec les identifiants administrateur (par défaut : **`admin`** / **`adminpassword`**).
3. Une fois authentifié, un jeton JWT sécurisé et un cookie `HttpOnly` sont générés, vous donnant accès au dashboard de contrôle.

Le dashboard interactif se compose de 7 onglets principaux :
* **Gestion des Watchlists** : **Consultation en direct de la base de données des listés** (`GET /api/watchlist/db` — recherche **sur n'importe quel champ** via le sélecteur de champ (`search_field` : alias, pays, adresses, documents, référence officielle… ou « tout champ »), **tolérante aux fautes de frappe** (repli fuzzy Jaro-Winkler classé par similarité, uniquement quand la recherche exacte ne donne rien), filtres par liste et par statut, pagination côté serveur ; y compris hors production : snapshots en attente d'homologation, remplacés, rejetés et entités exclues), fenêtre de détails des 26 attributs AML, **édition contrôlée des fiches en production** (`PATCH /api/watchlist/entity/{id}`, réservée aux rôles reviewer/admin) avec **journal des modifications** immuable (qui, quand, avant → après, consultable dans la fiche) et **référence officielle datée** (extraite des sources UE/ONU/DGT/OFSI ; sa date de mise à jour peut être ramenée à la date du jour lors d'un patch), **import de fichiers** (sous-onglet dédié), **Snapshots & Comparateur** (Delta Engine, filtre par liste), sources automatiques, **mode homologation** et ajouts manuels via formulaire adaptatif.
* **Criblage** : Crible temps réel unitaire (Sandbox avec champs s'adaptant au type de tiers), crible de masse (simulateur batch) et **filtrage transactionnel ISO 20022** (messages `pain.001` / `pacs.008`). Les trois acceptent un **périmètre de listes restreint** (`screening_lists`, défaut toutes — toute restriction est tracée dans l'audit) ; un criblage en alerte affiche un **lien direct « Instruire l'alerte »**.
* **Alertes** : Deux sous-onglets — **File de Travail** (cycle de vie complet, validation 4-yeux, filtre par liste, projet de narratif et adverse media dans la modale) et **Liste Blanche** client×listé.
* **Pilotage** : Page de KPI conformité (taux de faux positifs, délais de décision, volumétrie des listes, dernières synchronisations).
* **Audit** : Historique réglementaire complet (Compliance Audit Trail) conforme aux normes ACPR/AMF — **paginé et filtrable** par décision et type de liste.
* **Paramètres** *(Réservé aux Administrateurs)* : Les 7 **réglages de gouvernance à chaud** (homologation, exclusions, 4-yeux, liste blanche, re-criblage automatique) regroupés dans un onglet dédié.
* **Utilisateurs** *(Réservé aux Administrateurs)* : Interface de gestion des utilisateurs, création de comptes, réinitialisation de mots de passe et attribution des rôles empilables (`admin` / `reviewer` / `user`).

L'interface n'utilise **aucun popup natif** du navigateur : confirmations et saisies réglementaires passent par des modales intégrées, les résultats par des toasts ; les badges de la barre latérale (alertes ouvertes, homologations en attente) se rafraîchissent automatiquement (`GET /api/counters`).

Chaque utilisateur peut également cliquer sur son profil en bas de la barre latérale pour modifier son nom complet ou changer son mot de passe en autonomie.

### 2. Lancer la Suite de Tests
Exécutez la suite complète de 153 tests automatisés avec pytest :
```bash
python -m pytest
```



---

## 📜 Licence & Offre Commerciale

Fiskr est distribué sous la **[Sustainable Use License](LICENSE.md)** (modèle **[fair-code](https://faircode.io)**), copyright © 2026 **Alexis Vuadelle** :

* ✅ **Libre pour l'usage interne et personnel** : toute organisation peut déployer, utiliser et modifier Fiskr **gratuitement** pour ses propres besoins (y compris en production bancaire). Le code source est public, auditable et ouvert aux contributions.
* 💼 **Commercialisation réservée** : la revente du logiciel, son hébergement pour des tiers contre rémunération et les prestations associées sont réservés au titulaire. **Déploiement on-premise accompagné, support et licences commerciales : sur demande payante** — contactez [@fongkhan](https://github.com/fongkhan) sur GitHub.
* ❤️ **Soutenir le projet** : le sponsoring est bienvenu via [GitHub Sponsors](https://github.com/sponsors/fongkhan).

> Note de transparence : la Sustainable Use License est une licence *fair-code* « source disponible », pas une licence open source au sens de l'OSI (elle restreint l'usage commercial par des tiers).

---

## 📚 Documentation Complémentaire

* **[Document d'Architecture Technique](Documentation/Document%20Architecture%20Technique.md)** — conception détaillée des modules.
* **[Production des Listes — Parcours Guidé](Documentation/PRODUCTION_DES_LISTES.md)** — processus métier de mise en production d'une liste : import, delta détaillé, cahier de tests sur pseudo-clients (taux d'interception), Good Guys en masse, promotion, réglages de gouvernance et bonnes pratiques.
* **[Criblage, Filtrage, Blocking Keys & Règles Anti-Faux Positifs](Documentation/REGLES_ET_BLOCKING.md)** — séparation des deux canaux d'alertes, paramétrage des blocking keys par canal, et moteur de règles Python avec mode DEV (contrat `rule(ctx)`, cycle branche/tests/4-yeux/production, banc d'essai, gouvernance des droits).
* **[Traitement des Alertes & Surveillance Continue](Documentation/ALERTES_ET_SURVEILLANCE_CONTINUE.md)** — guide fonctionnel du flux post-criblage : cycle de vie des alertes et 4-yeux, liste blanche, re-criblage automatique et lookback, narratifs, adverse media, filtrage transactionnel ISO 20022, KPI et récapitulatif des réglages à chaud.
* **[Benchmark Concurrentiel & Feuille de Route](Documentation/BENCHMARK_CONCURRENTS.md)** — analyse du marché du criblage sanctions/PEP (World-Check, ComplyAdvantage, yente, Watchman...), cadre réglementaire (Wolfsberg, ACPR/DGT) et feuille de route d'amélioration priorisée — **intégralement livrée (P0 → P3)**.

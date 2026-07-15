# Fiskr - Moteur de Criblage LBA-CFT & Personnes Politiquement Exposées (PEP)

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

L'outil intègre quatre types de connecteurs pour charger les listes sources :
* **OFAC XML Connector** : Lecture et traitement séquentiels d'un flux XML via `ElementTree.iterparse` pour éviter la saturation de la mémoire vive.
* **CSV Connector** : Parseur de fichiers délimités personnalisables (délimiteur et dictionnaire de colonnes).
* **PDF Connector** : Extracteur textuel via `pypdf` avec analyseur heuritique NER (Named Entity Recognition) pour isoler les navires, identifiants et caractéristiques.
* **Smart Sanctions Ingestion Engine (SSIE)** : Connecteur XML générique et structurellement agnostique (`fiskr/ssie.py`) pour les flux à références croisées par ID (OFAC Advanced, SWIFT SLD, etc.).

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

Certaines banques exigent un **pointage humain** avant qu'une nouvelle liste ne serve au criblage. Le **mode homologation** répond à ce besoin : lorsqu'il est actif, **tout snapshot watchlist entrant** (upload manuel, synchronisation manuelle ou planifiée OFAC/EUR-Lex) prend le statut `PENDING_REVIEW` au lieu d'entrer directement en production. Il est alors **invisible du moteur de criblage** — la liste `READY` précédente reste active — jusqu'à la décision d'un réviseur.

Cycle de vie des snapshots : `PROCESSING → PENDING_REVIEW → READY | REJECTED → SUPERSEDED` (mode inactif : `PROCESSING → READY`, comportement historique inchangé).

* **Activation / désactivation à chaud** : réglage `Homologation obligatoire` modifiable par un admin depuis l'onglet **Gestion des Watchlists → Homologation** (ou `PUT /api/settings/ingestion`), stocké en base (table `app_settings`) avec repli sur les défauts de `config.yaml` (section `ingestion.require_approval`). Aucun redémarrage nécessaire ; désactiver le mode laisse les snapshots déjà en attente approuvables.
* **Revue** : le réviseur consulte le **delta calculé en direct par rapport à la production** (ajouts / modifications / suppressions), parcourt les entités du snapshot, puis **approuve** (promotion `READY`, remplacement des listes antérieures du même type, rechargement du cache) ou **rejette** (commentaire obligatoire, le snapshot n'entre jamais en production mais est conservé pour l'audit). L'identité du réviseur, la date et le commentaire sont tracés sur le snapshot.
* **Exclusions d'entités justifiées** : avant approbation, des listés individuels peuvent être **exclus de la mise en production** (conservés en base pour l'audit, jamais chargés dans le cache ni reconduits par la fusion EUR-Lex). Chaque exclusion s'accompagne d'une **justification texte** et d'une **pièce jointe justificative** (archivée sous `exclusion_evidence/`, retéléchargeable) ; le caractère **obligatoire de chacun des deux champs est modulaire** (`review.exclusion_justification_required`, `review.exclusion_file_required`).
* **Rôle `reviewer` et rôles empilables** : un nouveau rôle dédié à la validation, cumulable avec les autres (ex. `user,reviewer`). L'approbation, le rejet et les exclusions exigent le rôle `reviewer` ou `admin` ; la gestion des réglages reste réservée aux admins.
* **Déduplication consciente de l'attente** : une synchronisation quotidienne dont le fichier correspond à un snapshot déjà en attente d'homologation rend `NO_CHANGE` (pas de doublon chaque matin), et les JO EUR-Lex de jours successifs s'enchaînent sur le snapshot en attente le plus récent sans perte d'amendements.

Endpoints associés : `GET/PUT /api/settings/ingestion`, `GET /api/review/pending`, `GET /api/review/snapshots/{id}` (+ `/entities`), `POST /api/review/snapshots/{id}/exclusions` (+ `/remove`), `GET /api/review/exclusion-evidence/{id}`, `POST /api/review/snapshots/{id}/approve|reject`.

---

## 📋 Référentiel des 26 Champs Réglementaires de Criblage

Le moteur intègre 26 champs obligatoires de conformité AML/CFT, tous exploitables lors de l'ingestion de fichiers ou du screening temps réel :

1. **ID** (`entity_id` / `client_id`) : Identifiant unique de l'enregistrement.
2. **Type** (`entity_type` / `client_type`) : Catégorie d'entité (PP: Individu, PM: Personne Morale, V: Navire, O: Autre).
3. **Gender** (`gender` / `client_gender`) : Genre (M, F, U).
4. **Last Name** (`client_last_name` / `last_name`) : Nom de famille de l'individu.
5. **First Name** (`client_first_name` / `first_name`) : Prénom de l'individu.
6. **Maiden Name** (`client_maiden_name` / `maiden_name`) : Nom de jeune fille.
7. **Nationality** (`nationality`) : Code pays de nationalité.
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
* Dépendances principales : `fastapi`, `uvicorn`, `sqlalchemy`, `pydantic`, `pyyaml`, `python-dotenv`, `pyjwt`, `python-multipart`, `pypdf`, `faker`, `pytest`.

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

Le dashboard interactif se compose de 6 onglets principaux :
* **Gestion des Watchlists** : Permet de consulter la watchlist active (avec pagination rapide et **fenêtre de détails modale** affichant les 26 attributs AML au clic), d'importer de nouveaux snapshots de listes (XML, CSV, PDF, JSON), de comparer les versions historiques via le **Delta Engine**, de piloter le **mode homologation** et d'effectuer des **ajouts manuels à la volée via un formulaire adaptatif** (Individu, Entité, Navire, Autre).
* **Criblage** : Regroupe le crible temps réel unitaire (Sandbox avec **champs de saisie s'adaptant dynamiquement au type de tiers recherché**), le crible de masse (simulateur batch) et le **filtrage transactionnel ISO 20022** : soumission d'un message de paiement `pain.001` ou `pacs.008` (toute version mineure), extraction et criblage de **toutes les parties** (donneur d'ordre, bénéficiaire, ultimes, banques par BIC), verdict global **PASS / HIT** (`POST /api/transactions/screen`) — chaque partie criblée est tracée dans le journal d'audit et chaque hit ouvre une alerte de travail.
* **Alertes** : File de travail des alertes de criblage avec **cycle de vie complet** (ouverte → en cours → décision proposée → close vrai/faux positif, escalade possible), **validation 4-yeux** (le validateur, rôle `reviewer` ou `admin`, doit être différent du proposeur — désactivable à chaud), explication du score (decision tree) et **historique append-only** de chaque action. La modale d'investigation propose aussi un **projet de narratif** généré depuis les données d'audit (`POST /api/alerts/{id}/narrative` — déterministe par construction, reformulation Claude optionnelle via `narrative.llm_enabled`, la décision restant humaine) et une recherche **adverse media** (revue de presse négative par mots-clés LCB-FT, `GET /api/adverse-media` — purement informative).
* **Liste blanche & surveillance continue** *(dans l'onglet Alertes)* : paires client×listé en **liste blanche** (« Good Guys ») avec justification gouvernée, expiration de revue et révocation motivée — chaque suppression d'alerte reste tracée dans l'audit (statut `WHITELISTED`) ; **re-criblage automatique** du référentiel clients contre les seules entités nouvelles/modifiées à chaque mise à jour de liste (sync, upload, approbation d'homologation), plus un **lookback manuel** admin (`POST /api/rescreen/run`).
* **Pilotage** : Page de KPI conformité (`GET /api/kpi`) — encours d'alertes par statut, **taux de faux positifs**, délai moyen de décision, paires en liste blanche actives, volumétrie des listes en production par type, snapshots par statut, répartition des décisions de criblage et dernières synchronisations.
* **Audit** : Historique réglementaire complet (Compliance Audit Trail) conforme aux normes ACPR/AMF.
* **Utilisateurs** *(Réservé aux Administrateurs)* : Interface de gestion des utilisateurs, création de comptes, réinitialisation de mots de passe et attribution des rôles empilables (`admin` / `reviewer` / `user`).

Chaque utilisateur peut également cliquer sur son profil en bas de la barre latérale pour modifier son nom complet ou changer son mot de passe en autonomie.

### 2. Lancer la Suite de Tests
Exécutez la suite complète de 144 tests automatisés avec pytest :
```bash
python -m pytest
```



---

## 📚 Documentation Complémentaire

* **[Document d'Architecture Technique](Documentation/Document%20Architecture%20Technique.md)** — conception détaillée des modules.
* **[Benchmark Concurrentiel & Feuille de Route](Documentation/BENCHMARK_CONCURRENTS.md)** — analyse du marché du criblage sanctions/PEP (World-Check, ComplyAdvantage, yente, Watchman...), cadre réglementaire (Wolfsberg, ACPR/DGT) et feuille de route d'amélioration priorisée (P0 → P3).

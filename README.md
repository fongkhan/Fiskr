# Fiskr - Moteur de Criblage LBA-CFT & Personnes Politiquement Exposées (PEP)

[![CI](https://github.com/fongkhan/Fiskr/actions/workflows/ci.yml/badge.svg)](https://github.com/fongkhan/Fiskr/actions/workflows/ci.yml)
[![Licence: Sustainable Use](https://img.shields.io/badge/licence-Sustainable%20Use%20(fair--code)-blue)](LICENSE.md)
[![Sponsor](https://img.shields.io/badge/%E2%9D%A4-Sponsoriser-ff69b4)](https://github.com/sponsors/fongkhan)

Fiskr est un moteur de criblage (*screening engine*) destiné aux institutions financières. Il confronte le référentiel tiers (clients, mandataires, bénéficiaires effectifs) aux listes de sanctions et de Personnes Politiquement Exposées (PEP), conformément aux exigences ACPR/AMF, et conserve la preuve de chaque décision.

**Plus de quarante sources officielles** sont branchées et synchronisées automatiquement : OFAC, liste consolidée UE, ONU, DGT, OFSI, SECO, listes nationales antiterroristes, exclusions de bailleurs multilatéraux, PEP. Les sources payantes (Dow Jones, World-Check, LexisNexis, Moody's GRID…) ne sont pas incluses : ce qu'elles apportent et comment les brancher est décrit dans [SOURCES_PREMIUM](Documentation/SOURCES_PREMIUM.md).

Le produit couvre : la **production opposable du référentiel** (synchronisation, delta, cahier de tests, homologation à quatre yeux), le **criblage** de la base clients (temps réel, batch, re-criblage automatique après chaque mise en production), le **filtrage transactionnel** ISO 20022, le **traitement des alertes** à quatre yeux avec piste d'audit immuable, et le **pilotage** réglementaire.

> **Par où commencer.** L'application embarque un **guide en 7 chapitres** (onglet *Guide*) : c'est le point d'entrée pour s'en servir. Ce README couvre l'installation, la configuration et l'exploitation. La [documentation de référence](Documentation/README.md) explique pourquoi le moteur décide comme il décide.

---

## Sommaire

| Section | Contenu |
|---|---|
| [Architecture et modules](#architecture) | Cartographie du code, module par module |
| [Ingestion & connecteurs](#ingestion) | Formats d'entrée, détection de noms, moteur SSIE |
| [Synchronisation des sources](#sources) | Les sources officielles branchées et leur planification |
| [Mode homologation](#homologation) | Validation d'une liste avant production |
| [Alertes & surveillance](#alertes) | Cycle de vie, quatre yeux, intégration SI amont |
| [Notifications](#notifications) | Événements notifiables et configuration SMTP |
| [Les 26 champs réglementaires](#champs) | Schéma pivot et champs étendus |
| [Sécurité & `.env`](#securite) | Variables d'environnement et secrets |
| [Architecture d'exécution](#execution) | Processus API, démon travailleur, supervision, déploiement |
| [Installation & lancement](#installation) | Prérequis, déploiement local, suite de tests |
| [Licence](#licence) | Sustainable Use (fair-code) |
| [Documentation](#documentation) | Index des documents de référence |

---

<a id="architecture"></a>

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
   * **Ressources linguistiques (équivalences déclarées)** : les métriques de chaîne ne peuvent structurellement rien déduire de *Henri ≡ Harry* ni de *Londres ≡ London* — aucune proximité graphique n'existe. Des **fichiers de ressources** YAML (`resources/`) déclarent des groupes d'équivalence par type de champ (prénom, nom, ville, pays, état) couvrant homonymes et fautes installées, romanisations concurrentes (*Zhang / Chang / Cheung*), équivalents entre langues et exonymes (*Allemagne / Germany / DE*). Ils agissent **au blocking** (clés additives, sans quoi les termes équivalents ne sont jamais candidats donc jamais comparés) **et au scoring** (règle du croisement : un token n'est canonicalisé que si sa classe est présente des deux côtés, donc une paire sans classe commune garde son score exact). Chaque équivalence appliquée est tracée dans le `decision_tree`. **Prénoms et noms actifs à la livraison** (impact mesuré : +6 vrais positifs et 0 faux positif sur un panel de 716 clients, cf. [Documentation/MESURE_RESSOURCES.md](Documentation/MESURE_RESSOURCES.md)) ; villes, pays et états inactifs faute de mesure. Activables par type de champ à chaud — et **mesurables avant activation** (`POST /api/resources/simulate` : deux criblages à blanc du même panel contre les mêmes listes, un sous le paramétrage en vigueur, un sous le paramétrage candidat), une table d'équivalences élargissant mécaniquement le périmètre des alertes. **2 887 termes livrés** (prénoms, noms, villes, pays, états et régions), dont les familles de romanisation coréennes et japonaises (박 ≡ Park, 田中 ≡ Tanaka), enrichis chaque nuit par une **fouille automatique d'homonymes** : le moteur extrait de nouvelles variantes du graphe d'alias des listes officielles (l'autorité elle-même déclare que deux graphies désignent la même personne) et des alertes confirmées par un analyste, en n'acceptant que les couples de noms qui ne divergent que sur **un seul mot** — ce qui écarte les noms de guerre du type « Ali Hassan » / « Abu Muhammad ». Chaque découverte porte ses preuves, reste révocable et n'atteint le criblage que si son type de champ est activé. Guide : **[Documentation/RESSOURCES_LINGUISTIQUES.md](Documentation/RESSOURCES_LINGUISTIQUES.md)**.
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

<a id="ingestion"></a>

## 🏃 Ingestion & Connecteurs d'Entrée (`fiskr/ingest.py`, `fiskr/ssie.py`)

L'outil intègre quatre familles de connecteurs génériques pour charger les listes sources :
* **OFAC XML Connector** : Lecture et traitement séquentiels d'un flux XML via `ElementTree.iterparse` pour éviter la saturation de la mémoire vive.
* **CSV Connector** : Parseur de fichiers délimités personnalisables (délimiteur et dictionnaire de colonnes).
* **PDF Connector** : Extracteur textuel via `pypdf` avec analyseur heuritique NER (Named Entity Recognition) pour isoler les navires, identifiants et caractéristiques.
* **Smart Sanctions Ingestion Engine (SSIE)** : Connecteur XML générique et structurellement agnostique (`fiskr/ssie.py`) pour les flux à références croisées par ID (OFAC Advanced, SWIFT SLD, etc.).

S'y ajoutent des **parseurs dédiés aux formats des sources officielles** (voir la section « Synchronisation Automatique des Sources » ci-dessous) : registre DGT des gels des avoirs (JSON), liste consolidée UE FSF (XML), liste consolidée ONU (XML), dataset PEP OpenSanctions (CSV), liste UK OFSI `ConList` (CSV) liste suisse SECO (XML SESAM ou CSV OpenSanctions), liste consolidee Non-SDN de l'OFAC (XML) Consolidated Screening List americaine (JSON), sanctions autonomes canadiennes (CSV bilingue) liste consolidee australienne (CSV ou XLSX), listes d'alerte de regulateurs (HK SFC, AMF) et exclusions de la Banque mondiale — tous utilisables aussi bien par les synchronisations que par l'upload manuel du dashboard.

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

<a id="sources"></a>

## 🛰️ Synchronisation Automatique des Sources (`fiskr/sync.py`)

L'onglet **Gestion des Watchlists → Sources Automatiques** permet de récupérer les listes directement auprès des émetteurs officiels, manuellement ou automatiquement chaque matin :

* **🇺🇸 OFAC — SDN Advanced** : Téléchargement du fichier officiel [`SDN_ADVANCED.XML`](https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML), ingestion en snapshot, **delta** (ADDED / MODIFIED / REMOVED) par rapport à la liste OFAC active, puis application : le nouveau snapshot remplace l'ancien (statut `SUPERSEDED`) dans le cache de criblage. Si le hash du fichier est inchangé, le rapport indique `NO_CHANGE` sans retraitement.
* **🇺🇸 OFAC — Liste consolidée Non-SDN** *(opt-in)* : Téléchargement du second fichier officiel [`CONS_ADVANCED.XML`](https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_ADVANCED.XML), au **format strictement identique** à la SDN. Il porte les régimes qui **n'emportent pas de gel total des avoirs** et sont pour cette raison absents du fichier SDN : sanctions **sectorielles** (SSI, directives Russie), FSE, NS-MBS, PLC, MEU et CMIC. Un établissement exposé au dollar doit les cribler ; ne charger que la SDN laisse un trou. Le connecteur **ne re-parse rien** — il réutilise le parseur SDN déjà éprouvé — et ne change que deux choses : les identifiants sont préfixés `NONSDN-` (l'`entity_id` sert de clé aux alertes et à la liste blanche, une collision fusionnerait deux fiches distinctes) et les relations entre profils ne sont pas récoltées, pour ne pas créer d'arêtes pendantes entre deux espaces d'identifiants. Liste **séparée** (`WATCHLIST_OFAC_NONSDN`), donc seuillable à part : la conséquence opérationnelle d'une touche sectorielle n'est pas celle d'un gel. À activer (`sync.ofac_nonsdn.enabled`).
* **🇫🇷 DGT — Registre national des gels des avoirs** : Téléchargement du registre officiel de la Direction générale du Trésor via son **API publique** ([gels-avoirs.dgtresor.gouv.fr](https://gels-avoirs.dgtresor.gouv.fr/)), ingestion en snapshot (personnes physiques → I, personnes morales → E, navires → V, avec normalisation ISO2 des nationalités françaises pour le blocking), **delta** et remplacement de la liste DGT active. La mise en œuvre des mesures de gel nationales étant une **obligation autonome** des établissements assujettis (lignes directrices ACPR/DGT), ce connecteur couvre nativement l'exigence française. Compatible mode homologation et planification quotidienne.
* **🇺🇳 ONU — Liste consolidée du Conseil de sécurité** : Téléchargement du XML officiel public ([scsanctions.un.org](https://scsanctions.un.org/resources/xml/en/consolidated.xml)), ingestion (individus → I, entités → E, alias Good/Low → priorités haute/basse, pays anglais normalisés en ISO2), **delta** et remplacement de la liste ONU active.
* **🇪🇺 UE — Liste consolidée officielle (fichiers FSF)** : Téléchargement du XML consolidé des sanctions financières publié par la Commission (webgate FSD) — la source qui **fait autorité** sur le scraping du JO, avec des **radiations fiables**. Nécessite un token gratuit : créez un compte sur le webgate FSD puis renseignez `sync.eu_fsf.token` dans `config.yaml` et passez `sync.eu_fsf.enabled` à `true`. Partage le type `WATCHLIST_EU` : le snapshot FSF remplace la liste scrapée, et le scraping quotidien du JO (ci-dessous) reste un complément « fraîcheur J+0 » optionnel qui fusionne par-dessus.
* **🇬🇧 UK OFSI — Liste consolidée HM Treasury** *(opt-in)* : Téléchargement du fichier officiel `ConList.csv` (format 2022) publié par l'Office of Financial Sanctions Implementation, regroupement des lignes par Group ID (nom principal + alias), typage Individual → I / Ship → V / autres → E, conversion des dates `jj/mm/aaaa` et normalisation ISO2 des nationalités. À activer (`sync.ofsi.enabled`) selon l'exposition UK de l'établissement.
* **🇺🇸 US CSL — Consolidated Screening List** *(opt-in)* : Téléchargement de l'agrégat officiel du gouvernement américain publié par l'International Trade Administration ([api.trade.gov](https://api.trade.gov/static/consolidated_screening_list/consolidated.json)), public et **sans clé**. Son intérêt n'est pas de redonner la SDN — Fiskr la récupère déjà à sa source — mais d'apporter les listes de **contrôle des exportations**, absentes de toutes les autres sources branchées : **Entity List**, **Denied Persons**, **Unverified** et **Military End User** du Bureau of Industry and Security, **ITAR Debarred** et **Nonproliferation** du Département d'État. Ces listes conditionnent le financement du commerce international : une contrepartie de trade finance peut y figurer sans être sur aucune liste de gel. Les listes déjà récupérées à leur source sont **écartées à la lecture** (`sync.csl.exclude_sources`, par défaut la SDN) pour ne pas doubler les alertes — si `ofac_nonsdn` est aussi activé, y ajouter ses libellés. La liste américaine qui désigne la contrepartie est conservée dans `designation_reasons`, et l'exigence de licence BIS dans les informations complémentaires : c'est ce qui permet à l'analyste de trancher. À activer (`sync.csl.enabled`).
* **🇨🇦 Canada SEMA — Sanctions autonomes** *(opt-in)* : Téléchargement de la liste consolidée d'Affaires mondiales Canada (*Special Economic Measures Act*). Le Canada **désigne de façon autonome** : son périmètre ne recoupe ni celui de l'UE ni celui de l'OFAC. Le fichier existe en anglais **et en français** — les deux jeux d'intitulés de colonnes sont acceptés, pour qu'un téléchargement depuis la page francophone ne donne pas une liste vide. Le CSV ne portant **aucun identifiant technique**, la clé de rapprochement est reconstruite depuis la référence réglementaire (annexe + article) : sans clé stable, chaque publication paraîtrait remplacer intégralement la précédente et le delta serait illisible. À activer (`sync.canada.enabled`).
* **🇦🇺 Australie DFAT — Liste consolidée** *(opt-in)* : Téléchargement de la liste du *Department of Foreign Affairs and Trade*, qui réunit les sanctions **onusiennes transposées** et les **désignations autonomes** australiennes — l'origine est conservée dans `designating_state` selon la présence d'un comité ONU. Les lignes se répètent par variante de nom et sont regroupées par référence, comme le `ConList` britannique. L'extension de l'URL choisit le lecteur : **CSV en natif**, **XLSX** si le paquet optionnel `openpyxl` est installé — son absence produit un rapport d'erreur qui dit quoi installer, pas une pile d'appels. À activer (`sync.dfat.enabled`).
* **🇭🇰 Hong Kong SFC — Liste d'alerte** *(opt-in)* : Entités non autorisées, sites frauduleux et usurpations d'identité d'intermédiaires agréés recensés par la *Securities and Futures Commission*. **Ce n'est pas une liste de sanctions** : une touche est un signal de risque à instruire, pas une obligation de gel — d'où un type de liste distinct (`WATCHLIST_HK_SFC`), à seuiller à part via `scoring.cut_off_overrides`. Hong Kong n'ayant pas de régime de sanctions autonome, c'est cette liste qui apporte une couverture propre. À activer (`sync.hk_sfc.enabled`).
* **🇫🇷 AMF — Listes noires** *(opt-in)* : Acteurs et sites proposant des services d'investissement sans autorisation, signalés par l'Autorité des marchés financiers. Même nature que la précédente — mise en garde, pas gel des avoirs — et c'est la liste d'alerte dont un assujetti français a l'usage le plus direct. À activer (`sync.amf.enabled`).
* **🏛️ Banque mondiale — Fournisseurs exclus** *(opt-in)* : Entreprises et personnes exclues des marchés financés par le Groupe de la Banque mondiale, pour fraude ou corruption avérée. Encore une autre nature : ni gel, ni mise en garde, mais une **exclusion à durée déterminée** — la date de fin alimente `delisted_on`. Criblée au titre du risque de contrepartie sur le financement de projets et le trade finance. À activer (`sync.worldbank.enabled`).
  * **Fiche sans pays** : `COUNTRY_ISO` étant une composante de la clé de blocking, une fiche listée dépourvue de géographie tombe dans la partition « pays inconnu », que ne rejoint aucun client ayant un pays — elle serait **inatteignable**. Le client interroge donc aussi la variante « pays inconnu » de ses propres clés (`blocking.country_wildcard`, actif par défaut). C'est **strictement additif** — aucune alerte ne peut être perdue — et le partitionnement est préservé : une fiche qui *porte* un pays reste atteinte par les seuls clients de ce pays. Surcoût mesuré sur 5 000 fiches et 2 000 clients : **+24 %** de candidats pour 2 % de fiches sans pays, **+65 %** pour 5 %, **+133 %** pour 10 % — et la couverture de ces fiches passe de **0 à 100 %** dans tous les cas.
  * Pour les trois : le format est **déduit du contenu**, pas de l'extension de l'URL. Les régulateurs servent leur liste à une adresse sans extension ; s'y fier ferait lire un CSV comme une page web, et l'import ressortirait à **zéro fiche** — c'est-à-dire silencieusement, sous les traits d'une liste vide plutôt que d'une erreur. Quand la source ne publie pas de pays, la **juridiction du régulateur** prend le relais : `COUNTRY_ISO` étant une composante de la clé de blocking, une fiche sans pays tombe dans la partition « XX » que ne rejoint aucun client ayant un pays, et serait structurellement inatteignable.
* **🇨🇭 Suisse SECO — Liste consolidée** *(opt-in)* : Téléchargement de la liste consolidée du Secrétariat d'État à l'économie. **Deux voies au choix** (`sync.seco.format`) : l'**export officiel SESAM** de la Confédération au format XML — la voie qui fait foi, gratuite et sans licence, seule à porter la **base légale suisse** (l'ordonnance `RS` applicable, reprise en référence officielle) et les **dates d'inscription** — ou, en secours, le jeu `ch_seco_sanctions` agrégé par **OpenSanctions** au format plat `targets.simple.csv` (mêmes réserves de licence que le dataset PEP). Typage individus → I / entités → E / navires → V, alias de qualité faible dégradés en priorité basse, et **ordre du nom conservé comme alias** : les listes suisses écrivent le patronyme en premier, les deux graphies sont donc indexées. La Suisse **transposant** les mesures de l'ONU et de l'UE, l'autorité d'origine est conservée dans `designating_state`. À activer (`sync.seco.enabled`) selon l'exposition suisse.
* **🌐 PEP — OpenSanctions** *(opt-in)* : Téléchargement du dataset consolidé des Personnes Politiquement Exposées d'OpenSanctions (`targets.simple.csv`), ingestion en liste `WATCHLIST_PEP` (individus et organisations liées, alias, dates de naissance partielles normalisées, pays ISO2). ⚠️ **Licence** : l'usage commercial des données OpenSanctions requiert une licence payante ([opensanctions.org/licensing](https://www.opensanctions.org/licensing/)) — le connecteur est désactivé par défaut (`sync.pep.enabled`).
* **🇪🇺 EUR-Lex — Journal Officiel du jour (édition anglaise)** : Lecture de la page du Journal Officiel (série L, **version anglaise, qui fait référence pour la réglementation européenne**) de la date choisie et détection des actes dont le titre mentionne **« restrictive measures »**. Le comportement se règle par `sync.eurlex.mode` :
  * **`alert` (défaut) — signal d'alerte précoce.** Le connecteur signale qu'un acte est paru, archive son PDF officiel et prévient immédiatement les homologateurs, mais **n'écrit aucune fiche**. Les désignations viennent de la **liste consolidée UE (EUFSF)**, qui fait autorité et **porte les radiations** — que le Journal Officiel, lui, n'appliquait jamais. Une seule requête HTTP par jour. Si `sync.eu_fsf` est désactivé, le rapport **et** la notification le disent explicitement : sans source consolidée, ce signal ne débouche sur rien.
  * **`extract` — comportement historique.** Scraping heuristique des annexes (tableaux et listes numérotées) pour en extraire les listés — Individus (avec date de naissance), Entités, Navires (IMO) et Aéronefs — fusionnés de manière incrémentale avec la liste EU active. À n'utiliser **que** tant que le token FSF n'est pas obtenu : ce qui en sort sont des **suppositions** (une expression régulière décide si une chaîne est un nom), la mise en page des annexes varie d'un règlement à l'autre, et les radiations ne sont pas appliquées.
  * En l'absence d'acte pertinent, le rapport indique `NO_PUBLICATION` dans les deux modes.
* **Archivage probant** : le **PDF officiel** de chaque acte retenu — la version qui **fait foi lors des audits** — est téléchargé dans `eurlex_archives/` avec son empreinte SHA-256 d'intégrité, référencé dans le rapport de synchronisation et téléchargeable depuis l'application (`GET /api/sync/evidence/{fichier}`).

Dans les deux cas, les **ajouts manuels à la volée sont préservés** (les snapshots `manual-watchlist*` — le générique historique comme les snapshots « Ajouts manuels » dédiés par liste, alimentés un par un ou par lot depuis l'onglet Ajout Manuel — ne sont jamais remplacés), et chaque exécution génère un **rapport de suivi** consultable dans l'application (table `sync_reports`, avec le détail du delta) et envoyé **par email** si un serveur SMTP est configuré dans `.env` (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SYNC_EMAIL_TO`).

**Fiabilité réseau** (`sync.network` dans `config.yaml`) : toutes les récupérations HTTP reprennent automatiquement sur les **erreurs de connexion/timeout** (httpx transport) ET sur les statuts transitoires (202 anti-robot, 408/429/5xx) avec backoff linéaire — les 403/404 échouent immédiatement. Quand le serveur précise **`Retry-After`**, c'est **lui** qui fixe l'attente (secondes ou date HTTP), plafonnée à 300 s pour qu'une valeur aberrante n'immobilise pas une tâche de fond : respecter ce délai est ce qui évite de se faire limiter plus durement. Les téléchargements sont **conditionnels** (`If-None-Match` / `If-Modified-Since` à partir de validateurs mémorisés **par source en base**, donc partagés entre le démon travailleur, les processus API et une relance manuelle) : une source inchangée répond `304` et le cycle se termine en `NO_CHANGE` **sans rien retélécharger ni analyser**. Un **outil de diagnostic** (`python tools/diagnostic_sources.py`, en lecture seule) indique quelles voies répondent **depuis le serveur qui exécute réellement les synchronisations** — le résultat dépend de l'IP sortante et du filtrage de l'hébergeur, il ne se déduit pas d'un poste de développement ; les téléchargements de fichiers envoient un **User-Agent navigateur** (les portails officiels filtrent l'UA par défaut) avec un timeout de lecture **par bloc**, et un client HTTP keep-alive partagé évite un handshake TLS par requête. Les **échecs partiels sont visibles** : actes EUR-Lex inaccessibles restitués dans le rapport (`fetch_failures`/`pdf_failures`, badge ⚠ dans l'application, repris au prochain run) et **panne réseau totale → rapport `ERROR`** (jamais un faux `NO_CHANGE`). La **progression** des synchronisations et des imports volumineux est suivie en direct (`GET /api/progress?id=`, jeton d'ingestion, `sync:<source>` ou snapshot_id ; barre de progression pendant l'import, phase vivante dans la table des snapshots).

**La synchronisation automatique se pilote depuis l'application, pas depuis le serveur.** L'écran *Listes → Sources automatiques* porte une carte **⏰ Synchronisation Automatique réservée aux administrateurs** (masquée pour tout autre rôle, et l'API refuse l'écriture en `403`) : un interrupteur général, la participation de chaque source aux récupérations planifiées, et son expression cron. Tout est **appliqué à chaud** — le planificateur relit ces valeurs à chaque tic, sans redémarrage ni édition de fichier — et chaque changement part au journal d'administration (`SETTINGS_UPDATED`, cible `sync.automation`). Couper l'automatique **n'empêche jamais** un lancement manuel : « Synchroniser » reste disponible sur toutes les sources, y compris celles exclues de la planification.

`config.yaml` ne fournit plus que les **valeurs par défaut** au premier démarrage ; dès qu'un réglage est modifié dans l'application, c'est lui qui fait foi (la colonne d'origine de l'écran indique lequel s'applique) :

```yaml
sync:
  auto_enabled: true         # défaut au premier démarrage ; réglable ensuite dans l'application
  schedule_time: "06:00"     # heure locale de déclenchement (HH:MM)
  ofac:
    enabled: true
    url: "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN_ADVANCED.XML"
  ofac_nonsdn:
    enabled: false            # sanctions sectorielles et regimes sans gel total
    url: "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/CONS_ADVANCED.XML"
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
  csl:
    enabled: false            # controle des exportations US : opt-in
    exclude_sources:          # listes deja recuperees a leur source
      - "Specially Designated Nationals"
  hk_sfc:
    enabled: false            # liste d'alerte SFC Hong Kong : opt-in
  amf:
    enabled: false            # listes noires AMF : opt-in
  worldbank:
    enabled: false            # fournisseurs exclus Banque mondiale : opt-in
  canada:
    enabled: false            # sanctions autonomes canadiennes : opt-in
  dfat:
    enabled: false            # liste australienne : opt-in (.csv natif, .xlsx via openpyxl)
  seco:
    enabled: false            # liste suisse : opt-in selon l'exposition
    format: "xml"             # "xml" (export officiel SESAM) ou "opensanctions" (CSV plat)
    url: ""                   # vide = URL par defaut du format choisi
```

Les endpoints associés : `POST /api/sync/run` (déclenchement manuel, réservé aux administrateurs), `GET /api/sync/reports` (historique des rapports), `GET /api/sync/reports/{id}` (rapport complet, `delta_report` compris) et `GET /api/sync/config` (configuration active).

`GET /api/sync/reports` sert le rapport **entier** par défaut. `include_details=false` renvoie les lignes sans `delta_report` — ce que demande l'écran de suivi, où le delta complet ne servait qu'à compter les échecs partiels (repris tel quel dans `partial_failures`). Même contrat sur le journal d'audit : `GET /api/history` transporte `decision_tree` et `config_state` par défaut, `include_details=false` les omet et `GET /api/history/{id}` rend la décision complète.

`GET /api/snapshots` est **paginé** et rend une enveloppe `{total, page, page_size, items}` (50 lots par défaut, 500 au plus), avec les filtres serveur `file_type` et `status` (listes séparées par des virgules). `GET /api/snapshots/options` rend l'historique complet réduit aux quatre colonnes qu'une liste déroulante de comparaison affiche.

`GET /api/watchlist` est une vue de **contrôle** du cache de criblage chargé en mémoire, et sa réponse est **bornée** (`limit`, 100 par défaut, 1 000 au plus ; `entity_id` cible une fiche précise). `total` reste exact et `truncated` signale la coupe. Pour parcourir le référentiel, c'est `GET /api/watchlist/db` : paginé, filtrable, et lu en base.

---

<a id="homologation"></a>

## ✅ Mode Homologation — Environnement de Validation avant Production

Certaines banques exigent un **pointage humain** avant qu'une nouvelle liste ne serve au criblage. Le **mode homologation** répond à ce besoin : lorsqu'il est actif, **tout snapshot watchlist entrant** — upload manuel ou synchronisation (manuelle comme planifiée) de n'importe quelle source : OFAC (SDN et Non-SDN), EUR-Lex, DGT, ONU, UE FSF, PEP, OFSI, SECO, US CSL, Canada, Australie, HK SFC, AMF, Banque mondiale — prend le statut `PENDING_REVIEW` au lieu d'entrer directement en production. Il est alors **invisible du moteur de criblage** — la liste `READY` précédente reste active — jusqu'à la décision d'un réviseur.

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

<a id="alertes"></a>

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
* **Vérification rapide d'un nom** (`GET /api/screen/preview`) : criblage flou d'un nom saisi à la volée avec le **même moteur** que la production (blocking, translittération, phonétique, ajustements, seuils par liste) mais **strictement en lecture** — aucune ligne d'audit, aucune alerte. Le contrôle d'appoint (levée de doute, entrée en relation) qui manquait à côté du criblage réglementaire tracé (`POST /api/screen`). Méthode GET, donc accessible à l'auditeur. Sans pays fourni, la recherche balaie toutes les partitions pays pour ne rater aucune fiche ; la lentille GAFI est réutilisée.
* **Risque géographique GAFI/FATF** (`fiskr/country_risk.py`, `GET /api/country-risk`) : lentille **complémentaire** au criblage par nom — signale les clients et parties de paiement rattachés à une juridiction sous **appel à l'action** (contre-mesures : Iran, Corée du Nord, Myanmar) ou sous **surveillance renforcée** (grey list, 22 juridictions au 19 juin 2026), qu'un nom soit listé ou non. **Hors du moteur de score** : n'altère ni score, ni verdict, ni `decision_tree`. Référentiel daté (`as_of`) **surchargeable à chaud** via le bloc `country_risk` de `config.yaml` — les révisions du GAFI (~3×/an) n'exigent aucun redéploiement.
* **Filtrage transactionnel ISO 20022** : criblage de toutes les parties d'un message `pain.001` / `pacs.008`, verdict `PASS`/`HIT`, audit + alertes.
* **Pilotage** : KPI conformité (`GET /api/kpi`) — taux de faux positifs, délais de décision, volumétrie, synchronisations, **séries temporelles 30 jours**, ventilation par analyste et par liste, efficacité des règles anti-faux positifs.

* **Case management** : priorité explicite par alerte (CRITIQUE sur hard match, modifiable et journalisée), **échéances SLA** par priorité (réglage à chaud, badge « ⏰ En retard »), pièces jointes justificatives, **rapport d'alerte imprimable** (`GET /api/alerts/{id}/report`, prêt ACPR/FED).
* **Exports CSV** (Excel FR : `;` + BOM) : alertes, journal d'audit et vue base des listes, avec les filtres de l'écran (`/api/export/*.csv`).
* **Journal des actions d'administration** (`admin_audit_log`, append-only) : comptes, réglages (avant → après), purges, révocations — sous-onglet dédié de l'Audit.
* **Notifications par étape** : un mail à **chaque étape** de la production des listes, du criblage et du filtrage (voir la section dédiée ci-dessous) — email HTML avec lien direct + webhooks génériques (`notifications.webhooks`), fire-and-forget : jamais bloquant.
* **Graphe de relations & règle des 50 % (OFAC)** : les `ProfileRelationships` du SDN_ADVANCED sont extraits (détenu par, agit pour, associé, famille, dirigeant, soutien) et rafraîchis à chaque sync ; relations manuelles avec % de détention (reviewer/admin). Le **risque hérité par détention majoritaire** (≥ 50 %, transitif, présomption sur les liens OFAC sans %) est affiché dans la fiche et annoté dans le decision tree de chaque criblage. **Visualisation réseau** : modale « 🕸 Graphe » (SVG natif, rendu radial, flèches rouges = détention majoritaire, clic sur un nœud pour recentrer, profondeur 1-3).
* **Planification cron par source** (`fiskr/cron.py`, sans dépendance) : chaque source de synchronisation suit sa propre expression cron 5 champs, modifiable à chaud (`PUT /api/settings/sync`, admin) avec repli sur `config.yaml` puis sur l'horaire quotidien global ; prochaine exécution affichée par source, aucun chevauchement d'une même source.
* **Campagnes de criblage batch persistées** : un CSV de clients (upload ou **dépôt CFT dans l'inbox surveillée** `batch.inbox_dir`) est criblé côté serveur en tâche de fond avec les mêmes garanties que le temps réel (quality gate, liste blanche, règles, audit immuable, alertes) — progression en direct, résultats filtrables, export CSV, rejets quality gate conservés avec motif.
* **Vue client 360°** (`GET /api/clients/{id}/overview`, bouton 👤 de la modale d'alerte) : fiche KYC du dernier référentiel en production, historique de criblage, alertes et paires de liste blanche du client — tout au même endroit pendant l'instruction.
* **Progression de chaque source officielle** : les quatre implémentations de synchronisation (OFAC, DGT, EUR-Lex et le cycle générique UN/FSF/PEP/OFSI) publient les mêmes phases — téléchargement (octets reçus), empreinte, enregistrement (compteur de fiches), delta, rechargement du cache — EUR-Lex publiant en plus une progression **acte par acte**, là où passe le temps d'un scraping. L'état vivant apparaît dans la colonne « État » du tableau de planification cron **et** dans la pastille d'en-tête, qu'une synchronisation soit déclenchée à la main ou par le planificateur. Le budget réseau est réglable **par source** (`sync.<source>.network` surcharge `sync.network`) : EUR-Lex, dont le portail sert une page d'attente anti-robot (HTTP 202), reçoit d'office 6 reprises avec un backoff de 5 s, sans ralentir les autres sources.
* **Progression des opérations de fond** (`GET /api/progress/active`, pastille `⚙ N en cours · X %` de l'en-tête et section dédiée du centre de notifications 🔔) : **tout ce qui tourne** au même endroit — imports manuels et planifiés, synchronisations, cahier de tests, mise en production, campagnes batch, contrôle qualité post-import — avec la phase en clair, le pourcentage, l'auteur et un lien direct vers l'écran concerné. La progression **ne dépend plus de l'onglet ouvert ni de qui a lancé l'opération** : elle survit à la navigation et au rechargement de page, et une synchronisation déclenchée par le cron est aussi visible qu'un import lancé à la main. Un job en échec est signalé (statut `ERROR` et message), jamais simplement absent. Interrogation adaptative : 2 s quand quelque chose tourne, 8 s au repos.
* **Cahier de tests et mise en production asynchrones** : ces deux traitements gelaient l'application entière pendant leur exécution (event loop bloqué). Ils répondent désormais **202** avec un jeton et travaillent en tâche de fond — sur un panel de 4 000 fiches, la requête revient en 9 ms et l'application continue de répondre. Les **refus restent synchrones** (panel absent ou vide, règle candidate invalide, exigences d'approbation non satisfaites → 400 immédiat, aucun job lancé), et pour l'approbation l'acte de gouvernance lui-même (contrôles, bascule `READY`, supersede, commit) reste synchrone : seuls le rechargement du cache et le re-criblage post-delta partent en fond.
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
* **Envoi du projet au correspondant** (`POST /api/alerts/{id}/str-draft/send`, rôle reviewer/admin, bouton « ✉ Envoyer au correspondant » de la modale dossier) : transmet le projet **par email au correspondant TRACFIN déclaré** (`institution.correspondent_email`), le HTML imprimable servant de corps de message. Les refus sont explicites, jamais silencieux — **400** sans correspondant configuré, **503** sans SMTP, **502** avec l'erreur SMTP réelle — et l'envoi n'est tracé `STR_DRAFT_SENT` dans l'historique de l'alerte **qu'après** le départ effectif du mail. Toujours aucune transmission à TRACFIN : le correspondant reste seul décisionnaire de la télédéclaration ERMES.
* **Qualité des données clients** (`GET /api/quality/clients`, carte « 🧪 Qualité des Données Clients » de Pilotage) : complétude des champs KYC du référentiel en production (barres vert ≥ 95 % / orange ≥ 80 % / rouge), ventilation par segment, **fiches à risque pour le criblage** (PP sans date de naissance, fiches sans pays, PP sans prénom) et score global — un dossier incomplet dégrade la précision du criblage.
* **Seuil d'alerte sur la qualité** (réglage `quality_min_score_pct` de la carte de gouvernance, 0 = désactivé) : chaque import `CLIENT_BASE` réussi déclenche le contrôle de qualité **en tâche de fond** (jamais dans la requête : 750 000 fiches doubleraient le temps d'import perçu), met le résultat en cache et émet l'événement `client_quality_low` quand le score passe sous le seuil — activation, destinataires par rôle et journal d'envoi hérités du socle de notifications. Le tableau de bord affiche le verdict (`threshold.below`) et le digest KPI lit le cache : il annonce « non calculée depuis le dernier import » plutôt que de mentir quand un référentiel plus récent a été mis en production.

### 🔗 Intégration SI amont (webhooks entrants)

Deux endpoints permettent au SI amont (core banking, CRM) de pousser des demandes vers Fiskr, **authentifiés par clé d'API `fsk_`** (en-tête `X-API-Key`, comptes de service ci-dessus — les sessions humaines sont refusées) :

* **`POST /api/hooks/screening`** — criblage temps réel : même charge utile et même réponse que `POST /api/screen` (même cœur de criblage : quality gate, blocking, scoring, liste blanche, règles anti-FP, audit immuable, alerte).
* **`POST /api/hooks/client-upsert`** — création/mise à jour d'une fiche client unitaire dans le dernier référentiel `CLIENT_BASE` en production (tracée `CLIENT_UPSERT_HOOK` au journal des actions d'administration).

Garanties d'intégration :

* **Signature HMAC facultative** : si `hooks.secret` est renseigné dans `config.yaml`, l'en-tête `X-Fiskr-Signature` (HMAC-SHA256 hexadécimal du corps brut) devient obligatoire sur ces endpoints.
* **Idempotence** : l'en-tête `X-Idempotency-Key` (recommandé) garantit qu'une retransmission (retry réseau de l'appelant) **rejoue la réponse d'origine** sans recribler ni dupliquer (`X-Idempotency-Replayed: true` sur la réponse rejouée) ; les livraisons sont conservées 90 jours (table `hook_deliveries`, auto-nettoyée).
* **Supervision** (`GET /api/hooks/stats`, admin, carte « 📡 Webhooks Entrants » des Paramètres) : **toute** livraison est tracée, y compris sans en-tête d'idempotence (clé serveur `auto:` — tracée mais jamais rejouée) et y compris les refus (401 signature, 422 charge invalide) : une intégration amont cassée se voit en erreurs plutôt qu'en silence. Volumes sur 30 jours, ventilation par point d'entrée et par appelant, série quotidienne et 20 dernières livraisons. Ni la clé d'idempotence (choisie par l'appelant, elle peut porter une référence métier) ni la réponse stockée ne sortent de l'API. Les lignes `auto:` sont purgées à 30 jours, les clés client à 90 jours (elles seules servent au rejeu).

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

* **Vue d'ensemble personnalisable** (onglet d'accueil) : une grille de panneaux que **chaque utilisateur compose** (bouton 🎛 Personnaliser — ajout depuis une galerie par catégorie, retrait, trois tailles, réordonnancement par glisser-déposer ; disposition stockée par utilisateur via `GET/PUT/DELETE /api/me/dashboard`, remise à zéro possible). Catalogue livré : 7 tuiles d'indicateurs cliquables (alertes ouvertes par canal, 4-yeux, homologation, retards SLA, taux de FP, délai moyen), 3 graphiques SVG natifs sans dépendance (courbe 30 jours créées/clôturées, barres fiches par liste, donut des statuts), 5 tableaux (alertes les plus anciennes, dernières synchronisations, travaux récents, charge par analyste, qualité des données clients).
* **Thème clair / sombre** commutable (bouton 🌙/☀️ du header, persisté), design system 100 % piloté par tokens CSS.
* **Responsive** : sidebar rétractable (hamburger + overlay) sous 1024 px, formulaires en une colonne sur mobile.
* **Tri des colonnes** sur toutes les tables (tri API validé pour la vue base paginée), squelettes de chargement, états vides homogènes, fermeture des modales à Échap, statuts affichés en français.
* **Recherche globale Ctrl+K** : palette de commande (listés — y compris fuzzy —, alertes, navigation), entièrement au clavier.
* **Liens profonds** : chaque onglet/sous-onglet est adressable par l'URL (`#alerts/subtab-filtering-queue`, …) — écran restauré au chargement, navigation arrière/avant du navigateur respectée, liens partageables entre analystes.
* **Cloche de notifications** : badge du nombre d'éléments à traiter et panneau déroulant (alertes ouvertes par canal, 4-yeux en attente, alertes en retard SLA, snapshots à homologuer) avec accès direct en un clic. La section **Travaux récents** y est compacte : une ligne par travail (pastille de statut, libellé, temps relatif — détails en infobulle), les rafales de travaux de même nature (ex. mise en production multi-listes) repliées en une ligne dépliable, les échecs seuls déployés avec leur message et le bouton de relance.
* **Pagination serveur** des files d'alertes et de la liste blanche (100 par page) ; **glisser-déposer** des fichiers sur les zones d'import (listes, batch, transactions).

---

<a id="notifications"></a>

## 📬 Notifications par étape (email)

Fiskr envoie un mail à **chaque étape** de la production des listes, du criblage et du filtrage — **31 étapes notifiables** déclarées dans un catalogue unique (`fiskr/events.py`) dont dérivent les réglages, les libellés des mails et l'écran d'administration : ajouter une étape se fait à un seul endroit.

**Étapes couvertes**

| Domaine | Étapes |
|---|---|
| **Production des listes** | liste en attente d'homologation · **liste approuvée et mise en production** · **liste rejetée (avec motif)** · import mis en production · import en échec · synchronisation terminée · échec de synchronisation · exclusions posées/retirées · panel de test généré · cahier de tests exécuté · Good Guys en masse · re-criblage post-delta |
| **Criblage clients** | alerte créée · alerte assignée (unitaire et en masse) · alerte escaladée · décision en attente de validation 4-yeux · décision validée · proposition renvoyée · clôture directe (4-yeux désactivé) · **échéance SLA dépassée** · liste blanche créée/révoquée · **paire de liste blanche arrivant à revue** · règle anti-FP soumise/activée/renvoyée |
| **Filtrage transactionnel** | verdict HIT sur un message ISO 20022 · campagne batch terminée · campagne batch en échec · fichier de l'inbox CFT refusé |
| **Gouvernance** | synthèse conformité périodique · purge de rétention exécutée |

**Destinataires par rôle** — chaque compte porte une adresse (champ *Email* de la fiche utilisateur). L'homologation part vers les `reviewer`/`admin`, une alerte assignée vers l'analyste **et son délégué si une absence est déclarée**, les règles vers le rôle `rules`, les incidents techniques vers les `admin`, les décisions vers le proposeur d'origine. Sans adresse renseignée, tout retombe sur la liste globale historique (`NOTIFY_EMAIL_TO`, sinon `SYNC_EMAIL_TO`) : un déploiement existant garde exactement son comportement actuel.

**Immédiat ou récapitulatif** — les étapes structurantes partent tout de suite ; les étapes à fort volume sont mises en file et regroupées en **un seul mail par destinataire** (fréquence cron réglable à chaud, horaire par défaut). La même boucle détecte les **dépassements de SLA** (une alerte signalée une seule fois, tracée `SLA_OVERDUE` dans son historique) et les **paires de liste blanche arrivant à échéance de revue**.

**Configuration**

```bash
# .env — mêmes variables que les rapports de synchronisation
SMTP_HOST=smtp.banque.fr
SMTP_PORT=587
SMTP_USER=fiskr@banque.fr
SMTP_PASSWORD=…
SMTP_FROM=fiskr@banque.fr
NOTIFY_EMAIL_TO=conformite@banque.fr      # repli quand aucun compte ne correspond
FISKR_PUBLIC_URL=https://fiskr.banque.fr  # active le bouton « Ouvrir dans Fiskr » des mails
```

Écran **Paramètres → 🔔 Notifications métier** : activation étape par étape (regroupée en 4 catégories, badge « immédiat »/« récap » et audience affichés), fréquence du récapitulatif, adresses supplémentaires par catégorie, **journal des envois** (`GET /api/notifications/log` — statut, destinataires, erreur SMTP éventuelle) et bouton **« Envoyer un mail de test »** qui remonte l'erreur SMTP exacte. Sans `FISKR_PUBLIC_URL`, les mails partent sans bouton (jamais de lien cassé) ; sans SMTP, rien ne part et le journal l'indique (`SKIPPED`).

> Garantie de conception : **une notification ne bloque jamais et ne fait jamais échouer une opération métier**. Un envoi en erreur est journalisé (`FAILED`) et l'import, l'approbation ou la décision d'alerte se termine normalement.

---

<a id="champs"></a>

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

<a id="securite"></a>

### Configuration de Sécurité & Fichier `.env`

Les secrets de l'application et la chaîne de connexion à la base de données ne sont plus stockés en clair dans `config.yaml`. Ils sont configurables via les variables d'environnement ou le fichier `.env` à la racine du projet (un modèle est fourni dans [`.env.example`](.env.example)) :

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

<a id="execution"></a>

## ⚙️ Architecture d'exécution : processus API + démon travailleur

Depuis la migration de la file de travaux, Fiskr s'exécute en **deux types de
processus** — et plus aucun calcul lourd ne tourne dans un processus API :

```
Processus API (×N, Passenger/uvicorn)      Démon travailleur (×1, verrou flock)
┌────────────────────────────┐             ┌─────────────────────────────────┐
│ requêtes HTTP, criblage    │   table     │ boucle de claim → K slots       │
│ unitaire, cache des listes │   `jobs`    │ planificateurs (cron, inbox CFT,│
│ dépôt des jobs → 202+jeton │ ◄─────────► │ digest, rétention, notif, mine) │
│ autostart + watchdog démon │ (PostgreSQL)│ battement de cœur + REPRISE     │
│ lecture de la progression  │             │  ┌───────────────────────────┐  │
└────────────────────────────┘             │  │ pool fork() par job lourd │  │
                                           │  │ (tranches de clients)     │  │
                                           └──┴───────────────────────────┴──┘
```

- **La table `jobs` (PostgreSQL) est le canal unique** entre les deux mondes :
  file d'attente (claim par `SELECT … FOR UPDATE SKIP LOCKED`), progression
  inter-processus, résultats persistés (relisibles après redémarrage),
  exclusivité (`dedupe_key` → 409), et **reprise automatique** : un job
  interrompu par un arrêt brutal est détecté par son battement de cœur périmé
  et remis en file (relance de zéro, plafonnée par `attempts` ; au-delà, ERROR
  relançable d'un clic depuis la section **Travaux** du centre de
  notifications). Cette réparation tourne **au démarrage ET en continu**
  (toutes les ~60 s dans la boucle de battement du démon) : un job zombie
  laissé RUNNING par une incarnation morte est repris en une minute, sans
  redémarrage. La sérialisation ne compte que les jobs au **cœur frais** :
  un zombie ne bloque jamais son groupe.
- **Le démon est unique par construction** : verrou `flock` sur
  `fiskr-worker.lock`, rendu par le noyau à la mort du processus — pas de
  fichier de PID fantôme. Il héberge aussi **tous les planificateurs
  périodiques** : sous Passenger, N processus API signifiaient N
  planificateurs (N digests, N synchronisations) ; désormais un seul tic.
- **Autostart sans systemd** (hébergement mutualisé type o2switch) : chaque
  processus API vérifie le battement de cœur du démon (au démarrage, à chaque
  dépôt de job, et toutes les 60 s par un watchdog) et le relance détaché
  (`start_new_session`) s'il manque. La course entre N processus API est
  inoffensive : le flock n'en laisse vivre qu'un. Journal du démon :
  `worker.log` à la racine du projet.
- **Dans un job lourd, le calcul est parallélisé par `fork()`** : l'univers
  des listes est chargé une fois (projection mémoire aux seuls champs lus par
  le moteur, ~3,8 Ko/fiche au lieu de ~8,4), l'index de blocking est construit,
  puis le panel de clients est découpé en tranches sur un pool de processus
  enfants qui partagent cette mémoire en copy-on-write (`gc.freeze()`). Les
  enfants sont en **lecture seule** : toutes les écritures (alertes, audit,
  `hit_count` des règles) restent dans le parent — résultats déterministes,
  prouvés identiques au séquentiel par test. Le nombre de processus est borné
  par un budget CPU **et** mémoire (cas PEP 750 000 fiches pris en compte).
- **Invalidation du cache inter-processus par époque** : le démon ne peut pas
  toucher la mémoire d'un processus API ; quand la production change
  (synchronisation, approbation, import), il incrémente `watchlist.epoch` en
  base et chaque processus API recharge son cache local (vérification toutes
  les 5 s). Plusieurs processus API sont donc **sûrs** — la restriction
  historique à un seul worker ne s'applique qu'au mode dégradé `thread`.

Réglages (`config.yaml`, section `jobs:` — lus au démarrage du processus) :

| Réglage | Défaut | Rôle |
|---|---|---|
| `jobs.mode` | `worker` | `worker` : démon dédié (production) · `thread` : threads du processus API (repli sans démon, comportement historique) · `eager` : inline synchrone (tests) |
| `jobs.slots` | `2` | Jobs simultanés dans le démon (les jobs lourds délèguent leur CPU au pool de tranches) |
| `jobs.screen_processes` | `0` | Processus du pool de criblage : `0` = auto (budget CPU/mémoire), `1` = séquentiel forcé, `N` = imposé |
| `jobs.autostart` | `true` | L'API relance le démon absent (watchdog 60 s) |

> **`FISKR_JOBS_MODE` prime sur `jobs.mode`.** La variable d'environnement,
> quand elle est définie, l'emporte sur `config.yaml` — c'est ainsi que le
> démon se marque lui-même en `worker` et que la suite de tests bascule en
> `eager`. À savoir avant de chercher pourquoi un `jobs.mode` fraîchement
> modifié dans `config.yaml` reste sans effet : regardez d'abord
> l'environnement du processus.

> **Passenger (mutualisé)** : réglez `passenger_min_instances 1` — c'est le
> réglage le plus rentable de tout le déploiement, et il est mesuré. Sans lui,
> Passenger recycle le processus dès qu'il est inactif : chaque visiteur tombe
> sur un processus neuf, dont le cache moteur est vide, et la palette Ctrl+K
> met 31 s. Avec lui, le processus survit (vérifié au-delà de 12 min) et le
> cache chargé une fois sert tout le monde : **palette 1,4 s, criblage 5,2 s**.
> Le préchargement au démarrage (`FISKR_PRELOAD_CACHE=1`) ne remplace PAS ce
> réglage et n'a d'intérêt que sur un hébergement dédié — voir
> `Documentation/PERFORMANCE_BASE.md`. Laissez enfin `jobs.autostart: true` —
> c'est l'API qui fait naître le démon, aucun accès systemd/cron n'est
> nécessaire. Budget de connexions PostgreSQL : N processus
> API (pool SQLAlchemy) + le démon (2 slots) + les tranches de criblage (une
> connexion éphémère chacune, ≤ `screen_processes`) — largement sous le
> `max_connections = 100` par défaut.

### Supervision du démon : le voir s'il tombe, le relancer

En mode `worker`, **rien ne s'exécute sans démon vivant** — une synchronisation
(ou tout traitement lourd) déposée sans démon reste `QUEUED` indéfiniment. Deux
garde-fous rendent cette panne visible et réparable :

- **Bandeau d'alerte dans l'application** : dès qu'un démon est requis, absent
  (battement de cœur périmé > 120 s) **et** que des travaux attendent, un
  bandeau rouge s'affiche pour tous — « Le démon de traitement est arrêté :
  N opération(s) en attente ne démarreront pas » — avec un bouton **Relancer le
  démon** pour l'administrateur. La sonde interroge `GET /api/worker/status`
  toutes les 30 s ; la relance (`POST /api/worker/restart`, admin, tracée au
  journal) est sans risque grâce au flock. La dernière tentative d'autostart
  (succès/échec + interpréteur utilisé) est mémorisée dans
  `jobs.worker_autostart` : un `subprocess` refusé par l'hébergeur n'est plus
  invisible.
- **Filet cron (recommandé en mutualisé)** : l'autostart par l'API est un
  « best-effort » — sous Passenger, le processus API ne tourne que s'il y a du
  trafic, donc un démon mort la nuit ne redémarre qu'à la première visite. Le
  filet durable est une tâche **cron** (cPanel → *Cron Jobs*) qui tente de
  lancer le démon toutes les 5 minutes ; le verrou `flock` garantit qu'il n'y
  en aura jamais deux (un lancement de trop sort aussitôt) :
>
> ```cron
> */5 * * * * cd /home/UTILISATEUR/fiskr && /home/UTILISATEUR/virtualenv/fiskr/3.x/bin/python -m fiskr.worker >> worker.log 2>&1
> ```
>
> Adaptez le chemin de l'interpréteur à votre virtualenv cPanel (celui affiché
> par *Setup Python App*). Cette ligne décorrèle la vie du démon du trafic web :
> même sans visiteur, les synchronisations planifiées partent à l'heure.

### Déploiement en un geste : `tools/refresh_prod.sh`

Le script fait tout le rafraîchissement dans le bon ordre — venv, `git pull`
(**fast-forward uniquement**), `pip install`, arrêt du démon (qui garde
l'ancien code tant qu'il vit), relance immédiate sur le nouveau code :

```bash
bash tools/refresh_prod.sh
# chemins par défaut surchargables :
FISKR_VENV=/chemin/activate FISKR_DIR=/chemin/repo bash tools/refresh_prod.sh
```

Garde-fous intégrés : arrêt à la première erreur, refus si des modifications
locales ne sont pas commitées, refus d'un historique divergent (jamais de
merge silencieux en prod), et le `pkill` ne cible que les processus
`python -m fiskr.worker` **de votre compte** (SIGTERM d'abord, SIGKILL après
10 s seulement). La relance immédiate est sans risque : le verrou `flock`
n'en laisse vivre qu'un, même si le cron tente en même temps.

### Diagnostic à distance : `GET /api/diagnostic/jobs`

Quand la production bloque, un **seul appel lecture seule** rend toute la
radiographie de la file de travaux — conçu pour être interrogé **de
l'extérieur** (support, exploitation, assistant) sans accès shell au serveur :

- **`versions`** : empreinte du code chargé par l'API et par le démon,
  comparées au code présent sur le disque. C'est le piège n°1 des
  déploiements : le démon **garde l'ancien code tant qu'on ne le tue pas**
  (`kill $(head -1 fiskr-worker.lock)`) — `worker.outdated: true` le dit sans
  ambiguïté (et un démon vivant **sans** empreinte est lui aussi un ancien
  code) ;
- **`jobs`** : compteurs par statut, jobs RUNNING avec fraîcheur du battement
  de cœur, file d'attente dans l'ordre où le démon la servirait, dernières
  erreurs avec leur cause, et l'état du **groupe sérialisé** (quel job tient
  la place) ;
- **`worker`** : supervision + verrou flock (le PID inscrit vit-il ?) ;
- **`system`** : mémoire machine/processus, charge, moteur de base ;
- **`worker_log_tail`** : la queue de `worker.log` — les dernières volontés
  du démon quand il est mort.

Accès : session **admin**, ou **clé d'API de rôle `auditor`** (⚙️ Paramètres →
Clés d'API) passée en `X-API-Key` — lecture seule par construction (toute
écriture est refusée à ce rôle), révocable d'un clic :

```bash
curl -sS -H "X-API-Key: fsk_..." https://votre-instance/api/diagnostic/jobs | python -m json.tool
```

### Documentation de l'API : `/docs`, `/redoc`, `/openapi.json`

Le schéma OpenAPI complet est servi **aux utilisateurs authentifiés
uniquement**. Ouvrez `/docs` dans le navigateur où vous êtes connecté : le
cookie de session suit, Swagger UI se charge normalement.

Ces trois adresses répondaient auparavant à n'importe quel visiteur : 170
chemins dont 39 d'administration, 66 schémas, et les descriptions issues des
docstrings — c'est-à-dire le plan complet de l'application, défenses
comprises. Les points d'entrée eux-mêmes ont toujours été protégés ; ce qui
était offert, c'est leur carte.

<a id="installation"></a>

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

> **Plusieurs workers : possible en mode `worker` uniquement.** En mode
> `jobs.mode: worker` (défaut), plusieurs processus API sont sûrs : la
> progression vit dans la table `jobs`, le cache des listes est invalidé
> inter-processus par époque, et les planificateurs ne tournent que dans le
> démon. En mode **`thread`** (repli sans démon), gardez un seul worker : le
> registre de progression et le cache redeviennent propres au processus qui a
> lancé l'opération.

1. Vous serez automatiquement redirigé vers la page de connexion **`/login`**.
2. Connectez-vous avec les identifiants administrateur (par défaut : **`admin`** / **`adminpassword`**).
3. Une fois authentifié, un jeton JWT sécurisé et un cookie `HttpOnly` sont générés, vous donnant accès au dashboard de contrôle.

> **Première mise en route : l'écran vous attend.** Sur une base vide, Fiskr
> ouvre de lui-même **Guide → Mise en service** (`GET /api/setup/status`,
> réservé à l'administrateur). Chaque point y est **vérifié à l'instant**, pas
> mémorisé : secrets restés à la valeur du code source, repli SQLite, démon
> travailleur absent, index de performance différés, aucune liste en
> production, référentiel clients, seuils jamais revus, comptes, SMTP.
>
> Un bandeau persiste tant qu'un point **bloquant** subsiste — un poste de
> criblage sans liste en production répond « aucune correspondance » sans
> jamais s'en plaindre, et c'est l'état le plus dangereux du produit.
>
> Le relevé constate qu'un serveur SMTP est *configuré*, jamais qu'il
> *répond* : la sonde « Tester l'envoi de courriel » ouvre une vraie connexion,
> à la demande et bornée par un timeout court. La distinction n'est pas
> théorique — elle a été constatée en production, sur un SMTP correctement
> déclaré dont chaque envoi tombait en délai dépassé.

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
Exécutez la suite complète — **1 499 fonctions de test** réparties sur 152
fichiers — avec pytest :
```bash
python -m pytest
```

> Ce chiffre est tenu par un test (`test_documentation_exacte`) : il se dérive
> du contenu de `tests/`, il ne se recopie pas. Il annonçait 153 pendant que la
> suite en comptait dix fois plus — un nombre écrit une fois dans un document
> vieillit exactement comme une table recopiée à la main.



---

<a id="licence"></a>

## 📜 Licence & Offre Commerciale

Fiskr est distribué sous la **[Sustainable Use License](LICENSE.md)** (modèle **[fair-code](https://faircode.io)**), copyright © 2026 **Alexis Vuadelle** :

* ✅ **Libre pour l'usage interne et personnel** : toute organisation peut déployer, utiliser et modifier Fiskr **gratuitement** pour ses propres besoins (y compris en production bancaire). Le code source est public, auditable et ouvert aux contributions.
* 💼 **Commercialisation réservée** : la revente du logiciel, son hébergement pour des tiers contre rémunération et les prestations associées sont réservés au titulaire. **Déploiement on-premise accompagné, support et licences commerciales : sur demande payante** — contactez [@fongkhan](https://github.com/fongkhan) sur GitHub.
* ❤️ **Soutenir le projet** : le sponsoring est bienvenu via [GitHub Sponsors](https://github.com/sponsors/fongkhan).

> Note de transparence : la Sustainable Use License est une licence *fair-code* « source disponible », pas une licence open source au sens de l'OSI (elle restreint l'usage commercial par des tiers).

---

<a id="documentation"></a>

## 📚 Documentation

Le point d'entrée pour **se servir** de Fiskr est le guide en 7 chapitres embarqué dans l'application (onglet *Guide*). Les documents ci-dessous expliquent **pourquoi** le moteur décide comme il décide, et fournissent la matière opposable à un contrôleur.

**➜ [Index complet de la documentation](Documentation/README.md)** — classé par question, avec la nature de chaque document (référence, parcours, relevé daté, étude tranchée).

Les plus consultés :

| Document | Pour répondre à |
|---|---|
| [Algorithmes du moteur](Documentation/ALGORITHMES_DU_MOTEUR.md) | « Comment ce score est-il né ? » — inventaire opposable de chaque mécanisme de rapprochement, ce que chacun apporte et ce que sa désactivation coûte. |
| [Production des listes](Documentation/PRODUCTION_DES_LISTES.md) | « Comment une liste arrive-t-elle en production ? » — delta, exclusions, cahier de tests, homologation à quatre yeux. |
| [Règles & blocking](Documentation/REGLES_ET_BLOCKING.md) | « Comment limiter les faux positifs sans rien perdre ? » — clés de blocage par canal, règles Python et leur gouvernance. |
| [Alertes & surveillance continue](Documentation/ALERTES_ET_SURVEILLANCE_CONTINUE.md) | « Que devient une alerte ? » — cycle de vie, quatre yeux, re-criblage, preuve à trois niveaux. |
| [Injecter des clients par l'API](Documentation/INJECTION_CLIENTS.md) | « Comment alimenter le référentiel clients ? » — import de masse, webhook unitaire, criblage sans persistance. |
| [Performance de la base](Documentation/PERFORMANCE_BASE.md) | « Pourquoi cet écran est-il lent ? » — diagnostic mené sur la production, mesuré et corrigé. |
| [Architecture technique](Documentation/ARCHITECTURE_TECHNIQUE.md) | « Comment c'est fait ? » — composants, schéma de données, flux, déploiement. |

L'[historique daté](CHANGELOG.md) de tous les changements, avec les mesures qui les ont motivés, vit dans le CHANGELOG.

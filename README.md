# Fiskr - Moteur de Criblage LBA-CFT & Personnes Politiquement Exposées (PEP)

Fiskr est un moteur de criblage (Screening Engine) de nouvelle génération destiné aux institutions financières. Il permet de confronter le référentiel tiers (clients, mandataires, bénéficiaires effectifs) aux listes de sanctions et de Personnes Politiquement Exposées (PEP) fournies par les éditeurs officiels (OFAC, UE, ONU, Dow Jones, World-Check) conformément aux exigences réglementaires ACPR/AMF.

Le projet propose une API temps réel asynchrone, un script de traitement de masse (Batch) sous Apache Spark, un comparateur de snapshots historiques (Delta Engine), et un tableau de bord interactif pour les agents de conformité.

---

## 🛠️ Architecture et Modules

Le système est structuré autour des modules définis dans le Document d'Architecture Technique (DAT) :

1. **Module 1 : Data Quality Gate & Nettoyage (`fiskr/quality.py`)**
   * **Niveau 1 (Bloquant/Rejet)** : Vérification des champs vides (`Rule_B01`), types d'entités invalides (`Rule_B02`), structure individu invalide (`Rule_B04` - prénom/nom absents après parsing), et longueur de nom insuffisante (`Rule_B05` - moins de 2 caractères).
   - **Niveau 2 (Alerte/Dégradé)** : Absence de pays rattaché (`Rule_M01`), absence de DOB pour les individus vivants (`Rule_M02`), caractères non translittérés (`Rule_M03`), contradiction de statut vital (`Rule_M04` - décès avec date mais booléen à faux), formats de date invalides (`Rule_M05`), numéro de passeport suspect (`Rule_M06`), structure LEI invalide (`Rule_M07`), et score d'extraction PDF faible (`Rule_M08`).
   - **Nettoyage Automatique & Niveau 3** : Normalisation de la casse, aplatissement ASCII (diacritiques/accents Müller -> MULLER), gestion d'incohérence de genre multi-valuée (`Rule_I03` - repli sur `U`), et suppression des suffixes légaux corporatifs (SA, SARL, LLC, GMBH, LTD, SOCIETE) pour les personnes morales via expressions régulières.

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
   * **Score Textuel de Base (Fuzzy)** : Moyenne pondérée hybride : $S_{base} = (0.4 \times JW) + (0.4 \times DL) + (0.2 \times TS)$
     * *Jaro-Winkler (JW)* : Fautes d'orthographe en début de chaîne.
     * *Damerau-Levenshtein (DL)* : Inversions, omissions et insertions.
     * *Token Sort (TS)* : Inversions de mots (ex: *PUTIN Vladimir* vs *Vladimir PUTIN*).
   * **Alias Risk Categorization** : Ingestion dynamique séparant les alias en `high_priority` (inclus dans le fuzzy scoring) et `low_priority` (exclus du scoring, stockés pour consultation humaine).
   * **Ajustements Contextuels (Bonus/Malus)** :
     * Date de Naissance (DOB) : Match exact (`+15`), dans la fenêtre de tolérance (`+5`), hors tolérance (`-15`).
     * Genre : Contradiction homme/femme (`-20`).
     * Géographie : Match sur pays (`+10`), aucun contact trouvé (`-10`).
   * **Seuil Réglementaire (Cut-off)** : Alertes générées si le Score Final $\ge 75\%$.

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

## 🏃 Ingestion & Connecteurs d'Entrée (`fiskr/ingest.py`)

L'outil intègre trois types de connecteurs pour charger les listes sources :
* **OFAC XML Connector** : Lecture et traitement séquentiels d'un flux XML via `ElementTree.iterparse` pour éviter la saturation de la mémoire vive.
* **CSV Connector** : Parseur de fichiers délimités personnalisables (délimiteur et dictionnaire de colonnes).
* **PDF Connector** : Extracteur textuel via `pypdf` avec analyseur heuritique NER (Named Entity Recognition) pour isoler les navires, identifiants et caractéristiques.

---

## 🚀 Installation & Lancement

### Prérequis
* Python 3.10 ou supérieur (développé et validé sous Python 3.13.1)
* Dépendances principales : `fastapi`, `uvicorn`, `sqlalchemy`, `pydantic`, `pyyaml`, `python-multipart`, `pypdf`, `faker`, `pytest`.

### Déploiement local
1. Installez les dépendances :
   ```bash
   pip install -r requirements.txt
   ```
2. Installez également `python-multipart` et `faker` si besoin :
   ```bash
   pip install python-multipart faker
   ```

### 1. Démarrer le Serveur et le Dashboard
Lancez le serveur web avec Uvicorn :
```bash
python -m uvicorn fiskr.api:app --host 127.0.0.1 --port 8000 --reload
```
Ouvrez votre navigateur sur : **`http://127.0.0.1:8000/`**

Le dashboard interactif se compose de 3 onglets principaux :
* **Gestion des Watchlists** : Permet de consulter la watchlist active (avec pagination rapide), d'importer de nouveaux snapshots de listes (XML, CSV, PDF), de comparer les versions historiques via le **Delta Engine** et d'ajouter des entités manuellement à la volée.
* **Criblage** : Regroupe le crible temps réel unitaire (Sandbox) et le crible de masse (simulateur batch).
* **Audit** : Historique réglementaire complet (Compliance Audit Trail) conforme aux normes ACPR/AMF.

### 2. Lancer la Suite de Tests
Exécutez la suite complète de 47 tests avec pytest :
```bash
python -m pytest
```

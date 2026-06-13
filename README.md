# Fiskr - Moteur de Criblage LBA-CFT & Personnes Politiquement Exposées (PEP)

Fiskr est un moteur de criblage (Screening Engine) de nouvelle génération destiné aux institutions financières. Il permet de confronter le référentiel tiers (clients, mandataires, bénéficiaires effectifs) aux listes de sanctions et de Personnes Politiquement Exposées (PEP) fournies par les éditeurs officiels (OFAC, UE, ONU, Dow Jones, World-Check) conformément aux exigences réglementaires ACPR/AMF.

Le projet propose une API temps réel asynchrone, un script de traitement de masse (Batch) optimisé sous Apache Spark, et un tableau de bord interactif pour les agents de conformité.

---

## 🛠️ Architecture et Modules

Le système est structuré autour des modules définis dans le Document d'Architecture Technique (DAT) :

1. **Module 1 : Data Quality Gate & Nettoyage (`fiskr/quality.py`)**
   * **Niveau 1 (Bloquant/Rejet)** : Vérification des champs vides (`Rule_B01`), longueur insuffisante (`Rule_B02`), et type d'entité inconnu (`Rule_B03`).
   * **Niveau 2 (Alerte/Dégradé)** : Absence de pays rattaché (`Rule_M01`), absence de DOB pour les individus (`Rule_M02`), et détection de caractères non translittérés (`Rule_M03`).
   * **Nettoyage Automatique** : Normalisation de la casse, aplatissement ASCII (diacritiques/accents Müller -> MULLER) et suppression des suffixes légaux corporatifs (SA, SARL, LLC, GMBH, etc.) pour les personnes morales via expressions régulières.

2. **Module 2 : Custom Blocking Engine (`fiskr/blocking.py`)**
   * Partitionnement par clé configurable (`config.yaml`) pour éviter le produit cartésien.
   * Utilisation de l'algorithme phonétique **Double Metaphone** sur le premier mot du nom (ex: *Müller* ou *Meller* -> *MLR*).
   * Gestion automatique des valeurs manquantes avec des clés de secours (`XX`).
   * Produit cartésien des clés en cas d'alias multiples ou pays multiples pour garantir un criblage sans omission.

3. **Module 3 : Moteur de Scoring & Ajustements (`fiskr/scoring.py`)**
   * **Score Textuel de Base** : Moyenne pondérée hybride : $S_{base} = (0.4 \times JW) + (0.4 \times DL) + (0.2 \times TS)$
     * *Jaro-Winkler (JW)* : Fautes d'orthographe en début de chaîne.
     * *Damerau-Levenshtein (DL)* : Inversions, omissions et insertions.
     * *Token Sort (TS)* : Inversions de mots (ex: *PUTIN Vladimir* vs *Vladimir PUTIN*).
   * **Ajustements Contextuels (Bonus/Malus)** :
     * Date de Naissance (DOB) : Match exact (`+15`), dans la fenêtre de tolérance (`+5`), hors tolérance (`-15`).
     * Genre : Contradiction homme/femme (`-20`).
     * Géographie : Match sur pays (`+10`), aucun contact trouvé (`-10`).
   * **Seuil Réglementaire (Cut-off)** : Alertes générées si le Score Final $\ge 75\%$.

4. **Module 4 & 6 : API Temps Réel & Piste d'Audit (`fiskr/api.py`, `fiskr/database.py`)**
   * Service API asynchrone écrit en **FastAPI**.
   * Indexation et mise en cache des watchlists en mémoire vive à l'initialisation de l'application pour des performances optimales (latence $\le 200\text{ms}$).
   * Persistance immuable (SQLAlchemy) avec connexion PostgreSQL cible et **failover automatique sur base SQLite locale** (`fiskr.sqlite3`) pour faciliter les tests locaux. Sauvegarde de la version/hash de liste, de la configuration et de l'arbre de décision de conformité.

5. **Module 5 : PySpark Batch Engine (`fiskr/batch.py`)**
   * Algorithme Spark optimisé par **Broadcast Join** pour la watchlist afin d'éviter les shuffles réseau lors de traitements de masse.

---

## 🚀 Installation

### Prérequis
* Python 3.10 ou supérieur (développé et validé sous Python 3.13.1)
* *Optionnel* : Base de données PostgreSQL (si configurée), environnement Spark (pour exécuter le mode batch natif)

### Déploiement local
1. Clonez le projet dans votre répertoire local.
2. Installez les dépendances python :
   ```bash
   pip install -r requirements.txt
   ```

---

## 🏃 Exécution du Projet

### 1. Démarrer le Serveur et le Dashboard
Lancez le serveur web avec Uvicorn :
```bash
python -m uvicorn fiskr.api:app --host 127.0.0.1 --port 8000 --reload
```
Ouvrez votre navigateur sur : **`http://127.0.0.1:8000/`**

Le dashboard interactif se compose de 4 onglets :
* **Sandbox Temps Réel** : Testez unitairement des profils clients, affichez les jauges de conformité, visualisez le rapport Data Quality Gate et inspectez l'arbre de décision bonus/malus.
* **Mode Batch** : Simulez des scans de masse en envoyant des listes JSON de clients.
* **Watchlist active** : Consultez, recherchez ou ajoutez des fiches de sanctions indexées en mémoire.
* **Piste d'Audit** : Historique réglementaire complet des décisions avec un inspecteur de logs intégrant la configuration de criblage figée.

### 2. Lancer la Suite de Tests
Exécutez la suite de tests automatisés avec pytest :
```bash
python -m pytest
```

---

## ⚙️ Configuration (`config.yaml`)

Le fichier `config.yaml` à la racine vous permet d'administrer le moteur de filtrage :
```yaml
blocking:
  strategy: "standard_performance"
  custom_key_layout:
    - "COUNTRY_ISO"
    - "ENTITY_TYPE"
    - "PHONETIC_FIRST"

scoring:
  cut_off_threshold: 75.0
  weights:
    jaro_winkler: 0.4
    damerau_levenshtein: 0.4
    token_sort: 0.2
  contextual_rules:
    dob_tolerance_window: 2          # ans d'écart tolérés
    dob_exact_bonus: 15
    dob_tolerance_bonus: 5
    dob_out_of_window_malus: -15
    gender_conflict_malus: -20
    geography_match_bonus: 10
    geography_no_match_malus: -10
```

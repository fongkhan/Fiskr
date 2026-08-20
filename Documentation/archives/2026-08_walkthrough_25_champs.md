# Walkthrough Complet - Intégration Visuelle des 25 Champs & Criblage Dynamique

Ce document présente les améliorations apportées pour afficher les **25 champs de criblage** au sein du gestionnaire de watchlists, et adapter dynamiquement les formulaires de criblage unitaire (Sandbox) et d'**Ajout Manuel** selon le type de profil recherché.

---

## 🛠️ Synthèse des Nouveaux Changements Apportés

### 1. Affichage Complet des 25 Colonnes (Gestion des Watchlists)
Afin de ne pas surcharger visuellement le tableau de la Watchlist Active avec 25 colonnes horizontales :
* **Lignes de tableau cliquables** : Les lignes (`<tr>`) du tableau disposent dorénavant d'un style `cursor: pointer` et d'une info-bulle d'aide.
* **Fenêtre de Détails Modal (`#details-modal`)** : Un clic sur n'importe quelle ligne appelle la fonction `showWatchlistDetails(item)` dans [app.js](file:///e:/Program%20Files/git/Fiskr/fiskr/static/app.js).
* **Grid Layout CSS** : Une grille responsive à 2 colonnes (`.details-grid` et `.details-item` dans [styles.css](file:///e:/Program%20Files/git/Fiskr/fiskr/static/styles.css)) structure l'intégralité des 25 champs (Lieu de naissance, Adresses alternatives, IMO Code, LEI, Fonction/Désignation, Remarques, etc.) de manière claire et aérée.

---

### 2. Formulaire d'Ajout Manuel Complet & Adaptatif
L'onglet **Gestion des Watchlists** -> **Ajout Manuel** prend désormais en charge l'ensemble des 25 champs réglementaires :
* **Champs Adaptatifs (`toggleManualFormFields()`)** : Tout comme pour le criblage, le formulaire d'ajout manuel affiche uniquement les champs pertinents selon le type d'entité sélectionné :
  - **Individu (I)** : Prénom, Nom, Nom de jeune fille, Genre, Date de décès, Dates de naissance, ainsi que les identifiants Passeport et CNI.
  - **Entité (E)** : Affiche le Legal Entity Identifier (LEI).
  - **Navire (V)** : Affiche le numéro IMO.
  - **Autre (O)** : Affiche le numéro d'immatriculation d'aéronef (Tail Number).
* **Accordéons de Saisie Structurés** :
  - **Identifiants Uniques & Transports (Hard Match)** : Pour saisir les LEI, IMO, Tail Number, Passeports et CNI.
  - **Localisation, Origine & Détails Supplémentaires** : Pour saisir la Nationalité, la Résidence, le Lieu de Naissance, les Alias, l'Adresse, la Ville, l'État, le Pays, l'Origine, la Fonction/Désignation, les Adresses Alternatives et les Informations Additionnelles.
* **Intégration API & Base de données** : Le modèle `WatchlistEntityCreate` et la route API `POST /api/watchlist/entity` ont été étendus pour recevoir et persister ces nouveaux attributs (y compris les structures JSON complexes des passeports et CNI).

---

### 3. Formulaire de Criblage Unitaire Dynamique (Adapté au Type)
Dans l'onglet **Criblage** -> **Criblage Temps Réel** :
* **Sélecteur de Type d'Entité** : Le menu déroulant propose maintenant :
  - **Individu (PP)**
  - **Entité / Personne Morale (PM)**
  - **Navire (Vessel)**
  - **Autre**
* **Champs Adaptatifs (`toggleFormFields()`)** : La fonction dans [app.js](file:///e:/Program%20Files/git/Fiskr/fiskr/static/app.js) réagit au changement pour masquer/afficher uniquement les informations pertinentes (Passeports/CNI pour les individus, LEI pour les entreprises, IMO pour les navires, Tail Number pour les aéronefs).
* **Nouveaux Accordions** : Ajout d'un nouvel accordéon **Localisation, Origine & Détails Supplémentaires** pour renseigner l'Adresse, la Ville, l'État, le Pays, le Lieu de Naissance, la Date de Décès, l'Origine, la Fonction/Désignation, les informations additionnelles et les adresses alternatives.

---

### 4. Robustesse du Backend et Normalisation du Type Client
* **Normalisation automatique** : Pour pallier les problèmes de cache navigateur et d'appels API directs, l'API backend ([api.py](file:///e:/Program%20Files/git/Fiskr/fiskr/api.py)) convertit automatiquement à la volée le type client reçu (`I` ou `PP` en `PP`, et tout autre type en `PM`) avant de le soumettre au Data Quality Gate. Cela garantit un fonctionnement robuste sans aucune erreur d'incohérence de structure.
* **Cache-Busting v2.6** : Incrémentation du paramètre de version à `v=2.6` dans [index.html](file:///e:/Program%20Files/git/Fiskr/fiskr/static/index.html) pour garantir le chargement immédiat des formulaires étendus.

---

## 🧪 Validation & Tests

### Tests Automatisés (`python -m pytest`)
* **Résultat** : Les 47 tests passent tous avec succès en `19.53s`.

### Vérifications manuelles recommandées
1. **Détails de la Watchlist** : Dans *Gestion des Watchlists* -> *Watchlist Active*, cliquez sur n'importe quelle ligne du tableau pour ouvrir la fenêtre modale.
2. **Ajout Manuel Complet** : Naviguez vers *Ajout Manuel*. Saisissez un nouvel individu avec son Prénom, Nom, son Passeport dans le premier accordéon et son Adresse/Ville dans le second accordéon. Soumettez le formulaire et constatez que l'entité apparaît dans le tableau.
3. **Criblage Temps Réel** : Criblez la fiche ajoutée précédemment avec son numéro de passeport ; le moteur de screening doit renvoyer une correspondance à 100% (Hard Match).

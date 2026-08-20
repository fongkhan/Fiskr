# ANNEXE TECHNIQUE : SPÉCIFICATIONS DE CARTOGRAPHIE OFAC ADVANCED XML
## Complément au Document d'Architecture Technique (DAT) - Moteur de Criblage
**Version :** 1.0 (Alignée sur le schéma global de conformité)  
**Date :** Juin 2026  

---

## 1. Objectif de l'Annexe
Cette annexe fournit au développeur les chemins XPath stricts et les correspondances d'identifiants (`TypeID`) nécessaires pour extraire les données de l'Advanced XML de l'OFAC (nœud `<DistinctParty>`) et alimenter les structures de données éclatées requises pour le **Hard Match** et le **Fuzzy Scoring**.

---

## 2. Cartographie des Types d'Entités (`entity_type`)

L'OFAC qualifie le type d'entité via le nœud `/DistinctParty/Profile/PartySubTypeID`. Le moteur doit interroger la table de référence des sous-types pour faire remonter le type racine (via l'attribut `PartyTypeID`) et le mapper selon la nomenclature stricte du DAT :

| Valeur de l'attribut `PartyTypeID` (OFAC) | Libellé Référence | Code Cible DAT (`entity_type`) |
| :--- | :--- | :--- |
| **151** | Individual | **I** (Individual) |
| **152** | Entity | **E** (Entity) |
| **154** | Vessel | **V** (Vessel) |
| **153** | Aircraft | **O** (Other - Traité en sous-type) |

---

## 3. Extraction et Séparation des Noms (`Individual`)

Lorsque `entity_type == 'I'`, le moteur de diagnostic doit parser les éléments enfants de `<Identity>` et exploiter la granularité de `<DocumentedNamePart>` pour isoler les composants :

* **Nom de Famille / Last Name :** Extrait depuis le nœud dont l'attribut `NamePartTypeID == "1361"`.
* **Prénom / First Name :** Extrait depuis le nœud dont l'attribut `NamePartTypeID == "1360"`.
* **Nom de Jeune Fille / Maiden Name :** Repéré lorsque l'alias global possède un attribut `AliasType` ou un commentaire associé décrivant un *Maiden Name* (souvent lié au groupe de type de nom ID `1340`).

---

## 4. Spécifications de l'Éclatement des Identifiants (Hard Match Input)

Pour alimenter la règle de **Priorité Absolue (Section 5.5 du DAT)**, les données lues séquentiellement dans le bloc `<IDRegistrationDocument>` doivent être aiguillées dans leurs conteneurs respectifs selon la valeur de l'attribut **`IDRegistrationDocTypeID`**.

### 4.1 Pièces d'Identité (Personnes Physiques / Individus)
Le développeur doit implémenter le routage conditionnel suivant lors du parcours du fichier XML :

* **`passport_documents` :** * *Détection OFAC XML :* Déclenché si `IDRegistrationDocTypeID == "392"` (Passport) ou si la description textuelle résolue dans la référence contient `"Passport"`.
    * *Champs cibles :* `number` (lu dans `<IDRegistrationDocElement>`), `issuing_country` (lu dans `../IssuedBy/CountryISO2`).
* **`national_id_documents` :** * *Détection OFAC XML :* Déclenché si `IDRegistrationDocTypeID == "391"` (National ID Card).
* **`other_id_documents` :** * *Détection OFAC XML :* Déclenché pour tous les autres types de documents de personnes physiques, notamment :
        * `386` : Driver's License
        * `390` : Refugee ID Card
        * `394` : Visa

### 4.2 Identifiants d'Entreprises / Personnes Morales (Corporates)
* **`lei_number` :** * *Détection OFAC XML :* Déclenché si `IDRegistrationDocTypeID == "15502"` (Legal Entity Identifier - LEI). La chaîne doit subir un contrôle de structure (longueur 20 caractères alphanumériques) avant d'activer le raccourci de Hard Match.
* **`national_registry_ids` :** * *Détection OFAC XML :* Déclenché pour les identifiants officiels d'enregistrement nationaux :
        * `9436` : Commercial Registry Number
        * `376` : V.A.T. Number (Numéro de TVA)
        * `384` : Tax ID No. (Numéro d'identification fiscale)
* **`other_registration_ids` :** * *Détection OFAC XML :* Tout autre document d'enregistrement d'entreprise (ex: `14002` : SWIFT/BIC Code).

### 4.3 Données de Transport (Vessels & Aircraft)
* **`imo_number` :**
    * *Détection OFAC XML :* Déclenché si `IDRegistrationDocTypeID == "13886"` (Vessel IMO Number). La valeur extraite doit être nettoyée pour ne conserver que la séquence numérique à 7 chiffres obligatoires.
* **`aircraft_tail_number` :**
    * *Détection OFAC XML :* Déclenché si `IDRegistrationDocTypeID == "13887"` (Aircraft Tail Number / Immatriculation).

---

## 5. Données Contextuelles et Statut Vital

### 5.1 Gestion du Genre (`gender`)
L'OFAC consigne le genre comme une caractéristique physique (Feature) au sein du bloc profil.
* **Chemin XPath :** `/DistinctParty/Profile/Feature[FeatureTypeID="25"]/FeatureVersion/VersionDetail/DetailReferenceID`
* **Logique de transformation :** Le moteur résout le code de l'attribut `DetailReferenceID` dans la table de référence `DetailReference` :
    * Si la valeur textuelle résolue est `"Male"` $\rightarrow$ `gender = "M"`
    * Si la valeur textuelle résolue est `"Female"` $\rightarrow$ `gender = "F"`
    * Dans tous les autres cas (ou en cas d'absence) $\rightarrow$ `gender = "U"` (Unknown).

### 5.2 Statut Vital (`is_deceased` & `date_of_death`)
Le décès d'un individu est traité par l'OFAC comme une caractéristique datée.
* **Chemin XPath :** `/DistinctParty/Profile/Feature[FeatureTypeID="24"]` (Code `24` = Détermination de la mort).
* **Extraction :** * Dès que ce bloc de feature est détecté, le booléen `is_deceased` est positionné à `True`.
    * La date exacte est extraite du nœud enfant `../FeatureVersion/DatePeriod/Start/From` en reconstruisant la chaîne au format standardisé `YYYY-MM-DD` via les sous-éléments `<Year>`, `<Month>` et `<Day>`.
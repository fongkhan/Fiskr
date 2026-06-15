### Script de Génération de Données de Test Client (`generate_test_data.py`)

Ce script utilise la librairie `Faker` pour générer un fichier CSV de 10 000 clients fictifs. Il injecte automatiquement des scénarios de test critiques au début du fichier afin de valider le comportement de votre moteur de criblage (Fuzzy Matching, Hard Match sur identifiants éclatés, et règles de bonus/malus contextuels).

#### Dépendances requises
```bash
pip install Faker
```
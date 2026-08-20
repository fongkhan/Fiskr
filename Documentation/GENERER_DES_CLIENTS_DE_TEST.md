# Générer un jeu de clients de test

Script : [`tools/generate_test_data.py`](../tools/generate_test_data.py)

Produit un fichier `clients_test.csv` de profils clients réalistes (Faker,
localisation FR/EN) au format attendu par l'import du référentiel clients —
de quoi éprouver un criblage batch sans toucher à des données réelles.

```bash
pip install faker
python tools/generate_test_data.py
```

Le fichier obtenu s'importe depuis **Criblage → Batch**, ou par l'API décrite
dans [INJECTION_CLIENTS.md](INJECTION_CLIENTS.md).

Deux fichiers d'exemple, plus petits et commentés, vivent dans
[`exemples/`](exemples/) : un minimal (colonnes obligatoires seulement) et un
complet (toutes les colonnes reconnues).

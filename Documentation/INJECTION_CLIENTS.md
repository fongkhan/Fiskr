# Injecter des clients dans Fiskr par l'API

Trois voies, selon le besoin. Toutes les trois sont décrites d'après le code
(`fiskr/api.py`, `fiskr/ingest.py`, `fiskr/quality.py`) et vérifiées bout en
bout contre un serveur.

| Besoin | Endpoint | Volume | Persistance |
|---|---|---|---|
| Charger / remplacer le référentiel | `POST /api/ingest` | fichier entier | nouveau snapshot |
| Créer ou mettre à jour une fiche | `POST /api/hooks/client-upsert` | 1 fiche | référentiel en production |
| Cribler sans rien stocker | `POST /api/screen` | 1 fiche | aucune |

## Authentification

Compte de service (recommandé pour une intégration) :

```
X-API-Key: fsk_...
```

ou `Authorization: Bearer fsk_...`. À défaut, le cookie de session du
navigateur. Les **webhooks entrants sont réservés aux clés d'API** : une
session utilisateur reçoit `403`.

---

## 1. Import de masse — `POST /api/ingest`

Requête `multipart/form-data`.

```bash
curl -X POST https://<host>/api/ingest \
  -H "X-API-Key: fsk_..." \
  -F "file_type=CLIENT_BASE" \
  -F "delimiter=," \
  -F "file=@clients.csv" \
  -F "progress_id=import-clients-2026-07-26"
```

| Champ | Obligatoire | Rôle |
|---|---|---|
| `file_type` | oui | `CLIENT_BASE` pour un référentiel clients |
| `file` | oui | le CSV, en UTF-8 |
| `delimiter` | non | défaut `,` — mettre `;` pour un export Excel français |
| `progress_id` | non | jeton de suivi, voir *Suivre un gros import* |

Réponse :

```json
{
  "message": "Successfully imported 6 items.",
  "snapshot_id": "51ef6305-9a37-4780-9488-7aeffb9ef0d7",
  "record_count": 6,
  "status": "READY",
  "rescreen": null
}
```

**`CLIENT_BASE` ne passe pas par l'homologation**, même quand
`ingestion.require_approval` est actif : le snapshot est directement `READY` et
devient le référentiel en production. Le gate d'homologation ne concerne que
les listes de sanctions. Un contrôle de complétude démarre ensuite en tâche de
fond et alimente *Conformité → Qualité des données clients*.

### Format du fichier

**Les en-têtes du CSV sont les noms de champs** : `parse_csv_file` recopie les
colonnes telles quelles, sans table de correspondance.

Minimum viable (`Documentation/exemples/exemple_clients_minimal.csv`) :

```csv
client_id,client_type,client_first_name,client_last_name,client_company_name,client_dob,client_gender,nationality,residence
CLI-000001,PP,Jean,Martin,,1975-03-12,M,FR,FR
CLI-000002,PP,Amina,Benali,,1988-11-02,F,"DZ,FR",FR
CLI-000003,PM,,,Boulangerie Martin SARL,,,,FR
```

Fichier complet, toutes colonnes renseignées :
`Documentation/exemples/exemple_clients_complet.csv`.

### Colonnes

**Identité — obligatoires**

| Colonne | Valeurs | Remarque |
|---|---|---|
| `client_id` | texte | identifiant interne, sert de clé pour l'upsert |
| `client_type` | `PP` ou `PM` | toute autre valeur ⇒ **ligne rejetée** |
| `client_last_name` | texte | **obligatoire si `PP`** |
| `client_company_name` | texte | **obligatoire si `PM`** |

**Identité — recommandées**

`client_first_name`, `client_maiden_name`, `client_dob` (`AAAA-MM-JJ`),
`client_gender` (`M` / `F` / `U`), `client_is_deceased` (`true` / `false`),
`client_date_of_death`, `client_place_of_birth`.

**Pays — colonnes à plat, valeurs séparées par des virgules *dans* la cellule**

`nationality`, `residence`, `birth_country`, `registration_country`. Le moteur
les recompose en un objet `client_countries`. Codes ISO 2 lettres.

```csv
...,"DZ,FR",FR,DZ,
```

**Adresse** — `client_address`, `client_city`, `client_state`,
`client_country`, `client_alternative_addresses` *(séparateur `;`)*.

**Identifiants** — `client_lei_number`, `client_tax_id`, `client_iban`,
`client_bic`, `client_crypto_wallets` *(séparateur `;`)*.

**Documents et registres — JSON dans la cellule** :
`client_national_registry_ids`, `client_other_registration_ids`,
`client_passport_documents`, `client_national_id_documents`,
`client_other_id_documents`.

```csv
...,"[{""number"":""19AB45678"",""country"":""FR""}]",...
```

En CSV, les guillemets internes se doublent. Une cellule vide vaut `[]`.

**Contact** — `client_phone`, `client_email`, `client_website`.

**Pilotage** — `client_risk_rating` (mis en majuscules),
`client_pep_flag` (`true` / `1` / `oui` / `yes`), `client_segment`,
`client_activity_sector`, `client_activity_countries` *(séparateur `,`)*,
`client_relationship_start`, `client_status` (mis en majuscules),
`client_origin`, `client_designation`, `client_additional_informations`.

**Formes courtes acceptées** — la plupart des colonnes d'adresse et de contact
acceptent aussi le nom sans préfixe : `city`, `state`, `country`, `address`
(et la faute de frappe historique `adress`), `place_of_birth`, `origin`,
`designation`, `date_of_death`, `iban`, `bic`, `tax_id`, `phone`, `email`,
`website`, `alternative_addresses`, `crypto_wallets`. En cas de doute,
**utilisez la forme préfixée `client_`** : elle fonctionne partout.

### Les pièges, vérifiés

**Les lignes invalides sont ignorées en silence.** L'import répond `200` et
`record_count` compte les lignes *retenues*. Une ligne rejetée ne produit
aucune erreur HTTP.

> **Comparez toujours `record_count` au nombre de lignes envoyées.** C'est le
> seul signal de rejet.

Une ligne est rejetée si :

| Règle | Condition |
|---|---|
| `Rule_B01` | `PP` sans `client_last_name`, ou `PM` sans `client_company_name` |
| `Rule_B02` | `client_type` différent de `PP` / `PM` (y compris vide) |
| `Rule_B04` | `PP` sans aucun prénom **ni** nom |
| `Rule_B05` | nom principal de moins de 2 caractères alphanumériques |

**Le hash SHA-256 du fichier fait office de clé d'idempotence.** Réenvoyer un
fichier identique ne recrée rien :

```json
{"message": "Snapshot with this hash already uploaded.", "snapshot_id": "...", "status": "READY"}
```

Pour rejouer un import, le contenu doit changer (ne serait-ce qu'une colonne
d'horodatage). Les snapshots en `ERROR` portant le même hash sont purgés
automatiquement avant l'import.

**Le référentiel en production est le dernier snapshot `CLIENT_BASE` en
`READY`.** Un import ne fusionne pas avec le précédent : il le *remplace* comme
référence. Envoyez la base complète, pas un delta.

**L'encodage doit être UTF-8.** Un export Excel en Windows-1252 casse les
accents et les écritures non latines.

### Suivre un gros import

Passez un `progress_id` de votre choix, puis interrogez :

```bash
curl -H "X-API-Key: fsk_..." "https://<host>/api/progress?id=import-clients-2026-07-26"
```

Les phases sont `UPLOAD` (octets reçus), puis l'ingestion fiche par fiche.
L'endpoint reste servi pendant tout l'import.

---

## 2. Fiche unitaire, temps réel — `POST /api/hooks/client-upsert`

Pour brancher un CRM ou un outil KYC : création ou mise à jour **par
`client_id`** dans le dernier référentiel `CLIENT_BASE` en production.

```bash
curl -X POST https://<host>/api/hooks/client-upsert \
  -H "X-API-Key: fsk_..." \
  -H "X-Idempotency-Key: crm-evt-88213" \
  -H "Content-Type: application/json" \
  -d '{
        "client_id": "CLI-000001",
        "client_type": "PP",
        "client_first_name": "Jean",
        "client_last_name": "Martin",
        "client_dob": "1975-03-12",
        "client_countries": {"nationality": ["FR"], "residence": ["FR"]},
        "client_risk_rating": "LOW",
        "client_pep_flag": false
      }'
```

Ici `client_countries` est un **objet**, pas des colonnes à plat.

Champs acceptés : `client_id` (obligatoire), `client_type` (défaut `PP`),
`client_first_name`, `client_last_name`, `client_maiden_name`,
`client_company_name`, `client_dob`, `client_gender`, `client_countries`,
`client_address`, `client_city`, `client_country`, `client_iban`, `client_bic`,
`client_tax_id`, `client_phone`, `client_email`, `client_risk_rating`,
`client_pep_flag`, `client_segment`, `client_activity_sector`,
`client_relationship_start`, `client_status`.

**Le webhook couvre moins de champs que l'import CSV** (pas de documents
d'identité, pas de portefeuilles crypto, pas d'adresses alternatives). Pour ces
champs, passez par l'import.

Réponses à connaître :

| Code | Signification |
|---|---|
| `403` | appel avec une session utilisateur : réservé aux clés d'API |
| `401` | `X-Fiskr-Signature` absente ou fausse, quand `hooks.secret` est configuré |
| `409` | **aucune base `CLIENT_BASE` en production** — faites d'abord un import |
| `422` | charge utile invalide |
| `400` | `client_type` autre que `PP` / `PM` |

`X-Idempotency-Key` rend l'appel rejouable sans doublon : la même clé renvoie
la réponse d'origine. Chaque appel est tracé au journal d'administration sous
`CLIENT_UPSERT_HOOK`.

### Signature HMAC

Si `hooks.secret` est configuré, signez le **corps brut** en HMAC-SHA256 :

```bash
BODY='{"client_id":"CLI-000001","client_type":"PP","client_last_name":"Martin"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$HOOKS_SECRET" -hex | awk '{print $2}')
curl -X POST https://<host>/api/hooks/client-upsert \
  -H "X-API-Key: fsk_..." -H "X-Fiskr-Signature: $SIG" \
  -H "Content-Type: application/json" -d "$BODY"
```

---

## 3. Cribler sans persister — `POST /api/screen`

Mêmes champs que l'upsert, préfixés `client_`. Rien n'est écrit dans le
référentiel ; une alerte est créée si le score franchit le seuil.

```bash
curl -X POST https://<host>/api/screen \
  -H "X-API-Key: fsk_..." -H "Content-Type: application/json" \
  -d '{"client_type":"PP","client_first_name":"Mouammar","client_last_name":"Kadhafi",
       "client_countries":{"nationality":["LY"],"residence":["LY"]}}'
```

La réponse porte `best_match` (score, statut, `resource_equivalences`),
`all_matches`, `candidates_count`, `client_quality_report` et `alert_id`.

---

## Recette d'intégration recommandée

1. **Importez le fichier d'exemple** `Documentation/exemples/exemple_clients_complet.csv`
   sur un environnement de test. Trois de ses six fiches désignent des
   personnes ou entités réellement sanctionnées — Mouammar Kadhafi en graphie
   française, 김정은 en écriture d'origine, Rosneft — et doivent lever une
   alerte **dès lors que les listes correspondantes sont en production**. Si
   aucune liste n'est chargée, aucune alerte n'est attendue : c'est le
   référentiel de listes qu'il faut vérifier en premier.
2. **Vérifiez `record_count`** contre le nombre de lignes du fichier.
3. **Contrôlez la qualité** : `GET /api/clients/quality` donne la complétude
   par champ et les fiches à risque pour le criblage (DOB manquante, pays
   manquant, `PP` sans prénom).
4. **Constituez un panel et mesurez** avant d'ouvrir en production —
   `POST /api/resources/simulate`, voir
   [MESURE_RESSOURCES.md](MESURE_RESSOURCES.md).

## Notes

Les noms en écriture non latine (arabe, cyrillique, hangul, kana, hanzi) sont
importés tels quels et translittérés au criblage — voir
[RESSOURCES_LINGUISTIQUES.md](RESSOURCES_LINGUISTIQUES.md). Le contrôle qualité
les signale par `Rule_M03` : c'est un **avertissement**, jamais un rejet, et il
n'empêche ni l'import ni le criblage.

# Ressources linguistiques — fichiers d'équivalences

## Pourquoi

Le moteur de criblage ne sait comparer que des **chaînes de caractères** :
translittération des écritures non latines et suppression des diacritiques
(`fiskr/quality.py`), similarité Jaro-Winkler, distance de Damerau-Levenshtein,
token sort (`fiskr/scoring.py`), double métaphone pour le regroupement des
candidats (`fiskr/phonetics.py`).

Ces métriques rattrapent une **faute de frappe** — Damerau-Levenshtein gère les
substitutions, les insertions et jusqu'aux transpositions, donc *Mohammad* et
*Mohammed* se rapprochent tout seuls. Mais elles ne peuvent structurellement
**rien** déduire de :

| Cas | Exemple | Pourquoi les métriques échouent |
|---|---|---|
| Équivalents entre langues | Henri ≡ Harry ≡ Heinrich ≡ Enrique | Aucune proximité de chaîne |
| Romanisations concurrentes | Zhang ≡ Chang ≡ Cheung | Écritures d'origine identiques, alphabets d'arrivée différents |
| Exonymes | Londres ≡ London, Allemagne ≡ Germany ≡ DE | Deux mots sans rapport graphique |
| Fautes installées | Une graphie qui s'est trop éloignée de l'original | Distance d'édition trop grande |

C'est une **connaissance**, pas un calcul. Elle se déclare dans un fichier de
ressources.

## Format

Un fichier YAML par type de champ, dans le répertoire `resources/` (chemin
surchargeable par `resources.directory` dans `config.yaml`).

```yaml
# en-tête de provenance : une ressource de criblage doit être justifiable
# devant un contrôleur
type: given_name
label: "Prénoms — équivalents entre langues et translittérations"
source: "Fiskr, jeu de départ. À compléter selon le portefeuille."

groups:
  - id: MUHAMMAD
    terms: [Mohammad, Mohammed, Muhammad, Mohamed, Mohamad]
  - id: HENRY
    terms: [Henri, Henry, Harry, Heinrich, Enrique, Enrico, Hendrik]
```

| Clé | Rôle |
|---|---|
| `type` | Type de champ. Valeurs : `given_name`, `surname`, `city`, `country`, `state`. |
| `label` | Libellé affiché dans l'écran de diagnostic. Facultatif. |
| `source` | Provenance du jeu de données. Facultatif mais fortement recommandé. |
| `groups[].id` | Identifiant de la **classe canonique**. Majuscules, stable dans le temps. |
| `groups[].terms` | Termes équivalents. **Deux au minimum** — un terme seul ne déclare aucune équivalence, le chargement le refuse. |

Tous les termes d'un groupe partagent la même classe ; deux termes de la même
classe sont traités comme identiques par le moteur.

### Normalisation des termes

Les termes sont indexés après translittération, suppression des diacritiques,
passage en majuscules et réduction des espaces — la même normalisation que le
criblage. `Müller`, `MULLER` et `Мюллер` donnent donc la même clé : **déclarez
les termes dans l'écriture qui vous arrange**, ils restent trouvables.

### Ajouter un type de champ

Le type de champ n'est qu'une clé d'index. Pour couvrir un nouvel univers —
nationalités, secteurs d'activité, formes juridiques, alias de navires — il
suffit d'ajouter une valeur à `FIELD_TYPES` (`fiskr/resources.py`) et un
fichier. Le moteur n'a pas à changer.

## Collisions

Un terme rattaché à **deux classes différentes** rendrait le criblage non
déterministe. Le chargeur les détecte, les signale avec le fichier fautif, et
tranche de façon reproductible : **le premier déclarant l'emporte**. Les
collisions sont visibles dans `GET /api/resources` et dans la carte du
tableau de bord.

Exemple réel, arbitré dans `resources/surnames.yaml` : la romanisation
cantonaise « Wong » transcrit aussi bien 王 (Wang) que 黃 (Huang), et « Ng »
aussi bien 黃 que 吳 (Wu). Arbitrage retenu — Wong → WANG, Ng → WU, leurs
lectures les plus fréquentes — avec la conséquence assumée écrite dans le
fichier : un Huang écrit « Wong » ne sera pas rapproché par cette table.

## Où les équivalences agissent

Les deux points d'application sont nécessaires ; l'un sans l'autre est inopérant.

**1. Blocking** (`fiskr/blocking.py`)

Les candidats sont regroupés par clé. Sans intervention ici, « Henri » et
« Harry » ne tombent jamais dans le même seau et **ne sont donc jamais
comparés** — une table branchée uniquement sur le scoring n'aurait aucun effet.

- composante `PHONETIC_FIRST` : la classe du premier mot est émise comme clé
  `EQ<classe>`, **en plus** des clés métaphone ;
- composante `COUNTRY_ISO` : la classe du pays est émise **en plus** de la
  valeur brute — sans quoi un client dont la nationalité est saisie
  « Allemagne » ne rencontre jamais une fiche déposée sous « DE ».

Ces clés sont **additives** : aucune paire aujourd'hui candidate ne cesse de
l'être.

**2. Scoring** (`fiskr/scoring.py`)

- `compute_base_score` : un token n'est remplacé par sa classe que si cette
  classe est présente **des deux côtés** (règle du croisement). Toute paire
  sans classe commune ressort caractère pour caractère identique — le seul
  effet possible de la table est de rapprocher deux termes déclarés
  équivalents, jamais de déplacer un score qu'elle n'a pas à toucher.
- `calculate_geography_adjustment` : les pays sont comparés par classe, la
  description affiche les libellés d'origine (`ALLEMAGNE ≡ DE`).

**3. Traçabilité**

Chaque équivalence appliquée est inscrite dans le `decision_tree` sous
`resource_equivalences` et affichée dans le résultat de criblage, la modale
d'alerte et la modale d'audit. Un analyste doit pouvoir lire **pourquoi** deux
noms dissemblables ont matché.

## Activation et gouvernance

Une table d'équivalences **augmente le rappel au prix de la précision** :
chaque classe crée des rapprochements qui n'existaient pas. C'est l'effet
recherché, mais il se mesure avant la production.

- **Tout est désactivé par défaut.** Une installation existante ne change pas
  de comportement tant qu'un responsable n'a rien activé.
- **Activation par type de champ**, à chaud : Alertes → Blocking Keys → carte
  *Ressources Linguistiques*, ou `PUT /api/settings/ingestion` avec
  `{"resource_fields": {"given_name": true}}`. Le changement recharge l'index
  de criblage (sans quoi seule la sonde du client porterait les clés
  d'équivalence, et les deux côtés ne se rencontreraient jamais).
- **Mesurez avant de produire** : activer les ressources est un changement de
  paramétrage de criblage au même titre qu'un seuil. Passez-le au **cahier de
  tests** (`fiskr/backtest.py`), qui chiffre l'écart de taux d'interception
  avant/après.

## API

| Endpoint | Rôle |
|---|---|
| `GET /api/resources` | Fichiers chargés, empreinte SHA-256, compteurs par type, collisions, types actifs |
| `POST /api/resources/reload` | Rechargement à chaud (admin), tracé `RESOURCES_RELOADED` au journal d'administration |
| `GET /api/resources/lookup?term=Harry[&field=given_name]` | Classe et équivalents d'un terme — l'outil de diagnostic d'un analyste |
| `PUT /api/settings/ingestion` | `resource_fields` : activation par type |

L'empreinte de l'ensemble chargé rend la ressource **citable en audit** : on
peut affirmer avec quelle version d'une table un criblage a été fait.

## Modifier une ressource en production

1. Éditer le fichier YAML.
2. `POST /api/resources/reload` (ou le bouton *Recharger les fichiers*).
3. Vérifier l'empreinte et l'absence de collision dans la carte de diagnostic.
4. Passer le cahier de tests avant d'activer un type qui ne l'était pas.

Un fichier invalide (type inconnu, groupe à un seul terme, YAML cassé) est
**refusé** avec le nom du fichier : mieux vaut un refus explicite qu'un
criblage qui tourne avec une ressource à moitié chargée sans que personne ne
le sache.

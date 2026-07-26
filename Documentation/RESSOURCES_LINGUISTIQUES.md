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

---

# Fouille automatique d'homonymes

Les tables livrées sont un point de départ, pas un état final : chaque
portefeuille a ses graphies, chaque nouvelle liste apporte ses variantes. Un
moteur cherche donc chaque nuit de nouveaux homonymes et les propose — ou les
applique — selon le paramétrage.

## D'où viennent les découvertes

Pas d'une source externe : de deux données que l'installation possède déjà, et
dont la valeur probante dépasse n'importe quel dictionnaire acheté.

| Source | Ce qu'elle vaut |
|---|---|
| `ALIAS` — le graphe d'alias des listes en production | Quand l'OFAC déclare qu'une fiche « Muhammad AL-ASSAD » porte l'alias « Mohammed AL-ASAD », **l'autorité elle-même** établit que les deux graphies désignent la même personne. En extraire la paire (MUHAMMAD, MOHAMMED) n'est pas une inférence : c'est une lecture de la donnée officielle. |
| `ANALYST` — les alertes clôturées « vrai positif » | Un analyste a validé humainement que le nom du client et le nom listé désignent la même personne. C'est la preuve la plus forte disponible dans le système. |

## Le garde-fou qui rend la fouille utilisable

Le piège évident : « Ali HASSAN » alias « Abu MUHAMMAD » est un **nom de
guerre**, pas une variante d'écriture. Aligner naïvement les mots produirait
les paires absurdes Ali = Abu et Hassan = Muhammad, et le criblage se mettrait
à rapprocher des gens sans aucun rapport. **Une table d'équivalences fausse est
pire que pas de table du tout.**

La règle retenue élimine ce cas par construction : **les deux noms doivent
avoir le même nombre de mots et ne différer que sur UN SEUL**. Tout le reste
étant identique, le mot divergent est nécessairement une autre écriture du même
élément.

| Confrontation | Verdict |
|---|---|
| Mohammad **Al Assad** vs Mohammed **Al Assad** | 1 divergence → paire (MOHAMMAD, MOHAMMED) retenue |
| **Ali Hassan** vs **Abu Muhammad** | 2 divergences → écarté |
| Youssef vs Yusuf | un seul mot, aucun élément commun → écarté |
| Ali Hassan vs Ali Hassan Al Sayed | nombres de mots différents → écarté |

S'y ajoutent :

- **particules exclues** — AL, EL, BIN, IBN, ABU, DE, VAN, VON, MC… se
  répètent dans des milliers de noms sans jamais constituer un prénom ;
- **longueur minimale** de 3 caractères ;
- **proximité exigée** — concordance phonétique (double métaphone) *ou*
  similarité de chaîne ≥ seuil. Une variante d'écriture est proche
  graphiquement ou phonétiquement ; un couple qui n'est ni l'un ni l'autre
  n'est pas une variante ;
- **répétition** — la paire doit apparaître dans au moins N fiches
  **distinctes** (2 par défaut). Une coquille isolée dans un seul
  enregistrement ne devient pas une règle de criblage ;
- **individus seulement** — une raison sociale n'a ni prénom ni nom ; l'aligner
  produirait des paires de mots de vocabulaire.

## Confiance

Trois facteurs, tous explicables devant un contrôleur :

- la **répétition** (plafonnée à cinq occurrences, pour qu'un gros programme de
  sanctions n'écrase pas le reste) ;
- la **proximité** de chaîne, plus un bonus de concordance phonétique ;
- la **source** : une alerte confirmée par un analyste porte une validation
  humaine, l'alias officiel porte l'autorité de l'émetteur.

## Classement et refus

Une paire découverte rejoint une classe existante si l'un de ses termes y est
déjà. Si les deux termes appartiennent à **deux classes différentes**, la
découverte est **refusée** : fusionner deux classes sur la foi d'une trouvaille
automatique réunirait des univers que quelqu'un a délibérément séparés
(l'arbitrage Wong → WANG / Ng → WU, par exemple).

## Gouvernance

- **Planification** : `resources.mining` — activation, cron (3 h 15 par défaut,
  après les synchronisations nocturnes donc sur des listes fraîches),
  occurrences et similarité minimales, seuil d'auto-application, sources.
- **Auto-application** : `auto_approve_confidence`. À `0`, la fouille se
  contente de proposer et toute découverte passe par une décision humaine. Le
  défaut (`0.85`) applique les découvertes très sûres — mais **une équivalence
  appliquée n'atteint le criblage que si son type de champ est par ailleurs
  activé**, ce qui n'est jamais le cas par défaut. La chaîne de sécurité tient
  donc en deux verrous indépendants.
- **Révocable à tout moment** : rejeter une équivalence appliquée la retire de
  l'index et reconstruit le cache de criblage. C'est cette réversibilité qui
  rend l'auto-application acceptable.
- **Une décision humaine n'est jamais défaite** par une passe automatique : une
  équivalence rejetée ne revient pas la nuit suivante.
- **Notification** à chaque passe qui crée ou applique quelque chose.
- **Traçabilité** : chaque décision est inscrite au journal d'administration
  (`LEARNED_EQUIVALENCE_DECIDED`, `RESOURCE_MINING_RUN`), et chaque équivalence
  conserve ses **preuves** — les fiches ou les alertes qui l'ont fait
  apparaître. « Le moteur l'a trouvée » ne suffit pas devant un contrôleur.

## API

| Endpoint | Rôle |
|---|---|
| `GET /api/resources/learned?status=PROPOSED` | File de revue, triée par confiance décroissante, avec preuves et compteurs |
| `POST /api/resources/mine` | Passe à la demande (Admin, asynchrone avec progression) |
| `POST /api/resources/learned/{id}/decide` | `APPROVE` / `REJECT` (Admin, tracé, recharge l'index) |
| `PUT /api/settings/ingestion` | `resource_mining` : planification et seuils |

## Limite assumée

La fouille découvre des **variantes d'écriture** : translittérations
concurrentes, graphies proches, coquilles installées. Elle ne découvre **pas**
les équivalents inter-langues sans proximité graphique ni phonétique — *Henri*
≡ *Harry*, *Bill* ≡ *William* — parce que rien dans les données ne permet de
les déduire sans risque. Ces cas relèvent de la curation manuelle des fichiers.

Le type de champ est déduit de la **position** du mot divergent (premier =
prénom, dernier = nom). Sur un nom en ordre asiatique (« Zhang Wei »), cette
déduction est inversée. Sans conséquence pratique : au blocking comme au
scoring, les tables prénom et nom sont interrogées toutes les deux sur chaque
mot — le classement ne sert qu'à la lisibilité et à la gouvernance.

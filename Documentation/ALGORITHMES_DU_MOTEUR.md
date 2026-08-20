# Algorithmes du moteur — inventaire opposable

Ce document décrit **tous les mécanismes de rapprochement** du moteur de
Fiskr, ce que chacun apporte, ce que sa désactivation coûte, et où il agit.
Il est écrit pour être versé au dossier réglementaire : l'ACPR attend un
dispositif de criblage *documenté et justifié*, et « le moteur fait de la
translittération » n'est pas une réponse.

Le catalogue technique correspondant est `fiskr/capabilities.py`. C'est la
source de vérité unique : le réglage, l'écran, l'API et la traçabilité en
sont dérivés. Ajouter un mécanisme pilotable = ajouter une entrée.

---

## Ce qui est pilotable, et ce qui ne l'est pas

### Pilotable
Trente-quatre capacités, réparties en cinq familles, réglables **par canal**
(criblage clients / filtrage transactionnel) et **à chaud**.

### Les poids des métriques : réglables, mais pas des interrupteurs
`scoring.weights.*` se règle **à chaud** (Paramètres → Criblage), avec une
simulation d'impact avant application (`POST /api/settings/scoring/simulate`).

Ce qu'il faut savoir avant d'y toucher : les poids **ne sont pas normalisés**
— `compute_base_score` en fait une somme simple. Mettre un poids à zéro ne
neutralise donc pas la métrique, cela **change l'échelle du score** et invalide
tous les seuils de coupure calibrés. Ce n'est pas un interrupteur, c'est un
recalibrage : il se mesure au cahier de tests avant d'être appliqué.

| Métrique | Poids par défaut | Ce qu'elle apporte |
|---|---:|---|
| `jaro_winkler` | 0,4 | Proximité de chaînes, sensible au préfixe commun |
| `damerau_levenshtein` | 0,4 | Distance d'édition, transpositions comprises |
| `token_sort` | 0,2 | Corrige l'**ordre** des tokens (« Ivanov Ivan » ↔ « Ivan Ivanov ») |
| `token_set` | **0** | Corrige l'**inclusion** : un nom entièrement contenu dans l'autre marque 100 (« Vladimir Putin » ↔ « Vladimir Vladimirovitch Poutine ») |

`token_set` est livrée à **poids nul** délibérément : l'activer déplacerait
tous les scores d'un coup, donc les seuils, les règles anti-faux positifs et
les cahiers de tests déjà homologués. L'exploitant l'active quand il veut,
mesure l'écart au cahier de tests, puis décide.

### Déjà piloté ailleurs : les équivalences linguistiques
`resources.enabled_fields` a son propre écran et sa propre mesure d'impact.
Le catalogue ne les duplique pas ; il pilote en revanche leur **prérequis au
blocking** (`blocking.equivalences`), sans lequel elles sont inertes.

---

## Où les capacités agissent

Elles pilotent la **comparaison** : génération des clés de blocking,
normalisation des noms comparés, ajustements contextuels, rapprochement sur
identifiants.

Elles ne pilotent **pas** la normalisation faite à l'**ingestion**. Ce qui est
stocké est versé au dossier réglementaire avec son instantané de liste ; le
faire dépendre d'un réglage à chaud normaliserait deux fiches de la même liste
différemment selon l'heure de leur import.

> **Conséquence à connaître.** Une capacité d'écriture ou de nettoyage
> (translittération, diacritiques, suffixes juridiques) agit **immédiatement**
> sur la sonde client ; les fiches déjà ingérées gardent la forme sous
> laquelle elles ont été stockées, **jusqu'au prochain rechargement complet de
> leur liste**.

Le chemin **batch Spark** (`fiskr/batch.py`, `run_spark_batch_screening`) est
**hors périmètre**, et le module le dit noir sur blanc : son UDF ré-implémente
le scoring à la main, ignore la géographie, écrit le seuil en dur à 75 et ne
passe pas par `match_entities`. Aucun appelant dans le dépôt. Le chemin
`run_pandas_batch_screening`, lui, est le moteur complet.

---

## 🔤 Écritures et normalisation

| Capacité | Ce qu'elle fait | Ce qu'on perd en la coupant |
|---|---|---|
| `translit` | Translittère les écritures non latines avant comparaison | Les noms en cyrillique, chinois, arabe, coréen ou japonais n'atteignent plus aucune métrique ; le double métaphone rend une clé vide. Tous les alias en écriture d'origine de l'OFAC et de l'ONU deviennent invisibles. |
| `translit.<écriture>` × 10 | Même chose, **écriture par écriture** : cyrillique, han, arabe, hangul, kana, hébreu, grec, thaï, devanagari, autres | L'écriture coupée sort du périmètre — **sauf** les noms que les tables d'équivalences connaissent déjà, qui restent rapprochés par elles. La perte est donc réelle mais **inégale** : elle se mesure. |
| `diacritics` | Aplatit les diacritiques (Müller → Muller) | « Müller » cesse de rapprocher « MULLER ». |
| `noise_words` | Retire les suffixes juridiques (SA, SARL, LLC, GMBH…) | « ACME SARL » et « ACME LLC » restent comparés avec leur suffixe, qui pèse dans la distance d'édition. |

**Le classificateur d'écritures** (`quality.detect_scripts`) est le seul
développement neuf de ce dispositif. Avant lui, `has_non_latin_chars` était
**binaire** — latin ou non latin, sur un seuil de point de code — et **aucun
code du dépôt ne nommait une écriture**. Traiter le cyrillique autrement que
le chinois était impossible.

La classification se fait par plages Unicode, sans dépendance nouvelle. Ce qui
n'entre dans aucune plage tombe dans `other`, qui a sa propre bascule : **aucune
écriture ne peut échapper au réglage par oubli de plage**, et un test verrouille
l'accord entre le détecteur et le catalogue.

Le périmètre de translittération, lui, **n'a pas bougé d'un caractère** : le
critère historique reste le seul juge de « faut-il translittérer ce
caractère », le nommage d'écriture vient après.

### Limite connue, mesurée, et consignée en test
Le **han**, le **hangul** et l'**arabe** ne franchissent pas le blocking, et
c'est indépendant du réglage :

- la translittération d'une écriture syllabique rend **un seul mot** —
  « 习近平 » → « XiJinPing », « 김정은 » → « GimJeongEun » — alors que la clé
  phonétique est bâtie sur le premier mot, et que la liste porte « Xi
  Jinping », premier mot « Xi » ;
- l'arabe n'écrit pas les voyelles brèves : « محمد » rend « mhmd » là où la
  liste porte « Mohammed ».

Le cyrillique et le grec, alphabétiques, franchissent sans difficulté. Le
scoring rattrape une partie de ces cas quand les noms arrivent en champs
séparés ; le blocking, non.

---

## 🎯 Sélection des candidats

| Capacité | Ce qu'elle fait | Ce qu'on perd |
|---|---|---|
| `blocking.phonetic` | Clés double métaphone | « Shmit » et « Schmidt » ne tombent plus dans le même seau et ne sont **jamais comparés** : le scoring ne les voit même pas. |
| `blocking.equivalences` | Clés `EQ<classe>` issues des tables linguistiques | Les tables deviennent **inertes même si elles sont activées** : sans clé commune, « Henri » et « Harry » ne sont pas candidats l'un pour l'autre. |
| `blocking.country_wildcard` | Joker « pays inconnu » à l'interrogation | Toute fiche listée sans pays redevient structurellement inatteignable — les listes d'alerte de régulateurs n'en publient presque jamais. |

Ces deux premiers mécanismes sortaient de la **même branche de code** et
étaient indissociables. Ils sont désormais séparés, et deux tests le
verrouillent dans les deux sens.

Le layout `COUNTRY_ISO / ENTITY_TYPE / PHONETIC_FIRST` garde son écran
existant (sous-onglet **Blocking Keys**).

---

## 👤 Variantes de noms comparées

| Capacité | Ce qu'on perd |
|---|---|
| `names.reversed_order` | Les listes officielles écrivent les noms d'Asie de l'Est patronyme en tête ; le référentiel client concatène « prénom nom ». Sans cette variante, les deux chaînes sont systématiquement inversées et seul le token sort (20 % du poids) résiste — ce qui ne franchit aucun seuil. |
| `names.maiden` | Une personne listée sous son nom de naissance cesse d'être rapprochée d'un client connu sous son nom d'usage, et réciproquement. |
| `names.aliases_listed` | Seul le nom principal de chaque fiche est comparé : les alias officiels, souvent la graphie réellement utilisée, ne sont plus criblés. |
| `names.aliases_client` | Les dénominations alternatives du référentiel (nom commercial, ancienne raison sociale) ne sont plus criblées. |

---

## ⚖️ Ajustements contextuels

| Capacité | Ce qu'elle fait | Ce qu'on perd |
|---|---|---|
| `adjust.dob` | +15 / +5 / −15 selon la concordance de date de naissance | Le discriminant le plus fort sur les homonymes : deux « Mohamed Ali » nés à quarante ans d'écart cessent d'être départagés. |
| `adjust.gender` | −20 en cas de conflit de genre | Les couples homonymes de genres opposés ne sont plus écartés. |
| `adjust.geography` | +10 / −10 selon le pays | La confirmation géographique disparaît dans les deux sens. |
| `adjust.geography.missing_is_neutral` | Rend **neutre** un pays manquant, au lieu du malus historique | **Inactive par défaut.** L'activer élargit le périmètre d'alertes et se mesure donc avant de s'appliquer. |

Le dernier point mérite une explication. Aujourd'hui, l'absence de pays d'un
côté vaut **malus −10** : l'absence d'information est traitée comme une
information contraire. Un référentiel client mal renseigné voit ainsi ses
scores baisser sans qu'aucune donnée ne le justifie — risque de faux négatifs.
La capacité permet de corriger ce comportement, mais parce qu'elle **élargit**
le périmètre, elle est livrée inactive : c'est la doctrine du produit.

Sur le canal **filtrage**, `adjust.dob`, `adjust.gender`, `adjust.geography`
et `names.reversed_order` sont inactifs par défaut : un message de paiement ne
porte ni date de naissance, ni genre, ni pays fiable, et l'ordre des mots d'un
champ libre n'est pas signifiant.

---

## 🔑 Rapprochement sur identifiants

Dix bascules pour treize mécanismes : `hard.lei`, `hard.bic`, `hard.tax_id`,
`hard.crypto`, `hard.passport`, `hard.national_registry`, `hard.national_id`,
`hard.vessel` (IMO + MMSI + indicatif radio), `hard.aircraft`,
`hard.other_documents`.

Un hit force **ALERT à 100/100 et contourne le seuil de coupure**. Ce sont
donc les bascules dont la désactivation est la plus lourde : couper l'une
d'elles fait **retomber au scoring flou une identité pourtant certaine**. Un
client et une fiche portant le même LEI mais des raisons sociales différentes
(« ACME HOLDING SA » vs « ACME HLDG ») repassent sous le seuil — faux négatif
réglementaire assumé, pas réglage de confort.

Pour les navires et les aéronefs, l'identifiant est le **seul discriminant
fiable** : un navire change de nom bien plus souvent que de numéro IMO.

---

## Traçabilité

Une alerte doit rester explicable des années plus tard. Le réglage des
capacités vit en base ; il n'est pas recopié dans le `config_state` figé au
criblage. Sans trace, un contrôleur relisant en 2029 une alerte de 2026 ne
pourrait pas savoir quels mécanismes tournaient ce jour-là.

Le `decision_tree` porte donc `capabilities_applied`, posé sur **les trois
issues** de `match_entities` — une alerte de hard match à 100/100 doit être
aussi explicable qu'une alerte floue :

```json
"capabilities_applied": {
  "channel": "SCREENING",
  "disabled": ["names.reversed_order", "translit.cyrillic"],
  "enabled":  ["adjust.geography.missing_is_neutral"],
  "inert":    ["translit.han"]
}
```

La clé n'apparaît **que si le moteur s'écarte des défauts du catalogue** :
une installation standard produit exactement l'arbre de décision qu'elle
produisait avant ce dispositif, et ce que la trace porte est précisément
l'information que la lecture du code ne donne pas.

Côté administration, chaque écriture produit une ligne `ENGINE_UPDATED` au
journal : **qui, quand, avant → après**.

---

## Mesurer avant de décider

`POST /api/settings/engine/simulate` crible **deux fois le même panel contre
le même univers de listes**, sous le paramétrage en vigueur puis sous le
paramétrage candidat, et restitue l'écart : volumes avant/après, taux
d'interception, ventilation par liste, et **les paires perdues une à une**.

Le sens qui compte est généralement l'inverse de celui des équivalences :
couper une capacité fait **perdre** des alertes, et ce sont les paires perdues
qu'un responsable doit regarder avant de valider.

Aucune écriture : ni alerte, ni ligne d'audit, ni modification du réglage. La
mesure tourne sous une surcharge **limitée au thread courant**, propriété
verrouillée par test : une simulation en tâche de fond ne change rien aux
criblages servis en parallèle.

**Ce que la mesure ne dit pas** : si une alerte perdue était un vrai positif.
Aucune simulation ne possède la vérité terrain. Sur un panel de pseudo-clients
dont les correspondances attendues sont connues, le taux d'interception donne
un repérage plus solide qu'un simple volume. Et la mesure reflète l'état des
listes **au moment où elle tourne** (cf. la conséquence signalée plus haut).

---

## API

| Route | Rôle | Effet |
|---|---|---|
| `GET /api/settings/engine` | `blocking` ou `admin` | Catalogue complet + réglage effectif par canal + capacités inertes |
| `PUT /api/settings/engine` | `blocking` ou `admin` | Réglage partiel accepté ; réponse porte `losses`, le rappel de ce qui est perdu |
| `POST /api/settings/engine/simulate` | `blocking` ou `admin` | 202 + `job_token` ; le rapport est récupéré via `/api/progress?id=` |

**Double invalidation** à l'écriture du canal criblage : le contexte des
capacités **et** le cache de l'index. L'index fige ses clés de blocking au
chargement — sans rechargement, seule la sonde du client changerait, les deux
côtés ne se rencontreraient jamais et le réglage serait sans effet visible.

Le réglage est dans `_PORTABLE_SETTINGS` : il franchit recette → production
avec l'export de configuration.

---

## Écran

Sous-onglet **Alertes → 🧠 Algorithmes du Moteur**, même rôle que Blocking
Keys. Entièrement **généré depuis le catalogue** : ajouter une capacité suffit
à la faire apparaître, avec son avertissement de perte et sa dépendance.

Trois garde-fous y sont visibles :

1. chaque bascule affiche **ce qu'on perd** ;
2. couper déclenche une **confirmation explicite** qui énumère les pertes ;
3. une capacité cochée dont le prérequis est coupé est marquée **sans effet**
   — plutôt que de laisser croire qu'elle agit.

---

## Le risque, dit franchement

Ce dispositif permet de **rendre le moteur aveugle**. Couper la
translittération, c'est perdre tous les alias non latins de l'OFAC et de
l'ONU. Le pouvoir est légitime — un établissement sans exposition non latine
paie aujourd'hui ce coût sans bénéfice — mais il est dangereux.

Les trois contreparties sont automatiques, parce qu'elles découlent du
catalogue : le champ `loss` est **obligatoire** (on ne peut pas ajouter une
bascule en oubliant d'expliquer son risque), chaque changement est
**journalisé**, et l'écart est **chiffrable avant décision**.

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

### Les écritures sans espace : frontières rétablies

Le **han** et le **hangul** ne franchissaient pas le blocking. La cause n'était
pas la translittération elle-même, mais ce qu'elle laissait de côté : anyascii
rend un fragment **capitalisé par signe** — « 习近平 » → `XiJinPing`, « 김정은 » →
`GimJeongEun`. Les frontières de mots, absentes de la source, étaient donc bien
présentes dans le résultat, mais **sous forme de majuscules et non d'espaces**.
La clé phonétique étant bâtie sur le premier mot, `XIJINPING` ne pouvait pas
rencontrer `XI`.

Ce que cela coûtait, mesuré avant correction : « 习近平 » face au client
« Xi Jinping » obtenait **89,5** de score, largement au-dessus du seuil de 75 —
mais la paire n'était **jamais rapprochée**, donc jamais scorée. Un listé
déclaré non listé sur un nom que le moteur aurait reconnu s'il avait pu le
regarder.

Les espaces sont désormais rétablis **à la translittération, signe par signe**,
et seulement pour les écritures qui n'en écrivent pas (`han`, `hangul`). La
décision se prend sur la **source** : un « McDonald » latin ou un « Vladimir »
cyrillique, qui sortent aussi capitalisés, ne passent jamais par cette branche.
Après correction, la même paire obtient **94,3** et se rencontre.

La règle est partagée par la voie inconditionnelle (`strip_accents`, qui bat
l'index des équivalences) et par la voie réglable
(`strip_accents_for_matching`, qui compare) : les deux rendent la même chose
toutes capacités actives — sans quoi une équivalence déclarée en han cesserait
d'être trouvée. Coût : néant sur la voie rapide ASCII (0,08 µs/appel, 98,3 %
du réel), 0,74 µs cache froid sur un nom han.

### Une fiche listée se laisse rejoindre par son nom de famille

Le blocking décide qui sera comparé à qui. Côté client les champs sont
séparés : le criblage émet une clé phonétique pour le prénom **et** une pour le
nom de famille. Côté liste, le nom complet tient dans **une seule chaîne**
(« JOSE GARCIA LOPEZ ») et la clé n'était bâtie que sur le **premier mot** —
c'est-à-dire, dans la quasi-totalité des cas, le prénom.

Mesuré sur **393 fiches réelles** du référentiel en production, en fabriquant
pour chacune le client correspondant :

| écriture du client                  | tables inactives | tables actives |
|-------------------------------------|------------------|----------------|
| prénom + nom, identiques            | 100 %            | 100 %          |
| prénom réduit à l'initiale (« J. ») | **0,8 %**        | **12,7 %**     |
| prénom absent (nom de famille seul) | **0 %**          | **12,0 %**     |

Le dernier cas est le cas **ordinaire** d'un message de paiement, et une base
KYC en contient sa part. Le criblage rendait « aucune correspondance » sans
avoir comparé quoi que ce soit.

Le raisonnement était déjà écrit dans le même fichier, à propos des clés
d'équivalence : *« en ne regardant que le premier mot, une équivalence de nom
de famille ne pouvait jamais créer de pont vers une fiche listée »*. Le
correctif y avait été appliqué, et pas à la clé phonétique voisine. Ce pont-là
existait donc, mais il ne portait que 12 % des cas : les tables ne connaissent
qu'une part des noms de famille (« LOPEZ » oui, « GARCIA » non), là où la clé
phonétique ne demande rien à personne.

**Coût, mesuré sur 2 200 fiches réelles** : les clés émises passent de 1,19 à
2,30 par fiche, mais elles se répartissent sur des seaux **plus nombreux et non
plus gros** — le plus gros seau ne bouge pas (60 fiches). Les candidats à
comparer par client passent de 2,5 à 4,3. Réglable (`blocking.phonetic_last`)
pour un référentiel aux noms de famille très concentrés.

**Conséquence sur les tables linguistiques** : elles apportaient une partie de
ce pont, la clé phonétique l'absorbe. Ce qu'elles apportent encore se voit là
où deux graphies d'un même nom **ne se ressemblent pas à l'oreille** — les
romanisations chinoises en sont le cas type : « ZHANG » rend le métaphone XNK,
« TEOH » rend TH. Mesuré sur cette paire : 60,4 sans les tables, 100,0 avec.

### Limite connue, mesurée, et consignée en test

L'**arabe** et l'**hébreu** ne franchissent toujours pas, et pour une raison
d'une autre nature : les abjades n'écrivent pas les voyelles brèves. « محمد »
rend `mhmd` là où la liste porte « Mohammed ». Il n'y a **aucune frontière à
rétablir** — il manque des lettres, et un translittérateur caractère par
caractère ne peut pas les inventer. Mesuré : 59 à 63 de score contre un client
en latin, sous le seuil de 75, et les clés de blocage ne se croisent pas non
plus. Corriger ce cas demanderait une romanisation propre à la langue, pas un
réglage.

Le **kanji japonais** échoue encore autrement : anyascii rend la lecture
**chinoise** des signes — « 安倍晋三 » donne `An Bei Jin San` et non
« Abe Shinzo ». Là encore, il faudrait un dictionnaire de lectures, pas une
règle typographique.

Le cyrillique et le grec, alphabétiques, franchissent sans difficulté. Le
scoring rattrape une partie des cas restants quand les noms arrivent en champs
séparés ; le blocking, non.

### Voie rapide ASCII

Un texte **purement ASCII** traverse la normalisation de comparaison
**inchangé**, quelles que soient les capacités : `detect_scripts` n'y trouve
aucune écriture non latine (donc aucune translittération), et la décomposition
NFKD d'ASCII est ASCII sans caractère combinant (donc aucun diacritique à
retirer). La démonstration est exhaustive sur les 128 points de code, et tenue
par un test — un raccourci sur un chemin de conformité se prouve, il ne se
constate pas sur trois exemples.

C'est le chemin le plus chaud du moteur, emprunté **deux fois par
comparaison**, sur un univers entier de candidats. Et **98,3 %** des noms
listés de la production sont ASCII purs (mesuré sur un échantillon de 12 500
fiches).

| | Sans la voie rapide | Avec |
|---|---:|---:|
| normalisation, cache chaud | 1,01 µs | 0,23 µs (×4,5) |
| normalisation, sans cache | 5,39 µs | 0,34 µs (×16) |
| rapprochement complet | 191,3 µs | 178,7 µs (×1,07) |
| clés de blocking d'un univers de 832 470 fiches | 18,1 s | 16,0 s (×1,13) |

Le gain en bout de chaîne est modeste — les métriques de chaîne dominent — mais
il est gratuit, et il rend abordables les passes qui parcourent tout le corpus.

---

## 📊 Ce que le moteur mesure sans le noter : la rareté des noms

Rapprocher deux noms sur « MOHAMMED » et « ALI » n'identifie personne : des
milliers de fiches les portent. Le faire sur « TYURIN » identifie presque
sûrement. Le moteur compte donc, sur l'univers **réellement criblé**, dans
combien de fiches apparaît chaque mot de nom (fréquence *documentaire*), et
joint ce compte aux correspondances en **ALERTE**.

**Elle ne déplace aucun score.** Ajouter un terme au calcul déplacerait d'un
coup tous les seuils calibrés, toutes les règles écrites contre eux et tous les
cahiers de tests homologués. Le signal est donné à qui décide — la règle,
l'analyste — il n'est pas imposé au moteur. C'est la différence avec une
capacité : rien à activer, rien à recalibrer.

Où elle apparaît :

- **arbre de décision** des alertes (donc relisible en contrôle, des mois plus
  tard, avec le corpus qui l'a produite) ;
- `ctx["rarity"]` des règles anti-faux positifs — détail par mot, seuil de
  « répandu », drapeau `nom_repandu` (cf. `REGLES_ET_BLOCKING.md`) ;
- `GET /api/screening/name-rarity` et l'écran **Moteur**, pour calibrer une
  règle avant d'y écrire un seuil.

Coût mesuré : 13,7 µs par rapprochement en ALERTE (jamais sur les candidats
écartés — au criblage d'un univers entier, ce serait le chemin le plus chaud),
et 4,4 s pour bâtir la table sur 832 000 noms distincts, au chargement du
cache. La table ne garde que les 20 000 mots les plus fréquents : les mots qui
décident quelque chose se comptent en centaines, et tout mot absent est **plus
rare** que le dernier conservé — une borne *supérieure*, jamais zéro, pour
qu'un mot inconnu ne fasse jamais clôturer.

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

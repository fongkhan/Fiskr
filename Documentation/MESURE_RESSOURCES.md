# Mesure d'impact de l'activation des équivalences

Ce document consigne la mesure qui justifie que **`given_name` et `surname`
soient actifs à la livraison** (`fiskr/settings.py`, `DEFAULT_RESOURCE_FIELDS`),
et que `city`, `country` et `state` ne le soient pas.

Une table d'équivalences augmente le rappel au prix de la précision. Ce n'est
pas discutable dans l'abstrait : cela se chiffre. Voici les chiffres, la
méthode qui les produit, et ce qu'ils ne disent pas.

## Protocole

Deux passes du **même criblage**, sur le **même panel**, contre le **même
univers de listes**. Seul le paramétrage des équivalences change. C'est
exactement ce que fait `POST /api/resources/simulate`
(`fiskr/resource_impact.py`) ; la mesure ci-dessous a été conduite en appelant
directement `backtest._dry_run_screen` sous `resources.use_context`, pour
pouvoir ventiler les résultats par segment de panel.

**Univers** — 124 fiches désignées : personnes et entités réellement visées par
des programmes de sanctions publics (Russie/Belarus, Syrie, Iran, Liban, Irak,
Yémen, Libye, Sahel, Afrique subsaharienne, RPDC, Birmanie, Chine, Afghanistan,
Pakistan, Venezuela, Nicaragua, Cuba, Mexique, Balkans), en graphie latine
anglophone — le cas normal d'une liste internationale.

> Cette mesure a été conduite dans un environnement **sans accès réseau aux
> sources officielles** (le proxy refuse `scsanctions.un.org` et
> `eur-lex.europa.eu`). L'univers a donc été écrit à la main plutôt que
> téléchargé. Les noms sont réels, leur nombre ne l'est pas : une liste
> consolidée réelle compte plusieurs dizaines de milliers de fiches. Les
> **taux** ci-dessous sont robustes, les **volumes** ne se transposent pas.

**Panel** — 716 clients en quatre segments étiquetés :

| Segment | Effectif | Contenu | Ce qu'il mesure |
|---|---|---|---|
| `variantes` | 78 | graphies **françaises** de personnes désignées (Poutine, Loukachenko, Kadhafi, El Assad, Prigojine…) | rappel : ce sont des vrais positifs connus |
| `non latins` | 13 | clients saisis dans leur **écriture d'origine** (arabe, cyrillique, hangul, han) | rappel sur les bases non latines |
| `exacts` | 25 | graphie identique à la liste | témoin : doivent alerter dans les deux passes |
| `neutres` | 600 | clients ordinaires (prénoms et noms courants FR/BE/ES/IT/PT/DE), aucun rapport avec les listes | **bruit ajouté** — le coût de l'activation |

Les graphies françaises ont été écrites d'après l'usage courant de la presse
francophone, **pas d'après les tables de ressources** : sans cette précaution,
la mesure ne mesurerait qu'elle-même.

## Résultats

Base de référence = aucun type actif. Candidat = `given_name` + `surname`.

| Segment | Avant | Après | Écart | Taux avant | Taux après |
|---|---:|---:|---:|---:|---:|
| variantes | 76 | 77 | **+1** | 97,4 % | 98,7 % |
| non latins | 0 | 5 | **+5** | 0,0 % | 38,5 % |
| exacts | 25 | 25 | +0 | 100 % | 100 % |
| **neutres** | **0** | **0** | **+0** | **0,0 %** | **0,0 %** |
| TOTAL | 101 | 107 | +6 | | |

- **0 faux positif ajouté** sur les 600 clients ordinaires.
- **0 alerte perdue** — conséquence attendue des clés de blocking additives et
  de la règle du croisement, mais mesurée plutôt que postulée.
- **Paires candidates produites par le blocking : 122 → 149 (+22,1 %)**. C'est
  le coût de calcul de l'activation, à surveiller sur une base réelle.

### Ce que la mesure a appris de non évident

**1. Le moteur rattrapait déjà 97,4 % des graphies françaises.** Poutine /
Putin, Loukachenko / Lukashenko, Timtchenko / Timchenko : ces variantes sont
proches caractère par caractère, et Jaro-Winkler + Damerau-Levenshtein +
`anyascii` les traitent seuls. **L'apport des tables sur une base latine est
marginal en volume.**

**2. Il est décisif sur le score.** 18 des 101 paires déjà détectées voient
leur score monter, **aucune ne baisse** :

| Client | Fiche listée | Avant | Après |
|---|---|---:|---:|
| Evgueni Prigojine | Yevgeny Prigozhin | 78,03 | **100,00** |
| Iouri Kovaltchouk | Yuri Kovalchuk | 86,11 | **100,00** |
| Sergueï Choïgou | Sergei Shoigu | 92,10 | **100,00** |
| Seif Al Islam Kadhafi | Saif Al-Islam Gaddafi | 91,07 | 97,77 |
| Bachar El Assad | Bashar Al-Assad | 95,30 → *(nouveau)* | **100,00** |

Une alerte à 78 se traite comme un doute ; à 100 elle se traite comme une
certitude. Le gain porte sur la **qualité de la décision**, pas seulement sur
le nombre d'alertes.

**3. C'est sur les écritures non latines que l'apport est massif** : 0 → 5
sur 13, alors qu'aucune de ces fiches n'était même **candidate** avant. La
raison est structurelle : le double métaphone appliqué à « Владимир » ou
« قاسم » ne produit rien d'exploitable, donc le blocking ne les rapprochait
d'aucune fiche. La clé d'équivalence, elle, est calculée **après**
normalisation (`anyascii` puis suppression des diacritiques) et crée le pont.

Corollaire vérifié : **il est inutile de déclarer les termes cyrilliques**.
« Владимир » se normalise en `VLADIMIR`, terme déjà déclaré en caractères
latins — la table le trouve sans qu'on ait rien ajouté. Les termes qu'il faut
déclarer sont ceux dont la translittération **ne ressemble à rien** :
`سليماني` → `SLYMNY`, qu'aucune métrique de chaîne ne rapprocherait de
`SOLEIMANI`.

## Ce que la mesure ne dit pas

- Elle chiffre le **volume** d'alertes gagnées, pas leur qualité : aucune
  simulation ne détient la vérité terrain. Les 6 gains ci-dessus ont été
  vérifiés un par un ; sur une base réelle, ce contrôle incombe à l'analyste.
- Le segment `variantes` mesure la **sensibilité** (« ces graphies-là
  sont-elles rattrapées ? »), pas la **prévalence** (« quelle proportion de ma
  base est concernée ? »). Cette seconde question n'a de réponse que sur la
  base réelle de l'établissement, via `POST /api/resources/simulate` sur un
  panel constitué à partir d'elle.
- Un panel de 600 clients ordinaires établit qu'aucun bruit n'apparaît **à
  cette échelle**. Sur plusieurs millions de clients, un taux de faux positifs
  nul en absolu n'est pas garanti : la mesure est à refaire sur la base réelle
  avant mise en production.

## Asie de l'Est : trois défauts de moteur, pas des lacunes de données

La première rédaction de ce document classait les échecs asiatiques
(« 정은 김 », « 全国 陈 ») en *lacune de données*. C'était faux. Le diagnostic a
mis au jour **trois défauts du moteur**, corrigés depuis :

1. **Le hangul et les kana n'étaient jamais translittérés.** La détection
   d'écriture non latine se faisait par liste blanche, en cherchant
   `CYRILLIC` / `ARABIC` / `CJK` / `HEBREW` / `THAI` / `GREEK` dans le **nom
   Unicode** du caractère. « 김 » se nomme `HANGUL SYLLABLE GIM` — aucun de ces
   mots. Idem pour `HIRAGANA LETTER`, `KATAKANA LETTER`, le devanagari,
   l'arménien, le géorgien. Le test porte désormais sur le **point de code**
   (au-delà de Latin Extended-B), c'est-à-dire sur le critère réel.
2. **La clé de blocking était calculée sur la chaîne brute.** Le double
   métaphone ne connaît que l'alphabet latin : sur « 陈 » ou « Владимир » il
   renvoie une clé **vide**. Une fiche écrite dans son écriture d'origine ne
   produisait donc *aucune* clé phonétique et n'était candidate de rien — quel
   que soit le contenu des tables. Le scoring, lui, translittérait déjà des
   deux côtés : les deux étages du criblage se contredisaient.
3. **La mise en majuscules précédait la translittération.** `upper()` est sans
   effet sur une écriture non latine : « 习 近平 ».upper() reste « 习 近平 », et
   la translittération rendait ensuite « Xi JinPing » en casse mixte face à
   « XI JINPING ». Deux graphies pourtant identiques après translittération ne
   marquaient que **64,40**.

S'y ajoutait un point de conception, corrigé lui aussi : les listes écrivent
les noms d'Asie de l'Est **nom de famille en tête** (« Kim Jong Un »,
« Chen Quanguo ») quand une base clients concatène « prénom nom ». Les deux
chaînes comparées étaient systématiquement inversées, et Jaro-Winkler +
Damerau-Levenshtein — 80 % du poids — s'y effondrent. L'**ordre inverse** est
désormais comparé comme une variante de nom supplémentaire. Le cas dépasse
l'Asie (saisie inversée au guichet, formats d'échange « NOM Prénom »).

Restait alors la vraie part de données : la Corée. La translittération produit
la **romanisation révisée** officielle (박 → Bag, 이 → I, 최 → Choe) là où les
listes emploient la graphie consacrée (Park, Lee, Choi) — c'est exactement le
cas Henri/Harry, il se déclare. Le japonais aussi : les kanji sont lus **en
chinois** par la translittération (田中 → « Tianzhong », pas « Tanaka »). La
Chine, elle, n'a rien demandé : le pinyin tombe exactement sur le terme
romanisé déjà présent (陈 → `CHEN`), aucun idéogramme n'a été ajouté.

### Effet mesuré, même panel et même univers

| Segment | Avant le lot | Après le lot |
|---|---:|---:|
| asiatiques (19) | 2 — **10,5 %** | 19 — **100 %** |
| écritures non latines (13) | 5 — 38,5 % | 10 — 76,9 % |
| **clients ordinaires (600)** | **0** | **0** |
| TOTAL | 109 | **131** |

Toujours **aucun bruit ajouté** sur les 600 clients ordinaires, malgré la
comparaison de l'ordre inverse, et **aucune alerte perdue**.

## Angles morts restants

| Client | Fiche visée | Cause |
|---|---|---|
| محمد سعيد | Hafiz Muhammad Saeed | prénom seul commun, nom non déclaré |
| خالد مشعل, علي مملوك | — | aucun terme du couple prénom/nom déclaré en arabe |
| Viatcheslav Volodine | Vyacheslav Volodin | classe `VYACHESLAV` absente |

Ceux-là sont bien des **lacunes de données** : ils se comblent par
enrichissement des fichiers `resources/` ou par la fouille quotidienne
d'homonymes (`fiskr/resource_mining.py`).

## Reproduire la mesure

Sur une installation réelle, la voie normale est l'écran
**Ressources Linguistiques → Mesurer l'impact**, ou :

```bash
curl -X POST http://localhost:8000/api/resources/simulate \
  -H "Content-Type: application/json" \
  -d '{"panel_snapshot_id": "<id>", "candidate_fields": ["given_name", "surname"]}'
```

Le rapport revient sur le jeton du job (`GET /api/progress?id=<token>`) et
porte les paires gagnées avec l'équivalence qui les a produites, les paires
perdues, et la ventilation par liste.

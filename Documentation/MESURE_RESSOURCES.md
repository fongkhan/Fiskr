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

## Angles morts identifiés, non corrigés

Ces cas restaient manqués après activation. Ils sont consignés parce qu'ils
délimitent la portée réelle du dispositif.

| Client | Fiche visée | Cause |
|---|---|---|
| معمر القذافي | Muammar Gaddafi | candidat (74,13), sous le seuil : `معمر` n'est pas déclaré |
| محمد سعيد | Hafiz Muhammad Saeed | prénom seul commun, nom non déclaré |
| خالد مشعل, علي مملوك, رامي مخلوف | — | aucun terme du couple prénom/nom déclaré en arabe |
| Рамзан Кадыров | Ramzan Kadyrov | `KADYROV` absent des tables |
| 정은 김 / 全国 陈 | Kim Jong Un / Chen Quanguo | ordre nom-prénom coréen/chinois non géré |
| Viatcheslav Volodine | Vyacheslav Volodin | classe `VYACHESLAV` absente |

Aucun de ces cas n'est un défaut du moteur : ce sont des **lacunes de données**
(ou, pour l'ordre nom-prénom asiatique, une fonctionnalité absente). Ils se
comblent par enrichissement des fichiers `resources/`, ou par la fouille
quotidienne d'homonymes (`fiskr/resource_mining.py`).

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

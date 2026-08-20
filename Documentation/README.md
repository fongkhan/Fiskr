# Documentation Fiskr

Le **guide intégré à l'application** (onglet *Guide*, 7 chapitres) est le point
d'entrée pour se servir de Fiskr. Les documents ci-dessous vont plus loin :
ils expliquent **pourquoi** le produit décide comme il décide, et fournissent la
matière opposable à un contrôleur.

Chaque document porte une nature. Elle compte autant que son titre :

| Nature | Ce que ça veut dire pour le lecteur |
|---|---|
| **Référence** | Décrit l'état courant. Mis à jour avec le code. |
| **Parcours** | Se lit dans l'ordre, du début à la fin d'une opération. |
| **Relevé** | Photographie datée d'une mesure. Vrai à sa date, pas après. |
| **Étude** | Analyse menée pour trancher une question. Conclusion figée. |

---

## Je veux comprendre comment Fiskr décide

| Document | Nature | Contenu |
|---|---|---|
| [ALGORITHMES_DU_MOTEUR](ALGORITHMES_DU_MOTEUR.md) | Référence | Inventaire opposable de chaque algorithme du moteur : métriques de nom, phonétique, translittération, ajustements contextuels, correspondances fortes. Ce qu'un contrôleur demande quand il veut savoir comment un score est né. |
| [REGLES_ET_BLOCKING](REGLES_ET_BLOCKING.md) | Référence | Criblage et filtrage, clés de blocage par canal, règles anti-faux positifs : leur cycle de vie, leur portée, leur traçabilité. |
| [RESSOURCES_LINGUISTIQUES](RESSOURCES_LINGUISTIQUES.md) | Référence | Tables d'équivalences (prénoms, translittérations, homonymies apprises) : format des fichiers, chargement, effet sur le rapprochement. |
| [MESURE_RESSOURCES](MESURE_RESSOURCES.md) | Relevé | Ce que l'activation des équivalences change, mesuré sur un panel. |

## Je veux exploiter Fiskr au quotidien

| Document | Nature | Contenu |
|---|---|---|
| [PRODUCTION_DES_LISTES](PRODUCTION_DES_LISTES.md) | Parcours | De la synchronisation d'une source à la mise en production : delta, exclusions, cahier de tests, homologation à quatre yeux. |
| [ALERTES_ET_SURVEILLANCE_CONTINUE](ALERTES_ET_SURVEILLANCE_CONTINUE.md) | Parcours | Cycle de vie d'une alerte, quatre yeux, re-criblage post-delta, preuve à trois niveaux. |
| [INJECTION_CLIENTS](INJECTION_CLIENTS.md) | Référence | Alimenter le référentiel clients par l'API : formats, champs reconnus, webhook, criblage à la volée. |
| [GENERER_DES_CLIENTS_DE_TEST](GENERER_DES_CLIENTS_DE_TEST.md) | Parcours | Fabriquer un jeu de clients réaliste pour éprouver un criblage sans données réelles. |

## Je veux exploiter la plateforme

| Document | Nature | Contenu |
|---|---|---|
| [PERFORMANCE_BASE](PERFORMANCE_BASE.md) | Relevé | Diagnostic mené sur la production (9 Go) et correctifs : index manquants, coût des `COUNT`, charges utiles, cache navigateur. Tout y est mesuré, jamais supposé. |
| [REVUE_SECURITE](REVUE_SECURITE.md) | Relevé | Revue de sécurité : surface exposée, authentification, secrets, exécution de code des règles. |

## Je veux connaître les sources de données

| Document | Nature | Contenu |
|---|---|---|
| [VERIFICATION_DES_SOURCES](VERIFICATION_DES_SOURCES.md) | Relevé | Vérification de chaque source officielle branchée : accessibilité, format, fraîcheur. Daté — à refaire, pas à croire sur parole. |
| [SOURCES_PREMIUM](SOURCES_PREMIUM.md) | Étude | Ce qu'apportent les sources payantes, ce qu'elles coûtent, et comment les brancher le jour où c'est décidé. |

## Je veux comprendre l'architecture

| Document | Nature | Contenu |
|---|---|---|
| [ARCHITECTURE_TECHNIQUE](ARCHITECTURE_TECHNIQUE.md) | Référence | Document d'architecture technique : composants, schéma de données, flux, déploiement. |
| [ARCHITECTURE_ANNEXE_MAPPING_OFAC](ARCHITECTURE_ANNEXE_MAPPING_OFAC.md) | Référence | Annexe : cartographie du format OFAC Advanced XML vers le schéma pivot. |

## Décisions déjà tranchées

| Document | Nature | Contenu |
|---|---|---|
| [BENCHMARK_CONCURRENTS](BENCHMARK_CONCURRENTS.md) | Étude | Comparaison aux solutions du marché et feuille de route qui en découle. |
| [REFLEXION_LANGAGES](REFLEXION_LANGAGES.md) | Étude | « Un autre langage que Python aiderait-il Fiskr ? » — la question posée, mesurée, tranchée. |

---

## Ailleurs dans le dépôt

| Où | Quoi |
|---|---|
| [README](../README.md) | Installation, configuration, exploitation, référence de l'API |
| [CHANGELOG](../CHANGELOG.md) | Historique daté de tous les changements, avec les mesures qui les ont motivés |
| [`exemples/`](exemples/) | Fichiers CSV d'exemple : import client minimal et complet |
| [`archives/`](archives/) | États passés du produit, conservés pour la traçabilité — **pas une référence** |
| [`tools/`](../tools/) | Outils d'exploitation : index de performance, rafraîchissement de production, génération de données de test |

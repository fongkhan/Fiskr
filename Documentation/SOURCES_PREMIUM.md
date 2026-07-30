# Sources de données payantes — ce qu'elles apportent et comment les débloquer

Fiskr couvre aujourd'hui **26 listes publiques officielles** (sanctions,
alertes de régulateurs, exclusions de bailleurs, PEP). Les fournisseurs
commerciaux n'apportent pas « plus de sanctions » — les listes officielles
sont publiques par construction — mais trois choses que le public ne donne
pas :

1. **La consolidation avec identités résolues** : une seule fiche par
   personne, dédupliquée entre toutes les listes, avec identifiants croisés.
2. **Les PEP et l'adverse media de qualité éditoriale** : profils PEP tenus
   par des équipes de recherche (familles, proches, mandats), presse
   négative catégorisée.
3. **Un contrat et un SLA** : fraîcheur garantie, responsabilité du
   fournisseur — argument recevable en audit.

Chaque section ci-dessous dit : ce que le fournisseur apporte, ce qu'il faut
acheter et à qui s'adresser, ce que Fiskr devra recevoir, et l'état du
câblage côté code.

> **Le prérequis technique est déjà livré.** La couche HTTP de Fiskr accepte
> des en-têtes d'authentification par source (`sync.<source>.auth_headers`
> dans `config.yaml`), avec interpolation `${VAR}` depuis `.env` — le secret
> ne vit jamais dans un fichier versionné :
>
> ```yaml
> # config.yaml
> sync:
>   ma_source:
>     enabled: true
>     url: "https://feed.fournisseur.example/watchlist.xml"
>     auth_headers:
>       Authorization: "Bearer ${FOURNISSEUR_API_KEY}"   # valeur dans .env
> ```
>
> Le jour du contrat, il ne reste que le connecteur (parseur du format du
> fournisseur + entrée au registre) — pas de plomberie.

---

## OpenSanctions (licence commerciale) — débloquable immédiatement

**Ce que c'est.** L'agrégateur open-data utilisé par les connecteurs PEP,
SECO (voie de secours) et les 11 sources du registre (`fiskr/sources.py`).
Les données sont librement téléchargeables ; c'est **l'usage commercial**
qui exige une licence.

**Ce qu'il faut faire.** Rien de technique : le connecteur existe et tourne.
La licence s'achète en self-service sur
[opensanctions.org/licensing](https://www.opensanctions.org/licensing/) —
tarification par taille d'organisation, de l'ordre de quelques centaines
d'euros par mois. Une fois la licence souscrite, l'usage en production
commerciale est couvert ; aucun changement de configuration n'est requis.

**Ce que ça débloque en plus** (optionnel) : l'API `api.opensanctions.org`
(matching hébergé) et le dataset consolidé complet — non nécessaires à
Fiskr, qui fait son propre matching.

| | |
|---|---|
| Format | `targets.simple.csv` — **déjà parsé** (`parse_opensanctions_simple_csv`) |
| Auth | aucune pour le téléchargement ; licence = conformité juridique |
| État du câblage | ✅ opérationnel (PEP, SECO, + 11 sources du registre) |

---

## Dow Jones Risk & Compliance (Factiva)

**Ce qu'il apporte.** La référence du marché : *Watchlist* (sanctions + PEP
+ proches consolidés, identités résolues), *Adverse Media Entities*, et le
fonds documentaire Factiva pour la presse négative.

**Ce qu'il faut acheter.** Un contrat *Dow Jones Risk & Compliance* —
contact commercial via dowjones.com/professional/risk (pas de self-service).
Deux modes de livraison à demander au commercial :
- **Flux fichier** (SFTP/HTTPS) : exports XML/CSV périodiques de la
  Watchlist — le mode le plus simple à brancher sur le cycle de
  synchronisation de Fiskr ;
- **API** (REST, clé + secret) : requêtes unitaires, plutôt adapté au
  remplacement du connecteur adverse media.

**Ce que Fiskr devra recevoir.** L'URL du flux + les identifiants (déposés
en `.env`, référencés en `auth_headers`), et un échantillon du format pour
écrire le parseur. Point d'entrée adverse media : `adverse_media.provider`
(`fiskr/adverse_media.py`) est prévu pour être remplacé par configuration.

| | |
|---|---|
| Format | XML/CSV propriétaire (flux) ou JSON (API) |
| Auth | identifiants de contrat ; API : clé + secret |
| État du câblage | ⛔ connecteur à écrire au contrat (plomberie auth prête) |

---

## LSEG World-Check One (ex-Refinitiv)

**Ce qu'il apporte.** L'autre référence : base World-Check (sanctions, PEP,
« special interest »), résolution d'identités, mises à jour continues.

**Ce qu'il faut acheter.** Un abonnement *World-Check One* auprès de LSEG
(lseg.com/en/risk-intelligence) — contact commercial. L'accès machine passe
par la **World-Check One API** (REST, authentification par clé API + secret,
signature HMAC des requêtes).

**Ce que Fiskr devra recevoir.** `api-key` + `api-secret` (en `.env`), et le
choix du mode : synchronisation de groupes de fiches (à brancher sur le
cycle snapshot) ou criblage unitaire délégué (hors philosophie de Fiskr, qui
garde son moteur).

| | |
|---|---|
| Format | JSON (API REST) |
| Auth | clé + secret, signature HMAC par requête |
| État du câblage | ⛔ connecteur à écrire au contrat (plomberie auth prête) |

---

## LexisNexis WorldCompliance / Bridger Insight

**Ce qu'il apporte.** Base WorldCompliance (sanctions, PEP, enforcement),
souvent retenue pour la couverture Amériques ; Bridger comme outil de
criblage hébergé.

**Ce qu'il faut acheter.** Contrat *LexisNexis Risk Solutions*
(risk.lexisnexis.com) — demander la **livraison de données**
(WorldCompliance Data Feed) plutôt que l'outil Bridger si l'on veut garder
le moteur Fiskr.

| | |
|---|---|
| Format | XML/CSV (flux) |
| Auth | identifiants de contrat (SFTP ou HTTPS) |
| État du câblage | ⛔ connecteur à écrire au contrat (plomberie auth prête) |

---

## Moody's GRID (ex-RDC)

**Ce qu'il apporte.** Base GRID : risque réputationnel structuré par
catégories d'événements (fraude, corruption, crime organisé…), forte
couverture adverse media catégorisée — complémentaire d'une base sanctions.

**Ce qu'il faut acheter.** Contrat Moody's Analytics (KYC solutions).
Livraison par API ou flux — à préciser au contrat.

| | |
|---|---|
| Format | JSON/XML selon le canal |
| Auth | clé d'API |
| État du câblage | ⛔ connecteur à écrire au contrat (plomberie auth prête) |

---

## ComplyAdvantage

**Ce qu'il apporte.** Base sanctions/PEP/adverse media construite par
agrégation automatisée, API moderne, tarifs d'entrée plus accessibles que
les deux leaders — souvent le premier pas « payant » des équipes qui
partent d'open data.

**Ce qu'il faut acheter.** Abonnement API sur complyadvantage.com (démo puis
contrat ; self-service partiel). Clé d'API simple.

| | |
|---|---|
| Format | JSON (API REST) |
| Auth | `Authorization: Token <clé>` |
| État du câblage | ⛔ connecteur à écrire au contrat (plomberie auth prête) |

---

## Ordre de priorité suggéré

1. **Licence OpenSanctions** — seul déblocage sans développement : il
   régularise l'usage commercial de ce qui tourne déjà (PEP + 11 sources).
2. **Dow Jones ou World-Check** — si l'exigence d'un fonds PEP/adverse media
   éditorial se présente (attente fréquente des auditeurs de grands
   établissements). Compter le délai commercial + le connecteur.
3. **GRID / ComplyAdvantage** — selon le besoin : adverse media catégorisé
   (GRID) ou montée en gamme progressive (ComplyAdvantage).

Dans tous les cas : ne PAS remplacer les connecteurs officiels publics par
le fournisseur — les listes officielles restent la référence opposable ; le
payant s'ajoute (consolidation, PEP, presse), il ne se substitue pas.

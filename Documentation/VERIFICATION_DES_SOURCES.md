# Vérification générale des sources — 5 août 2026

Chaque source branchée a été interrogée **depuis Internet, une par une**, et
non déduite de la documentation. Ce document restitue le verdict, les
correctifs appliqués et la façon de refaire le contrôle.

## Comment refaire ce contrôle

```bash
python tools/diagnostic_sources.py
```

La sonde dérive désormais sa liste de **la configuration réelle**
(`get_sync_config()`) et du registre OpenSanctions : elle interroge donc
exactement ce que le produit télécharge. Elle en couvre **73** — auparavant
six sources n'y figuraient pas et trois URL y avaient vieilli, si bien
qu'elle pouvait annoncer « tout va bien » sur des adresses inutilisées.

> Le verdict qui compte est celui obtenu **depuis le serveur qui synchronise**
> : l'IP sortante et le filtrage de l'hébergeur changent le résultat.

## Verdict

**27 sources OpenSanctions : toutes répondent** (slugs validés contre le
catalogue en ligne, 462 jeux). **13 sources natives** : toutes répondent, à
l'exception de celles traitées ci-dessous.

### Corrigé

| Source | Constat | Correctif |
|---|---|---|
| **Israël NBCTF** | slug `il_nbctf_sanctions` — **404 `NoSuchKey` : il n'a jamais existé** au catalogue. La source ne rapportait donc rien depuis son branchement. | slug `il_mod_terrorists` (2 056 fiches) |
| **Australie DFAT** | CSV en erreur de flux HTTP/2, XLSX en 404 — la voie officielle ne répond plus | voie OpenSanctions `au_dfat_sanctions` (9 020 fiches). Le connecteur natif reste en place pour l'import manuel du fichier. |
| **Banque mondiale** | 401 « missing subscription key » — l'API exige désormais une clé | voie OpenSanctions `worldbank_debarred` (4 295 fiches), sans clé. La voie native reste documentée (`auth_headers`) pour qui obtient une clé. |
| **Canada, US CSL, AMF** | voir le correctif du 5 août (CSV retiré, certificat expiré, page refondue) | XML SEMA, `data.trade.gov`, export ouvert data.gouv.fr |

### Réponses normales à connaître

- **OFAC SDN / Non-SDN, ONU, AMF** répondent en **302** puis 200 : une
  redirection, suivie par le client HTTP. Ce n'est pas une panne.
- **EU FSF** exige un token (`sync.eu_fsf.token`) : non sondable sans lui.
- **EUR-Lex** a ses sondes dédiées (c'est un signal d'alerte, pas une liste).

## Sources ajoutées

Seize sources publiques, choisies pour ce qu'elles apportent **qu'aucune
liste déjà branchée ne porte** — pas pour faire du volume.

### Listes nationales de terrorisme (RCSNU 1373)

Le cœur du CFT : chaque État désigne sur son sol et **aucune** de ces
désignations ne remonte dans les listes onusiennes ou européennes.
Juridictions retenues pour l'exposition d'un établissement français (Golfe et
Levant pour les correspondants bancaires et les transferts de fonds, Maghreb,
Asie du Sud-Est, Afrique) : **Émirats arabes unis, Arabie saoudite, Qatar,
Égypte, Türkiye (gels MASAK), Indonésie (DTTOT), Afrique du Sud (FIC),
Tunisie**.

### Voisinage européen

**Monaco** (gels de fonds — 12 929 fiches, voisinage immédiat d'un
établissement français) et **Tchéquie** (désignations antiterroristes
nationales).

### Ce que nos listes anglo-saxonnes ne portent pas

- **US FTO** : l'OFAC porte le gel, **pas** la désignation d'organisation
  terroriste étrangère du Département d'État.
- **UK FCDO** et **organisations proscrites** : l'OFSI porte le gel
  financier, **pas** la liste de sanctions du FCDO ni les organisations
  proscrites au titre du Terrorism Act.

### Crypto

**Portefeuilles crypto désignés** par le ministère de la Défense israélien
(4 284) : Fiskr sait déjà faire une correspondance exacte sur adresse crypto,
cette liste lui donne de la matière.

## Sources privées

Aucune source payante n'a été branchée : elles exigent toutes un contrat.
`Documentation/SOURCES_PREMIUM.md` décrit ce que chacune apporte et comment
la débloquer (Dow Jones, LSEG World-Check, LexisNexis, Moody's GRID,
ComplyAdvantage). **OpenSanctions sous licence commerciale** reste la seule
débloquable immédiatement : le connecteur du registre **est** le connecteur,
il n'y a rien à développer.

## Toutes les nouvelles sources sont désactivées par défaut

Elles s'activent une par une dans **Sources → Synchronisation automatique**.
Chacune a son propre type de liste, donc **son propre seuil de score**
(`scoring.cut_off_overrides`) : une liste d'alerte nationale n'a pas à être
seuillée comme une liste de gel.

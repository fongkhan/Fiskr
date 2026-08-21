"""
Plafonds de taille des entrees de Fiskr, definis en un seul endroit.

Un artefact entre dans Fiskr par DEUX portes : le televersement (un operateur
depose un fichier depuis le navigateur) et le telechargement (une source
configuree sert une URL). C'est le meme artefact — une liste officielle reste
une liste officielle, qu'elle arrive par l'ecran d'import ou par https.

Tant que les plafonds vivaient du seul cote televersement, fermer une porte
laissait l'autre grande ouverte : `download_to_file` ecrivait sur le disque
sans limite et `http_get_text` chargeait en memoire tout ce que l'hote voulait
bien servir. Un module commun evite d'avoir a se souvenir des deux.

Tailles reelles, pour situer les plafonds :
  - OFAC SDN_ADVANCED.XML : 126 Mo — la plus grosse liste officielle connue
  - flux RSS de presse negative (Google News, requete sur un nom) : 12 Ko mesures
  - page HTML d'un acte du Journal officiel de l'UE : quelques centaines de Ko
"""

# Televersements : plafonds par NATURE de depot. Une liste officielle est
# volumineuse par construction ; une piece jointe d'alerte ne l'est pas.
TAILLE_MAX_TELEVERSEMENT = {
    "liste": 512 * 1024 * 1024,      # import de liste officielle
    "clients": 64 * 1024 * 1024,     # referentiel clients / campagne batch
    "message": 8 * 1024 * 1024,      # message de paiement ISO 20022
    "piece": 32 * 1024 * 1024,       # piece jointe, justificatif
}

# Telechargement d'une source vers le disque : DERIVE du plafond de
# televersement des listes, pas recopie. Les deux portes menent au meme
# repertoire de travail et au meme analyseur ; deux nombres qui devraient etre
# egaux et qu'on maintient a la main finissent toujours par diverger.
TAILLE_MAX_TELECHARGEMENT = TAILLE_MAX_TELEVERSEMENT["liste"]

# Page de texte recuperee en memoire (scraping EUR-Lex, flux RSS de presse
# negative). Rien de ce que Fiskr lit par cette voie n'est un fichier de
# donnees : ce sont des pages et des flux, mesures a 12 Ko pour le RSS. Le
# plafond est volontairement tres au-dessus du besoin — il n'est pas la pour
# ajuster au plus juste, il est la pour qu'un hote qui deraille ou qui a ete
# detourne ne puisse pas faire tomber le processus de synchronisation.
TAILLE_MAX_PAGE = 32 * 1024 * 1024

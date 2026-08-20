"""
Perimetres de criblage : SANCTION et HORS_SANCTION.

Les deux ne portent pas le meme risque, et n'appellent donc pas le meme
traitement des correspondances.

**SANCTION** — designations avec obligation de gel des avoirs (OFAC, UE, ONU,
DGT, OFSI, listes nationales antiterroristes...). Manquer une correspondance,
c'est manquer un terroriste ou un sanctionne : le manquement est constatable a
l'audit et sanctionnable financierement. Sur ce perimetre, TOUT est genere et
rien n'est cloture automatiquement par volumetrie.

**HORS_SANCTION** — PEP, listes d'alerte de regulateurs, exclusions de
bailleurs multilateraux. Ce sont des signaux de vigilance, pas des obligations
de gel : ne pas remonter un PEP n'a pas la meme portee. Ce perimetre supporte
une cloture plus agressive — seuil de coupure plus haut (la correspondance
n'est alors jamais creee) et regles anti-faux positifs volumetriques.

La classification est un ARBITRAGE DE CONFORMITE : elle est donc surchargeable
par reglage (`screening.perimeters`), et la valeur par defaut est derivee de la
famille declaree au registre des sources — une source ajoutee la-bas est
classee sans rien toucher ici.
"""
from typing import Dict, Optional

from fiskr.sources import (OPENSANCTIONS_SOURCES, FAMILY_SANCTIONS,
                           FAMILY_REGULATORY, FAMILY_DEBARMENT, FAMILY_AGGREGATED)

PERIMETRE_SANCTION = "SANCTION"
PERIMETRE_HORS_SANCTION = "HORS_SANCTION"
PERIMETRES = (PERIMETRE_SANCTION, PERIMETRE_HORS_SANCTION)

PERIMETRE_LABELS = {
    PERIMETRE_SANCTION: "Sanctions (gel des avoirs)",
    PERIMETRE_HORS_SANCTION: "Hors sanctions (PEP, régulateurs, exclusions)",
}

# Famille du registre des sources -> perimetre. Seule la famille « sanctions »
# porte une obligation de gel ; les trois autres sont des signaux de vigilance.
_PAR_FAMILLE = {
    FAMILY_SANCTIONS: PERIMETRE_SANCTION,
    FAMILY_REGULATORY: PERIMETRE_HORS_SANCTION,
    FAMILY_DEBARMENT: PERIMETRE_HORS_SANCTION,
    FAMILY_AGGREGATED: PERIMETRE_HORS_SANCTION,
}

# Types anterieurs au registre des sources : classes ici, un par un, avec la
# raison. Toute liste dont le non-respect expose a une sanction financiere est
# du cote SANCTION — en cas de doute, c'est ce cote qui gagne.
_HISTORIQUES = {
    # --- Gels d'avoirs : obligation reglementaire ---
    "WATCHLIST_OFAC": PERIMETRE_SANCTION,          # SDN, Tresor americain
    "WATCHLIST_OFAC_NONSDN": PERIMETRE_SANCTION,   # sanctions sectorielles
    "WATCHLIST_EU": PERIMETRE_SANCTION,            # liste consolidee UE (FSF)
    "WATCHLIST_DGT": PERIMETRE_SANCTION,           # registre national des gels
    "WATCHLIST_UN": PERIMETRE_SANCTION,            # Conseil de securite
    "WATCHLIST_OFSI": PERIMETRE_SANCTION,          # Royaume-Uni
    "WATCHLIST_SECO": PERIMETRE_SANCTION,          # Suisse
    "WATCHLIST_SSIE": PERIMETRE_SANCTION,          # Suisse (export SESAM)
    "WATCHLIST_CSL": PERIMETRE_SANCTION,           # Consolidated Screening List
    "WATCHLIST_CANADA": PERIMETRE_SANCTION,        # LMES/SEMA
    "WATCHLIST_DFAT": PERIMETRE_SANCTION,          # Australie
    # --- Signaux de vigilance : pas d'obligation de gel ---
    "WATCHLIST_PEP": PERIMETRE_HORS_SANCTION,      # personnes politiquement exposees
    "WATCHLIST_AMF": PERIMETRE_HORS_SANCTION,      # liste noire AMF (sites non autorises)
    "WATCHLIST_HK_SFC": PERIMETRE_HORS_SANCTION,   # alertes du regulateur hongkongais
    "WATCHLIST_WORLDBANK": PERIMETRE_HORS_SANCTION,  # exclusions Banque mondiale
}


def perimetres_par_defaut() -> Dict[str, str]:
    """Classification par defaut : registre des sources + types historiques."""
    table = {s.file_type: _PAR_FAMILLE.get(s.family, PERIMETRE_HORS_SANCTION)
             for s in OPENSANCTIONS_SOURCES}
    table.update(_HISTORIQUES)
    return table


def perimetre_de(list_type: Optional[str],
                 surcharges: Optional[Dict[str, str]] = None) -> str:
    """
    Perimetre d'un type de liste.

    Un type INCONNU est classe SANCTION : c'est le cote qui ne cloture rien
    automatiquement. Une liste mal classee du cote hors-sanction se ferait
    clore ses correspondances en volume — le defaut penche donc vers ce qui
    ne fait rien perdre.
    """
    if not list_type:
        return PERIMETRE_SANCTION
    cle = str(list_type).strip().upper()
    if surcharges:
        valeur = str(surcharges.get(cle, "") or "").strip().upper()
        if valeur in PERIMETRES:
            return valeur
    return perimetres_par_defaut().get(cle, PERIMETRE_SANCTION)


def est_sanction(list_type: Optional[str],
                 surcharges: Optional[Dict[str, str]] = None) -> bool:
    return perimetre_de(list_type, surcharges) == PERIMETRE_SANCTION

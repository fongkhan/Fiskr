"""
L'inventaire des pieces probantes : ce que la base AFFIRME detenir, confronte
a ce qui se trouve reellement sur le disque.

POURQUOI CE MODULE EXISTE
-------------------------
Trois familles de pieces justificatives vivent en deux endroits a la fois : une
ligne en base qui porte le nom du fichier, et le fichier lui-meme dans un
dossier. Les ecrans lisent la ligne. Le telechargement, lui, lit le fichier —
et decouvre son absence au moment ou quelqu'un clique.

Ce moment-la, dans un produit de conformite, porte un nom : le controle. Le
jour ou un auditeur demande la piece qui justifie une mise en liste blanche,
il est trop tard pour apprendre qu'elle a disparu — une preuve ne se
reconstitue pas. Un fichier disparait pourtant sans bruit : une purge, une
migration d'hebergement qui oublie les dossiers de pieces, une restauration
partielle. La base, elle, continue d'afficher le nom.

Ce module ne repare rien et n'efface rien : il CONSTATE. La ligne en base est
conservee telle quelle — effacer la reference d'une piece disparue reviendrait
a effacer la trace qu'elle a existe, ce qui est exactement le contraire du
service rendu.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class FamilleDePieces:
    cle: str                      # identifiant stable (API, tests)
    libelle: str                  # libelle humain
    colonne_chemin: str           # colonne portant le chemin sur disque
    colonne_nom: str              # colonne portant le nom affiche
    designe: Callable[[Any], str]  # comment nommer la ligne a un humain
    # Critere pose AVANT le filtre sur le chemin, quand il permet d'attraper
    # un index. Il ne doit jamais RETRANCHER de lignes : c'est une precision
    # sur le meme ensemble, pas un second filtre.
    prefiltre: Optional[Callable[[Any], Any]] = None


def _modeles():
    """Import differe : `database` importe ce module a son tour dans les tests."""
    from fiskr.database import AlertAttachment, WatchlistEntity, WhitelistPair
    return AlertAttachment, WatchlistEntity, WhitelistPair


def familles() -> Tuple[Tuple[Any, FamilleDePieces], ...]:
    AlertAttachment, WatchlistEntity, WhitelistPair = _modeles()
    return (
        (AlertAttachment, FamilleDePieces(
            "alertes", "Pièces jointes d'alertes", "file_path", "file_name",
            lambda r: f"alerte #{r.alert_id}")),
        (WhitelistPair, FamilleDePieces(
            "liste_blanche", "Justificatifs de liste blanche",
            "evidence_file_path", "evidence_file_name",
            lambda r: f"paire #{r.id} ({r.client_name or r.client_id or '—'})")),
        # `excluded IS TRUE` d'abord, et ce n'est pas une coquetterie : la
        # table des fiches listees compte des millions de lignes, aucune
        # colonne de piece n'y est indexee, et l'index PARTIEL
        # `ix_wl_entities_excluded` n'existe que sur ce critere-la. La lecon
        # est deja payee ici : la meme requete sans cet index mettait 21 a 35 s
        # en production pour rendre zero ligne. Le critere ne retranche rien —
        # une fiche ne porte de justificatif d'exclusion que si elle est
        # exclue, les deux sont ecrits ensemble.
        (WatchlistEntity, FamilleDePieces(
            "exclusions", "Justificatifs d'exclusion",
            "exclusion_file_path", "exclusion_file_name",
            lambda r: f"instantané {r.snapshot_id} — {r.primary_name}",
            prefiltre=lambda modele: modele.excluded.is_(True))),
    )


# Plafond de lignes examinees par famille. Un inventaire partiel le DIT
# (`tronque`) : annoncer « tout est la » apres n'avoir regarde qu'un tiers
# serait la faute meme que ce module traque.
PLAFOND_PAR_FAMILLE = 5000


def piece_presente(chemin: Optional[str]) -> bool:
    """
    Vrai si la piece annoncee est bien un fichier lisible.

    Un chemin vide n'est pas une piece manquante : c'est une ligne SANS piece,
    et les deux ne se confondent pas — l'une est une absence declaree, l'autre
    une promesse rompue.
    """
    if not chemin:
        return False
    try:
        return Path(chemin).is_file()
    except (OSError, ValueError):
        # Chemin illisible pour le systeme de fichiers : la piece n'est pas
        # servable, donc pas presente. Jamais une exception vers l'ecran.
        return False


def inventaire(db, plafond: int = PLAFOND_PAR_FAMILLE) -> Dict[str, Any]:
    """
    Ce que la base annonce, ce qui est reellement la, et ce qui manque.

    Retourne {"familles": [...], "annoncees": n, "manquantes": m,
    "tronque": bool} — les totaux servent au controle de mise en service, le
    detail sert a l'exploitant qui doit aller chercher les fichiers.
    """
    detail: List[Dict[str, Any]] = []
    total_annoncees = total_manquantes = 0
    tronque_global = False

    for modele, famille in familles():
        colonne = getattr(modele, famille.colonne_chemin)
        requete = db.query(modele)
        if famille.prefiltre is not None:
            requete = requete.filter(famille.prefiltre(modele))
        requete = requete.filter(colonne.isnot(None), colonne != "")
        annoncees = requete.count()
        lignes = requete.order_by(modele.id.desc()).limit(plafond).all()
        tronque = annoncees > len(lignes)
        tronque_global = tronque_global or tronque

        absentes = []
        for ligne in lignes:
            if piece_presente(getattr(ligne, famille.colonne_chemin)):
                continue
            absentes.append({
                "ou": famille.designe(ligne),
                "nom": getattr(ligne, famille.colonne_nom) or "—",
            })

        total_annoncees += annoncees
        total_manquantes += len(absentes)
        detail.append({
            "cle": famille.cle,
            "libelle": famille.libelle,
            "annoncees": annoncees,
            "verifiees": len(lignes),
            "manquantes": len(absentes),
            "tronque": tronque,
            "absentes": absentes[:20],  # de quoi agir, pas de quoi noyer
        })

    return {
        "familles": detail,
        "annoncees": total_annoncees,
        "manquantes": total_manquantes,
        "tronque": tronque_global,
    }

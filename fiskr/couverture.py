"""
Couverture du criblage : combien de clients n'ont JAMAIS été criblés.

Pourquoi cette question n'avait pas de réponse
----------------------------------------------
Importer un référentiel clients déclenche un contrôle de complétude — pas un
criblage. Le re-criblage automatique, lui, se déclenche quand une LISTE change
(`rescreen_after_snapshot_change`), jamais quand des CLIENTS arrivent. Un
référentiel fraîchement importé restait donc entier hors du criblage jusqu'à
ce que quelqu'un lance un lookback, ou qu'une liste bouge — et rien, nulle
part, ne le disait. Ni l'écran, ni le message d'import, ni un compteur.

C'est la première question d'un contrôle : « tous vos clients ont-ils été
criblés ? ». Le produit ne savait pas y répondre.

Ce que cette mesure dit, et ce qu'elle ne dit pas
------------------------------------------------
Elle répond à « ce client a-t-il été criblé au moins une fois ? », en cherchant
une trace au journal de criblage immuable. C'est une question franche, à
laquelle la base sait répondre.

Elle ne prétend PAS répondre à « ce client a-t-il été criblé contre la liste
exactement en production aujourd'hui ». Après une mise en production, le
re-criblage ne compare le référentiel qu'aux fiches NOUVELLES OU MODIFIÉES —
c'est ce qui le rend tenable — et n'écrit une ligne d'audit que pour les
correspondances. Un client propre ne laisse donc pas de trace datée de ce
passage. Compter les traces portant le hash du jour dirait « personne n'est
couvert » le lendemain de chaque mise en production : ce serait faux, et un
indicateur faux est pire que pas d'indicateur.

Coût
----
Mesuré : 20 000 clients dont 10 000 sans trace, **0,01 s**. Le planificateur
ne sonde pas l'index client par client, il résout la sous-requête corrélée en
anti-jointure — d'où ce coût, très en dessous de ce qu'on pouvait craindre.

Le plafond reste, en filet et non en nécessité : sur un référentiel de
plusieurs ordres de grandeur au-dessus, mieux vaut « plus de 10 000 » rendu
tout de suite qu'un chiffre exact qui immobilise un écran. Quand il mord, la
mesure le DIT (`plafonne`), au lieu de rendre un nombre qui aurait l'air exact.
"""
import logging
from typing import Any, Dict, Optional

from fiskr.database import AuditTrail, ClientEntity, Snapshot

logger = logging.getLogger("fiskr.couverture")

# Au-delà, on répond « plus de N » : l'ordre de grandeur suffit à décider, et
# aucun écran ne mérite d'attendre le compte exact.
PLAFOND_DE_COMPTAGE = 10_000


def _snapshots_clients(db):
    return [s.snapshot_id for s in db.query(Snapshot.snapshot_id).filter(
        Snapshot.file_type == "CLIENT_BASE", Snapshot.status == "READY").all()]


def couverture_du_criblage(db, plafond: int = PLAFOND_DE_COMPTAGE) -> Dict[str, Any]:
    """
    Combien de clients en production n'ont aucune décision de criblage.

    Retourne toujours la même forme, y compris quand il n'y a pas de
    référentiel : un écran ne doit pas avoir à distinguer « zéro » de
    « impossible à dire ».
    """
    vide = {"clients": 0, "jamais_cribles": 0, "plafonne": False,
            "sans_referentiel": True}
    try:
        snap_ids = _snapshots_clients(db)
        if not snap_ids:
            return vide

        clients = db.query(ClientEntity.id).filter(
            ClientEntity.snapshot_id.in_(snap_ids)).count()

        # Sous-requête corrélée : une sonde d'index par client, arrêtée net au
        # plafond. `LIMIT` sur la sélection, `COUNT` par-dessus — pas l'inverse.
        manquants = db.query(ClientEntity.id).filter(
            ClientEntity.snapshot_id.in_(snap_ids),
            ~db.query(AuditTrail.id).filter(
                AuditTrail.client_id == ClientEntity.client_id).exists(),
        ).limit(plafond + 1).count()

        return {
            "clients": int(clients),
            "jamais_cribles": min(int(manquants), plafond),
            "plafonne": manquants > plafond,
            "sans_referentiel": False,
        }
    except Exception as e:
        # Une mesure illisible ne doit jamais faire tomber l'écran qui
        # l'affiche : elle se tait, et le contrôle le dit à sa façon.
        logger.warning(f"Couverture du criblage illisible : {e}")
        return vide


def phrase_de_couverture(mesure: Dict[str, Any]) -> Optional[str]:
    """
    Une phrase, ou rien si tout est criblé. Utilisée par le message d'import
    et par l'écran de mise en service : une seule formulation, pas deux qui
    dériveraient.
    """
    if mesure.get("sans_referentiel") or not mesure.get("jamais_cribles"):
        return None
    combien = mesure["jamais_cribles"]
    quantite = f"Plus de {combien}" if mesure.get("plafonne") else str(combien)
    pluriel = "s" if combien > 1 else ""
    return (f"{quantite} client{pluriel} du référentiel n'{'ont' if combien > 1 else 'a'} "
            f"jamais été criblé{pluriel} : aucune décision à leur nom au journal de "
            f"criblage. Importer des clients ne les crible pas — c'est une mise à jour "
            f"de liste, ou un lookback, qui le fait.")

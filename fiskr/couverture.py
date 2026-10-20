"""
Couverture du criblage : combien de clients n'ont JAMAIS été criblés, et
combien ne le sont pas contre la version des listes en production.

Pourquoi cette question n'avait pas de réponse
----------------------------------------------
Importer un référentiel clients déclenchait un contrôle de complétude — pas un
criblage. Le re-criblage automatique, lui, se déclenche quand une LISTE change
(`rescreen_after_snapshot_change`), jamais quand des CLIENTS arrivent. Un
référentiel fraîchement importé restait donc entier hors du criblage jusqu'à
ce que quelqu'un lance un lookback, ou qu'une liste bouge — et rien, nulle
part, ne le disait. Ni l'écran, ni le message d'import, ni un compteur.

C'est la première question d'un contrôle : « tous vos clients ont-ils été
criblés ? ». Le produit ne savait pas y répondre.

Ce que cette mesure lit
-----------------------
La trace que chaque passage de criblage laisse sur chaque fiche
(`ClientEntity.screened_at`, `covered_universe_hash`, cf. fiskr/rescreen.py).
Elle se mesurait sur le journal d'audit, qui n'écrit une ligne que pour une
correspondance : un lookback sur des clients propres ne laissait rien, et
« jamais criblé » ne s'éteignait jamais — relevé en production, 382 clients
sur 406 après un lookback complet, et le même chiffre après chaque relance.

« Jamais criblé » : ni passage sur la fiche, ni décision au journal (les
criblages antérieurs aux passages n'ont laissé que la seconde).

« Non criblé contre la version en production » : la fiche n'est pas couverte
contre l'univers que `rescreen.reference_de_l_univers` décrit aujourd'hui. Un
passage complet (lookback, criblage des clients nouveaux ou modifiés) couvre
ses fiches ; un re-criblage après mise à jour de liste prolonge la couverture
des fiches couvertes contre la version d'avant — il les a confrontées à tout
ce qui a changé. C'est ce qui permet de répondre sans compter des lignes au
hash du jour, qui diraient « personne n'est couvert » le lendemain de chaque
mise en production.

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


def clients_en_production(db) -> int:
    """
    Le nombre de clients du referentiel EN PRODUCTION — la seule definition.

    La mise en service comptait toutes les lignes de la table des clients,
    panels de cahier de tests compris. Releve en production : « Referentiel
    clients : 2 500 fiches en base — OK », juste au-dessus de « Couverture du
    criblage : aucun referentiel clients en production ». Les 2 500 etaient
    les trois panels de test (1 000 + 1 000 + 500) ; il n'y avait aucun client.
    Deux controles du meme ecran se contredisaient, et le vert etait faux.
    """
    snap_ids = _snapshots_clients(db)
    if not snap_ids:
        return 0
    return int(db.query(ClientEntity.id).filter(
        ClientEntity.snapshot_id.in_(snap_ids)).count())


# Les travaux qui, en cours, vont changer la couverture d'eux-mêmes : tant que
# l'un d'eux tourne ou attend, un écart n'est pas encore un manquement.
TRAVAUX_DE_CRIBLAGE = ("client_screening", "lookback", "approve", "sync", "ingest")


def _contre_la_version_en_production(db, snap_ids, plafond: int) -> Dict[str, Any]:
    """
    Les clients qui ne sont pas couverts contre l'univers en production, et
    depuis quand ils attendent.

    La référence de l'univers est lue par `rescreen.reference_de_l_univers`,
    le point unique qu'empruntent aussi les passages et leurs lignes d'audit : comparée
    à la couverture portée par chaque fiche, elle dit qui a été confronté à
    TOUT ce qui est en production aujourd'hui.

    Depuis quand : un client qu'aucun passage complet n'a jamais couvert
    attend depuis son arrivée — qu'il ait été daté par des re-criblages delta
    ou non, il n'a jamais été confronté à tout l'univers ; un client couvert
    contre une version antérieure, depuis le premier passage qui a vu la
    version en production. Si aucun passage ne l'a vue — une fiche ajoutée ou
    corrigée à la main, un re-criblage coupé — la durée est INCONNUE, et la
    mesure le dit au lieu d'annoncer zéro.
    """
    from sqlalchemy import func, or_
    from fiskr.database import Job, ScreeningPass
    from fiskr.rescreen import lots_en_production, reference_de_l_univers

    version, empreinte, _composition = reference_de_l_univers(db)
    sortie = {"non_couverts": 0, "non_couverts_plafonne": False,
              "version_en_production": version, "en_attente_depuis": None,
              "attente_inconnue": False,
              "travaux_en_cours": db.query(Job.id).filter(
                  Job.kind.in_(TRAVAUX_DE_CRIBLAGE),
                  Job.status.in_(("QUEUED", "RUNNING"))).count(),
              "dernier_passage": _dernier_passage(db, empreinte)}
    if not lots_en_production(db):
        # Aucune liste : être « couvert contre » un univers vide ne veut rien
        # dire. Le contrôle des listes en production le dit, et bloque.
        return sortie

    non_couverts = db.query(ClientEntity.id).filter(
        ClientEntity.snapshot_id.in_(snap_ids),
        or_(ClientEntity.covered_universe_hash.is_(None),
            ClientEntity.covered_universe_hash != empreinte),
    ).limit(plafond + 1).count()
    sortie["non_couverts"] = min(int(non_couverts), plafond)
    sortie["non_couverts_plafonne"] = non_couverts > plafond
    if not non_couverts:
        return sortie

    depuis = []
    arrivee = db.query(func.min(Snapshot.uploaded_at)).join(
        ClientEntity, ClientEntity.snapshot_id == Snapshot.snapshot_id).filter(
        ClientEntity.snapshot_id.in_(snap_ids),
        ClientEntity.covered_universe_hash.is_(None)).scalar()
    if arrivee:
        depuis.append(arrivee)
    anterieurs = db.query(ClientEntity.id).filter(
        ClientEntity.snapshot_id.in_(snap_ids),
        ClientEntity.covered_universe_hash.isnot(None),
        ClientEntity.covered_universe_hash != empreinte).first()
    if anterieurs:
        vue = db.query(func.min(ScreeningPass.started_at)).filter(
            ScreeningPass.watchlist_hash == empreinte).scalar()
        if vue:
            depuis.append(vue)
        else:
            sortie["attente_inconnue"] = True
    if depuis:
        sortie["en_attente_depuis"] = min(depuis).isoformat()
    return sortie


def _dernier_passage(db, empreinte: str) -> Optional[Dict[str, Any]]:
    """Le passage de criblage le plus récent, tel qu'un écran le cite."""
    from fiskr.database import ScreeningPass
    passage = db.query(ScreeningPass).order_by(ScreeningPass.id.desc()).first()
    if passage is None:
        return None
    return {
        "id": passage.id, "nature": passage.nature, "statut": passage.status,
        "version": passage.watchlist_version,
        "version_en_production": passage.watchlist_hash == empreinte,
        "debut": passage.started_at.isoformat() if passage.started_at else None,
        "fin": passage.finished_at.isoformat() if passage.finished_at else None,
        "clients_cribles": passage.clients_screened or 0,
        "clients_couverts": passage.clients_covered or 0,
        "fiches_comparees": passage.entities_compared or 0,
        "erreur": passage.error,
    }


def couverture_du_criblage(db, plafond: int = PLAFOND_DE_COMPTAGE) -> Dict[str, Any]:
    """
    Combien de clients en production n'ont jamais été criblés, et combien ne
    le sont pas contre la version des listes en production.

    Retourne toujours la même forme, y compris quand il n'y a pas de
    référentiel ou que la mesure échoue — et ces deux cas ne se confondent
    pas : `illisible` dit « on ne sait pas », `sans_referentiel` dit « il n'y
    a rien ». Un écran ne doit pas avoir à les distinguer lui-même.
    """
    vide = {"clients": 0, "jamais_cribles": 0, "plafonne": False,
            "sans_referentiel": True, "illisible": False, "non_couverts": 0, "non_couverts_plafonne": False,
            "version_en_production": None, "en_attente_depuis": None,
            "attente_inconnue": False, "travaux_en_cours": 0, "dernier_passage": None}
    try:
        snap_ids = _snapshots_clients(db)
        if not snap_ids:
            return vide

        clients = db.query(ClientEntity.id).filter(
            ClientEntity.snapshot_id.in_(snap_ids)).count()

        # Jamais criblé : ni passage sur la fiche, ni décision au journal (les
        # criblages antérieurs aux passages n'ont laissé que la seconde).
        # Sous-requête corrélée : une sonde d'index par client, arrêtée net au
        # plafond. `LIMIT` sur la sélection, `COUNT` par-dessus — pas l'inverse.
        jamais = (ClientEntity.snapshot_id.in_(snap_ids),
                  ClientEntity.screened_at.is_(None),
                  ~db.query(AuditTrail.id).filter(
                      AuditTrail.client_id == ClientEntity.client_id).exists())
        manquants = db.query(ClientEntity.id).filter(*jamais).limit(plafond + 1).count()

        return {
            "clients": int(clients),
            "jamais_cribles": min(int(manquants), plafond),
            "plafonne": manquants > plafond,
            "sans_referentiel": False,
            "illisible": False,
            **_contre_la_version_en_production(db, snap_ids, plafond),
        }
    except Exception as e:
        # Une mesure illisible ne doit jamais faire tomber l'écran qui
        # l'affiche — mais elle ne doit pas non plus se faire passer pour une
        # réponse. Elle rendait « aucun référentiel » : sur une base qui en
        # porte un, c'était un constat faux, que la mise en service présentait
        # comme « rien à cribler pour l'instant ». Les chiffres sont None, pas
        # zéro : zéro serait encore une réponse.
        logger.warning(f"Couverture du criblage illisible : {e}")
        return {**vide, "clients": None, "jamais_cribles": None,
                "sans_referentiel": None, "illisible": True, "non_couverts": None,
                "erreur": f"{type(e).__name__} : {e}"}


def phrase_de_couverture(mesure: Dict[str, Any]) -> Optional[str]:
    """
    Une phrase, ou rien si tout est couvert. Utilisée par la carte de
    couverture et par l'écran de mise en service : une seule formulation, pas
    deux qui dériveraient. Elle nomme ce qui ne va pas, jamais ce qui va.
    """
    if mesure.get("illisible"):
        return (f"Couverture du criblage illisible : la mesure a échoué "
                f"({mesure.get('erreur') or 'sans détail'}). On ne sait donc pas si "
                f"le référentiel a été criblé — ce n'est pas l'absence de référentiel.")
    if mesure.get("sans_referentiel"):
        return None
    phrases = []
    combien = mesure.get("jamais_cribles") or 0
    if combien:
        quantite = f"Plus de {combien}" if mesure.get("plafonne") else str(combien)
        pluriel = "s" if combien > 1 else ""
        phrases.append(
            f"{quantite} client{pluriel} du référentiel n'{'ont' if combien > 1 else 'a'} "
            f"jamais été criblé{pluriel} : aucune trace de criblage à leur nom, ni "
            f"passage ni décision au journal.")
    # Les jamais criblés sont aussi non couverts : on ne nomme que les autres.
    autres = (mesure.get("non_couverts") or 0) - combien
    if autres > 0:
        quantite = f"Plus de {autres}" if mesure.get("non_couverts_plafonne") else str(autres)
        pluriel = "s" if autres > 1 else ""
        phrases.append(
            f"{quantite} client{pluriel} {'ont' if autres > 1 else 'a'} été criblé{pluriel}, "
            f"mais pas contre la version des listes en production "
            f"({mesure.get('version_en_production')}).")
    return " ".join(phrases) or None

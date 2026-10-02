"""
La fraicheur des listes en production : depuis quand ce contre quoi on crible
a cesse d'etre la derniere version connue.

POURQUOI CE MODULE EXISTE
-------------------------
Releve sur l'installation de production le 1er octobre 2026 : les 39 listes en
production avaient entre 29 et 69 jours. OFAC 33 jours, DGT — le registre
national des gels, obligation francaise autonome — 33 jours, UE 51, OFSI 69.
Pendant ce temps, chaque nuit, les synchronisations reussissaient : elles
deposaient une version plus recente en attente d'homologation, que personne
n'approuvait. 639 lots s'etaient ainsi empiles, jusqu'a 21 pour une meme
source.

Et l'ecran de mise en service etait entierement vert. « Listes en
production : 39 », « Homologation des lots : un lot ingere passe par une revue
avant production », « Recuperation automatique active ». Chaque controle
disait vrai sur ce qu'il regardait — la configuration — et aucun ne regardait
le resultat. Un gel des avoirs s'applique des sa publication : un referentiel
en retard d'un mois est un defaut de conformite, et il ne se signalait pas.

Ce module mesure le resultat. Il est la SEULE definition de la fraicheur :
le controle de mise en service, les indicateurs de pilotage et le badge de la
barre laterale la lisent ici, et aucun ne la recalcule — trois definitions
paralleles finiraient par dire trois choses differentes.

Un lot en attente se classe en deux especes, et la distinction porte tout :
- PLUS RECENT que la production : il porte une realite que le criblage
  ignore encore. C'est un retard, et il se mesure depuis le PLUS ANCIEN de ces
  lots — c'est depuis lui que la production n'est plus a jour.
- PLUS ANCIEN que la production : il est perime. L'approuver ferait REGRESSER
  la liste (cf. `est_une_regression`). Il encombre la file sans rien porter.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Les snapshots d'ajout manuel a la volee coexistent avec la version
# officielle en production : ils ne sont pas « la version » d'une liste.
_MANUEL = "manual-watchlist"


def _types_de_listes():
    from fiskr.database import WATCHLIST_FILE_TYPES
    return WATCHLIST_FILE_TYPES


def mesurer(db, maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Etat de fraicheur de chaque liste, et la synthese qui en decoule.

    Une seule requete sur la table des snapshots (quelques milliers de lignes
    au plus, colonnes legeres) : le tri se fait ici, pas en base, pour que la
    regle de classement soit ecrite une fois et lisible.
    """
    from fiskr.database import Snapshot

    maintenant = maintenant or datetime.utcnow()
    lignes = db.query(
        Snapshot.snapshot_id, Snapshot.file_type, Snapshot.status,
        Snapshot.uploaded_at, Snapshot.record_count,
    ).filter(
        Snapshot.file_type.in_(_types_de_listes()),
        Snapshot.status.in_(("READY", "PENDING_REVIEW")),
    ).all()

    par_type: Dict[str, Dict[str, list]] = {}
    for sid, ftype, statut, depose, fiches in lignes:
        if sid.startswith(_MANUEL):
            continue
        groupe = par_type.setdefault(ftype, {"READY": [], "PENDING_REVIEW": []})
        groupe[statut].append((depose or datetime.min, sid, fiches))

    listes: List[Dict[str, Any]] = []
    for ftype in sorted(par_type):
        groupe = par_type[ftype]
        production = max(groupe["READY"]) if groupe["READY"] else None
        prod_depose = production[0] if production else None
        recents = sorted(p for p in groupe["PENDING_REVIEW"]
                         if prod_depose is None or p[0] > prod_depose)
        perimes = [p for p in groupe["PENDING_REVIEW"]
                   if prod_depose is not None and p[0] <= prod_depose]
        en_retard_depuis = recents[0][0] if recents else None
        listes.append({
            "type": ftype,
            "production_id": production[1] if production else None,
            "production_du": prod_depose.isoformat() if prod_depose else None,
            "production_age_jours": (maintenant - prod_depose).days if prod_depose else None,
            "production_fiches": int(production[2] or 0) if production else None,
            "versions_en_attente": len(recents),
            "derniere_version_id": recents[-1][1] if recents else None,
            "en_retard_depuis": en_retard_depuis.isoformat() if en_retard_depuis else None,
            "retard_heures": (int((maintenant - en_retard_depuis).total_seconds() // 3600)
                              if en_retard_depuis else 0),
            "lots_perimes": len(perimes),
        })

    en_retard = [l for l in listes if l["versions_en_attente"]]
    return {
        "listes": listes,
        "listes_en_production": sum(1 for l in listes if l["production_id"]),
        "listes_en_retard": len(en_retard),
        "retard_max_heures": max((l["retard_heures"] for l in en_retard), default=0),
        "lots_en_attente": sum(l["versions_en_attente"] + l["lots_perimes"] for l in listes),
        "lots_perimes": sum(l["lots_perimes"] for l in listes),
        # Une liste en production sans aucune fiche ne crible rien : elle se
        # compte dans « 39 listes en production » et ne protege de personne.
        "listes_vides": [l["type"] for l in listes
                         if l["production_id"] and not l["production_fiches"]],
    }


def est_une_regression(db, snapshot) -> bool:
    """
    Vrai si promouvoir ce snapshot remplacerait une production PLUS RECENTE.

    Approuver un lot ancien apres un lot recent ferait sortir de production,
    sans un mot, toutes les designations parues entre les deux. Avec 21 lots
    empiles pour une meme source et une homologation groupee qui en accepte
    cinquante, « tout selectionner, approuver » suffisait a le faire.
    """
    from fiskr.database import Snapshot
    production = db.query(Snapshot.uploaded_at).filter(
        Snapshot.file_type == snapshot.file_type,
        Snapshot.status == "READY",
        ~Snapshot.snapshot_id.like(_MANUEL + "%"),
    ).order_by(Snapshot.uploaded_at.desc()).first()
    if production is None or production[0] is None or snapshot.uploaded_at is None:
        return False
    return snapshot.uploaded_at < production[0]


def phrase_de_retard(heures: int) -> str:
    """« 20 jours », « 36 h » : l'unite que lirait un humain."""
    if heures >= 48:
        return f"{heures // 24} jours"
    return f"{heures} h"


def a_homologuer(db) -> int:
    """
    Ce qu'un relecteur a reellement a homologuer : une unite par LISTE qui
    attend une version plus recente que sa production, plus chaque lot hors
    listes (referentiel clients) en attente.

    Le badge comptait les LOTS : 639 en production, pour 29 listes. Or il suffit
    d'approuver la derniere version d'une liste pour que ses versions
    anterieures quittent la file (cf. _retirer_les_versions_depassees) — le
    travail etait donc de 29 gestes, et le chiffre en annoncait vingt fois
    plus. Un compteur qui surestime le travail decourage de le commencer.

    Deux agregats sur la table des snapshots, servis par l'index
    statut/type : cet appel est interroge en boucle par la barre laterale.
    """
    from sqlalchemy import and_, exists, func
    from sqlalchemy.orm import aliased
    from fiskr.database import Snapshot

    types = _types_de_listes()
    production = aliased(Snapshot)
    plus_recente_que_la_prod = ~exists().where(and_(
        production.file_type == Snapshot.file_type,
        production.status == "READY",
        ~production.snapshot_id.like(_MANUEL + "%"),
        production.uploaded_at >= Snapshot.uploaded_at,
    ))
    listes = db.query(func.count(func.distinct(Snapshot.file_type))).filter(
        Snapshot.status == "PENDING_REVIEW",
        Snapshot.file_type.in_(types),
        plus_recente_que_la_prod,
    ).scalar() or 0
    autres = db.query(func.count(Snapshot.snapshot_id)).filter(
        Snapshot.status == "PENDING_REVIEW",
        ~Snapshot.file_type.in_(types),
    ).scalar() or 0
    return int(listes) + int(autres)

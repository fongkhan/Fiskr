"""
Indicateurs de pilotage du dispositif (GET /api/kpi).

POURQUOI CE MODULE EXISTE
-------------------------
Le calcul vivait dans le corps de l'endpoint, au milieu des 13 900 lignes
d'api.py, et le meme indicateur y etait defini TROIS fois. Le delai moyen de
decision se calculait :
  - sur le tableau de bord, sur les 500 dernieres alertes CLOSES ;
  - par analyste, sur ses 200 dernieres decisions QUEL QUE SOIT LEUR STATUT,
    a cote d'un compte de decisions qui, lui, filtrait les statuts clos — deux
    chiffres de la meme ligne portant sur deux populations differentes ;
  - dans le rapport d'activite, sur les decisions de la periode, en ecartant
    les paires incoherentes (decision anterieure a la creation) que les deux
    autres comptaient.
Trois definitions finissent par donner trois chiffres. Celle-ci est la seule,
et les trois lecteurs la partagent.

Le module ne depend que de la base : il se teste sans l'application, et
l'endpoint n'est plus qu'un appel.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from fiskr.database import (
    ALERT_CLOSED_STATUSES, ALERT_OPEN_STATUSES, WATCHLIST_FILE_TYPES,
    Alert, AuditTrail, FpRule, Snapshot, SyncReport, WhitelistPair,
)

# Une DECISION, au sens des indicateurs : une alerte instruite et close par un
# humain. CLOSED_BY_RULE en est exclu — personne ne l'a instruite, elle ne dit
# rien du travail de l'analyste.
STATUTS_DECIDES = ("CLOSED_CONFIRMED", "CLOSED_FALSE_POSITIVE")


def filtre_des_decisions() -> Tuple[Any, ...]:
    """
    Les criteres SQL d'une decision : a appliquer partout, a l'identique.

    La date de decision n'en fait PAS partie. Une decision ancienne sans date
    reste une decision : elle compte, et son delai est simplement inconnu. Le
    delai se calcule donc sur la partie DATABLE de la meme population — un
    sous-ensemble, pas un autre ensemble.
    """
    return (Alert.status.in_(STATUTS_DECIDES),)


def delai_moyen_secondes(paires: Iterable[Tuple[Optional[datetime], Optional[datetime]]]
                         ) -> Optional[int]:
    """
    Moyenne (creation -> decision), en secondes entieres, ou None sans donnee.

    Une paire incoherente — date manquante, ou decision ANTERIEURE a la
    creation (horloges desalignees, reprise de donnees) — est ecartee : elle
    ne mesure pas un delai, et un seul ecart negatif de plusieurs jours
    suffirait a faire mentir la moyenne.
    """
    delais = [(decide - cree).total_seconds() for cree, decide in paires
              if cree is not None and decide is not None and decide >= cree]
    if not delais:
        return None
    return int(round(sum(delais) / len(delais)))


def en_heures(secondes: Optional[int]) -> Optional[float]:
    """Arrondi historique a 0,1 h, conserve pour les lecteurs existants."""
    return None if secondes is None else round(secondes / 3600.0, 1)


# ------------------ GEL DES AVOIRS : DELAI CONFIRMATION -> DECLARATION ------------------

def delai_de_declaration_secondes(ouverte_le: Optional[datetime],
                                  declaree_le: Optional[datetime],
                                  maintenant: Optional[datetime] = None) -> Optional[int]:
    """
    Delai entre la confirmation d'une alerte du perimetre SANCTION (ouverture
    de la mesure de gel) et sa declaration a la DG Tresor — la SEULE
    definition, lue par le registre, l'indicateur, la notification de retard
    et le controle de mise en service.

    Les lignes directrices DGT/ACPR (§99) font des « delais de declaration »
    un point de controle interne : il faut donc un chiffre, et un seul.

    - Mesure declaree : declaree_le - ouverte_le.
    - Mesure NON declaree : le delai court encore (maintenant - ouverte_le) —
      c'est ce chiffre-la que surveille le seuil de retard.
    - Declaration ANTERIEURE a la confirmation dans Fiskr (la DG Tresor a ete
      prevenue pendant la validation 4-yeux, ce que le §182 attend
      justement) : 0. Elle n'est pas ecartee comme une horloge incoherente —
      l'ecarter retirerait de la moyenne les declarations les plus rapides.
    """
    if ouverte_le is None:
        return None
    fin = declaree_le if declaree_le is not None else (maintenant or datetime.utcnow())
    return max(0, int(round((fin - ouverte_le).total_seconds())))


def declaration_en_retard(statut: Optional[str], ouverte_le: Optional[datetime],
                          declaree_le: Optional[datetime], seuil_heures: float,
                          maintenant: Optional[datetime] = None) -> bool:
    """Une mesure ENCORE a declarer dont le delai depasse le seuil. Une mesure
    declaree tard n'est plus « en retard » : elle est « declaree hors delai »,
    un fait passe que l'indicateur compte, pas une action a mener."""
    from fiskr.gels import STATUT_A_DECLARER
    if statut != STATUT_A_DECLARER or declaree_le is not None:
        return False
    delai = delai_de_declaration_secondes(ouverte_le, None, maintenant)
    return delai is not None and delai > seuil_heures * 3600


def synthese_des_gels(db, maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """Le registre des mesures de gel, tel que le pilotage le lit."""
    from fiskr.database import MesureDeGel
    from fiskr.gels import STATUT_A_DECLARER, STATUT_DECLAREE, STATUT_LEVEE
    from fiskr.settings import gel_declaration_delai_heures

    now = maintenant or datetime.utcnow()
    seuil = gel_declaration_delai_heures(db)
    lignes = db.query(MesureDeGel.statut, MesureDeGel.ouverte_le,
                      MesureDeGel.declaree_le).all()
    delais = [delai_de_declaration_secondes(o, d) for _s, o, d in lignes if d is not None]
    delais = [d for d in delais if d is not None]
    return {
        "to_declare": sum(1 for s, _o, _d in lignes if s == STATUT_A_DECLARER),
        "overdue": sum(1 for s, o, d in lignes
                       if declaration_en_retard(s, o, d, seuil, now)),
        "declared": sum(1 for s, _o, _d in lignes if s == STATUT_DECLAREE),
        "lifted": sum(1 for s, _o, _d in lignes if s == STATUT_LEVEE),
        "declared_late": sum(1 for d in delais if d > seuil * 3600),
        "avg_declaration_delay_seconds": (int(round(sum(delais) / len(delais)))
                                          if delais else None),
        "max_declaration_delay_seconds": max(delais) if delais else None,
        "threshold_hours": seuil,
    }


# ------------------ PPE : DELAI CONFIRMATION -> DECISION DE LA DIRECTION ------------------

def delai_de_decision_ppe_secondes(ouvert_le: Optional[datetime],
                                   decidee_le: Optional[datetime],
                                   maintenant: Optional[datetime] = None) -> Optional[int]:
    """
    Delai entre la confirmation d'une correspondance sur une liste PEP
    (ouverture du dossier PPE) et la decision d'entrer ou de rester en
    relation, prise par l'organe executif ou une personne qu'il habilite
    (R.561-20-2 1°) — la SEULE definition, lue par le dossier, l'indicateur,
    la notification de retard et le controle de mise en service.

    - Dossier decide : decidee_le - ouvert_le.
    - Dossier SANS decision : le delai court encore (maintenant - ouvert_le).
    - Decision ANTERIEURE a la confirmation dans Fiskr (client deja connu
      PPE, decision prise a l'entree en relation et reportee au dossier) : 0.
      Elle n'est pas ecartee comme une horloge incoherente — l'ecarter
      retirerait de la moyenne les dossiers les mieux tenus.
    """
    if ouvert_le is None:
        return None
    fin = decidee_le if decidee_le is not None else (maintenant or datetime.utcnow())
    return max(0, int(round((fin - ouvert_le).total_seconds())))


def decision_ppe_en_retard(statut: Optional[str], ouvert_le: Optional[datetime],
                           decidee_le: Optional[datetime], seuil_jours: float,
                           maintenant: Optional[datetime] = None) -> bool:
    """Un dossier ENCORE sans decision dont le delai depasse le seuil. Une
    decision prise tard n'est plus « en retard » : c'est un fait passe que
    l'indicateur compte (`decided_late`), pas une action a mener."""
    from fiskr.ppe import STATUT_A_DECIDER
    if statut != STATUT_A_DECIDER or decidee_le is not None:
        return False
    delai = delai_de_decision_ppe_secondes(ouvert_le, None, maintenant)
    return delai is not None and delai > seuil_jours * 86400


def synthese_des_dossiers_ppe(db, maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """Les dossiers PPE, tels que le pilotage les lit."""
    from fiskr.database import DossierPPE
    from fiskr.ppe import STATUT_A_DECIDER, STATUT_DECIDE
    from fiskr.settings import ppe_decision_delai_jours

    now = maintenant or datetime.utcnow()
    seuil = ppe_decision_delai_jours(db)
    lignes = db.query(DossierPPE.statut, DossierPPE.ouvert_le, DossierPPE.decidee_le,
                      DossierPPE.ppe_non_declaree).all()
    delais = [delai_de_decision_ppe_secondes(o, d) for _s, o, d, _n in lignes if d is not None]
    delais = [d for d in delais if d is not None]
    return {
        "to_decide": sum(1 for s, _o, _d, _n in lignes if s == STATUT_A_DECIDER),
        "overdue": sum(1 for s, o, d, _n in lignes
                       if decision_ppe_en_retard(s, o, d, seuil, now)),
        "decided": sum(1 for s, _o, _d, _n in lignes if s == STATUT_DECIDE),
        "decided_late": sum(1 for d in delais if d > seuil * 86400),
        "undeclared_by_upstream": sum(1 for _s, _o, _d, n in lignes if n),
        "avg_decision_delay_seconds": (int(round(sum(delais) / len(delais)))
                                       if delais else None),
        "max_decision_delay_seconds": max(delais) if delais else None,
        "threshold_days": seuil,
    }


def fraicheur_resumee(db, maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """La fraicheur des listes, telle que le pilotage la lit."""
    from fiskr.fraicheur import mesurer
    etat = mesurer(db, maintenant)
    return {
        "lists_in_production": etat["listes_en_production"],
        "lists_behind": etat["listes_en_retard"],
        "max_delay_hours": etat["retard_max_heures"],
        "pending_snapshots": etat["lots_en_attente"],
        "obsolete_pending_snapshots": etat["lots_perimes"],
        "empty_lists": etat["listes_vides"],
        "by_list": [
            {"list_type": l["type"], "production_since": l["production_du"],
             "production_age_days": l["production_age_jours"],
             "newer_versions_waiting": l["versions_en_attente"],
             "behind_since": l["en_retard_depuis"],
             "delay_hours": l["retard_heures"]}
            for l in etat["listes"]
        ],
    }


def indicateurs(db, maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Indicateurs de pilotage du dispositif : volumes et statuts d'alertes,
    taux de faux positifs, delai moyen de decision, liste blanche, etat et
    fraicheur des listes en production, historique des synchronisations.
    """
    from sqlalchemy import func

    # Alertes par statut
    alert_counts = dict(
        db.query(Alert.status, func.count(Alert.id)).group_by(Alert.status).all()
    )
    open_alerts = sum(alert_counts.get(s, 0) for s in ALERT_OPEN_STATUSES)
    closed_fp = alert_counts.get("CLOSED_FALSE_POSITIVE", 0)
    closed_tp = alert_counts.get("CLOSED_CONFIRMED", 0)
    # CLOSED_BY_RULE est un statut CLOS, mais volontairement hors de ce taux :
    # une alerte close par regle n'a ete instruite par personne, elle ne dit
    # rien de la qualite de ce qui arrive a l'analyste. Le taux mesure donc les
    # alertes INSTRUITES — et c'est pour cela que le volume absorbe par les
    # regles doit etre publie A COTE : sans lui, plus les regles travaillent,
    # moins l'ecran montre le bruit reellement produit par le dispositif.
    closed_by_rule = alert_counts.get("CLOSED_BY_RULE", 0)
    closed_total = closed_fp + closed_tp
    fp_rate = round(closed_fp / closed_total * 100.0, 1) if closed_total else None

    # Delai moyen de decision (creation -> decision) sur les 500 dernieres
    # decisions instruites — meme population et meme calcul que la ventilation
    # par analyste et que le rapport d'activite (cf. delai_moyen_secondes).
    decisions = db.query(Alert.created_at, Alert.decided_at).filter(
        *filtre_des_decisions(), Alert.decided_at.isnot(None)
    ).order_by(Alert.decided_at.desc()).limit(500).all()
    avg_decision_seconds = delai_moyen_secondes(decisions)
    avg_decision_hours = en_heures(avg_decision_seconds)

    # Liste blanche active
    now = maintenant or datetime.utcnow()
    active_whitelist = db.query(WhitelistPair).filter(
        WhitelistPair.revoked_at.is_(None),
        (WhitelistPair.expires_at.is_(None)) | (WhitelistPair.expires_at > now)
    ).count()

    # Listes en production (entites par type) et snapshots par statut
    ready_by_type = dict(
        db.query(Snapshot.file_type, func.sum(Snapshot.record_count))
          .filter(Snapshot.status == "READY", Snapshot.file_type.in_(WATCHLIST_FILE_TYPES))
          .group_by(Snapshot.file_type).all()
    )
    snapshot_counts = dict(
        db.query(Snapshot.status, func.count(Snapshot.snapshot_id)).group_by(Snapshot.status).all()
    )

    # Decisions d'audit par statut (volumetrie de criblage)
    audit_counts = dict(
        db.query(AuditTrail.status, func.count(AuditTrail.id)).group_by(AuditTrail.status).all()
    )

    # Dernieres synchronisations
    recent_syncs = db.query(SyncReport).order_by(SyncReport.executed_at.desc()).limit(15).all()

    # ---- Series temporelles 30 jours (accueil / tendances) ----
    # func.date() est valide sur SQLite ET PostgreSQL
    since = now - timedelta(days=30)
    created_rows = (
        db.query(func.date(Alert.created_at), Alert.channel, func.count(Alert.id))
          .filter(Alert.created_at >= since)
          .group_by(func.date(Alert.created_at), Alert.channel).all()
    )
    closed_rows_series = (
        db.query(func.date(Alert.decided_at), func.count(Alert.id))
          .filter(Alert.decided_at.isnot(None), Alert.decided_at >= since)
          .group_by(func.date(Alert.decided_at)).all()
    )
    days = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(29, -1, -1)]
    created_map: Dict[str, Dict[str, int]] = {}
    for day, channel, count in created_rows:
        day_key = str(day)[:10]
        created_map.setdefault(day_key, {})[channel or "SCREENING"] = int(count)
    closed_map = {str(day)[:10]: int(count) for day, count in closed_rows_series}
    timeseries = [
        {
            "date": d,
            "created_screening": created_map.get(d, {}).get("SCREENING", 0),
            "created_filtering": created_map.get(d, {}).get("FILTERING", 0),
            "closed": closed_map.get(d, 0),
        }
        for d in days
    ]

    # ---- Ventilations : alertes ouvertes par liste, traitement par analyste ----
    open_by_list = dict(
        db.query(Alert.list_type, func.count(Alert.id))
          .filter(Alert.status.in_(ALERT_OPEN_STATUSES))
          .group_by(Alert.list_type).all()
    )
    analyst_rows = (
        db.query(Alert.decided_by, func.count(Alert.id))
          .filter(Alert.decided_by.isnot(None), *filtre_des_decisions())
          .group_by(Alert.decided_by).all()
    )
    # Delai moyen de decision, par analyste, sur ses 200 dernieres decisions.
    # C'etait UNE requete PAR analyste : le seul N+1 restant de l'application,
    # sur un tableau de bord. Une fonction de fenetrage numerote les decisions
    # de chaque analyste de la plus recente a la plus ancienne, et une seule
    # requete rend les 200 premieres de chacun. La soustraction de dates reste
    # en Python — elle n'est pas portable en SQL (`interval` PostgreSQL contre
    # dates texte SQLite) et le resultat doit rester au chiffre pres.
    from sqlalchemy import func as _f
    rang = _f.row_number().over(partition_by=Alert.decided_by,
                                order_by=Alert.decided_at.desc()).label("rang")
    numerotees = (db.query(Alert.decided_by.label("analyste"),
                           Alert.created_at.label("cree"),
                           Alert.decided_at.label("decide"), rang)
                    .filter(Alert.decided_by.isnot(None), *filtre_des_decisions(),
                            Alert.decided_at.isnot(None))
                    .subquery())
    paires: Dict[str, List[Any]] = {}
    for analyste, cree, decide, _rang in db.query(numerotees).filter(
            numerotees.c.rang <= 200).all():
        paires.setdefault(analyste, []).append((cree, decide))

    by_analyst = []
    for username, decided_count in sorted(analyst_rows, key=lambda r: -r[1]):
        secondes = delai_moyen_secondes(paires.get(username) or [])
        by_analyst.append({"analyst": username, "decided": int(decided_count),
                           "avg_decision_hours": en_heures(secondes),
                           "avg_decision_seconds": secondes})

    # ---- Efficacite des regles anti-faux positifs (hit_count en base) ----
    fp_rules_stats = [
        {
            "id": r.id, "name": r.name, "channel": r.channel, "status": r.status,
            "version": r.version, "enabled": bool(r.enabled), "hit_count": int(r.hit_count or 0),
        }
        for r in db.query(FpRule)
                   .filter(FpRule.status == "ACTIVE")
                   .order_by(FpRule.hit_count.desc()).limit(20).all()
    ]

    # Alertes ouvertes les plus anciennes (liste « à traiter » de l'accueil)
    oldest_open = (
        db.query(Alert)
          .filter(Alert.status.in_(ALERT_OPEN_STATUSES))
          .order_by(Alert.created_at.asc()).limit(5).all()
    )

    return {
        "alerts": {
            "by_status": alert_counts,
            "open": open_alerts,
            "open_by_list_type": {k or "UNKNOWN": int(v) for k, v in open_by_list.items()},
            "closed_false_positive": closed_fp,
            "closed_confirmed": closed_tp,
            "closed_by_rule": closed_by_rule,
            "false_positive_rate_pct": fp_rate,
            # Denominateur explicite : un taux sans son assiette ne se relit pas
            # en controle, des mois plus tard.
            "false_positive_rate_basis": closed_total,
            "avg_decision_hours": avg_decision_hours,
            # La mesure exacte, a cote de l'arrondi historique : 0,0 h se lit
            # « instantane » ou « pas de donnee », alors que deux decisions en
            # deux minutes valent 120 s. C'est l'ecran qui choisit l'unite.
            "avg_decision_seconds": avg_decision_seconds,
            "timeseries_30d": timeseries,
            "by_analyst": by_analyst,
            "oldest_open": [
                {
                    "id": a.id, "client_name": a.client_name, "watchlist_name": a.watchlist_name,
                    "channel": a.channel, "status": a.status, "final_score": float(a.final_score or 0.0),
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                }
                for a in oldest_open
            ],
        },
        "fp_rules": fp_rules_stats,
        "whitelist_active_pairs": active_whitelist,
        # Ce qui suit un vrai positif sur une liste de gel : mesures a
        # declarer, en retard, et le delai confirmation -> declaration
        # (LD DGT/ACPR §99 : « delais de declaration », point de controle).
        "asset_freezes": synthese_des_gels(db, now),
        # Ce qui suit une PPE confirmee : dossiers sans decision, en retard,
        # et le delai confirmation -> decision de la direction (R.561-20-2 1°)
        "pep_dossiers": synthese_des_dossiers_ppe(db, now),
        "screening": {"decisions_by_status": audit_counts},
        "lists": {
            "production_entities_by_type": {k: int(v or 0) for k, v in ready_by_type.items()},
            "snapshots_by_status": snapshot_counts,
            # Le volume disait « combien », jamais « depuis quand ». Releve en
            # production : 39 listes, 670 864 fiches — et toutes vieilles d'un
            # mois, sans qu'aucun indicateur le montre (cf. fiskr/fraicheur.py).
            "freshness": fraicheur_resumee(db, now),
        },
        "recent_syncs": [
            {
                "source": r.source,
                "executed_at": r.executed_at.isoformat() if r.executed_at else None,
                "trigger": r.trigger,
                "status": r.status,
                "added": r.added_count, "modified": r.modified_count, "removed": r.removed_count,
            }
            for r in recent_syncs
        ],
    }

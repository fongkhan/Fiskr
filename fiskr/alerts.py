"""
Logique partagee des alertes de criblage : ouverture/re-detection dedupliquee
et consultation de la liste blanche client x liste. Module separe pour etre
utilisable a la fois par l'API temps reel (fiskr.api) et par le moteur de
re-criblage post-delta (fiskr.rescreen) sans import circulaire.
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from fiskr.database import Alert, AlertEvent, ALERT_OPEN_STATUSES, AuditTrail, WhitelistPair
from fiskr.settings import alert_sla_hours

logger = logging.getLogger("fiskr.alerts")


def compute_alert_priority(best_match: Dict[str, Any]) -> str:
    """
    Priorite calculee a la creation (modifiable ensuite par l'analyste) :
    hard match (identifiant officiel identique) -> CRITICAL ; score tres
    eleve -> HIGH ; alerte standard -> MEDIUM ; proche du seuil -> LOW.
    """
    if best_match.get("hard_match_triggered"):
        return "CRITICAL"
    score = float(best_match.get("final_score") or 0.0)
    cut_off = float(best_match.get("cut_off_applied") or 75.0)
    if score >= 95.0:
        return "HIGH"
    if score >= cut_off + 5.0:
        return "MEDIUM"
    return "LOW"


def _echeance(sla: Dict[str, Any], priority: str,
              created_at: Optional[datetime] = None) -> Optional[datetime]:
    """Echeance SLA depuis un reglage DEJA lu (0 = aucune echeance)."""
    hours = (sla or {}).get(priority or "MEDIUM", 0)
    if not hours:
        return None
    return (created_at or datetime.utcnow()) + timedelta(hours=hours)


def compute_due_at(db, priority: str, created_at: Optional[datetime] = None) -> Optional[datetime]:
    """Echeance SLA de traitement selon la priorite (reglage a chaud ; 0 = aucune)."""
    return _echeance(alert_sla_hours(db), priority, created_at)


def is_whitelisted(db, client_id: Optional[str], entity_id: Optional[str]) -> Optional[WhitelistPair]:
    """
    Retourne la paire de liste blanche ACTIVE (non revoquee, non expiree)
    pour ce couple client x liste, ou None.
    """
    if not client_id or not entity_id:
        return None
    now = datetime.utcnow()
    return db.query(WhitelistPair).filter(
        WhitelistPair.client_id == client_id,
        WhitelistPair.watchlist_entity_id == entity_id,
        WhitelistPair.revoked_at.is_(None),
        (WhitelistPair.expires_at.is_(None)) | (WhitelistPair.expires_at > now)
    ).first()


def whitelisted_pairs(db, client_id: Optional[str],
                      entity_ids) -> Dict[str, WhitelistPair]:
    """
    Paires de liste blanche ACTIVES pour ce client, indexees par fiche listee.

    Version groupee de `is_whitelisted` : un criblage produit desormais AUTANT
    de correspondances qu'il en trouve au-dessus du seuil — 2 976 pour un nom
    tres courant, mesure en production. Une requete par correspondance aurait
    remis un N+1 sur le chemin le plus chaud de l'application.
    """
    ids = [e for e in dict.fromkeys(entity_ids) if e]
    if not client_id or not ids:
        return {}
    now = datetime.utcnow()
    lignes = db.query(WhitelistPair).filter(
        WhitelistPair.client_id == client_id,
        WhitelistPair.watchlist_entity_id.in_(ids),
        WhitelistPair.revoked_at.is_(None),
        (WhitelistPair.expires_at.is_(None)) | (WhitelistPair.expires_at > now)
    ).all()
    # Premiere paire gagnante par fiche (meme choix que `.first()`)
    trouvees: Dict[str, WhitelistPair] = {}
    for paire in lignes:
        trouvees.setdefault(paire.watchlist_entity_id, paire)
    return trouvees


# Au-dela de ce nombre d'alertes ouvertes par UN criblage, les notifications
# individuelles cedent la place a une seule notification de volumetrie : un
# nom tres courant sans contexte peut ouvrir des milliers d'alertes d'un coup,
# et autant de notifications serait un incident en soi.
NOTIFY_BURST_MAX = 10


def open_or_redetect_alert(db, audit_record: AuditTrail, client_id: Optional[str],
                           best_match: Dict[str, Any], username: str,
                           detail_suffix: str = "", channel: str = "SCREENING",
                           suppressed_by_rule=None) -> int:
    """
    Ouvre une alerte de travail pour une decision ALERT, ou marque la
    re-detection si une alerte non close existe deja pour la meme paire
    client x liste (pas de doublons a chaque re-criblage).

    `channel` : SCREENING (criblage clients) ou FILTERING (transactions).
    `suppressed_by_rule` (FpRule) : si fourni, l'alerte est creee puis
    immediatement auto-cloturee CLOSED_BY_RULE (jamais silencieuse : la ligne
    d'audit porte deja fp_rule_applied). La dedup vaut aussi pour les alertes
    deja cloturees par regle (pas de doublon a chaque re-criblage).
    """
    wl_entity = best_match.get("watchlist_entity") or {}
    wl_id = wl_entity.get("entity_id", "NONE")

    # Une alerte deja cloturee par regle pour la meme paire est re-detectee au
    # lieu d'etre recreee (maitrise des volumes d'alertes auto-cloturees)
    dedup_statuses = list(ALERT_OPEN_STATUSES)
    if suppressed_by_rule is not None:
        dedup_statuses.append("CLOSED_BY_RULE")

    existing = db.query(Alert).filter(
        Alert.client_id == client_id,
        Alert.watchlist_entity_id == wl_id,
        Alert.status.in_(dedup_statuses)
    ).first()
    if existing:
        if best_match["final_score"] > existing.final_score:
            existing.final_score = best_match["final_score"]
        # Rattrapage progressif des alertes anterieures a la colonne list_type
        if existing.list_type is None and wl_entity.get("_list_type"):
            existing.list_type = wl_entity.get("_list_type")
        if suppressed_by_rule is not None:
            detail = (f"Re-détectée puis à nouveau supprimée par la règle « {suppressed_by_rule.name} » "
                      f"(v{suppressed_by_rule.version}, audit #{audit_record.id}).{detail_suffix}")
        else:
            detail = (f"Re-détectée lors d'un nouveau criblage "
                      f"(score {best_match['final_score']:.1f}, audit #{audit_record.id}).{detail_suffix}")
        db.add(AlertEvent(alert_id=existing.id, username=username, action="REDETECTED", detail=detail))
        db.commit()
        return existing.id

    now = datetime.utcnow()
    suppressed = suppressed_by_rule is not None
    priority = compute_alert_priority(best_match)
    alert = Alert(
        audit_id=audit_record.id,
        channel=channel,
        client_id=client_id,
        client_name=audit_record.client_name,
        watchlist_entity_id=wl_id,
        watchlist_name=wl_entity.get("primary_name", "Inconnu"),
        final_score=best_match["final_score"],
        list_type=wl_entity.get("_list_type"),
        status="CLOSED_BY_RULE" if suppressed else "OPEN",
        priority=priority,
        due_at=None if suppressed else compute_due_at(db, priority, now),
    )
    if suppressed:
        alert.decided_by = "fp-rule"
        alert.decided_at = now
        alert.decision_comment = (
            f"Faux positif supprimé automatiquement par la règle « {suppressed_by_rule.name} » "
            f"(#{suppressed_by_rule.id} v{suppressed_by_rule.version}). Conservée pour l'audit (ACPR/FED)."
        )
    db.add(alert)
    db.flush()
    db.add(AlertEvent(
        alert_id=alert.id, username=username, action="CREATED",
        detail=f"Alerte créée par le criblage (score {best_match['final_score']:.1f}).{detail_suffix}"
    ))
    if suppressed:
        db.add(AlertEvent(
            alert_id=alert.id, username="fp-rule", action="RULE_SUPPRESSED",
            detail=(f"Auto-clôturée CLOSED_BY_RULE par la règle « {suppressed_by_rule.name} » "
                    f"(#{suppressed_by_rule.id} v{suppressed_by_rule.version}).")
        ))
    db.commit()
    # Notification metier (jamais bloquante) — pas de notification pour les
    # alertes auto-cloturees par regle anti-faux positifs
    if not suppressed:
        from fiskr.notifier import emit
        emit(db, "alert_created", {
            "_assignee": alert.assigned_to,
            "Alerte": alert.id, "Canal": channel, "Priorité": priority,
            "Client": alert.client_name, "Fiche listée": alert.watchlist_name,
            "Liste": alert.list_type, "Score": f"{alert.final_score:.1f}",
            "Échéance": alert.due_at.isoformat() if alert.due_at else "—",
        })
    return alert.id


def open_or_redetect_alerts(db, correspondances: Sequence[Dict[str, Any]], *,
                            client_id: Optional[str], username: str,
                            channel: str = "SCREENING",
                            detail_suffix: str = "") -> Dict[str, Any]:
    """
    Ouvre (ou re-detecte) une alerte pour CHAQUE correspondance au-dessus du
    seuil, en un seul aller-retour de base.

    Le criblage ne persistait que la MEILLEURE correspondance : un client
    homonyme de 2 976 fiches listees (mesure en production, « Mohammed Ali »
    sans pays) ne laissait qu'une seule trace, et 2 975 correspondances
    reglementaires disparaissaient sans laisser d'ecrit. Elles sont desormais
    toutes creees.

    `correspondances` : sequence de {"audit": AuditTrail, "match": dict,
    "rule": FpRule|None}. Une correspondance portant une regle est creee PUIS
    cloturee CLOSED_BY_RULE, avec le nom et la version de la regle en clair —
    jamais supprimee en silence.

    Les notifications individuelles s'arretent a `NOTIFY_BURST_MAX` et cedent
    la place a une seule notification de volumetrie.
    """
    resultat = {"alert_ids": [], "opened": 0, "closed_by_rule": 0, "redetected": 0}
    if not correspondances:
        return resultat

    ids_fiches = [(c["match"].get("watchlist_entity") or {}).get("entity_id", "NONE")
                  for c in correspondances]
    # Les alertes deja cloturees par regle comptent aussi pour la dedup : un
    # re-criblage ne doit pas recreer ce qu'une regle a deja tranche.
    statuts = list(ALERT_OPEN_STATUSES) + ["CLOSED_BY_RULE"]
    existantes: Dict[str, Alert] = {}
    if client_id and ids_fiches:
        for alerte in db.query(Alert).filter(
                Alert.client_id == client_id,
                Alert.watchlist_entity_id.in_([i for i in dict.fromkeys(ids_fiches) if i]),
                Alert.status.in_(statuts)).all():
            existantes.setdefault(alerte.watchlist_entity_id, alerte)

    now = datetime.utcnow()
    # Reglage SLA lu UNE fois pour tout le lot : `compute_due_at` le relisait
    # par alerte, soit une requete de reglages par correspondance.
    sla = alert_sla_hours(db)
    evenements: List[AlertEvent] = []
    a_notifier: List[Alert] = []

    for correspondance in correspondances:
        audit = correspondance["audit"]
        match = correspondance["match"]
        regle = correspondance.get("rule")
        fiche = match.get("watchlist_entity") or {}
        wl_id = fiche.get("entity_id", "NONE")
        supprimee = regle is not None

        deja = existantes.get(wl_id)
        if deja is not None:
            if match["final_score"] > deja.final_score:
                deja.final_score = match["final_score"]
            if deja.list_type is None and fiche.get("_list_type"):
                deja.list_type = fiche.get("_list_type")
            # Evenement de re-detection : ecrit pour les alertes que
            # quelqu'un doit encore traiter. Une alerte deja tranchee par une
            # regle n'en recoit pas a chaque passage — le re-criblage repasse
            # toute la base apres chaque mise en production, et un homonyme de
            # nom courant porte des milliers d'alertes cloturees par regle :
            # une ligne d'evenement par alerte et par passage ferait grossir
            # le journal sans rien apprendre. La ligne d'audit de CE criblage,
            # elle, est ecrite dans tous les cas.
            if deja.status != "CLOSED_BY_RULE":
                if supprimee:
                    detail = (f"Re-détectée puis à nouveau supprimée par la règle « {regle.name} » "
                              f"(v{regle.version}, audit #{audit.id}).{detail_suffix}")
                else:
                    detail = (f"Re-détectée lors d'un nouveau criblage "
                              f"(score {match['final_score']:.1f}, audit #{audit.id}).{detail_suffix}")
                evenements.append(AlertEvent(alert_id=deja.id, username=username,
                                             action="REDETECTED", detail=detail))
            resultat["alert_ids"].append(deja.id)
            resultat["redetected"] += 1
            continue

        priorite = compute_alert_priority(match)
        alerte = Alert(
            audit_id=audit.id, channel=channel, client_id=client_id,
            client_name=audit.client_name, watchlist_entity_id=wl_id,
            watchlist_name=fiche.get("primary_name", "Inconnu"),
            final_score=match["final_score"], list_type=fiche.get("_list_type"),
            status="CLOSED_BY_RULE" if supprimee else "OPEN",
            priority=priorite,
            due_at=None if supprimee else _echeance(sla, priorite, now),
        )
        if supprimee:
            alerte.decided_by = "fp-rule"
            alerte.decided_at = now
            alerte.decision_comment = (
                f"Faux positif supprimé automatiquement par la règle « {regle.name} » "
                f"(#{regle.id} v{regle.version}). Conservée pour l'audit (ACPR/FED)."
            )
        db.add(alerte)
        # La dedup vaut AUSSI a l'interieur d'un meme criblage : deux fiches
        # portant le meme entity_id ne doivent pas ouvrir deux alertes.
        existantes[wl_id] = alerte
        correspondance["_alerte"] = alerte
        correspondance["_regle"] = regle
        if supprimee:
            resultat["closed_by_rule"] += 1
        else:
            resultat["opened"] += 1
            a_notifier.append(alerte)

    db.flush()   # attribue les identifiants avant d'ecrire les evenements

    for correspondance in correspondances:
        alerte = correspondance.get("_alerte")
        if alerte is None:
            continue
        regle = correspondance.get("_regle")
        match = correspondance["match"]
        evenements.append(AlertEvent(
            alert_id=alerte.id, username=username, action="CREATED",
            detail=f"Alerte créée par le criblage (score {match['final_score']:.1f}).{detail_suffix}"))
        if regle is not None:
            evenements.append(AlertEvent(
                alert_id=alerte.id, username="fp-rule", action="RULE_SUPPRESSED",
                detail=(f"Auto-clôturée CLOSED_BY_RULE par la règle « {regle.name} » "
                        f"(#{regle.id} v{regle.version}).")))
        resultat["alert_ids"].append(alerte.id)
        correspondance.pop("_alerte", None)
        correspondance.pop("_regle", None)

    db.add_all(evenements)
    # Charges utiles de notification composees AVANT le commit : apres, la
    # session expire les objets et chaque attribut relu declenche un SELECT
    # par alerte — le N+1 rentrerait par la porte de derriere.
    charges = [{
        "_assignee": a.assigned_to,
        "Alerte": a.id, "Canal": channel, "Priorité": a.priority,
        "Client": a.client_name, "Fiche listée": a.watchlist_name,
        "Liste": a.list_type, "Score": f"{a.final_score:.1f}",
        "Échéance": a.due_at.isoformat() if a.due_at else "—",
    } for a in a_notifier[:NOTIFY_BURST_MAX]]
    resume = None
    if len(a_notifier) > NOTIFY_BURST_MAX:
        resume = {
            "Canal": channel,
            "Client": a_notifier[0].client_name,
            "Alertes ouvertes": len(a_notifier),
            "Notifiées individuellement": NOTIFY_BURST_MAX,
            "Score le plus élevé": f"{max(a.final_score for a in a_notifier):.1f}",
        }
    db.commit()

    _notifier_ouvertures(db, charges, resume)
    return resultat


def _notifier_ouvertures(db, charges: List[Dict[str, Any]],
                         resume: Optional[Dict[str, Any]]) -> None:
    """Notifie les alertes ouvertes — individuellement tant que le volume le
    permet, puis une seule fois pour le reste. Aucune notification pour les
    alertes auto-cloturees par regle (comportement d'origine)."""
    if not charges:
        return
    from fiskr.notifier import emit
    for charge in charges:
        emit(db, "alert_created", charge)
    if resume is not None:
        emit(db, "alert_volume", resume)

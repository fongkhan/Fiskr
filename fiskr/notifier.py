"""
Aiguillage des notifications d'etape : QUI recoit QUOI, et QUAND.

`emit(db, event_key, payload)` est le point d'entree unique appele apres chaque
etape metier (production des listes, criblage, filtrage). Il :
1. verifie que l'evenement est active (reglage a chaud `notifications.events`) ;
2. resout les destinataires par ROLE depuis le catalogue (fiskr/events.py),
   avec les audiences dynamiques `assignee` (analyste assigne + delegue en cas
   d'absence declaree) et `actor` (auteur de l'action), et un repli sur les
   destinataires globaux historiques (NOTIFY_EMAIL_TO / SYNC_EMAIL_TO) ;
3. envoie IMMEDIATEMENT (etapes rares et structurantes) ou met en FILE pour le
   recapitulatif periodique (etapes a fort volume) ;
4. journalise l'envoi dans `notification_deliveries` (« le mail est-il parti ? »).

Garantie fondamentale : `emit` n'echoue JAMAIS. Une notification en erreur ne
doit jamais faire echouer un import, une approbation de liste ou une decision
d'alerte — l'erreur est journalisee et l'operation metier continue.
"""
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence

from fiskr import notify
from fiskr.database import NotificationDelivery, User
from fiskr.events import (
    AUDIENCE_ACTOR, AUDIENCE_ASSIGNEE, CATEGORY_LABELS, DIGEST, EVENT_CATALOG, IMMEDIATE,
)
from fiskr.settings import notification_batch_settings, notification_events

logger = logging.getLogger("fiskr.notifier")

# Duree de conservation du journal des envois
DELIVERY_TTL_DAYS = 90


def _users_with_roles(db, roles: Iterable[str]) -> List[str]:
    """Emails des comptes portant l'un des roles demandes (roles empilables :
    la colonne `role` est une liste separee par des virgules)."""
    from fiskr.auth import parse_roles
    wanted = {r.strip().lower() for r in roles if r}
    if not wanted:
        return []
    emails: List[str] = []
    for user in db.query(User).all():
        if not (user.email or "").strip():
            continue
        user_roles = set(parse_roles(user.role))
        # Un admin recoit tout ce qui vise un role fonctionnel (meme regle que
        # require_roles : admin passe toujours)
        if wanted & user_roles or ("admin" in user_roles and wanted - {"admin"}):
            emails.append(user.email.strip())
    return emails


def _email_of(db, username: Optional[str]) -> List[str]:
    if not username:
        return []
    user = db.query(User).filter(User.username == username).first()
    if user and (user.email or "").strip():
        return [user.email.strip()]
    return []


def _resolve_assignee(db, payload: Dict[str, Any]) -> List[str]:
    """Analyste assigne, plus son delegue si une absence est declaree (meme
    regle de redirection que l'assignation des alertes)."""
    username = payload.get("_assignee") or payload.get("assigne_a") or payload.get("assigned_to")
    emails = _email_of(db, username)
    if username:
        user = db.query(User).filter(User.username == username).first()
        if user and user.absent_until and user.absent_until > datetime.utcnow() and user.delegate_to:
            emails += _email_of(db, user.delegate_to)
    return emails


def _opted_out_emails(db, category: str) -> set:
    """Adresses des comptes ayant coupe cette categorie (ou tout) dans leur
    espace « Mon compte ». Filtre personnel applique APRES le routage par
    role : couper ses notifications ne modifie le routage de personne d'autre."""
    out = set()
    for user in db.query(User).all():
        email = (user.email or "").strip()
        if not email:
            continue
        muted = user.notification_opt_out or []
        if "ALL" in muted or category in muted:
            out.add(email)
    return out


def resolve_recipients(db, event_key: str, payload: Dict[str, Any]) -> List[str]:
    """
    Destinataires effectifs d'un evenement : union des adresses des comptes
    correspondant a l'audience, plus les adresses supplementaires de la
    categorie. Repli sur les destinataires globaux quand rien ne correspond
    (un deploiement sans emails de comptes garde le comportement historique).
    """
    event = EVENT_CATALOG.get(event_key)
    if event is None:
        return []
    emails: List[str] = []
    roles = [a for a in event.audience if a not in (AUDIENCE_ASSIGNEE, AUDIENCE_ACTOR)]
    if roles:
        emails += _users_with_roles(db, roles)
    if AUDIENCE_ASSIGNEE in event.audience:
        emails += _resolve_assignee(db, payload)
    if AUDIENCE_ACTOR in event.audience:
        emails += _email_of(db, payload.get("_actor") or payload.get("par") or payload.get("auteur"))

    try:
        extras = notification_batch_settings(db)["extra_recipients"].get(event.category) or []
    except Exception:
        extras = []
    emails += [str(a).strip() for a in extras if str(a).strip()]

    if not emails:
        emails = notify.default_recipients()
    # Filtre personnel : les comptes qui ont coupe cette categorie (ou tout)
    # dans leur espace « Mon compte » sortent de la liste — apres le routage
    # par role, avant le repli global (une adresse de repli n'est pas un compte).
    muted = _opted_out_emails(db, event.category)
    if muted:
        emails = [a for a in emails if a.lower() not in {m.lower() for m in muted}]
    # Deduplication en conservant l'ordre
    seen, unique = set(), []
    for address in emails:
        key = address.lower()
        if key not in seen:
            seen.add(key)
            unique.append(address)
    return unique


def _link_for(event_key: str) -> str:
    base = notify.public_url()
    event = EVENT_CATALOG.get(event_key)
    if not base or event is None or not event.link:
        return base or ""
    return f"{base}/{event.link}"


def _public_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Retire les cles techniques de routage (prefixees `_`) du contenu affiche."""
    return {k: v for k, v in payload.items() if not str(k).startswith("_")}


def _record(db, event_key: str, event, recipients: Sequence[str], payload: Dict[str, Any],
            status: str, error: Optional[str] = None) -> Optional[int]:
    """Ecrit la ligne de journal / de file. Ne leve jamais."""
    try:
        row = NotificationDelivery(
            event_key=event_key,
            category=getattr(event, "category", None),
            urgency=getattr(event, "urgency", None),
            payload=_public_payload(payload),
            recipients=", ".join(recipients),
            status=status,
            sent_at=datetime.utcnow() if status == "SENT" else None,
            error=error,
        )
        db.add(row)
        db.commit()
        return row.id
    except Exception as e:
        logger.error(f"Journal de notification indisponible ({event_key}) : {e}")
        try:
            db.rollback()
        except Exception:
            pass
        return None


def _send_now(event_key: str, label: str, payload: Dict[str, Any],
              recipients: Sequence[str], link_url: str, delivery_id: Optional[int]) -> None:
    """Envoi immediat en arriere-plan + mise a jour du journal (session dediee :
    le thread ne doit pas partager la session de la requete)."""
    subject = f"[Fiskr] {label}"
    text, html = notify.render_event_email(label, _public_payload(payload), link_url=link_url)
    status, error = "SENT", None
    try:
        if not notify.smtp_configured() or not recipients:
            status, error = "SKIPPED", "SMTP non configuré ou aucun destinataire."
        else:
            notify.send_email(recipients, subject, text, html_body=html)
    except Exception as e:
        status, error = "FAILED", str(e)
        logger.error(f"Notification « {label} » en échec : {e}")

    # Webhooks generiques : meme charge utile que l'historique
    envelope = {"event": event_key, "label": label,
                "at": datetime.utcnow().isoformat() + "Z", "data": _public_payload(payload)}
    for url in notify._webhook_urls():
        try:
            notify._post_webhook(url, envelope)
        except Exception as e:
            logger.error(f"Webhook en échec ({event_key} -> {url}) : {e}")

    if delivery_id is None:
        return
    from fiskr.database import SessionLocal
    session = SessionLocal()
    try:
        row = session.query(NotificationDelivery).filter(NotificationDelivery.id == delivery_id).first()
        if row is not None:
            row.status = status
            row.error = error
            row.sent_at = datetime.utcnow() if status == "SENT" else None
            session.commit()
    except Exception as e:
        logger.error(f"Mise à jour du journal de notification impossible : {e}")
    finally:
        session.close()


def emit(db, event_key: str, payload: Optional[Dict[str, Any]] = None, *,
         urgency_override: Optional[str] = None,
         recipients_override: Optional[Sequence[str]] = None) -> None:
    """
    Notifie une etape metier. Ne leve JAMAIS : a appeler apres le commit de
    l'operation, sans try/except cote appelant.

    Les cles de charge utile prefixees `_` servent au routage et ne sont pas
    affichees : `_assignee` (audience assignee), `_actor` (audience actor).
    """
    try:
        event = EVENT_CATALOG.get(event_key)
        if event is None:
            logger.error(f"Évènement de notification inconnu : {event_key}")
            return
        payload = dict(payload or {})
        if not notification_events(db).get(event_key, event.default_enabled):
            return

        recipients = list(recipients_override) if recipients_override else resolve_recipients(db, event_key, payload)
        urgency = urgency_override or event.urgency

        if urgency == DIGEST:
            # Mise en file : la boucle de regroupement composera un seul mail
            _record(db, event_key, event, recipients, payload, "QUEUED")
            return

        delivery_id = _record(db, event_key, event, recipients, payload, "QUEUED")
        thread = threading.Thread(
            target=_send_now,
            args=(event_key, event.label, payload, recipients, _link_for(event_key), delivery_id),
            daemon=True,
        )
        thread.start()
    except Exception as e:  # aucune notification ne casse une opération métier
        logger.error(f"Notification impossible ({event_key}) : {e}")


# ------------------ RECAPITULATIF PERIODIQUE ------------------

def _summarize(payload: Dict[str, Any]) -> str:
    """Resume d'une ligne de recapitulatif : les 3 premieres valeurs utiles."""
    parts = [f"{k} : {v}" for k, v in _public_payload(payload or {}).items() if v not in (None, "", [], {})]
    return " · ".join(parts[:3])


def flush_digest(db, now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Compose et envoie le recapitulatif des evenements en file : UN mail par
    destinataire, regroupe par categorie. Retourne un compte rendu
    {recipients, events, sent, failed} — utilise par la boucle planifiee et
    testable en synchrone.
    """
    now = now or datetime.utcnow()
    queued = db.query(NotificationDelivery).filter(
        NotificationDelivery.status == "QUEUED",
        NotificationDelivery.urgency == DIGEST,
    ).order_by(NotificationDelivery.created_at.asc()).all()
    if not queued:
        return {"recipients": 0, "events": 0, "sent": 0, "failed": 0}

    # Regroupement par destinataire puis par categorie
    per_recipient: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for row in queued:
        label = EVENT_CATALOG[row.event_key].label if row.event_key in EVENT_CATALOG else row.event_key
        item = {"label": label, "summary": _summarize(row.payload or {}),
                "at": (row.created_at or now).isoformat()}
        for address in [a.strip() for a in (row.recipients or "").split(",") if a.strip()]:
            per_recipient.setdefault(address, {}).setdefault(row.category or "gouvernance", []).append(item)

    sent, failed = 0, 0
    base = notify.public_url()
    for address, groups in per_recipient.items():
        # Categories dans l'ordre canonique du catalogue
        ordered = {c: groups[c] for c in CATEGORY_LABELS if c in groups}
        ordered.update({c: v for c, v in groups.items() if c not in ordered})
        text, html, total = notify.render_digest_email(ordered, link_base=base)
        try:
            if notify.smtp_configured():
                notify.send_email([address], f"[Fiskr] Récapitulatif — {total} évènement(s)", text, html_body=html)
                sent += 1
        except Exception as e:
            failed += 1
            logger.error(f"Récapitulatif en échec vers {address} : {e}")

    marked = "SENT" if notify.smtp_configured() else "SKIPPED"
    for row in queued:
        row.status = marked
        row.sent_at = now if marked == "SENT" else None
    db.commit()
    return {"recipients": len(per_recipient), "events": len(queued), "sent": sent, "failed": failed}


def purge_deliveries(db, ttl_days: int = DELIVERY_TTL_DAYS) -> int:
    """Purge le journal des envois au-dela du TTL. Retourne le nombre supprime."""
    cutoff = datetime.utcnow() - timedelta(days=ttl_days)
    deleted = db.query(NotificationDelivery).filter(
        NotificationDelivery.created_at < cutoff,
        NotificationDelivery.status != "QUEUED",
    ).delete(synchronize_session=False)
    db.commit()
    return int(deleted or 0)

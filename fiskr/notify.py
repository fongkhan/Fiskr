"""
Notifications metier de Fiskr : email (SMTP, memes variables d'environnement
que les rapports de synchronisation) et webhooks generiques (POST JSON vers
les URLs de config.yaml `notifications.webhooks`).

Regles de conception :
- fire-and-forget en thread : une notification ne bloque JAMAIS le criblage
  et une erreur d'envoi ne remonte jamais a l'appelant (journalisee seulement) ;
- activation par evenement via le reglage a chaud `notifications.events`
  (fiskr.settings.notification_events) — l'appelant a deja filtre ;
- aucune donnee sensible au-dela du strict necessaire dans les payloads.
"""
import json
import logging
import os
import smtplib
import threading
import urllib.request
from datetime import datetime
from email.mime.text import MIMEText
from typing import Any, Dict, List

from fiskr.config import config

logger = logging.getLogger("fiskr.notify")

EVENT_LABELS = {
    "alert_created": "Nouvelle alerte",
    "alert_pending_validation": "Décision d'alerte en attente de validation 4-yeux",
    "snapshot_pending_review": "Snapshot en attente d'homologation",
    "sync_error": "Échec de synchronisation d'une source",
}


def _webhook_urls() -> List[str]:
    cfg = config.get("notifications", {}) or {}
    urls = cfg.get("webhooks") or []
    return [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]


def _send_email(subject: str, body: str) -> bool:
    """Memes variables SMTP que les rapports de sync ; NOTIFY_EMAIL_TO
    prioritaire, repli sur SYNC_EMAIL_TO."""
    host = os.getenv("SMTP_HOST")
    recipients = [
        r.strip() for r in (os.getenv("NOTIFY_EMAIL_TO") or os.getenv("SYNC_EMAIL_TO", "")).split(",")
        if r.strip()
    ]
    if not host or not recipients:
        return False
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", user or "fiskr@localhost")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    with smtplib.SMTP(host, port, timeout=30) as server:
        server.ehlo()
        try:
            server.starttls()
            server.ehlo()
        except smtplib.SMTPNotSupportedError:
            pass
        if user and password:
            server.login(user, password)
        server.sendmail(sender, recipients, msg.as_string())
    return True


def _post_webhook(url: str, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", "User-Agent": "Fiskr-Notify/1.0"}
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def _dispatch(event_type: str, payload: Dict[str, Any]) -> None:
    label = EVENT_LABELS.get(event_type, event_type)
    subject = f"[Fiskr] {label}"
    body_lines = [f"Événement : {label}", f"Horodatage : {datetime.utcnow().isoformat()}Z", ""]
    body_lines += [f"{k} : {v}" for k, v in payload.items()]
    try:
        _send_email(subject, "\n".join(body_lines))
    except Exception as e:
        logger.error(f"Notification email en échec ({event_type}) : {e}")
    envelope = {"event": event_type, "label": label, "at": datetime.utcnow().isoformat() + "Z", "data": payload}
    for url in _webhook_urls():
        try:
            _post_webhook(url, envelope)
        except Exception as e:
            logger.error(f"Webhook en échec ({event_type} -> {url}) : {e}")


def notify_event(event_type: str, payload: Dict[str, Any]) -> None:
    """Declenche la notification en arriere-plan (jamais bloquant)."""
    try:
        thread = threading.Thread(target=_dispatch, args=(event_type, dict(payload)), daemon=True)
        thread.start()
    except Exception as e:  # meme la creation du thread ne doit pas remonter
        logger.error(f"Notification impossible ({event_type}) : {e}")

"""
Transport des notifications metier de Fiskr : email (SMTP, memes variables
d'environnement que les rapports de synchronisation) et webhooks generiques
(POST JSON vers les URLs de config.yaml `notifications.webhooks`).

Deux niveaux, un seul coeur SMTP :
1. `send_email(...)` — envoi SYNCHRONE et cible qui PROPAGE ses erreurs :
   utilise par les actions declenchees au clic (envoi d'un document, mail de
   test) qui doivent afficher une erreur explicite, et par le dispatcher.
2. `notify_event(...)` / `_dispatch(...)` — chemin historique fire-and-forget :
   une notification ne bloque JAMAIS le metier et une erreur d'envoi ne remonte
   jamais a l'appelant (journalisee seulement).

Les libelles d'evenements sont derives du catalogue (fiskr/events.py) : une
etape notifiable se declare a un seul endroit.
"""
import json
import logging
import os
import re
import smtplib
import threading
import urllib.request
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fiskr.config import config
from fiskr.events import EVENT_CATALOG

logger = logging.getLogger("fiskr.notify")

# Libelles derives du catalogue : ajouter une entree a EVENT_CATALOG suffit
EVENT_LABELS = {key: event.label for key, event in EVENT_CATALOG.items()}


def _webhook_urls() -> List[str]:
    cfg = config.get("notifications", {}) or {}
    urls = cfg.get("webhooks") or []
    return [u for u in urls if isinstance(u, str) and u.startswith(("http://", "https://"))]


def public_url() -> str:
    """URL publique de l'application (liens directs dans les mails). Vide =
    les mails partent sans bouton (jamais de lien casse)."""
    cfg = config.get("notifications", {}) or {}
    url = str(cfg.get("public_url") or os.getenv("FISKR_PUBLIC_URL") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        return ""
    return url.rstrip("/")


def smtp_configured() -> bool:
    """True si un serveur SMTP est configure (envoi possible)."""
    return bool(os.getenv("SMTP_HOST"))


def default_recipients() -> List[str]:
    """Destinataires globaux historiques (repli quand aucun email de compte
    ne correspond a l'audience d'un evenement)."""
    raw = os.getenv("NOTIFY_EMAIL_TO") or os.getenv("SYNC_EMAIL_TO", "")
    return [r.strip() for r in raw.split(",") if r.strip()]


def send_email(recipients: Sequence[str], subject: str, body: str,
               html_body: Optional[str] = None) -> None:
    """
    Envoi SMTP synchrone et cible. PROPAGE les exceptions (smtplib, OSError) :
    c'est le point d'entree des envois declenches par un utilisateur, qui
    doivent remonter une erreur explicite plutot que d'echouer en silence.

    `body` est la version texte ; `html_body` (optionnel) ajoute une alternative
    HTML — les clients mail modernes affichent le HTML, les autres le texte.
    """
    recipients = [r for r in (recipients or []) if r]
    if not recipients:
        raise ValueError("Aucun destinataire pour l'envoi email.")
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP non configuré (variable d'environnement SMTP_HOST absente).")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_USER")
    password = os.getenv("SMTP_PASSWORD")
    sender = os.getenv("SMTP_FROM", user or "fiskr@localhost")

    if html_body:
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html", "utf-8"))
    else:
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


def _send_email(subject: str, body: str, html_body: Optional[str] = None,
                recipients: Optional[Sequence[str]] = None) -> bool:
    """
    Chemin historique tolerant : envoie aux destinataires globaux (ou a ceux
    fournis) et retourne False — sans lever — quand rien n'est configure.
    """
    targets = list(recipients) if recipients else default_recipients()
    if not smtp_configured() or not targets:
        return False
    send_email(targets, subject, body, html_body)
    return True


# ------------------ RENDU DES MAILS ------------------

_STYLE_WRAP = ("margin:0;padding:24px;background:#f4f5f7;"
               "font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827;")
_STYLE_CARD = ("max-width:640px;margin:0 auto;background:#ffffff;border-radius:10px;"
               "border:1px solid #e5e7eb;overflow:hidden;")
_STYLE_TH = ("text-align:left;padding:8px 12px;background:#f9fafb;border-bottom:1px solid #e5e7eb;"
             "font-size:13px;color:#4b5563;width:38%;vertical-align:top;")
_STYLE_TD = ("padding:8px 12px;border-bottom:1px solid #e5e7eb;font-size:13px;vertical-align:top;")
_STYLE_BTN = ("display:inline-block;padding:10px 18px;background:#4f46e5;color:#ffffff;"
              "text-decoration:none;border-radius:6px;font-size:14px;font-weight:600;")

# Cles de charge utile signalant un etat problematique (bandeau rouge)
_ALERT_HINTS = ("erreur", "echec", "échec", "error", "warn", "dépassé", "depasse", "refusé", "refuse")


def _humanize(key: str) -> str:
    """`snapshot_id` -> `Snapshot id` (les cles de payload sont deja en francais
    la plupart du temps ; cette normalisation ne fait que rendre lisible).
    Seule la premiere lettre est forcee en majuscule : `.capitalize()` mettrait
    en minuscules les acronymes metier du reste de la cle (PP, PM, SLA, KYC)."""
    label = re.sub(r"[_\-]+", " ", str(key)).strip()
    return label[:1].upper() + label[1:]


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "oui" if value else "non"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) if value else "—"
    if isinstance(value, dict):
        return ", ".join(f"{k} : {v}" for k, v in value.items()) if value else "—"
    if value in (None, ""):
        return "—"
    return str(value)


def render_event_email(label: str, payload: Dict[str, Any],
                       link_url: str = "", intro: str = "",
                       accent_alert: Optional[bool] = None) -> Tuple[str, str]:
    """
    Compose (texte, HTML) d'une notification d'etape. Le HTML est autonome
    (styles inline, aucune ressource externe) ; le bouton « Ouvrir dans Fiskr »
    n'apparait que si une URL publique est configuree.
    """
    if accent_alert is None:
        accent_alert = any(hint in label.lower() for hint in _ALERT_HINTS)
    accent = "#dc2626" if accent_alert else "#4f46e5"

    lines = [label, f"Horodatage : {datetime.utcnow().isoformat()}Z", ""]
    if intro:
        lines.insert(1, intro)
    lines += [f"{_humanize(k)} : {_format_value(v)}" for k, v in payload.items()]
    if link_url:
        lines += ["", f"Ouvrir dans Fiskr : {link_url}"]
    text = "\n".join(lines)

    rows = "".join(
        f'<tr><th style="{_STYLE_TH}">{escape(_humanize(k))}</th>'
        f'<td style="{_STYLE_TD}">{escape(_format_value(v))}</td></tr>'
        for k, v in payload.items()
    )
    button = (f'<p style="margin:20px 0 4px;"><a href="{escape(link_url)}" '
              f'style="{_STYLE_BTN}">Ouvrir dans Fiskr</a></p>') if link_url else ""
    intro_html = f'<p style="margin:0 0 14px;font-size:14px;color:#374151;">{escape(intro)}</p>' if intro else ""

    html = f"""<!DOCTYPE html>
<html lang="fr"><body style="{_STYLE_WRAP}">
<div style="{_STYLE_CARD}">
  <div style="padding:16px 20px;background:{accent};color:#ffffff;">
    <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.85;">Fiskr — Conformité LCB-FT</div>
    <div style="font-size:18px;font-weight:700;margin-top:4px;">{escape(label)}</div>
  </div>
  <div style="padding:20px;">
    {intro_html}
    <table style="width:100%;border-collapse:collapse;border-top:1px solid #e5e7eb;">{rows}</table>
    {button}
    <p style="margin:18px 0 0;font-size:11px;color:#9ca3af;">
      Message automatique émis le {datetime.utcnow().strftime('%d/%m/%Y à %H:%M')} UTC.
      Le paramétrage de ces notifications se règle dans Paramètres → Notifications métier.
    </p>
  </div>
</div>
</body></html>"""
    return text, html


def render_digest_email(groups: Dict[str, List[Dict[str, Any]]],
                        link_base: str = "") -> Tuple[str, str, int]:
    """
    Compose le mail RECAPITULATIF d'un destinataire : les evenements mis en
    file depuis le dernier envoi, groupes par categorie. Retourne
    (texte, HTML, nombre total d'evenements).
    """
    from fiskr.events import CATEGORY_LABELS

    total = sum(len(items) for items in groups.values())
    text_lines = [f"Récapitulatif Fiskr — {total} évènement(s) depuis le dernier envoi.", ""]
    sections_html = []
    for category, items in groups.items():
        cat_label = CATEGORY_LABELS.get(category, category)
        text_lines.append(f"— {cat_label} ({len(items)})")
        rows = []
        for item in items:
            when = (item.get("at") or "")[:16].replace("T", " ")
            summary = item.get("summary") or item.get("label") or ""
            text_lines.append(f"   • [{when}] {item.get('label', '')} — {summary}")
            rows.append(
                f'<tr><td style="{_STYLE_TD}white-space:nowrap;color:#6b7280;">{escape(when)}</td>'
                f'<td style="{_STYLE_TD}"><strong>{escape(str(item.get("label", "")))}</strong>'
                + (f'<br><span style="color:#4b5563;">{escape(str(summary))}</span>' if summary else "")
                + "</td></tr>"
            )
        text_lines.append("")
        sections_html.append(
            f'<h3 style="margin:18px 0 6px;font-size:14px;color:#111827;">{escape(cat_label)} '
            f'<span style="color:#6b7280;font-weight:400;">({len(items)})</span></h3>'
            f'<table style="width:100%;border-collapse:collapse;border-top:1px solid #e5e7eb;">{"".join(rows)}</table>'
        )
    if link_base:
        text_lines += [f"Ouvrir Fiskr : {link_base}"]
    button = (f'<p style="margin:22px 0 4px;"><a href="{escape(link_base)}" '
              f'style="{_STYLE_BTN}">Ouvrir dans Fiskr</a></p>') if link_base else ""

    html = f"""<!DOCTYPE html>
<html lang="fr"><body style="{_STYLE_WRAP}">
<div style="{_STYLE_CARD}">
  <div style="padding:16px 20px;background:#4f46e5;color:#ffffff;">
    <div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;opacity:.85;">Fiskr — Conformité LCB-FT</div>
    <div style="font-size:18px;font-weight:700;margin-top:4px;">Récapitulatif — {total} évènement(s)</div>
  </div>
  <div style="padding:20px;">{"".join(sections_html)}{button}
    <p style="margin:18px 0 0;font-size:11px;color:#9ca3af;">
      Regroupement automatique des étapes à fort volume. Fréquence et sélection des évènements :
      Paramètres → Notifications métier.
    </p>
  </div>
</div>
</body></html>"""
    return "\n".join(text_lines), html, total


# ------------------ CHEMIN FIRE-AND-FORGET (compatibilite) ------------------

def _post_webhook(url: str, payload: Dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json", "User-Agent": "Fiskr-Notify/1.0"}
    )
    with urllib.request.urlopen(request, timeout=10):
        pass


def _dispatch(event_type: str, payload: Dict[str, Any],
              recipients: Optional[Sequence[str]] = None,
              link_url: str = "") -> None:
    label = EVENT_LABELS.get(event_type, event_type)
    subject = f"[Fiskr] {label}"
    text, html = render_event_email(label, payload, link_url=link_url)
    try:
        _send_email(subject, text, html_body=html, recipients=recipients)
    except Exception as e:
        logger.error(f"Notification email en échec ({event_type}) : {e}")
    envelope = {"event": event_type, "label": label, "at": datetime.utcnow().isoformat() + "Z", "data": payload}
    for url in _webhook_urls():
        try:
            _post_webhook(url, envelope)
        except Exception as e:
            logger.error(f"Webhook en échec ({event_type} -> {url}) : {e}")


def notify_event(event_type: str, payload: Dict[str, Any],
                 recipients: Optional[Sequence[str]] = None,
                 link_url: str = "") -> None:
    """Declenche la notification en arriere-plan (jamais bloquant)."""
    try:
        thread = threading.Thread(
            target=_dispatch, args=(event_type, dict(payload), list(recipients) if recipients else None, link_url),
            daemon=True,
        )
        thread.start()
    except Exception as e:  # meme la creation du thread ne doit pas remonter
        logger.error(f"Notification impossible ({event_type}) : {e}")

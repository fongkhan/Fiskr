"""
Catalogue des evenements metier notifiables de Fiskr.

Ce module est volontairement SANS DEPENDANCE interne (il n'importe rien de
fiskr) : il est la source de verite lue par
- fiskr/settings.py    -> DEFAULT_NOTIFICATION_EVENTS (activation par defaut),
- fiskr/notify.py      -> EVENT_LABELS (libelles des mails/webhooks),
- fiskr/notifier.py    -> routage (destinataires, urgence, lien profond),
- l'API et le dashboard -> ecran de reglages genere depuis le catalogue.

Ajouter une etape notifiable = ajouter UNE entree ici. Rien d'autre n'est a
declarer : l'evenement devient activable, traduisible et routable partout.

Conventions :
- `audience` : roles destinataires (fiskr.auth.VALID_ROLES) ; la pseudo-audience
  AUDIENCE_ASSIGNEE cible l'analyste assigne a l'alerte (et son delegue en cas
  d'absence declaree) ; AUDIENCE_ACTOR cible l'auteur de l'action concernee
  (proposeur d'une decision, auteur d'une regle, createur d'une campagne).
- `urgency` : IMMEDIATE = mail des l'evenement (etapes rares et structurantes) ;
  DIGEST = mise en file, un seul mail recapitulatif periodique par destinataire
  (etapes a fort volume — jamais d'inondation de boites aux lettres).
- `link` : ancre du routage front (#onglet/sous-onglet, cf. applyHashRoute) ;
  combinee a notifications.public_url pour un bouton « Ouvrir dans Fiskr ».
"""
from dataclasses import dataclass, field
from typing import Dict, Tuple

# Categories d'evenements (regroupement de l'ecran de reglages et des recaps)
CATEGORY_LISTS = "production_listes"
CATEGORY_SCREENING = "criblage"
CATEGORY_FILTERING = "filtrage"
CATEGORY_GOVERNANCE = "gouvernance"

CATEGORY_LABELS = {
    CATEGORY_LISTS: "Production des listes",
    CATEGORY_SCREENING: "Criblage clients",
    CATEGORY_FILTERING: "Filtrage transactionnel",
    CATEGORY_GOVERNANCE: "Gouvernance & exploitation",
}

# Urgences
IMMEDIATE = "immediate"
DIGEST = "digest"
URGENCIES = (IMMEDIATE, DIGEST)

# Pseudo-audiences resolues dynamiquement depuis la charge utile
AUDIENCE_ASSIGNEE = "assignee"   # analyste assigne (+ delegue si absent)
AUDIENCE_ACTOR = "actor"         # auteur de l'action (proposeur, auteur de regle...)
PSEUDO_AUDIENCES = (AUDIENCE_ASSIGNEE, AUDIENCE_ACTOR)


@dataclass(frozen=True)
class Event:
    """Une etape notifiable du produit."""
    label: str
    category: str
    audience: Tuple[str, ...]
    urgency: str = DIGEST
    default_enabled: bool = True
    link: str = ""
    # Champs de la charge utile mis en avant dans le mail (ordre d'affichage) ;
    # vide = toutes les cles de la charge utile, dans leur ordre d'insertion
    highlight: Tuple[str, ...] = field(default_factory=tuple)


EVENT_CATALOG: Dict[str, Event] = {
    # ------------------ PRODUCTION DES LISTES ------------------
    "snapshot_pending_review": Event(
        label="Liste en attente d'homologation",
        category=CATEGORY_LISTS, audience=("reviewer", "admin"),
        urgency=IMMEDIATE, default_enabled=False,   # defaut historique conserve
        link="#watchlists/watchlists-review",
    ),
    "snapshot_approved": Event(
        label="Liste approuvée et mise en production",
        category=CATEGORY_LISTS, audience=("reviewer", "admin"),
        urgency=IMMEDIATE, link="#watchlists/watchlists-review",
    ),
    "snapshot_rejected": Event(
        label="Liste rejetée à l'homologation",
        category=CATEGORY_LISTS, audience=("reviewer", "admin"),
        urgency=IMMEDIATE, link="#watchlists/watchlists-review",
    ),
    "list_import_done": Event(
        label="Import de liste mis en production",
        category=CATEGORY_LISTS, audience=("reviewer", "admin"),
        urgency=DIGEST, link="#watchlists/watchlists-snapshots",
    ),
    "list_import_failed": Event(
        label="Import de liste en échec",
        category=CATEGORY_LISTS, audience=("admin",),
        urgency=IMMEDIATE, link="#watchlists/watchlists-snapshots",
    ),
    "eurlex_act_published": Event(
        label="Acte de mesures restrictives paru au Journal Officiel de l'UE",
        category=CATEGORY_LISTS, audience=("reviewer", "admin"),
        # Immediat par construction : c'est un signal d'alerte precoce, il
        # perd tout interet dans un recapitulatif du lendemain.
        urgency=IMMEDIATE, link="#watchlists/watchlists-sources",
    ),
    "sync_completed": Event(
        label="Synchronisation de source terminée",
        category=CATEGORY_LISTS, audience=("admin",),
        urgency=DIGEST, link="#watchlists/watchlists-sources",
    ),
    "sync_error": Event(
        label="Échec de synchronisation d'une source",
        category=CATEGORY_LISTS, audience=("admin",),
        urgency=IMMEDIATE, link="#watchlists/watchlists-sources",
    ),
    "review_exclusions_changed": Event(
        label="Exclusions posées ou retirées à l'homologation",
        category=CATEGORY_LISTS, audience=("reviewer", "admin"),
        urgency=DIGEST, link="#watchlists/watchlists-review",
    ),
    "test_panel_generated": Event(
        label="Panel de pseudo-clients généré",
        category=CATEGORY_LISTS, audience=("reviewer",),
        urgency=DIGEST, link="#watchlists/watchlists-review",
    ),
    "backtest_completed": Event(
        label="Cahier de tests exécuté (écart mesuré)",
        category=CATEGORY_LISTS, audience=("reviewer", "admin"),
        # Urgence dynamique : IMMEDIATE quand le verdict est WARN (ecart eleve),
        # DIGEST sinon — arbitre par l'appelant via urgency_override
        urgency=DIGEST, link="#watchlists/watchlists-review",
    ),
    "whitelist_bulk_created": Event(
        label="Good Guys mis en liste blanche en masse",
        category=CATEGORY_LISTS, audience=("reviewer",),
        urgency=DIGEST, link="#alerts/alerts-whitelist",
    ),
    "rescreen_completed": Event(
        label="Re-criblage post-delta terminé",
        category=CATEGORY_LISTS, audience=("reviewer", "admin"),
        urgency=DIGEST, link="#alerts/alerts-screening",
    ),

    # ------------------ CRIBLAGE CLIENTS ------------------
    "alert_created": Event(
        label="Nouvelle alerte créée",
        category=CATEGORY_SCREENING, audience=("user", "reviewer"),
        urgency=DIGEST, default_enabled=False,      # volume le plus eleve : opt-in
        link="#alerts/alerts-screening",
    ),
    "alert_volume": Event(
        label="Volumétrie d'alertes sur un même criblage",
        category=CATEGORY_SCREENING, audience=("reviewer", "admin"),
        urgency=IMMEDIATE,
        # Un homonyme d'un nom tres courant sans contexte discriminant peut
        # ouvrir des milliers d'alertes d'un coup. Elles sont TOUTES creees
        # (exigence d'audit), mais les notifier une par une serait un incident
        # en soi : au-dela du seuil, une seule notification les resume.
        link="#alerts/alerts-screening",
    ),
    "alert_assigned": Event(
        label="Alerte assignée à un analyste",
        category=CATEGORY_SCREENING, audience=(AUDIENCE_ASSIGNEE,),
        urgency=IMMEDIATE, link="#alerts/alerts-screening",
    ),
    "alert_escalated": Event(
        label="Alerte escaladée",
        category=CATEGORY_SCREENING, audience=("reviewer", "admin"),
        urgency=IMMEDIATE, link="#alerts/alerts-screening",
    ),
    "alert_pending_validation": Event(
        label="Décision d'alerte en attente de validation 4-yeux",
        category=CATEGORY_SCREENING, audience=("reviewer",),
        urgency=IMMEDIATE, default_enabled=False,   # defaut historique conserve
        link="#alerts/alerts-screening",
    ),
    "alert_decision_validated": Event(
        label="Décision d'alerte validée (clôture)",
        category=CATEGORY_SCREENING, audience=(AUDIENCE_ACTOR,),
        urgency=IMMEDIATE, link="#alerts/alerts-screening",
    ),
    "alert_decision_returned": Event(
        label="Proposition de décision renvoyée en instruction",
        category=CATEGORY_SCREENING, audience=(AUDIENCE_ACTOR,),
        urgency=IMMEDIATE, link="#alerts/alerts-screening",
    ),
    "alert_closed_direct": Event(
        label="Alerte clôturée sans validation 4-yeux",
        category=CATEGORY_SCREENING, audience=("reviewer",),
        urgency=DIGEST, link="#alerts/alerts-screening",
    ),
    "alert_overdue_sla": Event(
        label="Échéance SLA dépassée sur une alerte",
        category=CATEGORY_SCREENING, audience=(AUDIENCE_ASSIGNEE,),
        urgency=DIGEST, link="#alerts/alerts-screening",
    ),
    "whitelist_changed": Event(
        label="Liste blanche : paire créée ou révoquée",
        category=CATEGORY_SCREENING, audience=("reviewer",),
        urgency=DIGEST, link="#alerts/alerts-whitelist",
    ),
    "whitelist_expiring": Event(
        label="Liste blanche : paire arrivant à échéance de revue",
        category=CATEGORY_SCREENING, audience=("reviewer",),
        urgency=DIGEST, link="#alerts/alerts-whitelist",
    ),
    "fprule_submitted": Event(
        label="Règle anti-faux positifs soumise à validation",
        category=CATEGORY_SCREENING, audience=("rules", "admin"),
        urgency=IMMEDIATE, link="#alerts/alerts-rules",
    ),
    "fprule_activated": Event(
        label="Règle anti-faux positifs mise en production",
        category=CATEGORY_SCREENING, audience=("rules", "admin"),
        urgency=IMMEDIATE, link="#alerts/alerts-rules",
    ),
    "fprule_rejected": Event(
        label="Règle anti-faux positifs renvoyée en brouillon",
        category=CATEGORY_SCREENING, audience=(AUDIENCE_ACTOR, "rules"),
        urgency=IMMEDIATE, link="#alerts/alerts-rules",
    ),

    # ------------------ FILTRAGE TRANSACTIONNEL ------------------
    "filtering_hit": Event(
        label="Message de paiement en HIT au filtrage",
        category=CATEGORY_FILTERING, audience=("user", "reviewer"),
        urgency=DIGEST, default_enabled=False,      # volume eleve : opt-in
        link="#alerts/alerts-filtering",
    ),
    "batch_campaign_done": Event(
        label="Campagne de criblage batch terminée",
        category=CATEGORY_FILTERING, audience=(AUDIENCE_ACTOR, "admin"),
        urgency=IMMEDIATE, link="#batch",
    ),
    "batch_campaign_failed": Event(
        label="Campagne de criblage batch en échec",
        category=CATEGORY_FILTERING, audience=(AUDIENCE_ACTOR, "admin"),
        urgency=IMMEDIATE, link="#batch",
    ),
    "inbox_file_rejected": Event(
        label="Fichier de l'inbox CFT refusé",
        category=CATEGORY_FILTERING, audience=("admin",),
        urgency=IMMEDIATE, link="#batch",
    ),

    # ------------------ GOUVERNANCE & EXPLOITATION ------------------
    "kpi_digest": Event(
        label="Synthèse conformité périodique",
        category=CATEGORY_GOVERNANCE, audience=("admin", "reviewer"),
        urgency=IMMEDIATE,   # pilote par son propre cron (notifications.digest)
        link="#kpi",
    ),
    "retention_purge_done": Event(
        label="Purge de rétention exécutée",
        category=CATEGORY_GOVERNANCE, audience=("admin",),
        urgency=DIGEST, link="#settings",
    ),
    "resource_mining": Event(
        # Une passe qui applique des equivalences elargit le perimetre des
        # alertes : le responsable doit l'apprendre sans avoir a ouvrir l'ecran
        label="Nouveaux homonymes découverts",
        category=CATEGORY_GOVERNANCE, audience=("admin",),
        urgency=DIGEST, link="#alerts",
    ),
    "client_quality_low": Event(
        label="Qualité des données clients sous le seuil",
        category=CATEGORY_GOVERNANCE, audience=("admin", "reviewer"),
        # Un referentiel incomplet degrade la precision du criblage : l'alerte
        # part des l'import qui fait passer le score sous le seuil configure
        urgency=IMMEDIATE, link="#kpi",
    ),
}


def event_categories() -> Dict[str, str]:
    """Libelles des categories, dans l'ordre d'affichage."""
    return dict(CATEGORY_LABELS)


def catalog_payload() -> list:
    """Catalogue serialisable pour l'API et l'ecran de reglages du dashboard."""
    return [
        {
            "key": key,
            "label": event.label,
            "category": event.category,
            "category_label": CATEGORY_LABELS.get(event.category, event.category),
            "audience": list(event.audience),
            "urgency": event.urgency,
            "default_enabled": event.default_enabled,
            "link": event.link,
        }
        for key, event in EVENT_CATALOG.items()
    ]

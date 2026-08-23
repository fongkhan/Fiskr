"""
Mise en service : ce que cette installation-ci a encore a regler.

Une installation neuve demarre muette. Rien ne dit qu'aucune liste n'est en
production — donc que le criblage ne trouvera jamais rien —, que le demon
travailleur est absent, ou que les secrets sont restes ceux du code source.
Ces trois etats se lisaient dans un WARNING du demarrage, dans un journal que
personne n'ouvre, ou nulle part.

Ce module ne recopie AUCUN etat : il interroge la meme source que l'ecran qui
en depend — la base pour les listes, la file pour le demon, `fiskr.config` pour
les secrets, le dernier `init_db` pour les index. Un controle qui deviendrait
faux le deviendrait donc en meme temps que l'ecran qu'il decrit.

Trois niveaux, et la difference compte :

* `BLOQUANT`   — le produit ne peut pas faire son travail, ou une porte est
                 ouverte. Le bandeau reste tant qu'il en subsiste un.
* `ATTENTION`  — ca fonctionne, mais quelque chose se paiera plus tard
                 (repli SQLite, index differes, notifications muettes).
* `A_FAIRE`    — une etape normale de mise en route, pas un defaut.
* `OK`         — verifie a l'instant.

Ce que ce module ne peut PAS voir est dit franchement plutot que suppose : il
constate qu'un serveur SMTP est configure, jamais qu'il repond. Repondre
demande d'ouvrir une connexion, ce qu'une page de statut n'a pas le droit de
faire — d'ou la sonde explicite `sonder_smtp`, declenchee par un humain.
"""
import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger("fiskr.mise_en_service")

BLOQUANT = "BLOQUANT"
ATTENTION = "ATTENTION"
A_FAIRE = "A_FAIRE"
OK = "OK"

# Ordre d'affichage : on ne demande pas de charger une liste a quelqu'un dont
# le demon est mort. Les familles suivent l'ordre dans lequel on installe.
FAMILLES = ("Socle", "Listes", "Criblage", "Exploitation")


def _controle(cle, famille, titre, etat, constat, remede="", lien="") -> Dict[str, Any]:
    return {"cle": cle, "famille": famille, "titre": titre, "etat": etat,
            "constat": constat, "remede": remede, "lien": lien}


# ------------------------------------------------------------------ Socle

def _secrets() -> Dict[str, Any]:
    from fiskr import config as cfg
    defauts = []
    if getattr(cfg, "INSECURE_DEFAULT_SECRET_KEY", False):
        defauts.append("SECRET_KEY")
    if getattr(cfg, "INSECURE_DEFAULT_ADMIN_PASSWORD", False):
        defauts.append("ADMIN_PASSWORD")
    if defauts:
        return _controle(
            "secrets", "Socle", "Secrets de l'application", BLOQUANT,
            f"{' et '.join(defauts)} {'sont restés' if len(defauts) > 1 else 'est resté'} "
            f"à la valeur du code source. Une clé de signature publique permet de "
            f"forger un jeton de session administrateur.",
            "Définissez-les dans le fichier .env à la racine, puis redémarrez "
            "l'application. Un modèle est fourni dans .env.example.")
    return _controle("secrets", "Socle", "Secrets de l'application", OK,
                     "SECRET_KEY et ADMIN_PASSWORD sont définis hors du code source.")


def _base_de_donnees(db) -> Dict[str, Any]:
    moteur = db.bind.dialect.name if db.bind is not None else "inconnu"
    if moteur == "postgresql":
        return _controle("base", "Socle", "Base de données", OK,
                         "PostgreSQL — la base cible de production.")
    return _controle(
        "base", "Socle", "Base de données", ATTENTION,
        f"Repli sur {moteur}. La connexion PostgreSQL n'a pas abouti au démarrage.",
        "SQLite convient au développement et aux tests. En production, elle ne "
        "tient ni la volumétrie des listes ni les écritures concurrentes du "
        "démon travailleur : renseignez DATABASE_URL (ou DB_HOST/DB_USER/…) "
        "dans .env, puis redémarrez.")


def _demon(db) -> Dict[str, Any]:
    from fiskr import jobs as file_de_travaux
    mode = file_de_travaux.jobs_mode()
    if mode != "worker":
        return _controle(
            "demon", "Socle", "Démon travailleur", ATTENTION,
            f"Mode « {mode} » : les travaux longs s'exécutent dans le processus web.",
            "Le mode `worker` est celui de la production (jobs.mode dans "
            "config.yaml). En `thread`, une synchronisation lourde ralentit "
            "l'application ; en `eager`, elle la bloque.",
            "#settings/settings-integrations")
    from fiskr.database import Job
    from datetime import datetime, timedelta
    battement = db.query(Job).filter(Job.heartbeat_at.isnot(None)).order_by(
        Job.heartbeat_at.desc()).first()
    vivant = bool(battement and battement.heartbeat_at
                  and battement.heartbeat_at > datetime.utcnow() - timedelta(seconds=180))
    if vivant:
        return _controle("demon", "Socle", "Démon travailleur", OK,
                         "Le démon bat ; les travaux de fond sont pris en charge.")
    return _controle(
        "demon", "Socle", "Démon travailleur", BLOQUANT,
        "Mode `worker` exigé, mais aucun battement de cœur récent. Rien ne "
        "s'exécutera : synchronisations, imports, cahiers de tests et mises en "
        "production resteront en file.",
        "Lancez le démon (python -m fiskr.worker) ou laissez l'API le relancer "
        "(jobs.autostart). Vérifiez ensuite l'écran de diagnostic.")


def _index_de_performance() -> Dict[str, Any]:
    from fiskr.database import index_de_performance_manquants
    differes = index_de_performance_manquants()
    if not differes:
        return _controle("index", "Socle", "Index de performance", OK,
                         "Tous les index de performance sont en place.")
    noms = ", ".join(n for n, _ in differes)
    return _controle(
        "index", "Socle", "Index de performance", ATTENTION,
        f"{len(differes)} index non créés au démarrage : {noms}.",
        "Ce n'est pas un oubli : un CREATE INDEX ordinaire verrouille la table "
        "pendant toute sa construction et figerait le service plusieurs "
        "minutes. Lancez, service allumé : python tools/create_perf_indexes.py "
        "(construction CONCURRENTLY, sans interruption).")


# ------------------------------------------------------------------ Listes

def _listes_en_production(db) -> Dict[str, Any]:
    from fiskr.database import Snapshot, WATCHLIST_FILE_TYPES
    pretes = db.query(Snapshot).filter(
        Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
        Snapshot.status == "READY").count()
    if pretes:
        return _controle("listes", "Listes", "Listes en production", OK,
                         f"{pretes} liste(s) en production.",
                         lien="#watchlist-mgmt/watchlist-active")
    return _controle(
        "listes", "Listes", "Listes en production", BLOQUANT,
        "Aucune liste en production. Tout criblage répondra « aucune "
        "correspondance » — non pas parce qu'il n'y en a pas, mais parce qu'il "
        "n'y a rien à comparer. C'est l'état le plus dangereux du produit, et "
        "il ne se signale pas de lui-même.",
        "Récupérez une source officielle (écran Sources), ou téléversez un "
        "fichier (écran Imports), puis homologuez le lot.",
        "#watchlist-mgmt/watchlist-sync")


def _sources_automatiques(db) -> Dict[str, Any]:
    try:
        from fiskr.sync import get_sync_config
        cfg = get_sync_config(db)
    except Exception as e:
        logger.debug(f"Configuration de synchronisation illisible : {e}")
        return _controle("sources", "Listes", "Récupération automatique", ATTENTION,
                         "Configuration de synchronisation illisible.")
    if not cfg.get("auto_enabled"):
        return _controle(
            "sources", "Listes", "Récupération automatique", A_FAIRE,
            "La synchronisation automatique est coupée : les listes ne se "
            "rafraîchiront pas seules.",
            "Le gel des avoirs s'applique dès publication : un référentiel en "
            "retard d'un jour est un défaut de conformité. Activez-la et "
            "choisissez les sources.",
            "#watchlist-mgmt/watchlist-sync")
    return _controle("sources", "Listes", "Récupération automatique", OK,
                     "La récupération planifiée est active.",
                     lien="#watchlist-mgmt/watchlist-sync")


def _homologation(db) -> Dict[str, Any]:
    from fiskr.settings import get_setting, SETTING_REQUIRE_APPROVAL
    exige = get_setting(db, SETTING_REQUIRE_APPROVAL, True)
    if exige:
        return _controle("homologation", "Listes", "Homologation des lots", OK,
                         "Un lot ingéré passe par une revue avant production.",
                         lien="#watchlist-mgmt/watchlist-review")
    return _controle(
        "homologation", "Listes", "Homologation des lots", ATTENTION,
        "L'homologation est désactivée : imports et synchronisations passent "
        "directement en production, sans delta ni cahier de tests.",
        "Acceptable en recette, rarement en production : plus rien ne s'oppose "
        "à ce qu'une source défaillante remplace un référentiel opposable.",
        "#settings/settings-governance")


# ---------------------------------------------------------------- Criblage

def _referentiel_clients(db) -> Dict[str, Any]:
    from fiskr.database import ClientEntity
    n = db.query(ClientEntity).count()
    if n:
        return _controle("clients", "Criblage", "Référentiel clients", OK,
                         f"{n} fiche(s) client en base.",
                         lien="#screening/screening-realtime")
    return _controle(
        "clients", "Criblage", "Référentiel clients", A_FAIRE,
        "Aucune fiche client. Le criblage unitaire et le filtrage des paiements "
        "fonctionnent sans, mais il n'y a rien à cribler en masse.",
        "Importez un fichier CSV de clients (colonnes CLIENT_BASE), ou branchez "
        "le dépôt CFT surveillé.",
        "#watchlist-mgmt/watchlist-import")


def _seuils(db) -> Dict[str, Any]:
    from fiskr.settings import score_thresholds
    seuils = score_thresholds(db)
    origine = seuils.get("source")
    coupure = seuils.get("cut_off_threshold")
    if origine == "config":
        return _controle(
            "seuils", "Criblage", "Seuils de score", A_FAIRE,
            f"Seuil de coupure à {coupure} — la valeur livrée, jamais revue sur "
            f"cette installation.",
            "Un seuil se calibre sur SON univers : un portefeuille et des listes "
            "donnent un taux de faux positifs qui n'appartient qu'à eux. "
            "Mesurez avant d'arrêter une valeur (cahier de tests sur panel).",
            "#screening/alerts-blocking")
    return _controle("seuils", "Criblage", "Seuils de score", OK,
                     f"Seuil de coupure réglé à {coupure} depuis l'application.",
                     lien="#screening/alerts-blocking")


# ------------------------------------------------------------ Exploitation

def _smtp() -> Dict[str, Any]:
    hote = (os.getenv("SMTP_HOST") or "").strip()
    if not hote:
        return _controle(
            "smtp", "Exploitation", "Envoi des courriels", ATTENTION,
            "Aucun serveur SMTP configuré. Les rapports de synchronisation et "
            "les notifications d'étape ne partiront pas — y compris « Échec de "
            "synchronisation d'une source ».",
            "Renseignez SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD et "
            "SMTP_FROM dans .env. Le suivi dans l'application continue de "
            "fonctionner sans, mais personne n'est prévenu.")
    return _controle(
        "smtp", "Exploitation", "Envoi des courriels", OK,
        f"Serveur configuré ({hote}). Configuré ne veut pas dire joignable : "
        f"utilisez la sonde pour ouvrir une vraie connexion.")


def _comptes(db) -> Dict[str, Any]:
    from fiskr.database import User
    total = db.query(User).count()
    if total > 1:
        return _controle("comptes", "Exploitation", "Comptes utilisateurs", OK,
                         f"{total} comptes.", lien="#users")
    return _controle(
        "comptes", "Exploitation", "Comptes utilisateurs", A_FAIRE,
        "Le compte administrateur est le seul. La validation à quatre yeux "
        "suppose deux personnes distinctes : elle est inopérante à un seul "
        "compte.",
        "Créez les comptes analystes et réviseurs.", "#users")


def _url_publique() -> Dict[str, Any]:
    if (os.getenv("FISKR_PUBLIC_URL") or "").strip():
        return _controle("url", "Exploitation", "URL publique", OK,
                         "Les courriels porteront un lien direct vers l'écran concerné.")
    return _controle(
        "url", "Exploitation", "URL publique", A_FAIRE,
        "FISKR_PUBLIC_URL n'est pas renseignée : les courriels partent sans le "
        "bouton « Ouvrir dans Fiskr ».",
        "Renseignez l'adresse publique de l'application dans .env.")


_CONTROLES_BASE = (_base_de_donnees, _demon, _listes_en_production,
                   _sources_automatiques, _homologation, _referentiel_clients,
                   _seuils, _comptes)
_CONTROLES_SANS_BASE = (_secrets, _index_de_performance, _smtp, _url_publique)


def etat_de_mise_en_service(db) -> Dict[str, Any]:
    """
    Releve complet. Un controle qui echoue n'interrompt jamais les autres : un
    ecran de mise en service qui tombe en panne parce qu'un point est illisible
    serait la pire des reponses.
    """
    controles: List[Dict[str, Any]] = []
    for fonction in _CONTROLES_SANS_BASE:
        try:
            controles.append(fonction())
        except Exception as e:
            logger.warning(f"Contrôle {fonction.__name__} illisible : {e}")
    for fonction in _CONTROLES_BASE:
        try:
            controles.append(fonction(db))
        except Exception as e:
            logger.warning(f"Contrôle {fonction.__name__} illisible : {e}")

    rang = {f: i for i, f in enumerate(FAMILLES)}
    controles.sort(key=lambda c: rang.get(c["famille"], len(FAMILLES)))
    bloquants = [c for c in controles if c["etat"] == BLOQUANT]
    return {
        "controles": controles,
        "familles": list(FAMILLES),
        "bloquants": len(bloquants),
        "a_traiter": sum(1 for c in controles if c["etat"] in (BLOQUANT, ATTENTION, A_FAIRE)),
        # Premiere mise en route : aucune liste et aucun client. C'est l'etat
        # d'une installation qui vient de demarrer, pas un defaut.
        "premier_demarrage": _premier_demarrage(db),
    }


def _premier_demarrage(db) -> bool:
    from fiskr.database import ClientEntity, Snapshot, WATCHLIST_FILE_TYPES
    try:
        listes = db.query(Snapshot).filter(
            Snapshot.file_type.in_(WATCHLIST_FILE_TYPES)).limit(1).count()
        clients = db.query(ClientEntity).limit(1).count()
        return listes == 0 and clients == 0
    except Exception as e:
        logger.warning(f"Détection de premier démarrage impossible : {e}")
        return False


def sonder_smtp(timeout: float = 8.0) -> Dict[str, Any]:
    """
    Ouvre une VRAIE connexion au serveur configure. « Configure » et
    « joignable » sont deux choses differentes : constate en production, un
    SMTP declare correctement dont chaque envoi tombait en timeout, pendant que
    l'application se croyait capable de prevenir.

    Bornee par un timeout court : une sonde qui pend serait une panne de plus.
    """
    import smtplib
    import socket

    hote = (os.getenv("SMTP_HOST") or "").strip()
    if not hote:
        return {"ok": False, "detail": "Aucun serveur SMTP configuré (SMTP_HOST)."}
    port = int((os.getenv("SMTP_PORT") or "587").strip() or 587)
    try:
        if port == 465:
            serveur = smtplib.SMTP_SSL(hote, port, timeout=timeout)
        else:
            serveur = smtplib.SMTP(hote, port, timeout=timeout)
            try:
                serveur.starttls()
            except Exception:
                pass  # serveur en clair : ce n'est pas l'objet de la sonde
        with serveur:
            serveur.ehlo()
            utilisateur = (os.getenv("SMTP_USER") or "").strip()
            motdepasse = os.getenv("SMTP_PASSWORD") or ""
            if utilisateur and motdepasse:
                serveur.login(utilisateur, motdepasse)
                return {"ok": True, "detail": f"Connexion et authentification réussies sur {hote}:{port}."}
            return {"ok": True, "detail": f"Connexion réussie sur {hote}:{port} (aucun identifiant à vérifier)."}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "detail": (
            f"Délai dépassé sur {hote}:{port} après {timeout:.0f} s. Le serveur "
            f"n'a pas répondu : aucun courriel ne partira.")}
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "detail": f"Identifiants refusés par {hote} : {e.smtp_code}."}
    except Exception as e:
        return {"ok": False, "detail": f"Échec sur {hote}:{port} — {type(e).__name__} : {e}"}

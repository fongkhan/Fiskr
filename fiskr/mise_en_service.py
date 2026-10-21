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


def _controle(cle, famille, titre, etat, constat, remede="", lien="",
              constat_public=None) -> Dict[str, Any]:
    """
    `constat_public` : la version du constat que la sonde de conformite peut
    rendre a un outil de supervision externe, quand le constat de l'ecran
    nomme des personnes. Une supervision tierce n'a pas a recevoir le nom d'un
    client sous mesure de gel.
    """
    return {"cle": cle, "famille": famille, "titre": titre, "etat": etat,
            "constat": constat, "remede": remede, "lien": lien,
            "constat_public": constat_public}


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


# Marge accordee au travail periodique par-dessus sa propre cadence : une
# passe planifiee a 4 h peut demarrer a 4 h 20 sans que rien n'aille mal.
# Elle s'ajoute a la periode DEDUITE des expressions cron des sources
# activees, plutot que de la remplacer par un chiffre en dur — une source
# hebdomadaire ne doit pas etre declaree en retard le lendemain.
_MARGE_TRAVAIL_PERIODIQUE_H = 2

# Plancher de la fenetre, quand la cadence deduite est plus serree que cela :
# un ecran de mise en service ne doit pas passer au rouge parce qu'une source
# horaire a saute un tour.
_FENETRE_PERIODIQUE_MINIMALE_H = 26


def _cadence_periodique_attendue(db):
    """
    (nombre de sources planifiees, fenetre au-dela de laquelle le travail
    periodique a manque son rendez-vous) — ou (0, None) si RIEN n'est
    planifie.

    La fenetre est DEDUITE des expressions cron des sources activees : on
    prend la plus frequente, puisque c'est elle qui donnera signe de vie en
    premier. Un chiffre en dur aurait declare en retard une installation qui
    ne synchronise que le lundi.
    """
    from datetime import timedelta
    from fiskr.cron import next_run, CronError
    from fiskr.settings import sync_schedules
    from fiskr.sync import get_sync_config

    cfg = get_sync_config(db)
    if not cfg.get("auto_enabled"):
        return 0, None
    periodes, planifiees = [], 0
    for source, expr in (sync_schedules(db) or {}).items():
        if not (cfg.get(source) or {}).get("enabled"):
            continue
        planifiees += 1
        try:
            premier = next_run(expr)
            second = next_run(expr, after=premier) if premier else None
        except (CronError, ValueError, TypeError):
            continue  # expression illisible : elle est signalee ailleurs
        if premier and second:
            periodes.append(second - premier)
    if not planifiees:
        return 0, None
    if not periodes:
        return planifiees, timedelta(hours=_FENETRE_PERIODIQUE_MINIMALE_H)
    fenetre = min(periodes) + timedelta(hours=_MARGE_TRAVAIL_PERIODIQUE_H)
    return planifiees, max(fenetre, timedelta(hours=_FENETRE_PERIODIQUE_MINIMALE_H))


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
        # Un demon qui bat peut porter un travail qui n'avance plus : le
        # battement est rafraichi pour tous ses jobs par un fil independant.
        # Releve en production : « Le demon bat » affiche ici pendant qu'une
        # synchronisation PEP etait figee depuis 26 jours.
        from fiskr.jobs import stall_after
        seuil = datetime.utcnow() - stall_after()
        figes = [j for j in db.query(Job).filter(Job.status == "RUNNING").all()
                 if (j.progress_at or j.started_at) and (j.progress_at or j.started_at) < seuil]
        if figes:
            pire = min(figes, key=lambda j: j.progress_at or j.started_at)
            depuis = datetime.utcnow() - (pire.progress_at or pire.started_at)
            return _controle(
                "demon", "Socle", "Démon travailleur", BLOQUANT,
                f"Le démon bat, mais {len(figes)} travail(aux) n'avancent plus : "
                f"« {pire.label or pire.token} » sans progression depuis "
                f"{int(depuis.total_seconds() // 3600)} h (phase {pire.phase or '?'}). "
                f"Tant qu'il reste « en cours », il bloque toute relance de la même "
                f"tâche et occupe un emplacement du démon.",
                "Un démon à jour déclare ce travail figé et se relance de lui-même. "
                "Si ce constat persiste, le démon tourne sur un code antérieur : "
                "redémarrez-le (python -m fiskr.worker) après avoir mis le code à jour.",
                "#settings/settings-integrations")
        return _controle("demon", "Socle", "Démon travailleur", OK,
                         "Le démon bat ; les travaux de fond sont pris en charge.")

    # Pas de battement. Le verdict se prend sur les CONSEQUENCES, pas sur le
    # pouls : sur un hebergement mutualise, le demon travaille sa fenetre puis
    # s'eteint, et l'hote recupere le processus. Mesure sur l'installation de
    # production : controle BLOQUANT en permanence, pendant que les 41 sources
    # tournaient chaque nuit et que les listes avaient moins d'un jour d'age.
    # Un ecran qui crie au loup vingt-trois heures sur vingt-quatre apprend a
    # ignorer le seul endroit cense dire la verite.
    en_file = db.query(Job).filter(Job.status.in_(("QUEUED", "RUNNING"))).count()
    if en_file:
        return _controle(
            "demon", "Socle", "Démon travailleur", BLOQUANT,
            f"Aucun battement de cœur, et {en_file} travail(aux) en file : "
            "personne ne les prend. Synchronisations, imports, cahiers de "
            "tests et mises en production restent en attente.",
            "Lancez le démon (python -m fiskr.worker) ou laissez l'API le "
            "relancer (jobs.autostart). Vérifiez ensuite l'écran de diagnostic.",
            "#settings/settings-integrations")

    # Rien n'attend. Un demon qui s'eteint quand il n'a rien a traiter n'est
    # pas en panne : c'est le comportement attendu sur un hebergement
    # mutualise, et le battement de coeur n'est donc PAS le juge.
    #
    # Reste la seule question qui compte, et elle a un piege : le demon
    # heberge les planificateurs. S'il est mort, plus rien ne s'inscrit en
    # file — « file vide » est exactement ce que produit un planificateur
    # eteint. Le temoin ne peut donc pas etre la file : c'est la derniere
    # synchronisation PLANIFIEE, qui ne peut exister que si un planificateur
    # a tourne pour la soumettre.
    planifiees, fenetre = _cadence_periodique_attendue(db)
    if not planifiees:
        return _controle(
            "demon", "Socle", "Démon travailleur", OK,
            "Le démon ne bat pas, mais rien n'attend en file et aucune "
            "synchronisation automatique n'est programmée : il n'y a rien à "
            "traiter, et un démon sans travail a le droit d'être éteint.")

    from fiskr.database import SyncReport
    dernier_passage = db.query(SyncReport).filter(
        SyncReport.trigger == "SCHEDULED").order_by(
        SyncReport.executed_at.desc()).first()
    depuis = None
    if dernier_passage and dernier_passage.executed_at:
        depuis = datetime.utcnow() - dernier_passage.executed_at
    heures_fenetre = int(fenetre.total_seconds() // 3600)
    if depuis is not None and depuis < fenetre:
        heures = int(depuis.total_seconds() // 3600)
        return _controle(
            "demon", "Socle", "Démon travailleur", OK,
            f"Le démon ne bat pas en ce moment, mais rien n'attend en file et "
            f"la dernière synchronisation planifiée remonte à {heures} h : les "
            f"planificateurs ont bien tourné. Il travaille sa fenêtre, puis "
            f"l'hôte récupère le processus — c'est le fonctionnement normal "
            f"d'un hébergement mutualisé.")
    jamais = ("aucune synchronisation planifiée n'a jamais eu lieu" if depuis is None
              else f"la dernière remonte à plus de {heures_fenetre} h")
    return _controle(
        "demon", "Socle", "Démon travailleur", BLOQUANT,
        f"{planifiees} source(s) sont programmées et {jamais}. Ce n'est pas le "
        "démon éteint qui pose problème — c'est que le travail périodique "
        "n'a plus lieu : synchronisations, re-criblages, récapitulatifs.",
        "Lancez le démon (python -m fiskr.worker) ou laissez l'API le relancer "
        "(jobs.autostart) : c'est lui qui héberge les planificateurs. "
        "Vérifiez ensuite l'écran de diagnostic.",
        "#settings/settings-integrations")


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
    """
    Compter les listes ne suffit pas : une liste en production SANS FICHE se
    compte comme les autres et ne protege de personne. Releve en production :
    la liste canadienne y etait vide depuis deux mois, comptee parmi « 39
    listes en production », pendant que sa synchronisation rapportait chaque
    nuit un « contenu identique a la liste active » — zero compare a zero.
    """
    from fiskr.database import Snapshot, WATCHLIST_FILE_TYPES
    from fiskr.fraicheur import mesurer
    pretes = db.query(Snapshot).filter(
        Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
        Snapshot.status == "READY").count()
    vides = mesurer(db)["listes_vides"] if pretes else []
    if pretes and vides:
        return _controle(
            "listes", "Listes", "Listes en production", ATTENTION,
            f"{pretes} liste(s) en production, dont {len(vides)} sans aucune "
            f"fiche : {', '.join(vides)}. Une liste vide ne crible rien, et "
            f"rien d'autre ne le signale — le criblage contre elle répond "
            f"« aucune correspondance ».",
            "Ouvrez la source concernée : un lecteur qui ne reconnaît plus le "
            "format publié rend une liste vide sans lever d'erreur. Rétablissez "
            "la source, ou retirez la liste de la production.",
            "#watchlist-mgmt/watchlist-active")
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


def _sources_de_reference(db) -> Dict[str, Any]:
    """
    Une liste de sanctions en production dont la source de reference ne
    tourne pas — celle qui la REMPLACE a chaque passage (fiskr/sync.py,
    `sources_de_reference`).

    Releve en production le 19 octobre 2026 : la liste UE comptait 320 fiches,
    toutes extraites du Journal Officiel par EUR-Lex. La liste consolidee
    (eu_fsf), qui fait autorite et en porte plusieurs milliers, etait coupee
    faute de jeton. L'ecran disait « 39 liste(s) en production » : il comptait
    la liste UE sans dire qu'elle n'etait pas celle que son nom annonce.

    Deux etats, qui n'ont pas la meme gravite :
    - source NON CONFIGUREE (jeton absent, aucune adresse) : elle ne peut pas
      tourner, pas meme a la main. La liste ne sera jamais celle de son
      autorite — BLOQUANT ;
    - source DESACTIVEE des passages planifies : la liste se fige sur sa
      derniere version, et le controle de fraicheur ne le voit pas, faute de
      version plus recente en attente — ATTENTION. Sans objet quand toute la
      recuperation automatique est coupee : « Récupération automatique » le dit.

    Seules les listes du perimetre SANCTION sont nommees : une liste de
    vigilance (PEP, regulateurs) figee ne porte pas d'obligation de gel, et un
    signal qui les nommerait toutes crierait au loup.
    """
    from fiskr.database import SyncReport
    from fiskr.fraicheur import mesurer
    from fiskr.perimeters import est_sanction
    from fiskr.settings import perimeter_overrides
    from fiskr.sync import (SOURCES_COMPLEMENTAIRES, get_sync_config,
                            source_non_configuree, sources_de_reference)

    cfg = get_sync_config(db)
    reference = sources_de_reference()
    surcharges = perimeter_overrides(db)
    impossibles, figees = [], []
    for liste in mesurer(db)["listes"]:
        cle = reference.get(liste["type"])
        if not liste["production_id"] or not cle or not est_sanction(liste["type"], surcharges):
            continue
        source = cfg.get(cle) or {}
        empechement = source_non_configuree(source)
        if empechement:
            impossibles.append((liste, cle, empechement))
        elif cfg.get("auto_enabled") and not source.get("enabled"):
            figees.append((liste, cle))
    if not impossibles and not figees:
        return _controle(
            "sources_reference", "Listes", "Sources de référence", OK,
            "Chaque liste de sanctions en production a sa source de référence "
            "configurée et active.", lien="#watchlist-mgmt/watchlist-sync")

    def _nom(liste):
        return liste["type"].replace("WATCHLIST_", "")

    # Qui a produit la version en production, quand ce n'est pas la reference :
    # un fait lu au rapport de synchronisation, pas deduit de la configuration.
    origines = {}
    if impossibles:
        origines = dict(db.query(SyncReport.snapshot_id, SyncReport.source).filter(
            SyncReport.snapshot_id.in_([l["production_id"] for l, _, _ in impossibles])).all())
    constats, remedes = [], []
    for liste, cle, (champ, empechement) in impossibles:
        origine = origines.get(liste["production_id"])
        complements = [c for c, t in SOURCES_COMPLEMENTAIRES.items() if t == liste["type"]]
        provenance = (f"la version en production vient de {origine}" if origine
                      else "la version en production vient d'un import manuel")
        constats.append(
            f"{_nom(liste)} ({liste['production_fiches']} fiches) : sa source de "
            f"référence, {cle}, ne peut pas tourner ({empechement}) ; {provenance}"
            + (f" ; {', '.join(complements)} ne fait que la compléter, sans "
               f"jamais porter la liste entière ni ses radiations" if complements else "")
            + ".")
        remedes.append(f"renseignez sync.{cle}.{champ} dans config.yaml, puis activez "
                       f"la source {cle}")
    for liste, cle in figees:
        constats.append(
            f"{_nom(liste)} : sa source de référence, {cle}, est coupée des "
            f"passages planifiés — la version en production a "
            f"{liste['production_age_jours']} jour(s) et ne se renouvellera pas seule.")
    if figees:
        remedes.append("réactivez les sources coupées (écran Sources), ou retirez de "
                       "la production les listes qui ne sont plus suivies")
    etat = BLOQUANT if impossibles else ATTENTION
    return _controle(
        "sources_reference", "Listes", "Sources de référence", etat,
        f"{len(impossibles) + len(figees)} liste(s) de sanctions en production "
        f"sans leur source de référence. " + " ".join(constats)
        + " Le contrôle « Listes en production » les compte pourtant comme les autres.",
        "Pour chacune : " + " ; ".join(remedes) + ". Le gel des avoirs s'applique "
        "à la liste de l'autorité, pas à ce qu'il en reste.",
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


# Retard tolere entre la parution d'une version et sa mise en production :
# le temps d'une relecture, week-end compris. Au-dela d'une semaine, le
# criblage porte sur une liste qui n'existe plus sous cette forme.
_RETARD_TOLERE_H = 48
_RETARD_BLOQUANT_H = 7 * 24
SEUILS_DE_RETARD_H = {ATTENTION: _RETARD_TOLERE_H, BLOQUANT: _RETARD_BLOQUANT_H}


def niveau_de_retard(retard_heures: int) -> str:
    """
    OK / ATTENTION / BLOQUANT pour un retard donne — la seule regle. Le
    controle de fraicheur la lit, et l'evenement `fraicheur_depassee` aussi
    (fiskr/fraicheur.py) : un signal qui partirait sur d'autres seuils que
    l'ecran dirait une chose que l'ecran contredit.
    """
    return (BLOQUANT if retard_heures >= _RETARD_BLOQUANT_H
            else ATTENTION if retard_heures >= _RETARD_TOLERE_H else OK)


def _fraicheur_des_listes(db) -> Dict[str, Any]:
    """
    Depuis quand ce contre quoi on crible n'est plus la derniere version.

    Le controle « Homologation des lots » regarde le REGLAGE (une revue est-elle
    exigee ?) ; celui-ci regarde le RESULTAT (les revues ont-elles lieu ?). Le
    premier etait vert pendant que 639 lots attendaient et que toutes les
    listes avaient un mois de retard. Cf. fiskr/fraicheur.py.
    """
    from fiskr.fraicheur import mesurer, phrase_de_retard
    etat = mesurer(db)
    if not etat["listes_en_production"]:
        # Rien a dater. « Chaque liste en production est la derniere version »
        # est vrai de zero liste : c'est le vert qu'affichait une installation
        # neuve. Le controle « Listes en production » porte deja le bloquant ;
        # celui-ci ne le repete pas, il cesse seulement d'affirmer.
        return _controle(
            "fraicheur", "Listes", "Fraîcheur des listes", A_FAIRE,
            "Aucune liste en production : rien à dater.",
            "La fraîcheur se mesure dès qu'une liste est en production (voir "
            "« Listes en production »).",
            "#watchlist-mgmt/watchlist-review")
    if not etat["listes_en_retard"]:
        perimes = etat["lots_perimes"]
        return _controle(
            "fraicheur", "Listes", "Fraîcheur des listes", OK,
            "Chaque liste en production est la dernière version récupérée."
            + (f" {perimes} lot(s) périmé(s) restent en file — plus anciens "
               f"que la production, ils ne peuvent plus être approuvés."
               if perimes else ""),
            lien="#watchlist-mgmt/watchlist-review")
    retard = etat["retard_max_heures"]
    pires = sorted((l for l in etat["listes"] if l["versions_en_attente"]),
                   key=lambda l: -l["retard_heures"])[:5]
    detail = ", ".join(f"{l['type'].replace('WATCHLIST_', '')} "
                       f"({phrase_de_retard(l['retard_heures'])})" for l in pires)
    etat_du_controle = niveau_de_retard(retard)
    if etat_du_controle == OK:
        return _controle(
            "fraicheur", "Listes", "Fraîcheur des listes", OK,
            f"{etat['listes_en_retard']} liste(s) ont une version récente en "
            f"attente de relecture, depuis {phrase_de_retard(retard)} au plus.",
            lien="#watchlist-mgmt/watchlist-review")
    return _controle(
        "fraicheur", "Listes", "Fraîcheur des listes", etat_du_controle,
        f"{etat['listes_en_retard']} liste(s) sur {etat['listes_en_production']} "
        f"criblent contre une version dépassée : une plus récente attend "
        f"l'homologation, depuis {phrase_de_retard(retard)} pour la plus "
        f"ancienne. Les plus en retard : {detail}. "
        f"{etat['lots_en_attente']} lot(s) en file au total.",
        "Un gel des avoirs s'applique dès sa publication. Homologuez la "
        "dernière version de chaque liste (homologation groupée) : les lots "
        "plus anciens de la même liste sont alors retirés de la file d'eux-"
        "mêmes. Si personne ne peut relire chaque nuit, c'est le réglage "
        "d'homologation qu'il faut revoir, pas la file qu'il faut laisser monter.",
        "#watchlist-mgmt/watchlist-review")


def _homologation(db) -> Dict[str, Any]:
    # Ce que l'import APPLIQUE, pas une relecture du réglage avec un défaut à
    # soi : un « Vérifié » ne sort que de la condition que le moteur teste.
    from fiskr.settings import require_approval_enabled
    if require_approval_enabled(db):
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
    # Meme definition que « Couverture du criblage » : le referentiel EN
    # PRODUCTION, pas toute ligne de la table (cf. couverture.clients_en_production).
    from fiskr.couverture import clients_en_production
    n = clients_en_production(db)
    if n:
        return _controle("clients", "Criblage", "Référentiel clients", OK,
                         f"{n} client(s) dans le référentiel en production.",
                         lien="#screening/screening-realtime")
    return _controle(
        "clients", "Criblage", "Référentiel clients", A_FAIRE,
        "Aucun client dans le référentiel en production (les panels de cahier "
        "de tests n'en font pas partie). Le criblage unitaire et le filtrage "
        "des paiements fonctionnent sans, mais il n'y a rien à cribler en masse.",
        "Importez un fichier CSV de clients (colonnes CLIENT_BASE), ou branchez "
        "le dépôt CFT surveillé.",
        "#watchlist-mgmt/watchlist-import")


def _seuils(db) -> Dict[str, Any]:
    """
    Un seuil se calibre sur SON univers : un portefeuille et des listes donnent
    un taux de faux positifs qui n'appartient qu'à eux.

    D'où la dépendance que ce contrôle énonce désormais au lieu de la taire.
    Sans référentiel clients, il n'y a rien à mesurer — demander de calibrer
    est alors demander un travail que personne ne peut faire, et une consigne
    impossible s'ignore aussi vite qu'une alarme qui crie au loup. Le contrôle
    reste À FAIRE (le seuil livré n'a effectivement jamais été revu), mais il
    dit par quoi commencer, et son lien pointe vers ce premier geste — l'import
    — plutôt que vers l'écran des seuils où il n'y a rien à faire encore.
    """
    from fiskr.couverture import clients_en_production
    from fiskr.settings import score_thresholds
    seuils = score_thresholds(db)
    origine = seuils.get("source")
    coupure = seuils.get("cut_off_threshold")
    if origine == "config":
        if not clients_en_production(db):
            return _controle(
                "seuils", "Criblage", "Seuils de score", A_FAIRE,
                f"Seuil de coupure à {coupure} — la valeur livrée, jamais revue "
                f"sur cette installation, et rien ici ne permet encore de la "
                f"revoir : sans référentiel clients, il n'y a pas de taux de "
                f"faux positifs à mesurer.",
                "L'ordre compte : importez d'abord le référentiel clients, "
                "lancez un lookback sur les listes déjà en production, puis "
                "simulez des seuils candidats sur les décisions ainsi produites "
                "(« Simuler » sur l'écran des seuils) avant d'en arrêter un.",
                "#watchlist-mgmt/watchlist-import")
        return _controle(
            "seuils", "Criblage", "Seuils de score", A_FAIRE,
            f"Seuil de coupure à {coupure} — la valeur livrée, jamais revue sur "
            f"cette installation.",
            "Le portefeuille est là : lancez un lookback, puis simulez des "
            "seuils candidats sur les décisions produites (« Simuler » sur "
            "l'écran des seuils) avant d'en arrêter un. Un seuil se calibre sur "
            "SON univers, jamais sur une valeur reprise d'ailleurs.",
            "#screening/alerts-blocking")
    return _controle("seuils", "Criblage", "Seuils de score", OK,
                     f"Seuil de coupure réglé à {coupure} depuis l'application.",
                     lien="#screening/alerts-blocking")


# ------------------------------------------------------------ Exploitation

# Nombre d'envois récents examinés. Assez pour distinguer un incident isolé
# d'une panne installée, assez peu pour que la requête reste une lecture d'index.
_DERNIERS_ENVOIS_EXAMINES = 20


def _smtp(db) -> Dict[str, Any]:
    """
    Trois états se ressemblent et n'ont pas les mêmes conséquences : SMTP
    **configuré**, SMTP **joignable**, courriels **réellement partis**.

    Ce contrôle disait le premier et s'arrêtait là, en signalant honnêtement
    que « configuré ne veut pas dire joignable » — mais le produit CONNAÎT la
    réponse au troisième : chaque notification laisse une ligne dans son
    journal, avec son statut et son erreur. Elle n'était lue par personne.

    Constaté sur une installation réelle : toutes les notifications échouaient
    depuis des jours (« Connection unexpectedly closed: timed out »), l'écran
    affichait « Serveur configuré », et l'exploitant l'a appris autrement.
    """
    hote = (os.getenv("SMTP_HOST") or "").strip()
    if not hote:
        return _controle(
            "smtp", "Exploitation", "Envoi des courriels", ATTENTION,
            "Aucun serveur SMTP configuré. Les rapports de synchronisation et "
            "les notifications d'étape ne partiront pas — y compris « Échec de "
            "synchronisation d'une source ».",
            "Renseignez SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD et "
            "SMTP_FROM dans .env. Le suivi dans l'application continue de "
            "fonctionner sans, mais personne n'est prévenu.",
            "#settings/settings-integrations")

    from fiskr.database import NotificationDelivery
    recents = db.query(NotificationDelivery).order_by(
        NotificationDelivery.created_at.desc(),
        NotificationDelivery.id.desc()).limit(_DERNIERS_ENVOIS_EXAMINES).all()
    tentatives = [r for r in recents if r.status in ("SENT", "FAILED")]
    echecs = [r for r in tentatives if r.status == "FAILED"]

    if not tentatives:
        return _controle(
            "smtp", "Exploitation", "Envoi des courriels", OK,
            f"Serveur configuré ({hote}). Aucun envoi encore tenté : configuré "
            f"ne veut pas dire joignable — utilisez la sonde pour ouvrir une "
            f"vraie connexion.",
            lien="#settings/settings-integrations")

    if not echecs:
        return _controle(
            "smtp", "Exploitation", "Envoi des courriels", OK,
            f"Serveur configuré ({hote}) et {len(tentatives)} envoi(s) récent(s) "
            f"aboutis.",
            lien="#settings/settings-integrations")

    motif = (echecs[0].error or "").strip().splitlines()[0][:160] if echecs[0].error else "sans détail"
    tous = len(echecs) == len(tentatives)
    return _controle(
        "smtp", "Exploitation", "Envoi des courriels", ATTENTION,
        f"{len(echecs)} échec(s) sur les {len(tentatives)} derniers envois"
        + (" — AUCUN ne part" if tous else "") + f". Dernière erreur : {motif}. "
        f"Le criblage et les alertes continuent ; ce qui manque, c'est le fait "
        f"que quelqu'un soit prévenu — y compris pour un échec de "
        f"synchronisation de source.",
        "Vérifiez SMTP_HOST/PORT/USER/PASSWORD dans .env, puis lancez la sonde "
        "pour ouvrir une vraie connexion. Le journal des envois donne l'erreur "
        "exacte de chaque tentative.",
        "#settings/settings-integrations")


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


def _conservation(db) -> Dict[str, Any]:
    """
    La durée de conservation des pièces probantes, confrontée à ce que la loi
    exige — pas au garde-fou technique de 30 jours, qui empêche seulement de
    vider la base par inadvertance.

    Le contrôle se pose ici et pas seulement à l'instant du réglage : une
    politique se change un jour et se vit des années.

    La purge applique désormais elle-même le plancher (fiskr/conservation.py :
    cinq années civiles depuis la fin de la relation), donc une politique plus
    courte ne détruit plus rien trop tôt. Le point reste en ATTENTION parce que
    l'écran annonce alors une durée que le produit n'applique pas — et c'est
    celle-là qu'un registre des traitements recopiera.
    """
    from fiskr.settings import (retention_policy, retention_sous_la_duree_legale,
                                DUREE_LEGALE_JOURS)
    ecarts = retention_sous_la_duree_legale(retention_policy(db))
    if not ecarts:
        return _controle(
            "conservation", "Exploitation", "Conservation des pièces probantes", OK,
            f"Journal de criblage et alertes clôturées réglés à {DUREE_LEGALE_JOURS} "
            f"jours au moins (cinq années civiles) ou sans limite ; la purge compte "
            f"depuis la fin de la relation d'affaires.",
            lien="#settings/settings-governance")
    return _controle(
        "conservation", "Exploitation", "Conservation des pièces probantes", ATTENTION,
        " ; ".join(e["message"] for e in ecarts)
        + " Rien n'est purgé trop tôt, mais la durée affichée n'est pas celle "
          "que le produit applique.",
        f"Portez ces durées à {DUREE_LEGALE_JOURS} jours au moins (0 = conservation "
        f"illimitée) : le réglage dira alors ce que fait la purge.",
        "#settings/settings-retention")


def _pieces_probantes(db) -> Dict[str, Any]:
    """
    Les pieces justificatives que la base annonce sont-elles encore la ?

    Une piece vit en deux endroits : une ligne en base qui porte son nom, et
    un fichier sur le disque. Les ecrans lisent la ligne ; le telechargement
    lit le fichier, et decouvre son absence au moment ou quelqu'un clique.
    Dans un produit de conformite, ce moment-la porte un nom : le controle.

    D'ou ce controle, qui pose la question AVANT — a froid, pendant qu'il
    reste un espoir de retrouver les fichiers dans une sauvegarde. C'est le
    pendant exact du controle de conservation : celui-la protege une preuve
    qu'on pourrait perdre demain, celui-ci constate celle qui manque deja.
    """
    from fiskr.preuves import inventaire
    etat = inventaire(db)
    if not etat["annoncees"]:
        return _controle(
            "pieces", "Exploitation", "Pièces probantes", OK,
            "Aucune pièce justificative n'est encore rattachée : rien n'est "
            "annoncé, donc rien ne manque.")
    verifiees = sum(f["verifiees"] for f in etat["familles"])
    portee = (f"{verifiees} vérifiée(s) sur {etat['annoncees']} annoncée(s)"
              if etat["tronque"] else f"{etat['annoncees']} pièce(s)")
    if not etat["manquantes"]:
        return _controle(
            "pieces", "Exploitation", "Pièces probantes", OK,
            f"{portee} : chaque fichier annoncé est bien présent sur le disque.")
    detail = " ; ".join(
        f"{f['libelle']} : {f['manquantes']}"
        for f in etat["familles"] if f["manquantes"])
    return _controle(
        "pieces", "Exploitation", "Pièces probantes", ATTENTION,
        f"{etat['manquantes']} pièce(s) annoncée(s) en base sont introuvables "
        f"sur le disque ({detail}). Les écrans affichent leur nom ; le "
        f"téléchargement échouera. Portée du contrôle : {portee}.",
        "Restaurez les dossiers de pièces depuis une sauvegarde (ils ne sont "
        "pas dans la base : une restauration de base seule ne les ramène pas). "
        "Les références restent en place — effacer la ligne effacerait la "
        "trace que la pièce a existé.",
        "#settings/settings-governance")


def _couverture_du_criblage(db) -> Dict[str, Any]:
    """
    « Tous vos clients ont-ils été criblés, et contre la version des listes en
    production ? » — la première question d'un contrôle.

    Deux constats, le pire l'emporte :

    - le re-criblage automatique est coupé : plus aucune mise à jour de liste
      n'est confrontée au référentiel (lignes directrices DGT/ACPR, §123 :
      « sans délai » à chaque modification des mesures), et le réglage se
      coupait sans qu'aucun contrôle le relève ;
    - des clients ne sont pas couverts contre la version en production. Le
      délai toléré est celui de la fraîcheur des listes, et pour la même
      raison : une liste en retard et un client non recriblé exposent au même
      gel manqué. En deçà, un travail en cours est laissé à son œuvre ; sans
      travail en cours, l'écart ne se résorbera pas seul et il est signalé.
    """
    from datetime import datetime
    from fiskr.couverture import couverture_du_criblage, phrase_de_couverture
    from fiskr.fraicheur import phrase_de_retard
    from fiskr.settings import auto_rescreen_enabled

    mesure = couverture_du_criblage(db)
    if mesure.get("illisible"):
        # Une mesure en echec ne dit rien du referentiel : ni « vide », ni
        # « tout est crible ». Elle le dit, au lieu de prendre l'un des deux.
        return _controle(
            "couverture", "Criblage", "Couverture du criblage", ATTENTION,
            phrase_de_couverture(mesure),
            "Relancez la vérification ; si l'échec persiste, le journal du "
            "serveur donne l'erreur complète (fiskr.couverture).",
            "#screening/screening-batch")
    if mesure["sans_referentiel"]:
        return _controle(
            "couverture", "Criblage", "Couverture du criblage", A_FAIRE,
            "Aucun référentiel clients en production : rien à cribler pour "
            "l'instant.",
            "Importez une base clients (CLIENT_BASE) depuis l'écran Imports. "
            "Le criblage n'a rien à comparer tant qu'elle manque.",
            "#watchlist-mgmt/watchlist-import")

    rang = {OK: 0, ATTENTION: 1, BLOQUANT: 2}
    etat, constats, remedes, lien = OK, [], [], "#screening/screening-batch"
    if not auto_rescreen_enabled(db):
        etat = ATTENTION
        constats.append(
            "Le re-criblage automatique est coupé : une liste mise en production "
            "n'est plus confrontée au référentiel clients, et rien d'autre ne le fait.")
        remedes.append(
            "Rallumez « Re-criblage automatique du référentiel clients après chaque "
            "mise à jour de liste » (Paramètres → Gouvernance).")
        lien = "#settings/settings-governance"

    phrase = phrase_de_couverture(mesure)
    if phrase:
        heures = None
        if mesure.get("en_attente_depuis"):
            depuis = datetime.fromisoformat(mesure["en_attente_depuis"])
            heures = max(0, int((datetime.utcnow() - depuis).total_seconds() // 3600))
        if heures is not None and heures >= _RETARD_BLOQUANT_H:
            ecart = BLOQUANT
        elif heures is not None and heures >= _RETARD_TOLERE_H:
            ecart = ATTENTION
        elif mesure.get("travaux_en_cours"):
            ecart = OK
        else:
            ecart = ATTENTION
        if heures is not None:
            phrase += f" Le plus ancien attend depuis {phrase_de_retard(heures)}."
        elif mesure.get("attente_inconnue"):
            phrase += (" Aucun passage n'a encore confronté le référentiel à cette "
                       "version : une fiche listée ajoutée ou corrigée sans re-criblage.")
        if ecart == OK:
            phrase += " Un criblage est en cours : il les couvrira."
        else:
            remedes.append(
                "Lancez un lookback (Criblage → Batch) : il confronte tout le "
                "référentiel aux listes en production et date chaque fiche. C'est "
                "l'opération la plus lourde du produit — elle part en tâche de fond.")
            lien = "#screening/screening-batch"
        constats.append(phrase)
        if rang[ecart] > rang[etat]:
            etat = ecart

    if not constats:
        return _controle(
            "couverture", "Criblage", "Couverture du criblage", OK,
            f"Les {mesure['clients']} clients du référentiel sont criblés contre la "
            f"version des listes en production.", lien="#screening/screening-batch")
    return _controle("couverture", "Criblage", "Couverture du criblage", etat,
                     " ".join(constats), " ".join(remedes), lien)


def _declarations_de_gel(db) -> Dict[str, Any]:
    """
    Les gels confirmes ont-ils ete declares a la DG Tresor ?

    La declaration est due « immediatement » (CMF L.562-4 ; LD DGT/ACPR du
    18 mars 2026, §182), et les « delais de declaration » sont un point de
    controle interne nomme (§99). Releve en production : deux vrais positifs
    OFAC, aucune declaration preparee, rien ne le disait. Ce controle lit le
    registre des mesures de gel — la meme definition du retard que la
    notification et l'indicateur (fiskr/kpi.py).
    """
    from fiskr.database import MesureDeGel
    from fiskr.gels import STATUT_A_DECLARER, alertes_confirmees_sans_mesure, mesures_en_retard
    from fiskr.settings import gel_declaration_delai_heures
    seuil = gel_declaration_delai_heures(db)
    en_retard = mesures_en_retard(db)
    lien = "#screening/alerts-gels"
    orphelines = alertes_confirmees_sans_mesure(db)
    if orphelines:
        # Le cas de production : des vrais positifs confirmes AVANT que le
        # registre n'existe. Rien ne les y fera entrer tout seuls.
        noms = ", ".join(f"alerte #{a.id} {a.client_name} × {a.watchlist_name}"
                         for a in orphelines[:5])
        return _controle(
            "gels", "Criblage", "Déclarations de gel à la DG Trésor", BLOQUANT,
            f"{len(orphelines)} vrai(s) positif(s) sur une liste de gel sans mesure au "
            f"registre : {noms}{' …' if len(orphelines) > 5 else ''}. Aucune déclaration "
            f"à la DG Trésor n'y est tracée.",
            "Ouvrez chaque alerte et créez sa mesure de gel (bouton « Ouvrir la mesure de "
            "gel ») : son délai courra depuis la confirmation, puis déclarez-la.", lien,
            constat_public=f"{len(orphelines)} vrai(s) positif(s) sur une liste de gel "
                           f"sans mesure au registre ni déclaration tracée.")
    if en_retard:
        noms = ", ".join(f"#{m.id} {m.client_name} × {m.watchlist_name}" for m in en_retard[:5])
        return _controle(
            "gels", "Criblage", "Déclarations de gel à la DG Trésor", BLOQUANT,
            f"{len(en_retard)} mesure(s) de gel non déclarée(s) plus de {seuil} h après la "
            f"confirmation : {noms}{' …' if len(en_retard) > 5 else ''}. La déclaration est "
            f"due immédiatement (CMF L.562-4, R.562-3).",
            "Préparez le projet de déclaration depuis le registre des gels, transmettez-le "
            "à la DG Trésor, puis enregistrez la déclaration avec la pièce de transmission "
            "(accusé de réception ou copie du courriel).", lien,
            constat_public=f"{len(en_retard)} mesure(s) de gel non déclarée(s) plus de "
                           f"{seuil} h après la confirmation.")
    a_declarer = db.query(MesureDeGel).filter(MesureDeGel.statut == STATUT_A_DECLARER).count()
    if a_declarer:
        return _controle(
            "gels", "Criblage", "Déclarations de gel à la DG Trésor", ATTENTION,
            f"{a_declarer} mesure(s) de gel à déclarer, encore dans le délai de {seuil} h.",
            "Déclarez-les à la DG Trésor maintenant : le délai est un plafond d'alarme, "
            "pas un délai accordé.", lien)
    return _controle(
        "gels", "Criblage", "Déclarations de gel à la DG Trésor", OK,
        "Aucune mesure de gel en attente de déclaration au registre.", lien=lien)


def _dossiers_ppe(db) -> Dict[str, Any]:
    """
    Les PPE confirmees ont-elles leur decision d'entrer ou de rester en
    relation ?

    R.561-20-2 1° reserve cette decision a l'organe executif ou a la personne
    qu'il habilite ; avant le dossier PPE, une PPE confirmee se closait sans
    rien qui la suive. Ce controle lit les dossiers — la meme definition du
    retard que la notification et l'indicateur (fiskr/kpi.py) — et nomme les
    PPE confirmees restees sans dossier (confirmees avant lui) dont le delai,
    compte depuis leur confirmation, est depasse.
    """
    from datetime import datetime, timedelta
    from fiskr.ppe import alertes_confirmees_sans_dossier, dossiers_en_retard
    from fiskr.settings import ppe_decision_delai_jours
    seuil = ppe_decision_delai_jours(db)
    lien = "#screening/alerts-ppe"
    titre = "Décisions sur les relations avec des PPE"
    limite = datetime.utcnow() - timedelta(days=seuil)
    orphelines = alertes_confirmees_sans_dossier(db)
    orphelines_en_retard = [a for a in orphelines if a.decided_at is None or a.decided_at < limite]
    if orphelines_en_retard:
        noms = ", ".join(f"alerte #{a.id} {a.client_name} × {a.watchlist_name}"
                         for a in orphelines_en_retard[:5])
        return _controle(
            "ppe", "Criblage", titre, BLOQUANT,
            f"{len(orphelines_en_retard)} PPE confirmée(s) depuis plus de {seuil} jours sans "
            f"dossier : {noms}{' …' if len(orphelines_en_retard) > 5 else ''}. Aucune décision "
            f"d'entrer ou de rester en relation n'y est tracée (CMF R.561-20-2 1°).",
            "Ouvrez chaque alerte et créez son dossier PPE (bouton « Ouvrir le dossier PPE ») : "
            "son délai courra depuis la confirmation. Faites-y décider la relation par une "
            "personne habilitée.", lien,
            constat_public=f"{len(orphelines_en_retard)} PPE confirmée(s) depuis plus de "
                           f"{seuil} jours sans dossier ni décision tracée.")
    en_retard = dossiers_en_retard(db)
    if en_retard:
        noms = ", ".join(f"#{d.id} {d.client_name} × {d.watchlist_name}" for d in en_retard[:5])
        return _controle(
            "ppe", "Criblage", titre, BLOQUANT,
            f"{len(en_retard)} dossier(s) PPE sans décision plus de {seuil} jours après la "
            f"confirmation : {noms}{' …' if len(en_retard) > 5 else ''}. La relation court sans "
            f"la décision que R.561-20-2 1° réserve à l'organe exécutif ou à la personne qu'il "
            f"habilite.",
            "Complétez l'origine des fonds et du patrimoine, puis faites décider la relation par "
            "une personne habilitée (rôle réviseur) qui n'a pas instruit l'alerte.", lien,
            constat_public=f"{len(en_retard)} dossier(s) PPE sans décision plus de {seuil} "
                           f"jours après la confirmation.")
    # La revue de vigilance renforcee datee a la decision (R.561-20-2 3°) : la
    # notification part a l'echeance ; echue depuis plus que le delai, la
    # relation court sans la vigilance que le texte exige (`ppe.revue_en_retard`).
    from fiskr.ppe import revues_en_retard
    revues = revues_en_retard(db)
    if revues:
        noms = ", ".join(f"#{d.id} {d.client_name} × {d.watchlist_name}" for d in revues[:5])
        return _controle(
            "ppe", "Criblage", titre, BLOQUANT,
            f"{len(revues)} dossier(s) PPE dont la revue de vigilance renforcée est échue depuis "
            f"plus de {seuil} jours : {noms}{' …' if len(revues) > 5 else ''}. La relation court "
            f"sans la revue datée à la décision (CMF R.561-20-2 3°).",
            "Ouvrez chaque dossier et enregistrez sa revue : maintien de la relation, avec la "
            "revue suivante datée, ou fin de la relation — par une personne habilitée (rôle "
            "réviseur) qui n'a pas instruit l'alerte.", lien,
            constat_public=f"{len(revues)} dossier(s) PPE dont la revue de vigilance renforcée "
                           f"est échue depuis plus de {seuil} jours.")
    if orphelines:
        return _controle(
            "ppe", "Criblage", titre, ATTENTION,
            f"{len(orphelines)} PPE confirmée(s) sans dossier, encore dans le délai de "
            f"{seuil} jours.",
            "Ouvrez leur dossier PPE depuis l'alerte : sans lui, rien ne portera la décision.",
            lien)
    return _controle("ppe", "Criblage", titre, OK,
                     f"Aucun dossier PPE sans décision ni revue échue au-delà de {seuil} jours.",
                     lien=lien)


# Fenetre d'observation des echecs de synchronisation : assez large pour
# distinguer un incident d'une panne installee, assez courte pour qu'une
# source reparee sorte vite du constat.
_FENETRE_ECHECS_JOURS = 3
_RAPPORTS_EXAMINES = 200

def _sources_en_echec_repete(db) -> Dict[str, Any]:
    """
    Sources dont CHAQUE passage recent echoue.

    Une source qui echoue une nuit est un incident ; une source qui echoue
    toutes les nuits depuis des semaines est autre chose : c'est une alerte
    qu'on a appris a ignorer. Et le jour ou une source vivante tombe, son
    echec se range dans la meme pile, sans se distinguer.

    Le controle ne juge pas de la CAUSE — cle d'API manquante, portail qui
    refuse, adresse morte : elles se traitent differemment. Il dit seulement
    ce que personne ne dit aujourd'hui : celle-ci n'a pas fonctionne une seule
    fois sur ses N derniers passages.
    """
    from fiskr.database import SyncReport
    from datetime import datetime, timedelta

    depuis = datetime.utcnow() - timedelta(days=_FENETRE_ECHECS_JOURS)
    lignes = db.query(SyncReport).filter(SyncReport.executed_at >= depuis) \
               .order_by(SyncReport.executed_at.desc()).limit(_RAPPORTS_EXAMINES).all()
    if not lignes:
        return _controle("sources_echec", "Exploitation", "Sources en échec répété", OK,
                         "Aucune synchronisation sur la période : rien à juger.")
    passages: Dict[str, List[str]] = {}
    for ligne in lignes:
        passages.setdefault((ligne.source or "?").upper(), []).append(ligne.status or "?")
    # Un seul passage ne fait pas une repetition : on exige au moins deux
    # tentatives, toutes en echec.
    condamnees = sorted(source for source, statuts in passages.items()
                        if len(statuts) >= 2 and all(st == "ERROR" for st in statuts))
    if not condamnees:
        return _controle("sources_echec", "Exploitation", "Sources en échec répété", OK,
                         f"Aucune source en échec systématique sur les {_FENETRE_ECHECS_JOURS} derniers jours.")
    detail = ", ".join(f"{source} ({len(passages[source])} passages)" for source in condamnees)
    return _controle(
        "sources_echec", "Exploitation", "Sources en échec répété", ATTENTION,
        f"{len(condamnees)} source(s) n'ont pas abouti une seule fois sur "
        f"{_FENETRE_ECHECS_JOURS} jours : {detail}.",
        "Traitez ou désactivez : une source qui échoue chaque nuit finit par "
        "rendre invisible celle qui tombera vraiment. Le journal de "
        "synchronisation donne l'erreur exacte de chaque passage.",
        "#watchlists/watchlists-sources")


_CONTROLES_BASE = (_base_de_donnees, _demon, _listes_en_production, _sources_de_reference,
                   _sources_automatiques, _sources_en_echec_repete,
                   _homologation, _fraicheur_des_listes, _referentiel_clients,
                   _seuils, _comptes, _conservation, _couverture_du_criblage,
                   _declarations_de_gel, _dossiers_ppe, _pieces_probantes, _smtp)
_CONTROLES_SANS_BASE = (_secrets, _index_de_performance, _url_publique)


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


# Ce que rejoue la sonde de conformite (GET /api/health/conformite) : les
# controles qui peuvent conclure BLOQUANT et ne coutent que quelques lectures
# d'index. Elle est interrogee chaque minute par une supervision externe, elle
# ne doit rien faire de lourd. Restent dehors, a dessein, ceux qui ne
# concluent jamais BLOQUANT : une sonde ne doit faire sonner que ce qui
# bloque. Un test verifie qu'aucun controle capable de BLOQUANT n'en manque.
_CONTROLES_DE_LA_SONDE_SANS_BASE = (_secrets,)
# La couverture du criblage y est aussi : un referentiel que rien n'a confronte
# a la version en production depuis une semaine est un defaut de conformite,
# pas un detail d'ecran. Son comptage est borne (PLAFOND_DE_COMPTAGE).
# Les dossiers PPE aussi : quelques lectures d'index (dossiers sans decision,
# PPE confirmees sans dossier, filtrees en SQL sur les types PEP), et leur
# constat public ne nomme personne.
_CONTROLES_DE_LA_SONDE = (_demon, _listes_en_production, _sources_de_reference,
                          _fraicheur_des_listes, _declarations_de_gel, _dossiers_ppe,
                          _couverture_du_criblage)


def etat_de_conformite(db) -> Dict[str, Any]:
    """
    Les controles BLOQUANT parmi ceux que rejoue la sonde, et sa portee.

    Releve en production le 19 octobre 2026 : 47 jours sans homologation, la
    notification desactivee, les envois immediats en echec (0 reussite sur
    335) — et `/api/health` repondait « ok ». La sonde de vie dit que le
    processus repond ; celle-ci dit si ce qu'il crible est opposable, sans
    passer par le courriel.

    Contrairement a l'ecran de mise en service, qui doit rester affichable et
    saute un controle illisible, la sonde ne saute rien : un controle qui ne
    peut pas conclure ne l'autorise pas a dire « conforme ». Il est rendu
    comme un echec, nomme.
    """
    bloquants: List[Dict[str, Any]] = []
    portee: List[str] = []
    fonctions = ([(f, False) for f in _CONTROLES_DE_LA_SONDE_SANS_BASE]
                 + [(f, True) for f in _CONTROLES_DE_LA_SONDE])
    for fonction, avec_base in fonctions:
        try:
            controle = fonction(db) if avec_base else fonction()
        except Exception as e:
            logger.warning(f"Sonde de conformité : contrôle {fonction.__name__} illisible : {e}")
            nom = fonction.__name__.strip("_")
            portee.append(nom)
            bloquants.append({"cle": nom, "titre": nom, "remede": "",
                              "constat": f"Contrôle illisible ({type(e).__name__} : {e}) — "
                                         f"la conformité ne peut pas être établie."})
            continue
        portee.append(controle["cle"])
        if controle["etat"] == BLOQUANT:
            bloquants.append({"cle": controle["cle"], "titre": controle["titre"],
                              "constat": controle.get("constat_public") or controle["constat"],
                              "remede": controle["remede"]})
    return {"conforme": not bloquants, "bloquants": bloquants, "portee": portee}


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

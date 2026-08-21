"""
Corps des taches de fond, enregistrees dans la file de travaux (fiskr.jobs).

Deplacees ici depuis les closures d'api.py pour deux raisons :
- un demon travailleur (fiskr/worker.py) doit pouvoir les executer dans un
  AUTRE processus : leurs parametres sont donc strictement serialisables
  (JSON), jamais des objets vivants ;
- une tache relancee apres un redemarrage (reprise automatique) est reconstruite
  depuis sa ligne `jobs` : seul un corps nomme + des params JSON le permettent.

Regle du cache de production : les taches qui changent les listes en
production appellent `_refresh_production_cache`. Dans le processus API
(modes thread/eager), le cache memoire est recharge immediatement ; depuis le
demon, seule l'EPOQUE en base est incrementee — chaque processus API la
surveille et recharge son propre cache (fiskr/api.py, verification throttlee).
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fiskr import jobs
from fiskr.database import Snapshot
from fiskr.settings import bump_watchlist_epoch

logger = logging.getLogger("fiskr.tasks")


def _refresh_production_cache(session) -> None:
    """
    Les listes en production ont change : incremente l'epoque (tous les
    processus API rechargeront leur cache), et recharge TOUT DE SUITE le
    cache local si nous sommes dans un processus API — un test ou un
    deploiement sans demon doit voir le changement au retour du job.
    """
    bump_watchlist_epoch(session)
    if not jobs.IN_WORKER:
        from fiskr.api import load_watchlist_cache
        load_watchlist_cache(session)


CONSOLIDATED_BACKTEST_TOKEN = "backtest:homologation"


def pending_backtest_scope(session) -> List[Snapshot]:
    """
    Snapshots en attente d'homologation qui restent a tester.

    Resolu A L'EXECUTION, jamais fige a la soumission : une vague de
    synchronisations depose ses snapshots au fil de l'eau, et un cahier
    consolide encore en file doit couvrir ceux qui arrivent apres lui.
    """
    snaps = session.query(Snapshot).filter(
        Snapshot.status == "PENDING_REVIEW",
        Snapshot.backtest_report.is_(None),
    ).order_by(Snapshot.uploaded_at.asc()).all()
    # Un snapshot sans delta n'a rien a prouver : le tester ferait une passe
    # d'univers pour un rapport vide.
    return [s for s in snaps if (s.record_count or 0) > 0]


@jobs.task("backtest")
def backtest_task(ctx: jobs.JobContext, *, snapshot_id: Optional[str] = None,
                  snapshot_ids: Optional[List[str]] = None,
                  panel_snapshot_id: str = "", resolve_pending: bool = False,
                  candidate_rule_id: Optional[int] = None, username: str = "?") -> None:
    """
    Cahier de tests d'homologation : criblage A/B a blanc. Persiste le rapport
    sur CHAQUE snapshot couvert — le front le relit par
    GET /api/review/snapshots/{id}, y compris apres un rechargement de page ou
    un redemarrage.

    Trois facons de designer le perimetre :
    - `snapshot_id` : un seul snapshot (lancement manuel, regle candidate) ;
    - `snapshot_ids` : une liste figee ;
    - `resolve_pending` : la liste est resolue A L'EXECUTION (tous les
      snapshots en attente restant a tester). C'est le mode des vagues de
      synchronisation : un cahier encore en file couvre les snapshots deposes
      apres sa soumission, au lieu d'en faire naitre un par source.
    """
    from fiskr.backtest import run_backtest
    from fiskr.notifier import emit
    from fiskr.settings import backtest_max_gap_pct

    session = ctx.session()
    try:
        if resolve_pending:
            snaps = pending_backtest_scope(session)
        else:
            ids = list(snapshot_ids or ([snapshot_id] if snapshot_id else []))
            snaps = session.query(Snapshot).filter(
                Snapshot.snapshot_id.in_(ids)).all() if ids else []
        if not snaps:
            # Rien a tester : ce n'est pas une erreur (les snapshots ont pu
            # etre approuves ou rejetes entre la soumission et l'execution).
            ctx.set_result({"skipped": True, "reason": "aucun snapshot à tester"})
            return

        premier = snaps[0].snapshot_id
        report = run_backtest(session, snaps, panel_snapshot_id,
                              threshold_pct=backtest_max_gap_pct(session),
                              executed_by=username,
                              candidate_rule_id=candidate_rule_id,
                              progress=lambda phase, done, total: ctx.update(
                                  phase=phase, processed=done, total=total,
                                  snapshot_id=premier))
        acheve = datetime.utcnow()
        for snap in snaps:
            # Le MEME rapport sur chaque snapshot couvert : l'univers candidat
            # les contenait tous, le verdict porte donc sur leur ensemble et
            # n'aurait aucun sens decoupe liste par liste.
            snap.backtest_report = report
            snap.backtest_at = acheve
            snap.backtest_by = username
        session.commit()
        # Un ecart eleve doit remonter tout de suite (il bloque l'approbation) ;
        # un verdict OK part dans le recapitulatif periodique
        emit(session, "backtest_completed", {
            "Snapshot": ", ".join(s.snapshot_id for s in snaps),
            "Liste": ", ".join(sorted({s.file_type for s in snaps})),
            "Verdict": report.get("verdict"),
            "Écart": f"{report.get('gap_pct')} % (seuil {report.get('threshold_pct')} %)",
            "Alertes production": (report.get("current") or {}).get("alerts"),
            "Alertes candidate": (report.get("candidate") or {}).get("alerts"),
            "Nouvelles paires": report.get("new_pairs_count"),
            "Exécuté par": username,
        }, urgency_override="immediate" if report.get("verdict") != "OK" else None)
        ctx.set_result({"verdict": report.get("verdict"), "gap_pct": report.get("gap_pct"),
                        "snapshot_id": premier,
                        "snapshot_ids": [s.snapshot_id for s in snaps]})

        # Des snapshots deposes PENDANT ce cahier ne sont pas couverts par lui :
        # on relance alors un cahier consolide pour eux, et un seul. Sans cela,
        # une synchronisation terminee en cours de route resterait sans rapport.
        if resolve_pending:
            restants = pending_backtest_scope(session)
            if restants:
                try:
                    jobs.submit("backtest", token=CONSOLIDATED_BACKTEST_TOKEN,
                                label=f"Cahier de tests — {len(restants)} liste(s) en attente",
                                params={"resolve_pending": True,
                                        "panel_snapshot_id": panel_snapshot_id,
                                        "candidate_rule_id": None,
                                        "username": "système"},
                                created_by="système",
                                dedupe_key=CONSOLIDATED_BACKTEST_TOKEN)
                except jobs.JobConflict:
                    pass  # un cahier consolide est deja en file : il les prendra
    finally:
        session.close()


@jobs.task("approve")
def approve_followup_task(ctx: jobs.JobContext, *, snapshot_id: str,
                          previous_snapshot_id: Optional[str] = None,
                          excluded_count: int = 0, username: str = "?") -> None:
    """
    Suites d'une approbation : rafraichissement du cache de production puis
    re-criblage post-delta du referentiel clients. La promotion elle-meme,
    acte de gouvernance, a deja ete commitee de facon synchrone par l'endpoint.
    """
    from fiskr.notifier import emit
    from fiskr.rescreen import rescreen_after_snapshot_change
    from fiskr.settings import auto_rescreen_enabled

    session = ctx.session()
    try:
        snap = session.query(Snapshot).filter(Snapshot.snapshot_id == snapshot_id).first()
        if snap is None:
            raise ValueError("Snapshot introuvable.")

        ctx.update(phase="RELOAD", snapshot_id=snapshot_id)
        _refresh_production_cache(session)

        rescreen_result = None
        if auto_rescreen_enabled(session):
            rescreen_result = rescreen_after_snapshot_change(
                session, snap.file_type, snap.snapshot_id, previous_snapshot_id,
                progress=lambda done, total: ctx.update(
                    phase="RESCREEN", processed=done, total=total,
                    snapshot_id=snapshot_id),
            )
        # Étape structurante de la production des listes : mail immédiat
        report = snap.backtest_report or {}
        emit(session, "snapshot_approved", {
            "Liste": snap.file_type, "Fichier": snap.file_name, "Snapshot": snap.snapshot_id,
            "Fiches": snap.record_count, "Exclusions": excluded_count,
            "Approuvé par": username,
            "Commentaire": snap.review_comment or "—",
            "Cahier de tests": f"{report.get('verdict')} (écart {report.get('gap_pct')} %)" if report else "non exécuté",
            "Nouvelles alertes (re-criblage)": (rescreen_result or {}).get("new_alerts", 0),
            "Alertes re-détectées (re-criblage)": (rescreen_result or {}).get("redetected_alerts", 0),
        })
        ctx.set_result({"snapshot_id": snapshot_id, "rescreen": rescreen_result})
    finally:
        session.close()


def _resolve_auto_backtest_panel(session):
    """
    Panel du cahier de tests automatique : celui impose par le reglage s'il
    est toujours utilisable (READY, non vide), sinon le panel de
    pseudo-clients GENERE le plus recent — jamais la base clients reelle par
    defaut : un criblage A/B de tout le referentiel ne se declenche pas seul.
    Retourne None si aucun panel n'est utilisable (l'automatisme s'abstient).
    """
    from fiskr.backtest import PANEL_FILE_TYPES, TEST_PANEL_FILE_TYPE
    from fiskr.settings import auto_backtest_panel

    forced = auto_backtest_panel(session)
    if forced:
        panel = session.query(Snapshot).filter(
            Snapshot.snapshot_id == forced,
            Snapshot.file_type.in_(PANEL_FILE_TYPES),
            Snapshot.status == "READY",
        ).first()
        if panel is not None and (panel.record_count or 0):
            return panel.snapshot_id
        logger.warning(f"Panel d'auto-backtest configure ({forced}) inutilisable : repli sur le dernier panel genere.")
    panel = session.query(Snapshot).filter(
        Snapshot.file_type == TEST_PANEL_FILE_TYPE,
        Snapshot.status == "READY",
        Snapshot.record_count > 0,
    ).order_by(Snapshot.uploaded_at.desc()).first()
    if panel is not None:
        return panel.snapshot_id

    # Aucun panel : l'automatisme s'en genere un (500 pseudo-clients derives
    # de la production) plutot que de s'abstenir en silence — c'etait la
    # cause la plus frequente d'un cahier de tests qui « ne demarre pas ».
    try:
        from fiskr.backtest import generate_test_panel
        from fiskr.database import WATCHLIST_FILE_TYPES

        prod_ids = [s.snapshot_id for s in session.query(Snapshot).filter(
            Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
            Snapshot.status == "READY").all()]
        if not prod_ids:
            return None
        generated = generate_test_panel(session, prod_ids, size=500,
                                        created_by="système (auto-backtest)")
        logger.info(f"Panel d'auto-backtest généré automatiquement : {generated.snapshot_id}")
        return generated.snapshot_id
    except Exception as e:
        logger.warning(f"Génération automatique du panel d'auto-backtest impossible : {e}")
        return None


def _maybe_auto_backtest(session, report) -> Optional[Dict[str, Any]]:
    """
    Apres une synchronisation RETENUE en homologation avec un delta non nul :
    soumet le cahier de tests automatiquement, pour que le reviseur trouve le
    delta ET le rapport A/B deja prets et n'ait plus qu'a decider.
    Jamais bloquant : toute impossibilite (reglage coupe, aucun panel, job
    equivalent deja en cours) est rendue dans le rapport, pas levee.
    """
    from fiskr.settings import auto_backtest_enabled

    if report.status != "PENDING_REVIEW" or not report.snapshot_id:
        return None
    delta_size = (report.added_count or 0) + (report.modified_count or 0) + (report.removed_count or 0)
    if not delta_size:
        return {"submitted": False, "reason": "delta vide"}
    if not auto_backtest_enabled(session):
        return {"submitted": False, "reason": "désactivé (réglage review.auto_backtest_enabled)"}
    panel_id = _resolve_auto_backtest_panel(session)
    if panel_id is None:
        return {"submitted": False,
                "reason": "aucun panel de test généré disponible (générez-en un dans l'homologation)"}
    # UN SEUL cahier pour toute la vague. Synchroniser les sources activées
    # deposait autant de snapshots que de sources, donc autant de cahiers — et
    # comme les cahiers sont serialises, la file s'allongeait de plusieurs
    # dizaines de minutes pour un travail largement redondant : chacun
    # recriblait le MEME univers partage. Le jeton commun fait que le second
    # depot ne cree rien, et le cahier resout son perimetre a l'execution : il
    # couvre donc aussi les synchronisations terminees apres lui.
    token = CONSOLIDATED_BACKTEST_TOKEN
    try:
        jobs.submit("backtest", token=token,
                    label="Cahier de tests — listes en attente d'homologation",
                    params={"resolve_pending": True,
                            "panel_snapshot_id": panel_id,
                            "candidate_rule_id": None, "username": "système"},
                    created_by="système", dedupe_key=token)
    except jobs.JobConflict:
        # Deja en file : il resoudra son perimetre a l'execution et prendra ce
        # snapshot avec les autres. Rien a relancer.
        return {"submitted": False, "consolidated": True,
                "reason": "rattaché au cahier de tests consolidé déjà en file",
                "job_token": token}
    return {"submitted": True, "consolidated": True,
            "panel_snapshot_id": panel_id, "job_token": token}


@jobs.task("sync")
def sync_source_task(ctx: jobs.JobContext, *, run_key: str, engine_source: str,
                     for_date: Optional[str] = None, username: str = "?") -> None:
    """
    Cycle complet de synchronisation d'une source officielle : telechargement,
    delta, application (ou attente d'homologation), re-criblage post-delta.
    Le rapport est publie sur la ligne du job ET archive en base (SyncReport).
    Si le snapshot est retenu en homologation avec un delta, le cahier de
    tests part automatiquement (reglage review.auto_backtest_enabled).
    """
    from fiskr.rescreen import rescreen_after_snapshot_change
    from fiskr.settings import auto_rescreen_enabled

    session = ctx.session()
    try:
        from fiskr.api import _SYNC_RUNNERS, _serialize_sync_report

        kwargs: Dict[str, Any] = {
            "trigger": "MANUAL" if username != "système" else "SCHEDULED",
            "reload_cache": lambda: _refresh_production_cache(session),
        }
        if run_key == "eurlex" and for_date:
            kwargs["for_date"] = datetime.strptime(for_date, "%Y-%m-%d").date()
        report = _SYNC_RUNNERS[run_key](session, **kwargs)
        result = _serialize_sync_report(report)
        # Surveillance continue : re-criblage du referentiel clients contre
        # les entites nouvelles/modifiees du snapshot applique
        if report.status == "SUCCESS" and report.snapshot_id and auto_rescreen_enabled(session):
            snap = session.query(Snapshot).filter(
                Snapshot.snapshot_id == report.snapshot_id).first()
            if snap:
                result["rescreen"] = rescreen_after_snapshot_change(
                    session, snap.file_type, report.snapshot_id,
                    report.previous_snapshot_id,
                    progress=lambda done, total: ctx.update(
                        phase="RESCREEN", processed=done, total=total,
                        snapshot_id=report.snapshot_id),
                )
        # Snapshot retenu en homologation : cahier de tests automatique pour
        # que l'examen s'ouvre avec le rapport A/B deja pret (ou en cours)
        try:
            auto_bt = _maybe_auto_backtest(session, report)
        except Exception as e:  # l'automatisme ne casse jamais la sync
            logger.error(f"Auto-backtest impossible apres la sync {run_key} : {e}")
            auto_bt = {"submitted": False, "reason": f"erreur : {e}"}
        if auto_bt is not None:
            result["auto_backtest"] = auto_bt
        ctx.set_result(result)
    finally:
        session.close()


@jobs.task("engine_simulation")
def engine_simulation_task(ctx: jobs.JobContext, *, panel_snapshot_id: str,
                           candidate: List[str], baseline: List[str],
                           channel: str = "SCREENING", username: str = "?") -> None:
    """Mesure d'impact des capacites du moteur : deux passes a blanc, aucune
    ecriture. Le rapport vit sur la ligne du job (relisible apres redemarrage)."""
    from fiskr import engine_impact

    session = ctx.session()
    try:
        report = engine_impact.simulate_engine_impact(
            session, panel_snapshot_id, set(candidate),
            baseline_capabilities=set(baseline), channel=channel,
            progress=lambda phase, done, total: ctx.update(
                phase=phase, processed=done, total=total),
        )
        ctx.set_result(report)
    finally:
        session.close()


@jobs.task("resource_simulation")
def resource_simulation_task(ctx: jobs.JobContext, *, panel_snapshot_id: str,
                             candidate: List[str], baseline: Optional[List[str]] = None,
                             include_pending_ids: Optional[List[int]] = None,
                             username: str = "?") -> None:
    """Mesure d'impact des equivalences linguistiques : memes garanties que la
    mesure des capacites (a blanc, rapport persiste sur la ligne du job)."""
    from fiskr import resource_impact

    session = ctx.session()
    try:
        report = resource_impact.simulate_resource_impact(
            session, panel_snapshot_id, set(candidate),
            baseline_fields=set(baseline) if baseline is not None else None,
            include_pending_ids=list(include_pending_ids or []),
            progress=lambda phase, done, total: ctx.update(
                phase=phase, processed=done, total=total),
        )
        ctx.set_result(report)
    finally:
        session.close()


@jobs.task("ingest")
def ingest_task(ctx: jobs.JobContext, *, snapshot_id: str, temp_path: str,
                original_filename: str, file_type: str, delimiter: str = ",",
                ssie_selector_overrides=None, ssie_source_format=None,
                progress_id: Optional[str] = None, username: str = "?") -> None:
    """
    Import d'un fichier deja televerse : parsing, Quality Gate, persistance,
    bascule de statut, cache, re-criblage. L'upload et l'empreinte ont deja eu
    lieu dans la requete (les refus y restent synchrones) ; le snapshot existe
    en PROCESSING. Le fichier temporaire appartient a ce job, qui le supprime.
    """
    from fiskr.api import _ingest_parse_and_finalize

    session = ctx.session()
    try:
        snap = session.query(Snapshot).filter(Snapshot.snapshot_id == snapshot_id).first()
        if snap is None:
            raise ValueError("Snapshot introuvable (supprimé avant l'import ?).")
        result = _ingest_parse_and_finalize(
            session, snap, temp_path, original_filename, file_type, delimiter,
            ssie_selector_overrides, ssie_source_format, progress_id, username)
        ctx.set_result(result)
    finally:
        session.close()


@jobs.task("quality_check")
def quality_check_task(ctx: jobs.JobContext, *, snapshot_id: str) -> None:
    """Controle de completude du referentiel clients apres import : delegue au
    corps historique (cache du resultat + notification sous seuil). La file
    porte desormais le cycle de vie — fini le jeton RUNNING jamais clos."""
    from fiskr.api import _client_quality_post_import

    _client_quality_post_import(snapshot_id)


@jobs.task("lookback")
def lookback_task(ctx: jobs.JobContext, *, file_type: Optional[str] = None,
                  username: str = "?") -> None:
    """Lookback manuel (guidance Wolfsberg) : tout le referentiel contre
    toutes les listes en production — l'operation la plus lourde du produit,
    executee et suivie par la file."""
    from fiskr.rescreen import rescreen_lookback

    session = ctx.session()
    try:
        result = rescreen_lookback(
            session, file_type,
            progress=lambda done, total: ctx.update(
                phase="RESCREEN", processed=done, total=total))
        ctx.set_result({"message": "Lookback exécuté.", **result})
    finally:
        session.close()


@jobs.task("batch_campaign")
def batch_campaign_task(ctx: jobs.JobContext, *, campaign_id: int, profiles_path: str,
                        username: str = "?", requested_lists: Optional[List[str]] = None) -> None:
    """
    Campagne de criblage batch : les profils clients ont ete deposes dans un
    fichier temporaire par l'endpoint (la ligne jobs ne porte que des
    parametres bornes). La progression fine vit dans BatchCampaign, comme
    toujours ; la file porte le cycle de vie et la reprise.
    """
    import json
    from pathlib import Path

    from fiskr.database import BatchCampaign, BatchResult

    path = Path(profiles_path)
    try:
        profiles = json.loads(path.read_text(encoding="utf-8"))

        # Reprise apres interruption (demon tue en pleine campagne) : la
        # campagne repart de zero — les resultats partiels de la tentative
        # interrompue sont purges pour ne pas etre comptes deux fois.
        session = ctx.session()
        try:
            campaign = session.query(BatchCampaign).filter(
                BatchCampaign.id == campaign_id).first()
            if campaign is None:
                return
            if campaign.processed_clients:
                session.query(BatchResult).filter(
                    BatchResult.campaign_id == campaign_id).delete()
                campaign.processed_clients = 0
                campaign.alert_count = 0
                campaign.no_match_count = 0
                campaign.rejected_count = 0
            campaign.status = "RUNNING"
            campaign.error_message = None
            campaign.finished_at = None
            session.commit()
        finally:
            session.close()

        from fiskr.api import _run_batch_campaign
        _run_batch_campaign(campaign_id, profiles, username, requested_lists)
        ctx.set_result({"campaign_id": campaign_id, "clients": len(profiles)})
    finally:
        path.unlink(missing_ok=True)


@jobs.task("fprules_bench")
def fprules_bench_task(ctx: jobs.JobContext, *, rule_id: int, panel_snapshot_id: str,
                       username: str = "?") -> None:
    """Banc d'essai d'une regle anti-FP sur panel : criblage a blanc
    O(panel × univers), aucune ecriture. Rapport sur la ligne du job."""
    from fiskr.api import _fprules_bench_panel

    session = ctx.session()
    try:
        result = _fprules_bench_panel(
            session, rule_id, panel_snapshot_id,
            progress=lambda done, total: ctx.update(
                phase="BENCH", processed=done, total=total))
        ctx.set_result(result)
    finally:
        session.close()


@jobs.task("testpanel_generate")
def testpanel_generate_task(ctx: jobs.JobContext, *, source_ids: List[str],
                            size: int = 500, seed: Optional[int] = None,
                            username: str = "?") -> None:
    """Generation d'un panel de pseudo-clients (copies, typos, quasi-collisions,
    neutres) : O(univers) en lecture — le resultat legacy vit sur la ligne du
    job pour que le mode eager reponde 200 comme l'endpoint d'origine."""
    from fiskr.backtest import generate_test_panel
    from fiskr.notifier import emit

    session = ctx.session()
    try:
        ctx.update(phase="GENERATE")
        snap = generate_test_panel(session, source_ids, size=size,
                                   seed=seed, created_by=username)
        emit(session, "test_panel_generated", {
            "Panel": snap.file_name, "Pseudo-clients": snap.record_count,
            "Snapshot": snap.snapshot_id, "Généré par": username,
        })
        ctx.set_result({
            "message": f"Panel de {snap.record_count} pseudo-clients généré.",
            "snapshot_id": snap.snapshot_id,
            "file_name": snap.file_name,
            "record_count": snap.record_count,
        })
    finally:
        session.close()


@jobs.task("mining")
def mining_task(ctx: jobs.JobContext, *, username: str = "?") -> None:
    """Fouille d'homonymes : parcours des listes en production, propositions
    d'equivalences apprises."""
    from fiskr.api import run_resource_mining
    from fiskr.database import log_admin_action

    session = ctx.session()
    try:
        report = run_resource_mining(session, started_by=username, token=ctx.token)
        log_admin_action(session, username, "RESOURCE_MINING_RUN",
                         target="resources", after=report)
        session.commit()
        # Notification des decouvertes : portee par la tache (et non par le
        # planificateur) pour que les passes manuelles ET planifiees signalent
        if report and (report.get("created") or report.get("auto_approved")):
            from fiskr.notify import notify_event
            notify_event("resource_mining", report)
        ctx.set_result(report)
    finally:
        session.close()

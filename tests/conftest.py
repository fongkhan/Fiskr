"""
Outillage partage des tests.

Les operations longues (cahier de tests, mise en production) repondent 202
avec un jeton et travaillent en tache de fond : les tests doivent attendre la
fin du job avant d'observer son resultat.

Ce module pose aussi le FILET DE SECURITE qui empeche la suite de sedimenter
dans la base de developpement (voir `_isolate_database` plus bas).
"""
import os
import time

import pytest

# Mode d'execution de la file de travaux : INLINE pour les tests. Un endpoint
# 202 termine ainsi son job avant de repondre — wait_for_job et post_and_wait
# fonctionnent tels quels, et aucun thread/demon ne survit a un test. Pose
# AVANT tout import de fiskr (conftest est charge par pytest en premier).
os.environ.setdefault("FISKR_JOBS_MODE", "eager")


def wait_for_job(client, token, timeout=60.0):
    """
    Attend la fin d'une operation de fond et retourne son etat final.

    `token` peut etre le `job_token` renvoye par un 202 (ou None : la fonction
    ne fait alors rien et retourne None, pour les chemins ou aucun job n'est
    lance). Leve AssertionError si le job n'a pas fini dans le delai imparti :
    un test ne doit jamais se poursuivre sur un resultat incomplet.
    """
    if not token:
        return None
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/progress?id={token}")
        if response.status_code == 200:
            last = response.json()
            if last.get("status") in ("DONE", "ERROR"):
                return last
        time.sleep(0.05)
    raise AssertionError(f"Job {token} toujours en cours après {timeout} s : {last}")


def post_and_wait(client, url, timeout=60.0, **kwargs):
    """POST sur un endpoint asynchrone puis attente du job. Retourne la
    reponse HTTP : les refus synchrones (400/403/404) sont rendus tels quels,
    aucun job n'ayant demarre."""
    response = client.post(url, **kwargs)
    if response.status_code in (200, 202):
        wait_for_job(client, (response.json() or {}).get("job_token"), timeout=timeout)
    return response


# ------------------ ISOLATION DE LA BASE DE DEVELOPPEMENT ------------------

# La suite tourne contre la base de developpement reelle (pas de base dediee).
# Chaque passe y laissait environ 200 lignes : alertes, journal d'audit, journal
# d'administration, snapshots, rapports de synchronisation. Sur quelques
# dizaines de passes, cela avait produit 3 488 lignes d'audit et 5 270 lignes
# d'administration — au point de faire echouer un test qui cherchait son alerte
# dans la premiere page de la file de travail.
#
# Le filet ci-dessous nettoie ce que LA SESSION a cree, et rien d'autre :
# - tables a cle entiere : on releve le plus grand identifiant AVANT la session
#   et on ne supprime que les lignes creees au-dela. Une ligne anterieure ne
#   peut donc jamais etre touchee, quel que soit le contenu de la base ;
# - snapshots (cle textuelle) : on releve l'ensemble des identifiants existants
#   et on ne supprime que ceux qui apparaissent ensuite ;
# - reglages a chaud : les cles neuves sont retirees, les valeurs modifiees
#   sont RESTAUREES a leur etat d'origine (un test ne doit pas laisser
#   l'installation dans un parametrage qu'il a choisi).
#
# Ce filet ne dispense pas les tests de nettoyer derriere eux — il rattrape ce
# qui passe au travers.

# Ordre de suppression : les tables porteuses de cles etrangeres d'abord.
_INT_PK_TABLES = (
    "AlertEvent", "AlertAttachment", "Alert", "AuditTrail",
    "FpRuleTest", "FpRuleChange", "FpRule",
    "WatchlistEntityChange", "EntityRelationship",
    "BatchResult", "BatchCampaign",
    "NotificationDelivery", "HookDelivery", "LearnedEquivalence",
    "WhitelistPair", "SavedView", "ApiKey", "SyncReport", "AdminAuditLog",
    "ClientEntity", "WatchlistEntity", "Job",
)


def _models():
    from fiskr import database

    out = []
    for name in _INT_PK_TABLES:
        model = getattr(database, name, None)
        if model is not None:
            out.append(model)
    return out


@pytest.fixture(autouse=True, scope="session")
def _isolate_database():
    from sqlalchemy import func
    from fiskr import database
    from fiskr.database import AppSetting, Snapshot, WatchlistEntity, ClientEntity

    database.init_db()
    session = next(database.get_db())
    try:
        watermarks = {m.__name__: (session.query(func.max(m.id)).scalar() or 0)
                      for m in _models()}
        known_snapshots = {s.snapshot_id for s in session.query(Snapshot.snapshot_id).all()}
        known_settings = {s.key: s.value for s in session.query(AppSetting).all()}
    finally:
        session.close()

    yield

    session = next(database.get_db())
    try:
        new_snapshots = [
            s.snapshot_id for s in session.query(Snapshot.snapshot_id).all()
            if s.snapshot_id not in known_snapshots
        ]
        if new_snapshots:
            for model in (WatchlistEntity, ClientEntity):
                session.query(model).filter(
                    model.snapshot_id.in_(new_snapshots)).delete(synchronize_session=False)
        for model in _models():
            floor = watermarks.get(model.__name__, 0)
            session.query(model).filter(model.id > floor).delete(synchronize_session=False)
        if new_snapshots:
            session.query(Snapshot).filter(
                Snapshot.snapshot_id.in_(new_snapshots)).delete(synchronize_session=False)

        for setting in session.query(AppSetting).all():
            if setting.key not in known_settings:
                session.delete(setting)
            elif setting.value != known_settings[setting.key]:
                setting.value = known_settings[setting.key]
        session.commit()
    except Exception as e:   # un nettoyage qui casse ne doit pas casser la suite
        session.rollback()
        print(f"\n[conftest] nettoyage de fin de session incomplet : {e}")
    finally:
        session.close()

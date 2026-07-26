"""
Registre de progression des operations longues (imports de listes,
synchronisations, cahier de tests, re-criblage) : etat en memoire,
thread-safe, interroge par GET /api/progress (une operation) et
GET /api/progress/active (tout ce qui tourne).

Complementaire des colonnes Snapshot.processed_count/total_hint/phase
(persistees par commits periodiques) : le registre couvre aussi les phases
SANS ligne Snapshot (televersement, telechargement, empreinte) et les
operations de synchronisation. Si le processus redemarre, le front retombe
sur les colonnes Snapshot via snapshot_id.
"""
import threading
import time
from typing import Any, Dict, List, Optional

_registry: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# Une entree terminee ou abandonnee disparait apres ce delai
_TTL_SECONDS = 15 * 60

# Fenetre pendant laquelle une operation TERMINEE reste listee par
# list_active : le front (interrogation toutes les 2 s) voit ainsi toujours
# la transition et peut annoncer la fin, meme s'il etait sur un autre ecran.
_FINISHED_WINDOW_SECONDS = 120

# Phases connues (libellees en francais cote front)
PHASES = ("UPLOAD", "DOWNLOAD", "HASH", "PARSE", "PERSIST", "DELTA", "RELOAD",
          "INDEX", "SCREEN_CURRENT", "SCREEN_CANDIDATE", "RESCREEN", "QUALITY",
          "DONE")

# Natures d'operation (pilotent l'icone et le lien profond cote front)
KINDS = ("import", "sync", "backtest", "approve", "batch", "quality")


def _purge_expired_locked() -> None:
    now = time.time()
    expired = [token for token, entry in _registry.items()
               if now - entry.get("_touched", now) > _TTL_SECONDS]
    for token in expired:
        _registry.pop(token, None)


def update(token: Optional[str], *, phase: str, processed: int = 0,
           total: Optional[int] = None, snapshot_id: Optional[str] = None,
           status: str = "RUNNING", error: Optional[str] = None,
           kind: Optional[str] = None, label: Optional[str] = None,
           started_by: Optional[str] = None) -> None:
    """Ecrit/actualise l'etat d'une operation. token None = no-op (la
    progression est optionnelle partout : jamais bloquante).

    `kind`, `label` et `started_by` ne servent qu'a l'affichage global
    (GET /api/progress/active) : facultatifs, poses une seule fois a la
    premiere ecriture qui les fournit, jamais ecrases par un tick suivant."""
    if not token:
        return
    with _lock:
        _purge_expired_locked()
        entry = _registry.setdefault(token, {})
        now = time.time()
        entry.setdefault("_started", now)
        entry.update({
            "phase": phase,
            "processed": int(processed or 0),
            "total": int(total) if total else entry.get("total"),
            "snapshot_id": snapshot_id or entry.get("snapshot_id"),
            "status": status,
            "error": error,
            "_touched": now,
        })
        # Identite de l'operation : posee par le premier appel qui la connait
        for field, value in (("kind", kind), ("label", label),
                             ("started_by", started_by)):
            if value and not entry.get(field):
                entry[field] = value


def _snapshot_entry(token: str, entry: Dict[str, Any],
                    include_result: bool = False) -> Dict[str, Any]:
    """
    Vue publique d'une entree (pourcentage calcule, cles internes retirees).

    `include_result` n'est vrai que sur l'interrogation par JETON : le
    resultat d'une mesure contient des noms de clients et de fiches listees,
    il n'a rien a faire dans la liste globale des operations en cours, que le
    tableau de bord de CHAQUE utilisateur interroge en boucle.
    """
    total = entry.get("total")
    processed = entry.get("processed", 0)
    pct = round(100.0 * processed / total, 1) if total and processed <= total else None
    payload = {"result": entry.get("result")} if include_result else {}
    return {
        **payload,
        "token": token,
        "kind": entry.get("kind"),
        "label": entry.get("label"),
        "started_by": entry.get("started_by"),
        "phase": entry.get("phase"),
        "processed": processed,
        "total": total,
        "pct": pct,
        "snapshot_id": entry.get("snapshot_id"),
        "status": entry.get("status", "RUNNING"),
        "error": entry.get("error"),
        "started_at": entry.get("_started"),
        "updated_at": entry.get("_touched"),
    }


def get(token: str) -> Optional[Dict[str, Any]]:
    """Etat courant d'une operation, ou None si inconnue/expiree."""
    with _lock:
        _purge_expired_locked()
        entry = _registry.get(token)
        if entry is None:
            return None
        state = _snapshot_entry(token, entry, include_result=True)
    state.pop("token", None)  # contrat historique de GET /api/progress?id=
    return state


def list_active(finished_window: int = _FINISHED_WINDOW_SECONDS) -> List[Dict[str, Any]]:
    """Operations en cours, plus celles terminees depuis moins de
    `finished_window` secondes (pour que le front annonce la fin). Triees par
    date de demarrage : l'operation la plus ancienne d'abord."""
    now = time.time()
    with _lock:
        _purge_expired_locked()
        items = [
            _snapshot_entry(token, entry)
            for token, entry in _registry.items()
            if entry.get("status") == "RUNNING"
            or now - entry.get("_touched", now) <= finished_window
        ]
    items.sort(key=lambda item: item.get("started_at") or 0)
    return items


def finish(token: Optional[str], status: str = "DONE", error: Optional[str] = None,
           result: Optional[Dict[str, Any]] = None) -> None:
    """
    Marque l'operation terminee (l'entree reste lisible jusqu'au TTL).

    `result` porte le rendu d'une operation qui n'ecrit rien en base — une
    mesure a blanc n'a pas d'objet ou s'accrocher. Il expire avec l'entree :
    c'est un tampon de restitution, pas un stockage.
    """
    if not token:
        return
    with _lock:
        entry = _registry.get(token)
        if entry is not None:
            entry.update({"phase": "DONE" if status == "DONE" else entry.get("phase"),
                          "status": status, "error": error, "_touched": time.time()})
            if result is not None:
                entry["result"] = result


def clear() -> None:
    """Vide le registre (tests : isolation entre cas)."""
    with _lock:
        _registry.clear()

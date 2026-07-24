"""
Registre de progression des operations longues (imports de listes,
synchronisations) : etat en memoire, thread-safe, interroge par
GET /api/progress pendant que la requete d'origine est encore en vol.

Complementaire des colonnes Snapshot.processed_count/total_hint/phase
(persistees par commits periodiques) : le registre couvre aussi les phases
SANS ligne Snapshot (televersement, telechargement, empreinte) et les
operations de synchronisation. Si le processus redemarre, le front retombe
sur les colonnes Snapshot via snapshot_id.
"""
import threading
import time
from typing import Any, Dict, Optional

_registry: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()

# Une entree terminee ou abandonnee disparait apres ce delai
_TTL_SECONDS = 15 * 60

# Phases connues (libellees en francais cote front)
PHASES = ("UPLOAD", "DOWNLOAD", "HASH", "PARSE", "PERSIST", "DELTA", "RELOAD", "DONE")


def _purge_expired_locked() -> None:
    now = time.time()
    expired = [token for token, entry in _registry.items()
               if now - entry.get("_touched", now) > _TTL_SECONDS]
    for token in expired:
        _registry.pop(token, None)


def update(token: Optional[str], *, phase: str, processed: int = 0,
           total: Optional[int] = None, snapshot_id: Optional[str] = None,
           status: str = "RUNNING", error: Optional[str] = None) -> None:
    """Ecrit/actualise l'etat d'une operation. token None = no-op (la
    progression est optionnelle partout : jamais bloquante)."""
    if not token:
        return
    with _lock:
        _purge_expired_locked()
        entry = _registry.setdefault(token, {})
        entry.update({
            "phase": phase,
            "processed": int(processed or 0),
            "total": int(total) if total else entry.get("total"),
            "snapshot_id": snapshot_id or entry.get("snapshot_id"),
            "status": status,
            "error": error,
            "_touched": time.time(),
        })


def get(token: str) -> Optional[Dict[str, Any]]:
    """Etat courant d'une operation, ou None si inconnue/expiree."""
    with _lock:
        _purge_expired_locked()
        entry = _registry.get(token)
        if entry is None:
            return None
        total = entry.get("total")
        processed = entry.get("processed", 0)
        pct = round(100.0 * processed / total, 1) if total and processed <= total else None
        return {
            "phase": entry.get("phase"),
            "processed": processed,
            "total": total,
            "pct": pct,
            "snapshot_id": entry.get("snapshot_id"),
            "status": entry.get("status", "RUNNING"),
            "error": entry.get("error"),
            "updated_at": entry.get("_touched"),
        }


def finish(token: Optional[str], status: str = "DONE", error: Optional[str] = None) -> None:
    """Marque l'operation terminee (l'entree reste lisible jusqu'au TTL)."""
    if not token:
        return
    with _lock:
        entry = _registry.get(token)
        if entry is not None:
            entry.update({"phase": "DONE" if status == "DONE" else entry.get("phase"),
                          "status": status, "error": error, "_touched": time.time()})

"""
Empreinte du code charge par CE processus vs le code present sur le disque.

Le demon travailleur garde le code charge a son demarrage jusqu'a sa mort :
apres un `git pull`, l'API (recyclee par Passenger) et le demon (toujours
vivant) peuvent tourner sur deux versions differentes — la cause la plus
frequente de « correctif deploye mais symptome inchange ». Chaque processus
fige donc son empreinte a l'import (donc a son demarrage), et le diagnostic
la compare a l'empreinte du disque calculee a la demande : un ecart designe
sans ambiguite le processus a relancer.
"""
import hashlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from fiskr.config import PROJECT_ROOT

_SRC_DIR = Path(__file__).resolve().parent


def source_fingerprint() -> str:
    """Empreinte courte (12 hex) de tous les modules fiskr/*.py tels qu'ils
    sont sur le DISQUE a l'instant de l'appel — independante de ce que le
    processus courant a charge en memoire."""
    h = hashlib.sha256()
    for path in sorted(_SRC_DIR.glob("*.py")):
        h.update(path.name.encode("utf-8"))
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
    return h.hexdigest()[:12]


def git_head() -> Optional[str]:
    """SHA court du HEAD git si le deploiement est un clone — lecture directe
    des fichiers .git (pas de binaire git requis). None sinon, jamais
    bloquant."""
    try:
        git_dir = PROJECT_ROOT / ".git"
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:12] or None
        ref = head[5:].strip()
        ref_file = git_dir / ref
        if ref_file.exists():
            return ref_file.read_text(encoding="utf-8").strip()[:12] or None
        packed = git_dir / "packed-refs"
        if packed.exists():
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.endswith(" " + ref):
                    return line.split(" ", 1)[0][:12]
        return None
    except OSError:
        return None


# Figes a l'import : la version du code que CE processus execute reellement
# (au demarrage, disque et memoire coincident ; ensuite seul le disque bouge).
LOADED_FINGERPRINT = source_fingerprint()
PROCESS_STARTED_AT = datetime.utcnow().isoformat() + "Z"
PYTHON_VERSION = sys.version.split()[0]

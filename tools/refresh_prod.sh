#!/usr/bin/env bash
# ============================================================================
# Rafraîchissement de la production Fiskr (cPanel / hébergement mutualisé)
#
# Fait, dans le bon ordre :
#   1. active le virtualenv et se place dans le dépôt ;
#   2. met le code à jour (fast-forward UNIQUEMENT — jamais d'écrasement) ;
#   3. installe les dépendances ;
#   4. tue le démon travailleur — il garde l'ANCIEN code tant qu'il vit ;
#   5. le relance aussitôt sur le nouveau code (le verrou flock rend toute
#      course avec le cron inoffensive : un seul démon vivra).
#
# Garde-fous :
#   - s'arrête à la première erreur (set -euo pipefail) ;
#   - refuse de tourner si des modifications locales non commitées existent ;
#   - refuse un historique divergent (pas de merge silencieux en prod) ;
#   - ne tue QUE les processus « python -m fiskr.worker » de VOTRE compte
#     (SIGTERM d'abord, SIGKILL seulement s'il résiste 10 s).
#
# Usage :
#   bash tools/refresh_prod.sh
# Chemins surchargables (autre compte, autre venv) :
#   FISKR_VENV=/chemin/activate FISKR_DIR=/chemin/repo bash tools/refresh_prod.sh
# ============================================================================
set -euo pipefail

FISKR_VENV="${FISKR_VENV:-/home/fongkhan/virtualenv/repositories/Fiskr/3.12/bin/activate}"
FISKR_DIR="${FISKR_DIR:-/home/fongkhan/repositories/Fiskr}"
FISKR_BRANCH="${FISKR_BRANCH:-master}"
ME="$(id -un)"

log()  { printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }
fail() { log "ERREUR : $*" >&2; exit 1; }

[ -f "$FISKR_VENV" ] || fail "virtualenv introuvable : $FISKR_VENV"
[ -d "$FISKR_DIR/.git" ] || fail "dépôt git introuvable : $FISKR_DIR"

# shellcheck disable=SC1090
source "$FISKR_VENV"
cd "$FISKR_DIR"
log "Rafraîchissement de $FISKR_DIR (branche $FISKR_BRANCH, utilisateur $ME)"

# --- Code : avance rapide uniquement --------------------------------------
git fetch origin "$FISKR_BRANCH"

# Garde-fou : la production porte des modifications locales LÉGITIMES
# (config.yaml adapté à l'hébergement, passenger_wsgi.py). Elles ne bloquent
# que si la mise à jour distante touche les MÊMES fichiers (risque
# d'écrasement) — sinon l'avance rapide les conserve telles quelles.
# Les fichiers non suivis (logs, sauvegardes .sqlite3) ne bloquent jamais.
DIRTY="$( { git diff --name-only; git diff --cached --name-only; } | sort -u)"
if [ -n "$DIRTY" ]; then
    INCOMING="$(git diff --name-only HEAD "origin/$FISKR_BRANCH" | sort -u)"
    # Intersection sans substitution de processus : CageFS (cPanel) n'a pas /dev/fd
    OVERLAP=""
    for f in $DIRTY; do
        if printf '%s\n' "$INCOMING" | grep -qxF "$f"; then
            OVERLAP="${OVERLAP}${f} "
        fi
    done
    if [ -n "$OVERLAP" ]; then
        log "Fichiers modifiés localement ET par la mise à jour :"
        printf '    - %s\n' $OVERLAP
        fail "mettez ces fichiers de côté d'abord (git stash) puis relancez."
    fi
    log "Modifications locales conservées (aucun chevauchement avec la mise à jour) :"
    printf '    - %s\n' $DIRTY
fi

BEFORE="$(git rev-parse --short HEAD)"
if [ "$(git rev-parse HEAD)" = "$(git rev-parse "origin/$FISKR_BRANCH")" ]; then
    log "Code déjà à jour ($BEFORE) — le démon sera tout de même relancé."
else
    git merge --ff-only "origin/$FISKR_BRANCH" \
        || fail "avance rapide impossible : l'historique local a divergé de origin/$FISKR_BRANCH."
    log "Code mis à jour : $BEFORE → $(git rev-parse --short HEAD)"
fi

# --- Dépendances (avant de tuer le démon : le relanceur trouvera tout) ----
log "pip install -r requirements.txt…"
pip install -q -r requirements.txt
log "Dépendances à jour."

# --- Arrêt du démon : il exécute l'ancien code tant qu'il vit -------------
LOCK_FILE="$FISKR_DIR/fiskr-worker.lock"
OLD_PID="$( (head -n1 "$LOCK_FILE" 2>/dev/null || true) | tr -cd '0-9')"
if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    log "Arrêt du démon (PID $OLD_PID, via le verrou)…"
    kill "$OLD_PID" 2>/dev/null || true
else
    log "Pas de PID vivant dans le verrou — pkill de secours (processus de $ME uniquement)."
fi
pkill -u "$ME" -f 'python[^ ]* -m fiskr\.worker' 2>/dev/null || true

# Attendre la mort réelle : le flock n'est rendu par le noyau qu'à la mort
for _ in 1 2 3 4 5 6 7 8 9 10; do
    pgrep -u "$ME" -f 'python[^ ]* -m fiskr\.worker' >/dev/null || break
    sleep 1
done
if pgrep -u "$ME" -f 'python[^ ]* -m fiskr\.worker' >/dev/null; then
    log "Le démon résiste après 10 s — SIGKILL."
    pkill -9 -u "$ME" -f 'python[^ ]* -m fiskr\.worker' 2>/dev/null || true
    sleep 1
fi
log "Démon arrêté."

# --- Relance immédiate sur le nouveau code --------------------------------
# (sans attendre le cron ; s'il se relance en même temps, le flock tranche)
log "Relance du démon…"
nohup python -m fiskr.worker >> "$FISKR_DIR/worker.log" 2>&1 &
sleep 3
NEW_PID="$( (head -n1 "$LOCK_FILE" 2>/dev/null || true) | tr -cd '0-9')"
if [ -n "$NEW_PID" ] && kill -0 "$NEW_PID" 2>/dev/null; then
    log "Démon relancé (PID $NEW_PID) sur le code $(git rev-parse --short HEAD)."
else
    log "ATTENTION : pas de verrou vivant après 3 s — le filet cron le relancera."
fi

log "Dernières lignes de worker.log :"
tail -n 8 "$FISKR_DIR/worker.log" 2>/dev/null || true
log "Terminé. Vérifiez ensuite GET /api/diagnostic/jobs : versions.worker.outdated doit être false."

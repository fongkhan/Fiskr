"""
Amorce WSGI de Fiskr — la SEULE source de verite du demarrage web.

Pourquoi ce module : cPanel (« Setup Python App ») REGENERE passenger_wsgi.py
a chaque passage dans son interface (Stop/Start, modification des reglages),
ecrasant tout code qui y vivrait — vu en production : site a terre sur
« module 'wsgi' has no attribute 'application' ». Toute la logique vit donc
ici, et les points d'entree a la racine (passenger_wsgi.py, api.py) se
reduisent a un import : quel que soit le fichier que Passenger ou le stub
cPanel charge, la meme application demarre.
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Sorties standard vers des fichiers locaux, en ecriture immediate : sous
# Passenger il n'y a pas de terminal, ces fichiers sont le seul journal.
sys.stdout = open(os.path.join(PROJECT_ROOT, 'passenger_stdout.log'), 'a', encoding='utf-8')
sys.stderr = open(os.path.join(PROJECT_ROOT, 'passenger_stderr.log'), 'a', encoding='utf-8')


class _Unbuffered:
    def __init__(self, stream):
        self.stream = stream

    def write(self, data):
        self.stream.write(data)
        self.stream.flush()

    def writelines(self, datas):
        self.stream.writelines(datas)
        self.stream.flush()

    def __getattr__(self, attr):
        return getattr(self.stream, attr)


sys.stdout = _Unbuffered(sys.stdout)
sys.stderr = _Unbuffered(sys.stderr)

print("\n--- Démarrage de l'application via Passenger WSGI ---")

try:
    from a2wsgi import ASGIMiddleware

    from fiskr.api import app

    application = ASGIMiddleware(app)
    print("Application FastAPI enveloppée avec succès dans ASGIMiddleware.")
except Exception:
    import traceback
    print("Échec du démarrage de l'application :")
    traceback.print_exc()
    raise


# --- Prechargement du cache moteur ----------------------------------------
# ASGIMiddleware ne construit qu'un scope « http » par requete : elle
# n'implemente PAS le protocole ASGI `lifespan`. Le demarrage de FastAPI ne
# s'execute donc jamais ici, et rien n'avait jamais charge le cache moteur de
# ce processus. Consequence mesuree en production : le PREMIER criblage payait
# le chargement complet en pleine requete (64 s, puis 5,6 s a chaud) et la
# palette Ctrl+K se repliait sur la base (30,9 s).
#
# On le charge donc au demarrage — mais DANS UN THREAD DE FOND, et c'est
# deliberé : bloquer ici retarderait le demarrage d'une minute, et Passenger
# tue un processus qui n'a pas fini de demarrer (`passenger_start_timeout`,
# 90 s par defaut). Un depassement mettrait le site a terre — bien pire que la
# lenteur qu'on corrige. L'application repond donc immediatement pendant que
# le cache chauffe ; une requete arrivee entre-temps se comporte exactement
# comme avant, et le verrou de `fiskr.api` garantit qu'un criblage attendra ce
# chargement-ci au lieu d'en lancer un second en parallele.
#
# FISKR_PRELOAD_CACHE=0 desactive le prechargement sans redeploiement.
if os.environ.get("FISKR_PRELOAD_CACHE", "1").strip() not in ("0", "false", "no"):
    import threading

    def _prechauffer():
        from fiskr.api import warm_watchlist_cache
        import time
        debut = time.monotonic()
        ok = warm_watchlist_cache()
        duree = time.monotonic() - debut
        print(f"Cache moteur : {'chargé' if ok else 'NON chargé'} "
              f"en {duree:.1f} s.")

    # daemon : ce thread ne doit jamais retenir l'arret du processus.
    threading.Thread(target=_prechauffer, name="fiskr-cache-warmup",
                     daemon=True).start()
    print("Préchargement du cache moteur lancé en arrière-plan.")

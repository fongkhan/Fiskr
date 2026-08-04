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

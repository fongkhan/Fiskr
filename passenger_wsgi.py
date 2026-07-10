import os
import sys

# Répertoire racine du projet
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# Redirection des sorties standards et d'erreurs vers des fichiers locaux
sys.stdout = open(os.path.join(PROJECT_ROOT, 'passenger_stdout.log'), 'a', encoding='utf-8')
sys.stderr = open(os.path.join(PROJECT_ROOT, 'passenger_stderr.log'), 'a', encoding='utf-8')

# Écriture immédiate (unbuffered) pour voir les logs en temps réel
class Unbuffered(object):
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

sys.stdout = Unbuffered(sys.stdout)
sys.stderr = Unbuffered(sys.stderr)

print("\n--- Démarrage de l'application via Passenger WSGI ---")

try:
    # 1. Importation du middleware ASGI vers WSGI
    from a2wsgi import ASGIMiddleware
    
    # 2. Importation de l'application FastAPI
    from fiskr.api import app
    
    # 3. Enveloppement pour Passenger
    application = ASGIMiddleware(app)
    print("Application FastAPI enveloppée avec succès dans ASGIMiddleware.")
    
except Exception as e:
    import traceback
    print("Échec du démarrage de l'application :")
    traceback.print_exc()
    raise e

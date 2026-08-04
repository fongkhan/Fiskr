"""
Point d'entree pour le stub genere par cPanel (« Application startup file »).

Quand on passe par l'interface Setup Python App, cPanel reecrit
passenger_wsgi.py en un stub qui charge CE fichier (module nomme « wsgi »)
et y lit `application`. Ce fichier doit donc exister et exposer la meme
application que passenger_wsgi.py — la logique vit dans fiskr/wsgi.py.
NE PAS SUPPRIMER, meme s'il semble redondant.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fiskr.wsgi import application  # noqa: E402, F401

"""
Point d'entree Passenger. NE RIEN AJOUTER ICI : cPanel (« Setup Python App »)
regenere ce fichier a chaque passage dans son interface — tout code qui y
vivrait serait perdu. La logique de demarrage vit dans fiskr/wsgi.py, et
api.py (racine) expose la meme application pour le stub genere par cPanel.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fiskr.wsgi import application  # noqa: E402, F401

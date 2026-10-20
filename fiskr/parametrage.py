"""
Ce que l'EPOQUE fait relire dans un processus : le parametrage du moteur qui
vit en memoire de processus.

POURQUOI CE MODULE EXISTE
-------------------------
Les capacites du moteur (fiskr/capabilities.py) et les equivalences de noms
(fiskr/resources.py) sont lues une fois puis gardees en memoire : le criblage
compare des milliers de paires par seconde, il ne relit pas la base a chaque
comparaison. Seul le processus qui recoit le geste vidait sa memoire. Les
autres — les N processus web de Passenger, et surtout le demon travailleur,
qui porte le re-criblage et les cahiers de tests — n'ont que la base pour
canal : l'epoque du cache de production (fiskr/settings.py).

Deux trous, reproduits avec un processus B simule dont le seul lien avec A
est la base :
- le rechargement par epoque relisait les capacites et l'ACTIVATION des
  equivalences, pas leur CONTENU : une equivalence approuvee dans A, epoque
  incrementee, B scorait toujours sans elle ;
- le demon ne passe par ce rechargement que lorsqu'il crible un profil
  (campagne, upsert) : son re-criblage et ses cahiers de tests gardaient les
  capacites lues au premier passage. Translitteration coupee dans A, epoque
  incrementee : le re-criblage suivant du demon la voyait active.

Une seule definition de « ce que l'epoque fait relire », ici ; le chargement
du cache (fiskr.api.load_watchlist_cache) l'appelle sans condition, les
chemins qui criblent sans tenir le cache (fiskr.rescreen, fiskr.backtest)
l'appellent quand l'epoque a bouge depuis leur derniere lecture.
"""
import logging
from typing import Optional, Tuple

logger = logging.getLogger("fiskr.parametrage")

# (base, epoque) de la derniere relecture dans CE processus. La base fait partie
# de la cle pour la meme raison que dans fiskr/univers.py : une epoque n'est
# qu'un compteur, deux bases au meme compteur ne disent pas la meme chose.
_vue: Optional[Tuple[str, int]] = None


def _epoque(db) -> Tuple[str, int]:
    from fiskr.settings import watchlist_epoch
    return str(db.get_bind().url), watchlist_epoch(db)


def relire(db) -> None:
    """
    Relit, sans condition, les capacites du moteur et les equivalences de noms
    (activation ET contenu : fichiers et equivalences approuvees en base).

    L'epoque est lue AVANT : un geste survenu pendant la relecture laisse une
    epoque plus ancienne que ce qui a ete lu, donc une relecture de plus au
    prochain appel — jamais l'inverse, qui figerait l'ancien parametrage.
    """
    global _vue
    from fiskr import capabilities as caps
    from fiskr import resources
    epoque = _epoque(db)
    caps.invalidate_context()
    resources.actualiser(db)
    _vue = epoque


def a_jour(db) -> None:
    """Relit le parametrage si l'epoque a change depuis la derniere relecture
    de ce processus. Une lecture d'AppSetting quand rien n'a bouge."""
    if _epoque(db) != _vue:
        relire(db)

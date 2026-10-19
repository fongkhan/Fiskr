"""
Les elements d'identification d'un client : les champs que le moteur LIT
pour trouver un candidat et noter une correspondance.

POURQUOI UN RELEVE ET PAS UNE LISTE
-----------------------------------
Les lignes directrices conjointes DGT/ACPR sur le gel des avoirs (18 mars
2026) demandent de cribler un client « a chaque mise a jour des elements
d'identification » (§141). Encore faut-il savoir lesquels le sont pour CE
moteur. Une liste tenue a la main a cote du scoring dirait vrai le jour de son
ecriture ; le jour ou le moteur apprend a lire un champ de plus — un
identifiant, une denomination alternative —, elle se tairait, et la mise a
jour de ce champ cesserait de declencher le criblage sans que rien ne le
signale.

La liste est donc RELEVEE sur le moteur lui-meme. Un profil sonde, dont chaque
lecture est notee, traverse les trois etages qui decident d'une
correspondance :

- le quality gate et le nettoyage de la sonde (`evaluate_and_clean`) ;
- les cles de blocking, sur TOUTES les composantes possibles : le layout se
  regle a chaud, un champ hors du layout du jour peut y entrer demain ;
- le rapprochement complet contre une fiche listee (`match_entities` :
  identifiants exacts, noms, date de naissance, genre, geographie).

Toutes les capacites du moteur sont actives pendant le releve, sur ce thread
seulement (`capabilities.use_context`) : une capacite coupee aujourd'hui ne
doit pas retirer durablement un champ de la liste — elle peut etre rallumee,
et la mise a jour d'hier n'aurait alors jamais ete criblee.

Ce qui est lu est un element d'identification. Le reste (telephone, segment,
notation de risque, statut de la relation...) ne change aucun score : le
modifier ne demande pas de recribler. Les regles anti-faux positifs, elles,
peuvent lire n'importe quel champ du profil — elles ne trouvent ni ne notent
rien, elles tranchent une correspondance deja trouvee.
"""
from functools import lru_cache
from typing import FrozenSet


class _Releve(dict):
    """Dictionnaire qui note chaque cle qu'on lui demande, presente ou non :
    `get` d'un champ absent est une lecture comme une autre — c'est celle d'un
    champ que le moteur exploiterait s'il etait renseigne."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lues = set()

    def get(self, cle, defaut=None):
        self.lues.add(cle)
        return super().get(cle, defaut)

    def __getitem__(self, cle):
        self.lues.add(cle)
        return super().__getitem__(cle)

    def __contains__(self, cle):
        self.lues.add(cle)
        return super().__contains__(cle)


# Les deux natures de client prennent des chemins differents dans le moteur
# (prenom/nom/nom de naissance d'un cote, raison sociale de l'autre) : les
# deux sont relevees. Noms quelconques, mais renseignes — sans nom, le quality
# gate refuse et le rapprochement s'arrete avant les ajustements.
_SONDES = (
    {"client_type": "PP", "client_first_name": "Sonde", "client_last_name": "Releve"},
    {"client_type": "PM", "client_company_name": "Sonde Releve"},
)

# Fiche listee volontairement SANS rapport avec la sonde : aucun identifiant
# commun, donc aucun hard match qui court-circuiterait les etages suivants.
_FICHE = {
    "entity_id": "RELEVE-1", "entity_type": "I", "primary_name": "Zzyzx Qwerty",
    "individual_name_parsed": {"first_name": "Zzyzx", "last_name": "Qwerty",
                               "maiden_name": "Wxyz"},
    "aliases": {"high_priority": ["Qwerty Zzyzx"], "low_priority": []},
    "dates_of_birth": ["1901-01-01"], "gender": "F",
    "countries": {"citizenship": ["ZZ"], "residence": [], "birth_country": [],
                  "jurisdiction_country": []},
}


@lru_cache(maxsize=1)
def champs_lus_par_le_moteur() -> FrozenSet[str]:
    """Toutes les cles de profil client que le moteur de criblage lit."""
    from fiskr import capabilities as caps
    from fiskr.blocking import generate_blocking_keys
    from fiskr.config import config
    from fiskr.quality import evaluate_and_clean
    from fiskr.scoring import match_entities
    from fiskr.settings import BLOCKING_COMPONENTS

    canal = caps.CHANNEL_SCREENING
    blocking = {"blocking": {"custom_key_layout": list(BLOCKING_COMPONENTS),
                             "channel": canal}}
    etages = (
        lambda sonde: evaluate_and_clean(sonde, channel=canal),
        lambda sonde: generate_blocking_keys(sonde, blocking),
        lambda sonde: match_entities(sonde, dict(_FICHE), config),
    )
    lues = set()
    with caps.use_context(canal, caps.CAPABILITY_CATALOG):
        for modele in _SONDES:
            for etage in etages:
                sonde = _Releve(modele)
                etage(sonde)
                lues |= sonde.lues
    return frozenset(lues)


def elements_d_identification(champs) -> list:
    """Ceux de ces champs qui sont des elements d'identification, tries."""
    lus = champs_lus_par_le_moteur()
    return sorted(c for c in champs if c in lus)

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
import hashlib
import json
import re
import sys
import unicodedata
from functools import lru_cache
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional, Tuple


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


@lru_cache(maxsize=1)
def champs_du_profil_lus_par_le_moteur() -> Tuple[str, ...]:
    """
    Les elements d'identification d'un PROFIL client, tels qu'un appelant les
    porte : les champs `client_*` (et `transaction_*` pour les identifiants de
    moyen de transport) que le releve a vus lire. La sonde note aussi les cles
    internes que le moteur derive en chemin (`primary_name`, `__joker_*`...) :
    elles ne sont pas des champs qu'on saisit. `client_id` n'en est pas : c'est
    la cle de la relation, pas un element d'identification de la personne.

    Lu par l'empreinte d'une paire de liste blanche (fiskr/empreintes.py) et
    par l'identite d'une partie liee (fiskr/parties.py) : un beneficiaire
    effectif s'identifie avec les MEMES champs que le client — ceux que le
    moteur sait comparer, et rien d'autre.

    Echec franc si le releve ne trouve rien : une identite definie sur rien
    couvrirait tout.
    """
    champs = {c for c in champs_lus_par_le_moteur()
              if c.startswith(("client_", "transaction_"))}
    champs.discard("client_id")
    if not champs:
        raise RuntimeError("Aucun champ client lu par le moteur n'a été trouvé : "
                           "l'identification d'un profil ne peut pas être établie.")
    return tuple(sorted(champs))


# Cle d'un profil client qui porte les profils de ses parties liees
# (fiskr/parties.py) : beneficiaires effectifs, mandataires, dirigeants,
# associes. Ce ne sont pas des champs que le moteur lit sur le client — chaque
# partie est criblee pour elle-meme —, mais elles entrent dans l'empreinte
# d'identification de la RELATION : un beneficiaire effectif ajoute ou
# renomme est une « mise a jour des elements d'identification » (LD DGT/ACPR
# §141), et l'import suivant doit recribler ce client et ses parties.
CLE_DES_PARTIES = "_parties_liees"


def empreinte_d_identification(profil) -> str:
    """
    Empreinte des elements d'identification d'un profil client : ce que le
    moteur lit, et rien d'autre.

    Un import de referentiel ne recrible que les clients dont elle a change
    depuis leur dernier criblage. L'empreinte de la fiche entiere
    (`entity_checksum`) ne convient pas : un telephone ou un segment modifie
    la ferait changer et recriblerait un client dont rien de ce que le moteur
    lit n'a bouge. Celle-ci suit le releve : le jour ou le moteur apprend a
    lire un champ de plus, toutes les empreintes changent — et le prochain
    import recrible tout le monde, ce qui est juste, puisque le criblage
    d'hier n'avait pas regarde ce champ.

    Les parties liees du profil (`CLE_DES_PARTIES`) y entrent par leurs
    propres elements d'identification. Un client SANS partie garde exactement
    l'empreinte d'avant : la mise a jour ne recrible personne pour rien.
    """
    lus = champs_lus_par_le_moteur()
    identite = {cle: profil[cle] for cle in sorted(lus) if cle in profil}
    parties = profil.get(CLE_DES_PARTIES)
    if parties:
        identite[CLE_DES_PARTIES] = sorted(
            json.dumps({cle: partie[cle] for cle in sorted(lus) if cle in partie},
                       sort_keys=True, default=str, ensure_ascii=False)
            for partie in parties)
    return hashlib.sha256(json.dumps(identite, sort_keys=True, default=str,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------ CE QUE LE MOTEUR COMPARE, ET A QUOI ------------------
#
# Le releve ci-dessus dit QUELS champs du client le moteur lit. L'alerte a
# besoin d'une chose de plus : avec QUEL champ de la fiche listee chacun est
# compare. L'analyste qui coche « identite verifiee (nom, date de naissance,
# pays) » ouvrait trois modales empilees et la palette pour mettre les deux
# cotes en regard (mesure : environ neuf gestes par alerte). Le tableau de
# comparaison de l'alerte lit ce releve-ci, pas une liste de correspondances
# tenue a la main : le jour ou le moteur compare un champ de plus, la colonne
# le montre.
#
# Methode : le meme couple de sondes traverse `match_entities`, chaque lecture
# notee dans UN journal commun (cote client ou cote fiche, dans l'ordre). Le
# moteur conclut chaque comparaison par une etape nommee ; les lectures qui la
# precedent sont celles qu'elle compare. Les etapes sont reperees a leur sortie
# par un profileur pose sur CE thread seulement (`sys.setprofile`) : rien n'est
# remplace dans le module de scoring, un criblage servi en parallele ne voit
# rien.

# Etapes qui CONCLUENT une comparaison, et la famille qu'elles produisent dans
# l'arbre de decision : `adjustments` porte dob, gender et geography, le hard
# match les identifiants, le score de base les noms.
ETAPES_DE_COMPARAISON = {
    "check_hard_matches": "identifiers",
    "compute_base_score": "names",
    "calculate_dob_adjustment": "dob",
    "calculate_gender_adjustment": "gender",
    "calculate_geography_adjustment": "geography",
}


class _Journal(dict):
    """Comme `_Releve`, mais chaque lecture part dans un journal partage avec
    l'autre cote, dans l'ordre ou le moteur la fait."""

    def __init__(self, contenu, cote, journal):
        super().__init__(contenu)
        self.cote = cote
        self.journal = journal

    def get(self, cle, defaut=None):
        self.journal.append((self.cote, cle))
        return super().get(cle, defaut)

    def __getitem__(self, cle):
        self.journal.append((self.cote, cle))
        return super().__getitem__(cle)

    def __contains__(self, cle):
        self.journal.append((self.cote, cle))
        return super().__contains__(cle)


class Comparaison(NamedTuple):
    """Une famille de comparaison du moteur : les champs du client et ceux de
    la fiche qu'elle met en regard. `paires` decoupe la famille en
    comparaisons elementaires — le moteur lit un champ client puis son
    homologue cote fiche (un identifiant contre le meme identifiant)."""
    famille: str
    client: Tuple[str, ...]
    fiche: Tuple[str, ...]
    paires: Tuple[Tuple[Tuple[str, ...], Tuple[str, ...]], ...]


def _paires(lectures) -> List[Tuple[Tuple[str, ...], Tuple[str, ...]]]:
    """Alternance client puis fiche : une lecture client apres une lecture
    fiche ouvre une nouvelle comparaison elementaire."""
    paires: List[Tuple[List[str], List[str]]] = []
    for cote, cle in lectures:
        if not paires or (cote == "client" and paires[-1][1]):
            paires.append(([], []))
        cible = paires[-1][0] if cote == "client" else paires[-1][1]
        if cle not in cible:
            cible.append(cle)
    return [(tuple(c), tuple(f)) for c, f in paires]


@lru_cache(maxsize=1)
def comparaisons_du_moteur() -> Tuple[Comparaison, ...]:
    """Familles de comparaison de `match_entities`, une par etape de
    `ETAPES_DE_COMPARAISON`.

    Echec franc si une etape attendue n'est jamais vue, ou ne compare rien
    d'un cote : un tableau de comparaison bati sur un releve casse montrerait
    des colonnes vides comme si les donnees manquaient."""
    from fiskr import capabilities as caps
    from fiskr.config import config
    from fiskr.scoring import match_entities

    canal = caps.CHANNEL_SCREENING
    familles: Dict[str, Dict[Tuple[str, ...], List[str]]] = {}
    for modele in _SONDES:
        journal: List[Tuple[str, str]] = []

        def crochet(cadre, evenement, _arg, journal=journal):
            if evenement == "return" and cadre.f_globals.get("__name__") == "fiskr.scoring":
                famille = ETAPES_DE_COMPARAISON.get(cadre.f_code.co_name)
                if famille:
                    journal.append(("etape", famille))

        precedent = sys.getprofile()
        with caps.use_context(canal, caps.CAPABILITY_CATALOG):
            sys.setprofile(crochet)
            try:
                match_entities(_Journal(modele, "client", journal),
                               _Journal(_FICHE, "fiche", journal), config)
            finally:
                sys.setprofile(precedent)

        lectures: List[Tuple[str, str]] = []
        for cote, cle in journal:
            if cote != "etape":
                lectures.append((cote, cle))
                continue
            # Les lectures depuis l'etape precedente sont celles que CETTE
            # etape compare. Une etape rappelee sans lecture neuve (le score de
            # base, une fois par couple de noms) n'ajoute rien.
            if lectures:
                par_fiche = familles.setdefault(cle, {})
                for client, fiche in _paires(lectures):
                    deja = par_fiche.setdefault(fiche, [])
                    deja.extend(c for c in client if c not in deja)
            lectures = []
        # Ce qui suit la derniere etape (seuil de coupure de la liste) ne
        # compare rien au client : ignore.

    manquantes = [f for f in ETAPES_DE_COMPARAISON.values() if f not in familles]
    if manquantes:
        raise RuntimeError("Relevé des comparaisons du moteur incomplet : étape(s) "
                           f"{', '.join(manquantes)} jamais vue(s).")
    resultat = []
    for famille in dict.fromkeys(ETAPES_DE_COMPARAISON.values()):
        paires = tuple((tuple(client), fiche) for fiche, client in familles[famille].items())
        client = tuple(dict.fromkeys(c for cl, _f in paires for c in cl))
        fiche = tuple(dict.fromkeys(f for _c, fi in paires for f in fi))
        if not client or not fiche:
            raise RuntimeError(f"Relevé des comparaisons du moteur : la famille {famille} "
                               "ne compare rien d'un côté.")
        resultat.append(Comparaison(famille, client, fiche, paires))
    return tuple(resultat)


# ------------------ IDENTIFIANT CALCULE (criblage sans identifiant) ------------------
#
# Releve en production : 24 decisions sur 28 portaient un identifiant
# `SCREEN-<alea>` tire par le formulaire (Math.random() * 10 000, collisions
# possibles), et les deux alertes de production visaient la meme fiche, au meme
# score, sous deux identifiants differents — jamais rapprochees. Or la
# deduplication des alertes (fiskr/alerts.py), la liste blanche et la vue 360
# reposent sur cet identifiant : un client qui en change a chaque saisie est
# un client nouveau a chaque saisie. Sans identifiant fourni, il est donc
# DERIVE de ce qui le decrit — nom normalise, date de naissance, type — et le
# meme client saisi deux fois retombe sur la meme alerte.
#
# Deux homonymes saisis sans date de naissance partagent cet identifiant : rien
# ne les distingue dans ce qui est saisi, et leur criblage est le meme. Ce qui
# les distinguerait (pays, documents) entre dans l'empreinte du client
# (fiskr/empreintes.py) : une paire de liste blanche posee pour l'un ne couvre
# pas l'autre, elle passe « a reexaminer ».

PREFIXE_IDENTIFIANT_CALCULE = "CALC-"


def _forme_stable(texte: Any) -> str:
    """Casse, accents, ponctuation et blancs ne distinguent personne : deux
    saisies d'un meme nom doivent donner le meme identifiant."""
    t = unicodedata.normalize("NFKD", str(texte or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    return " ".join(re.sub(r"[\W_]+", " ", t).split())


def identifiant_calcule(client: Dict[str, Any]) -> Optional[str]:
    """Identifiant deterministe d'un client saisi sans identifiant, ou None
    s'il n'a pas de nom (le quality gate le refusera de toute facon)."""
    personne_physique = client.get("client_type") in ("PP", "I")
    if personne_physique:
        nom = f"{client.get('client_first_name') or ''} {client.get('client_last_name') or ''}"
    else:
        nom = client.get("client_company_name") or ""
    nom = _forme_stable(nom)
    if not nom:
        return None
    cle = "|".join(("PP" if personne_physique else "PM", nom,
                    str(client.get("client_dob") or "").strip()))
    return PREFIXE_IDENTIFIANT_CALCULE + hashlib.sha256(cle.encode("utf-8")).hexdigest()[:12].upper()

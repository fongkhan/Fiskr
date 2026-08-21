"""
Rarete des noms : ce que vaut une correspondance selon la frequence du nom
dans le corpus liste.

Le probleme
-----------
Le criblage conserve TOUTES les correspondances au-dessus du seuil — c'est
l'exigence d'audit. Sa consequence est mesuree en production : « Mohammed Ali »
sans pays remonte 17 649 candidats et 2 976 correspondances >= seuil, dont
plusieurs dizaines a 100,00. Ce ne sont pas des erreurs de score : les noms
SONT identiques. Aucune metrique de chaine ne peut les separer, et aucune ne le
devrait.

Ce qui manque a ces 2 976 lignes n'est pas dans le score. C'est une information
que le score ne porte pas : « MOHAMMED » et « ALI » sont, dans le corpus liste,
des mots omnipresents. Rapprocher deux noms sur ces mots-la n'identifie
personne. Rapprocher deux noms sur « TYURIN » ou « QUANGUO » identifie presque
surement.

Ce que fait ce module
---------------------
Il compte, sur le corpus liste effectivement crible, dans COMBIEN de fiches
chaque mot de nom apparait (frequence documentaire), puis rend pour un couple
de noms rapproches un profil lisible :

    MOHAMMED  porte par 24 318 fiches (2,9 % du corpus)
    ALI       porte par  9 887 fiches (1,2 % du corpus)
    -> le rapprochement ne repose que sur des mots tres repandus

Ce profil est passe aux regles anti-faux positifs (`ctx["rarity"]`) et affiche
a l'analyste. Il ne DEPLACE AUCUN SCORE : ajouter un terme au score deplacerait
d'un coup tous les seuils calibres, toutes les regles ecrites contre eux et
tous les cahiers de tests homologues. Le signal est donne a qui decide — la
regle, l'analyste — pas impose au moteur.

Choix de mesure
---------------
- Frequence DOCUMENTAIRE : une fiche qui ecrit « MOHAMMED MOHAMMED » compte
  pour UNE fiche portant MOHAMMED, pas deux. C'est bien « combien de fiches
  puis-je confondre », pas « combien de fois le mot est ecrit ».
- Le corpus indexe les memes chaines que le moteur compare : nom principal ET
  alias haute priorite, puisque les deux produisent des correspondances.
- Les mots d'UN caractere sont ignores : ce sont des initiales et des
  particules, jamais un element d'identification.
- La table ne CONSERVE que les mots vus au moins `DF_MIN_CONSERVE` fois. Un mot
  absent de la table est rare par construction — c'est l'ecrasante majorite des
  patronymes — et la table tient alors en quelques dizaines de milliers
  d'entrees au lieu de plusieurs centaines de milliers, dans chaque processus.
"""
import logging
import math
from typing import Any, Dict, Iterable, List, Optional

from fiskr import capabilities as caps

logger = logging.getLogger("fiskr.rarete")

# Un mot vu moins de trois fois n'apprend rien qu'on ne sache deja : il est
# rare. Ne pas le stocker divise la table par un ordre de grandeur.
DF_MIN_CONSERVE = 3

# Plafond du nombre de mots conserves. Mesure par extrapolation (loi de Heaps
# ajustee sur un echantillon reel de 12 500 fiches de la production) : le
# corpus complet de 832 470 fiches porte environ 584 000 mots distincts, dont
# ~346 000 vus au moins trois fois. Les garder tous couterait ~40 Mo par
# processus pour une information dont le signal n'a pas besoin : seuls les mots
# REPANDUS decident quelque chose, et il y en a quelques centaines. On garde
# les 20 000 plus frequents — deux ordres de grandeur de marge — et tout le
# reste est, par construction, plus rare que le dernier conserve.
MAX_TOKENS_TABLE = 20_000

# Longueur minimale d'un mot retenu (les initiales et particules d'un caractere
# ne participent a aucune identification).
LONGUEUR_MIN_TOKEN = 2

# Un mot porte par au moins cette part du corpus est dit REPANDU. 0,25 % de
# 832 470 fiches = 2 081 fiches : se rapprocher d'un tel mot laisse deux mille
# confusions possibles, ce n'est pas une identification.
PART_REPANDU = 0.0025

# Plancher ABSOLU du seuil. Sur un petit corpus, une part relative rendrait
# « repandu » un mot porte par trois fiches — or trois confusions se lisent, ce
# n'est pas une volumetrie. En dessous de ce nombre de fiches, aucun mot n'est
# dit repandu, quelle que soit la taille du corpus.
SEUIL_REPANDU_MIN = 25


def _tokens(texte: str, channel: str = caps.CHANNEL_SCREENING) -> List[str]:
    """Mots normalises d'un nom, dans la meme forme que celle que compare le
    moteur (translitteration et diacritiques selon les capacites du canal)."""
    from fiskr.quality import strip_accents_for_matching
    if not texte:
        return []
    normalise = strip_accents_for_matching(str(texte).strip(), channel).upper()
    return [t for t in normalise.split() if len(t) >= LONGUEUR_MIN_TOKEN]


class TableRarete:
    """Frequence documentaire des mots de nom sur un corpus liste donne."""

    __slots__ = ("total", "_df", "_seuil_repandu", "empreinte", "plancher")

    def __init__(self, total: int, df: Dict[str, int], empreinte: str = "",
                 plancher: int = DF_MIN_CONSERVE):
        self.total = max(0, int(total))
        self._df = df
        self._seuil_repandu = max(SEUIL_REPANDU_MIN, int(self.total * PART_REPANDU))
        # Frequence du dernier mot conserve : tout mot absent de la table est
        # PLUS RARE que celui-la. C'est ce que rend `df()` pour un inconnu —
        # une borne SUPERIEURE, jamais zero, pour qu'un mot inconnu ne fasse
        # jamais cloturer.
        self.plancher = max(1, int(plancher))
        # Ce qui identifie le corpus mesure : la table voyage avec l'audit,
        # une rarete se relit avec le corpus qui l'a produite.
        self.empreinte = empreinte

    def __len__(self) -> int:
        return len(self._df)

    @property
    def seuil_repandu(self) -> int:
        return self._seuil_repandu

    def df(self, token: str) -> int:
        """Nombre de fiches portant ce mot. Un mot absent de la table est plus
        rare que le dernier conserve : on rend cette borne superieure, jamais
        zero — un mot inconnu doit compter comme RARE, donc ne rien faire
        cloturer."""
        return self._df.get(token, max(1, self.plancher - 1))

    def idf(self, token: str) -> float:
        """Information portee par ce mot, en nats : log(N / df)."""
        if self.total <= 0:
            return 0.0
        return math.log(self.total / max(1, self.df(token)))

    def repandu(self, token: str) -> bool:
        return self.df(token) >= self._seuil_repandu

    # ------------------ PROFIL D'UN RAPPROCHEMENT ------------------

    def profil(self, nom_client: str, nom_liste: str,
               channel: str = caps.CHANNEL_SCREENING) -> Dict[str, Any]:
        """
        Ce que vaut, en information, le rapprochement de ces deux noms.

        `couverture` repond a « quelle part de l'identite du nom LISTE ai-je
        effectivement rapprochee ? » : 1,0 quand tous ses mots sont partages,
        moins quand le nom liste porte des mots que le client n'a pas.
        """
        tokens_client = set(_tokens(nom_client, channel))
        tokens_liste = _tokens(nom_liste, channel)
        partages = sorted(tokens_client.intersection(tokens_liste))

        detail = [{"token": t, "df": self.df(t),
                   "part": round(100.0 * self.df(t) / self.total, 4) if self.total else 0.0,
                   "repandu": self.repandu(t)}
                  for t in sorted(partages, key=lambda t: -self.df(t))]

        idf_partage = sum(self.idf(t) for t in partages)
        idf_liste = sum(self.idf(t) for t in set(tokens_liste))
        df_max = max((self.df(t) for t in partages), default=0)
        df_min = min((self.df(t) for t in partages), default=0)

        return {
            "disponible": True,
            "corpus": self.total,
            "tokens": detail,
            # Le mot partage le PLUS discriminant : c'est lui qui decide si le
            # rapprochement identifie quelqu'un. Un seul mot rare suffit.
            "df_min": df_min,
            "df_max": df_max,
            "seuil_repandu": self._seuil_repandu,
            # En dessous de ce nombre de fiches, la table ne compte plus : un
            # `df` egal a `plancher - 1` signifie « au plus autant », pas
            # « exactement autant ».
            "plancher": self.plancher,
            "information": round(idf_partage, 3),
            "information_nom_liste": round(idf_liste, 3),
            # Quand TOUS les mots du nom liste sont portes par TOUT le corpus,
            # l'information est nulle des deux cotes et le rapport n'existe
            # pas : on retombe alors sur la part des mots partages, sinon un
            # nom entierement rapproche afficherait « 0 % rapproche ».
            "couverture": (round(idf_partage / idf_liste, 3) if idf_liste > 0
                           else (round(len(partages) / len(set(tokens_liste)), 3)
                                 if tokens_liste else 0.0)),
            "rarete": self._rarete(df_min) if partages else 0.0,
            # VRAI quand TOUS les mots partages sont repandus : le rapprochement
            # ne repose alors que sur des mots que des milliers de fiches
            # portent. Faux des qu'un seul mot rare est partage.
            "nom_repandu": bool(partages) and all(self.repandu(t) for t in partages),
            # Rapprochement purement flou : aucun mot exactement commun. La
            # rarete ne dit rien de ce cas, et ne doit rien y faire cloturer.
            "sans_token_commun": not partages,
        }

    def _rarete(self, df: int) -> float:
        """Rarete du mot partage le plus discriminant, ramenee sur 0-100 :
        100 = mot unique dans le corpus, 0 = mot porte par tout le corpus."""
        if self.total <= 1:
            return 0.0
        plafond = math.log(self.total)
        valeur = math.log(self.total / max(1, df))
        return round(max(0.0, min(100.0, 100.0 * valeur / plafond)), 2)


def profil_indisponible() -> Dict[str, Any]:
    """
    Profil rendu quand aucune table n'est construite (processus qui n'a pas
    charge le cache, corpus vide). TOUS les drapeaux sont au repos : une regle
    ecrite contre la rarete ne cloture alors rien, elle ne plante pas.
    """
    return {
        "disponible": False, "corpus": 0, "tokens": [], "plancher": 0,
        "df_min": 0, "df_max": 0, "seuil_repandu": 0,
        "information": 0.0, "information_nom_liste": 0.0, "couverture": 0.0,
        "rarete": 0.0, "nom_repandu": False, "sans_token_commun": True,
    }


# ------------------ CONSTRUCTION ET CACHE DE PROCESSUS ------------------

def noms_de_fiche(entite: Dict[str, Any]) -> List[str]:
    """Chaines d'une fiche listee que le moteur compare effectivement : nom
    principal et alias haute priorite."""
    noms = [entite.get("primary_name")]
    alias = entite.get("aliases")
    if isinstance(alias, dict):
        noms.extend(alias.get("high_priority") or [])
    elif isinstance(alias, list):
        noms.extend(alias)
    return [str(n) for n in noms if n and str(n).strip()]


def construire(entites: Iterable[Dict[str, Any]],
               channel: str = caps.CHANNEL_SCREENING,
               empreinte: str = "") -> TableRarete:
    """Compte la frequence DOCUMENTAIRE des mots sur un corpus de fiches."""
    brut: Dict[str, int] = {}
    total = 0
    for entite in entites:
        total += 1
        vus = set()
        for nom in noms_de_fiche(entite):
            vus.update(_tokens(nom, channel))
        for token in vus:
            brut[token] = brut.get(token, 0) + 1
    retenus = sorted(((n, t) for t, n in brut.items() if n >= DF_MIN_CONSERVE),
                     reverse=True)[:MAX_TOKENS_TABLE]
    conserve = {t: n for n, t in retenus}
    plancher = retenus[-1][0] if len(retenus) >= MAX_TOKENS_TABLE else DF_MIN_CONSERVE
    logger.info(
        f"Table de rareté construite : {total} fiches, {len(brut)} mots distincts, "
        f"{len(conserve)} conservés (plancher {plancher})."
    )
    return TableRarete(total, conserve, empreinte, plancher)


def construire_depuis_base(db, channel: str = caps.CHANNEL_SCREENING) -> TableRarete:
    """
    Construit la table depuis la BASE, pour les processus qui n'ont pas
    l'univers en memoire.

    Le re-criblage et les cahiers de tests tournent dans le demon travailleur,
    qui ne voit pas le cache du processus API. Sans cette voie, une regle
    fondee sur la rarete cloturerait dans un processus et pas dans l'autre —
    deux canaux, deux verites, exactement ce qu'un controle reproche. La base
    fait foi partout.

    Ne lit que les deux colonnes utiles : le corpus complet en fiches entieres
    ne tiendrait pas ici.
    """
    from fiskr.database import Snapshot, WatchlistEntity, WATCHLIST_FILE_TYPES
    _, empreinte = _reference(db)
    snapshot_ids = [s.snapshot_id for s in db.query(Snapshot.snapshot_id).filter(
        Snapshot.file_type.in_(WATCHLIST_FILE_TYPES), Snapshot.status == "READY").all()]
    if not snapshot_ids:
        return TableRarete(0, {}, empreinte)
    lignes = db.query(WatchlistEntity.primary_name, WatchlistEntity.aliases).filter(
        WatchlistEntity.snapshot_id.in_(snapshot_ids),
        WatchlistEntity.excluded.isnot(True),
    ).yield_per(5000)
    return construire(({"primary_name": nom, "aliases": alias}
                       for nom, alias in lignes), channel, empreinte)


def _reference(db):
    from fiskr.database import production_watchlist_reference
    return production_watchlist_reference(db)


def table_pour(db, channel: str = caps.CHANNEL_SCREENING) -> TableRarete:
    """
    Table du corpus en production dans CE processus, construite au besoin.

    Memoisee par empreinte de liste : elle n'est reconstruite qu'apres une mise
    en production. A appeler AVANT un fork de pool — les enfants en heritent.
    """
    _, empreinte = _reference(db)
    courante = _TABLE
    if courante is not None and courante.empreinte == empreinte:
        return courante
    table = construire_depuis_base(db, channel)
    installer(table)
    return table


_TABLE: Optional[TableRarete] = None


def installer(table: Optional[TableRarete]) -> None:
    """Pose la table du processus (appele avec le chargement du cache liste)."""
    global _TABLE
    _TABLE = table


def table_courante() -> Optional[TableRarete]:
    return _TABLE


def profil(nom_client: str, nom_liste: str,
           channel: str = caps.CHANNEL_SCREENING) -> Dict[str, Any]:
    """Profil de rareté d'un rapprochement, ou profil au repos sans table."""
    table = _TABLE
    if table is None or table.total <= 0:
        return profil_indisponible()
    try:
        return table.profil(nom_client, nom_liste, channel)
    except Exception as e:  # la rarete n'est jamais une raison de rater un criblage
        logger.warning(f"Profil de rareté indisponible : {e}")
        return profil_indisponible()


def mots_les_plus_repandus(table: TableRarete, combien: int = 50) -> List[Dict[str, Any]]:
    """
    Les mots que le corpus porte le plus souvent, du plus repandu au moins.

    C'est l'outil de CALIBRAGE : avant d'ecrire un seuil dans une regle, on
    regarde ce que le corpus contient reellement. Le classement change avec les
    listes activees — un univers a 80 % PEP latino-americain ne porte pas les
    memes mots qu'un univers de gels d'avoirs.
    """
    tries = sorted(table._df.items(), key=lambda kv: (-kv[1], kv[0]))[:max(1, combien)]
    return [{"token": token, "df": df,
             "part": round(100.0 * df / table.total, 4) if table.total else 0.0,
             "repandu": df >= table.seuil_repandu}
            for token, df in tries]

"""
La reference de l'univers crible : CONTRE QUOI une decision a ete rendue.

POURQUOI CE MODULE EXISTE
-------------------------
Chaque ligne du journal d'audit porte une « version des listes » et une
empreinte. Releve en production le 19 octobre 2026 : la version etait une
constante — « Database Active Snapshot » sur 28 decisions sur 28 — et
l'empreinte le hash du SEUL lot en production depose le plus recemment. Une
empreinte pour 39 listes et 670 864 fiches.

Trois gestes changeaient l'univers sans la changer : homologuer un lot qui
n'est pas le dernier depose, exclure une fiche, corriger une fiche. Reproduit
sur base jetable : le meme client sort NO_MATCH puis ALERT sous la meme
version et la meme empreinte. Et l'index de filtrage ISO 20022, dont la
signature se fiait a cette empreinte pour « couvrir tout ce qui change les
cles », n'etait pas reconstruit : un paiement a une personne designee le jour
meme passait en PASS, zero candidat, pendant que /api/screen rendait ALERT
sur le meme nom.

LA DEFINITION
-------------
L'univers est ce que `load_watchlist_cache` charge : les lots READY des types
WATCHLIST_*, moins leurs fiches exclues, avec les valeurs que les corrections
manuelles leur ont donnees. Son empreinte couvre donc :
- chaque lot en production : (type, identifiant, hash du fichier, nombre de
  fiches). Le nombre, parce que les lots d'ajouts manuels gardent le meme
  hash quand on leur ajoute une fiche ;
- les fiches exclues de ces lots ;
- le dernier identifiant du journal des corrections de fiches : une fiche
  corrigee ne change ni de lot ni de hash, seul ce journal en garde trace.

Elle est calculee ICI et nulle part ailleurs. Le journal d'audit, l'index de
filtrage, la table de rarete, le rapport d'alerte et le badge de la barre
laterale la lisent ; aucun ne la recompose.

Ce qu'elle coute : trois requetes bornees (les lots READY, quelques dizaines
de lignes ; les fiches exclues, servies par l'index partiel
`ix_wl_entities_excluded` ; le maximum d'une cle primaire). Elle n'est pas
relue a chaque criblage : le processus qui crible la calcule en chargeant son
cache — chargement que declenche l'epoque — et la tient avec lui.
"""
import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence, Tuple

# Empreinte d'un univers VIDE. Valeur historique, conservee telle quelle :
# tools/audit_empty_cache_decisions.py la recherche pour retrouver les
# decisions rendues sur un cache jamais charge.
EMPREINTE_VIDE = "N/A"


@dataclass(frozen=True)
class ReferenceUnivers:
    """Ce contre quoi l'on crible, sous une forme qui se cite et se relit."""

    empreinte: str
    # (type de liste, identifiant de lot) des lots en production, tries.
    composition: Tuple[Tuple[str, str], ...]
    fiches_exclues: int = 0
    derniere_correction: Optional[int] = None

    @property
    def vide(self) -> bool:
        return not self.composition

    @property
    def version(self) -> str:
        """
        Libelle court de l'univers, celui de la colonne `watchlist_version`
        (50 caracteres) et du rapport d'alerte. Derive de l'empreinte : deux
        univers differents portent deux libelles differents (a une collision
        de 48 bits pres), ce que la constante d'avant ne pouvait pas dire.
        """
        if self.vide:
            return "Aucune liste en production"
        n = len(self.composition)
        return f"Univers {self.empreinte[:12]} ({n} lot{'s' if n > 1 else ''})"

    def composition_journal(self) -> List[List[str]]:
        """La composition telle qu'elle est figee dans le journal d'audit."""
        return [list(lot) for lot in self.composition]


VIDE = ReferenceUnivers(empreinte=EMPREINTE_VIDE, composition=())

# Libelle CONSTANT des decisions anterieures : il ne designe aucun univers.
# Le journal le garde tel quel — il est immuable — mais une piece qui le cite
# doit dire ce qu'il vaut, sinon elle affirme une version qui n'en est pas une.
VERSION_HISTORIQUE = "Database Active Snapshot"


# ------------------ CE QU'UNE PIECE CITE D'UNE DECISION ------------------
# Le rapport d'alerte imprimable et le narratif lisent ces deux fonctions :
# une decision se cite partout dans les memes termes.

def version_citee(version: Optional[str]) -> Optional[str]:
    """La version d'une decision telle qu'une piece doit la citer."""
    if version == VERSION_HISTORIQUE:
        return (f"{version} (libellé constant des décisions antérieures au "
                f"19/10/2026 : il ne désigne pas l'univers criblé)")
    return version


def composition_citee(composition: Optional[Sequence[Sequence[str]]]) -> str:
    """Les lots en production au moment d'une decision, en une ligne."""
    if composition is None:
        return "Non enregistrée (décision antérieure à cette trace)"
    if not composition:
        return "Aucune liste en production"
    return " ; ".join(f"{lot[0]} · {lot[1]}" for lot in composition)


def _types_de_listes():
    from fiskr.database import WATCHLIST_FILE_TYPES
    return WATCHLIST_FILE_TYPES


def lots_en_production(db) -> list:
    """
    Les lots qui forment l'univers : READY, de type WATCHLIST_*. Le plus
    recent d'abord — l'ordre n'entre pas dans l'empreinte, il sert aux
    lecteurs qui affichent les lots.
    """
    from sqlalchemy.orm import load_only
    from fiskr.database import Snapshot
    # Les seules colonnes que lisent l'empreinte et ses lecteurs. Chaque lot
    # porte son rapport de cahier de tests (jusqu'a 400 paires, ~150 Kio de
    # JSON) : hydrater les 39 lots en production decodait ~6 Mio de JSON a
    # chaque ouverture de page pour n'en lire que l'identifiant et le hash —
    # 55 a 70 ms par appel du badge, contre 2 a 3 ms ainsi (SQLite, 39 lots,
    # 670 864 fiches). Une autre colonne reste lisible, chargee a la demande.
    return db.query(Snapshot).options(load_only(
        Snapshot.snapshot_id, Snapshot.file_type, Snapshot.file_hash,
        Snapshot.record_count, Snapshot.uploaded_at, Snapshot.status,
    )).filter(
        Snapshot.file_type.in_(_types_de_listes()),
        Snapshot.status == "READY",
    ).order_by(Snapshot.uploaded_at.desc()).all()


def calculer(db, lots: Optional[Sequence[Any]] = None) -> ReferenceUnivers:
    """
    Reference de l'univers forme par `lots` (par defaut : les lots en
    production lus en base), avec l'etat des exclusions et des corrections
    tel qu'il est en base a cet instant.

    `lots` est passe par `load_watchlist_cache` : la reference doit decrire
    EXACTEMENT les lots qu'il vient de charger, pas ceux qu'une seconde
    lecture trouverait.
    """
    from sqlalchemy import func
    from fiskr.database import WatchlistEntity, WatchlistEntityChange

    lots = list(lots_en_production(db) if lots is None else lots)
    if not lots:
        return VIDE
    identifiants = [s.snapshot_id for s in lots]
    exclues = sorted(
        pk for (pk,) in db.query(WatchlistEntity.id).filter(
            WatchlistEntity.excluded.is_(True),
            WatchlistEntity.snapshot_id.in_(identifiants),
        ).all()
    )
    derniere_correction = db.query(func.max(WatchlistEntityChange.id)).scalar()

    digest = hashlib.sha256()
    tries = sorted(lots, key=lambda s: (s.file_type or "", s.snapshot_id))
    for lot in tries:
        digest.update(
            f"{lot.file_type}\x1f{lot.snapshot_id}\x1f{lot.file_hash}"
            f"\x1f{int(lot.record_count or 0)}\x1e".encode("utf-8"))
    digest.update(("exclues:" + ",".join(str(pk) for pk in exclues)).encode("ascii"))
    digest.update(f"\x1ecorrections:{derniere_correction or 0}".encode("ascii"))

    reference = ReferenceUnivers(
        empreinte=digest.hexdigest(),
        composition=tuple((lot.file_type, lot.snapshot_id) for lot in tries),
        fiches_exclues=len(exclues),
        derniere_correction=derniere_correction,
    )
    _retenir(reference)
    return reference


# ------------------ MEMOIRE DE PROCESSUS ------------------

# Les references deja calculees par ce processus, par empreinte. C'est ce qui
# permet au journal d'audit de figer la COMPOSITION a cote de l'empreinte sans
# qu'aucun appelant n'ait a la transporter : deux parametres passes separement
# finissent toujours par diverger, alors que la composition se relit ici
# depuis l'empreinte meme qui sera ecrite. Bornee : un processus ne voit que
# quelques univers successifs.
_CONNUES: "OrderedDict[str, ReferenceUnivers]" = OrderedDict()
_CONNUES_MAX = 16
_verrou = threading.Lock()


def _retenir(reference: ReferenceUnivers) -> None:
    with _verrou:
        _CONNUES[reference.empreinte] = reference
        _CONNUES.move_to_end(reference.empreinte)
        while len(_CONNUES) > _CONNUES_MAX:
            _CONNUES.popitem(last=False)


def connue(empreinte: Optional[str]) -> Optional[ReferenceUnivers]:
    """La reference d'empreinte donnee, si ce processus l'a calculee."""
    if not empreinte:
        return None
    if empreinte == EMPREINTE_VIDE:
        return VIDE
    with _verrou:
        reference = _CONNUES.get(empreinte)
        if reference is not None:
            # Lue a chaque decision journalisee : l'univers contre lequel on
            # crible reste ainsi en tete, quelles que soient les references
            # calculees entre-temps (badge, re-criblage).
            _CONNUES.move_to_end(empreinte)
        return reference


# Reference lue en base, memorisee par EPOQUE du cache de production : chaque
# geste qui change l'univers incremente l'epoque (approbation, synchronisation
# appliquee, import, ajout, correction, purge, reparation au demarrage). Sert
# les lecteurs qui ne tiennent pas l'univers en memoire : le re-criblage du
# demon et la table de rarete construite depuis la base. Un seul tuple,
# remplace d'un bloc : un fil ne lit jamais l'epoque de l'un avec la reference
# de l'autre. La cle porte aussi la BASE lue : une epoque n'est qu'un compteur,
# et deux bases au meme compteur (les bases jetables d'une suite de tests, une
# migration) ne decrivent pas le meme univers.
_par_epoque: Tuple[Optional[tuple], Optional[ReferenceUnivers]] = (None, None)


def reference_en_production(db) -> ReferenceUnivers:
    """Reference de l'univers en production, lue en base (memorisee par epoque)."""
    global _par_epoque
    from fiskr.settings import watchlist_epoch

    # L'epoque est lue AVANT les lots : un geste survenu entre les deux
    # lectures laisse une epoque plus ancienne que la reference, donc un
    # recalcul au prochain appel — jamais l'inverse, qui figerait l'ancienne.
    epoque = (str(db.get_bind().url), watchlist_epoch(db))
    memoire_epoque, memoire = _par_epoque
    if memoire is not None and memoire_epoque == epoque:
        return memoire
    reference = calculer(db)
    _par_epoque = (epoque, reference)
    return reference


def invalider() -> None:
    """
    Oublie la reference lue en base. Appele a chaque chargement du cache : un
    processus qui vient de relire ses listes ne doit pas citer, ailleurs, une
    reference anterieure a ce qu'il crible.
    """
    global _par_epoque
    _par_epoque = (None, None)

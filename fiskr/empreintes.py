"""
Empreintes d'une paire de liste blanche : ce que la decision a COUVERT.

Lignes directrices conjointes DGT/ACPR sur le gel des avoirs (18 mars 2026),
§169 : les « good guys » sont admis a condition que l'outil re-detecte la
personne « immediatement apres l'entree en vigueur [...] d'une modification
d'une mesure existante, ou si les informations relatives au client ont
change ».

La paire ne retenait que le couple (client, fiche). La liste blanche
s'appliquant avant tout autre examen, une fiche modifiee puis re-criblee
retombait en WHITELISTED : la v2 de DGT-2 ajoutant le numero de passeport du
client CUST-777 donnait `whitelisted_suppressed == 1` et aucune alerte. La
decision portait sur la v1 ; rien ne le disait.

La paire memorise donc, a sa pose, deux empreintes :
- celle de la fiche listee : `entity_checksum`, calculee a l'ingestion et
  recalculee a chaque correction manuelle — la version que le delta et le
  re-criblage comparent deja ;
- celle des elements d'identification du client QUE LE MOTEUR LIT, derives du
  code du moteur (`champs_client_cribles`).

Une empreinte differente rend la paire « a reexaminer » : elle n'absorbe plus,
l'alerte s'ouvre avec un evenement WHITELIST_STALE qui dit ce qui a change. La
paire n'est pas supprimee — son historique reste ; on la reconduit (nouvelles
empreintes, journalisees) ou on la retire.

Une paire SANS empreinte (posee avant ce suivi) est a reexaminer, elle aussi :
rien ne permet d'etablir que rien n'a change depuis sa decision, et absorber
une alerte est un verdict qui ne sort que d'une condition etablie. Aucune
empreinte ne lui est posee retroactivement : celle du jour de la migration se
ferait passer pour celle de la decision.
"""
import hashlib
import inspect
import json
import re
from datetime import datetime
from functools import lru_cache
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

# ------------------ MOTIFS DE REEXAMEN (une seule definition) ------------------

MOTIF_FICHE = "ENTITY"
MOTIF_CLIENT = "CLIENT"
MOTIF_FICHE_ET_CLIENT = "ENTITY_AND_CLIENT"
MOTIF_SANS_EMPREINTE = "NO_FINGERPRINT"

# Libelles rendus tels quels par l'API (ecran de la liste blanche, traduits par
# fiskr/static/i18n.js) et repris dans l'evenement WHITELIST_STALE. Une ligne
# par libelle : le controle des traductions orphelines les cherche entiers.
MOTIFS_DE_REEXAMEN = {
    MOTIF_FICHE: "la fiche listée a changé depuis la décision",
    MOTIF_CLIENT: "l'identification du client a changé depuis la décision",
    MOTIF_FICHE_ET_CLIENT: "la fiche listée et l'identification du client ont changé depuis la décision",
    MOTIF_SANS_EMPREINTE: "paire antérieure au suivi : rien n'établit ce que sa décision couvrait",
}

_COTES = {
    MOTIF_FICHE: frozenset({MOTIF_FICHE}),
    MOTIF_CLIENT: frozenset({MOTIF_CLIENT}),
    MOTIF_FICHE_ET_CLIENT: frozenset({MOTIF_FICHE, MOTIF_CLIENT}),
}


def _motif_des_cotes(cotes) -> Optional[str]:
    cotes = frozenset(cotes)
    for motif, ensemble in _COTES.items():
        if cotes == ensemble:
            return motif
    return None


# ------------------ CE QUE LE MOTEUR LIT DU CLIENT ------------------

# Lecture d'un champ client par le moteur : `x.get("client_...")`. Les champs
# `transaction_*` sont les identifiants de moyen de transport du profil.
_LECTURE_CLIENT = re.compile(r"""\.get\(\s*['"]((?:client|transaction)_[a-z_]+)['"]""")


@lru_cache(maxsize=1)
def champs_client_cribles() -> Tuple[str, ...]:
    """
    Elements d'identification du client que le moteur LIT, derives du code de
    `fiskr.scoring` (rapprochement flou, correspondances fortes) et de
    `fiskr.blocking` (cles de blocage) — jamais recopies.

    Une liste tenue a la main deriverait : le jour ou le moteur apprend a lire
    un champ de plus, la paire cesserait de le surveiller sans que rien ne le
    dise, ce qui est le defaut meme que cette empreinte corrige. Meme methode
    que `screenpool.projection_for` : un scan lexical ne peut pas rater un
    champ cite, au pire il en retient un de trop — ici sans danger, un champ
    jamais renseigne n'entre pas dans l'empreinte.

    Les champs de gouvernance (notation de risque, segment, statut…) n'en sont
    pas : le moteur ne les lit pas, et une paire qui se rouvrirait parce que la
    notation interne du client a bouge crierait au loup. `client_id` non plus :
    c'est la cle de la paire, pas un element d'identification.

    Echec franc si le code du moteur est illisible (deploiement sans sources) :
    une empreinte calculee sur rien couvrirait tout.
    """
    from fiskr import blocking, scoring
    champs = set()
    for module in (scoring, blocking):
        champs.update(_LECTURE_CLIENT.findall(inspect.getsource(module)))
    champs.discard("client_id")
    if not champs:
        raise RuntimeError("Aucun champ client lu par le moteur n'a été trouvé : "
                           "l'empreinte du client ne peut pas être établie.")
    return tuple(sorted(champs))


def _canonique(valeur: Any) -> Any:
    """
    Forme comparable d'une valeur, quelle que soit la porte d'entree : le
    meme client arrive en ligne du referentiel, en charge utile d'API ou en
    ligne de campagne, avec des listes vides la ou l'autre porte un NULL. Vide,
    absent et NULL se confondent ; la casse et les blancs ne comptent pas (le
    moteur ne les distingue pas) ; l'ordre d'une liste non plus.
    """
    if valeur is None:
        return None
    if isinstance(valeur, bool):
        return valeur
    if isinstance(valeur, (int, float)):
        return valeur
    if isinstance(valeur, str):
        texte = " ".join(valeur.split()).casefold()
        return texte or None
    if isinstance(valeur, dict):
        forme = {}
        for cle, v in valeur.items():
            c = _canonique(v)
            if c is not None:
                forme[str(cle)] = c
        return forme or None
    if isinstance(valeur, (list, tuple, set)):
        elements = [c for c in (_canonique(v) for v in valeur) if c is not None]
        elements.sort(key=lambda e: json.dumps(e, sort_keys=True, ensure_ascii=False))
        return elements or None
    return _canonique(str(valeur))


def empreinte_client(client: Dict[str, Any]) -> str:
    """Empreinte des elements d'identification du client que le moteur lit."""
    identite = {}
    for champ in champs_client_cribles():
        forme = _canonique(client.get(champ))
        if forme is not None:
            identite[champ] = forme
    brut = json.dumps(identite, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(brut.encode("utf-8")).hexdigest()


# ------------------ LA PAIRE COUVRE-T-ELLE ENCORE CE QU'ELLE A COUVERT ? ------------------

class EmpreintesDePaire(NamedTuple):
    """Ce qu'il faut d'une paire pour l'appliquer hors session : le cahier de
    tests la transporte jusque dans son pool de processus forkes."""
    id: Optional[int]
    entity_checksum: Optional[str]
    client_fingerprint: Optional[str]
    stale_since: Optional[datetime] = None
    stale_reason: Optional[str] = None


def motif_de_reexamen(paire, fiche: Dict[str, Any], empreinte: str) -> Optional[str]:
    """
    None : la paire couvre encore ce que sa decision a couvert, elle absorbe.
    Sinon, le motif de reexamen (MOTIFS_DE_REEXAMEN).

    Une paire deja signalee le reste jusqu'a sa reconduction, meme si les
    donnees reviennent a l'identique : quelqu'un doit regarder, et la
    correspondance a deja ouvert une alerte. Le motif s'enrichit si l'autre
    cote change a son tour.
    """
    if not paire.entity_checksum or not paire.client_fingerprint:
        return MOTIF_SANS_EMPREINTE
    cotes = set()
    if (fiche or {}).get("entity_checksum") != paire.entity_checksum:
        cotes.add(MOTIF_FICHE)
    if empreinte != paire.client_fingerprint:
        cotes.add(MOTIF_CLIENT)
    signalee = getattr(paire, "stale_since", None) is not None
    if signalee:
        cotes |= _COTES.get(getattr(paire, "stale_reason", None), frozenset())
    motif = _motif_des_cotes(cotes)
    if motif is None and signalee:
        # Signalee pour un motif que ce module ne sait pas lire : elle
        # n'absorbe pas pour autant.
        motif = getattr(paire, "stale_reason", None) or MOTIF_SANS_EMPREINTE
    return motif


def motif_en_attente(paire) -> Optional[str]:
    """Motif de reexamen deja ETABLI pour cette paire (ecran de la liste
    blanche) : signalee par un criblage, ou sans empreinte."""
    if not paire.entity_checksum or not paire.client_fingerprint:
        return MOTIF_SANS_EMPREINTE
    if paire.stale_since is not None:
        return paire.stale_reason or MOTIF_SANS_EMPREINTE
    return None


def filtre_a_reexaminer(modele):
    """Meme definition que `motif_en_attente`, en SQL (filtre de l'ecran)."""
    from sqlalchemy import or_
    return or_(modele.stale_since.isnot(None),
               modele.entity_checksum.is_(None),
               modele.client_fingerprint.is_(None))


# ------------------ CE QUI A CHANGE ------------------

def champs_modifies_de_la_fiche(db, paire, fiche: Dict[str, Any]) -> Optional[List[str]]:
    """
    Champs de la fiche modifies depuis la version couverte, par le moteur de
    delta (`delta.find_differences`) — celui qui produit deja le champ-a-champ
    des mises a jour de listes. None : la version couverte n'est plus en base
    (purgee par la retention), la difference n'est pas restituable.
    """
    from fiskr.database import CHECKSUM_EXCLUDED_COLS, WatchlistEntity
    from fiskr.delta import find_differences

    ancienne = db.query(WatchlistEntity).filter(
        WatchlistEntity.entity_id == paire.watchlist_entity_id,
        WatchlistEntity.entity_checksum == paire.entity_checksum,
    ).first()
    if ancienne is None:
        return None
    colonnes = [c.name for c in WatchlistEntity.__table__.columns
                if c.name not in CHECKSUM_EXCLUDED_COLS and c.name in (fiche or {})]
    avant = {c: getattr(ancienne, c) for c in colonnes}
    apres = {c: fiche.get(c) for c in colonnes}
    changements, _avant, _apres = find_differences(avant, apres)
    return changements


def _date_de_decision(paire) -> str:
    quand = paire.fingerprinted_at or paire.created_at
    return quand.strftime("%d/%m/%Y") if quand else "date inconnue"


def detail_de_reexamen(db, paire, fiche: Dict[str, Any], motif: str) -> str:
    """Texte de l'evenement WHITELIST_STALE : ce qui a change, et quoi faire."""
    morceaux = [f"Liste blanche à réexaminer (paire #{paire.id}, décision du "
                f"{_date_de_decision(paire)}) : {MOTIFS_DE_REEXAMEN[motif]}."]
    if motif in (MOTIF_FICHE, MOTIF_FICHE_ET_CLIENT):
        champs = champs_modifies_de_la_fiche(db, paire, fiche)
        if champs:
            morceaux.append(f"Champs modifiés de la fiche : {', '.join(champs)}.")
        elif champs is None:
            morceaux.append("La version couverte n'est plus conservée en base : "
                            "la différence n'est pas restituable.")
    morceaux.append("La paire n'absorbe plus cette correspondance : reconduisez-la "
                    "ou retirez-la depuis l'écran Liste blanche.")
    return " ".join(morceaux)


def examiner_la_paire(db, paire, fiche: Dict[str, Any], empreinte: str) -> Optional[Dict[str, Any]]:
    """
    Applique une paire ACTIVE a une correspondance. None : elle l'absorbe.
    Sinon le reexamen, a joindre a l'alerte qui va s'ouvrir ; la paire est
    marquee (commit par l'appelant, avec le journal et les alertes).

    `nouveau` : la paire vient de basculer. C'est ce qui decide la
    notification et l'evenement sur une alerte deja ouverte — un criblage par
    jour sur une paire deja signalee n'apprend rien a personne.
    """
    motif = motif_de_reexamen(paire, fiche, empreinte)
    if motif is None:
        return None
    nouveau = paire.stale_since is None
    if nouveau:
        paire.stale_since = datetime.utcnow()
    paire.stale_reason = motif
    return {
        "pair_id": paire.id, "motif": motif, "nouveau": nouveau,
        "detail": detail_de_reexamen(db, paire, fiche, motif),
        # Charge utile composee AVANT le commit (apres, chaque attribut relu
        # coute un SELECT).
        "notification": {
            "Paire": paire.id, "Client": paire.client_name or paire.client_id,
            "Fiche listée": paire.watchlist_name or paire.watchlist_entity_id,
            "Liste": paire.list_type or "—", "Motif": MOTIFS_DE_REEXAMEN[motif],
        },
    }


def notifier_les_bascules(db, reexamens) -> None:
    """Une notification par paire qui VIENT de basculer, apres le commit."""
    from fiskr.notifier import emit
    for reexamen in reexamens:
        if reexamen and reexamen.get("nouveau"):
            emit(db, "whitelist_stale", reexamen["notification"])


# ------------------ CE QUE LA DECISION COUVRE ------------------

def empreintes_de_reference(db, client_id: str, entity_id: str) -> Dict[str, Any]:
    """
    Empreintes que pose une decision (creation ou reconduction) sur ce couple.

    1. Le DERNIER criblage journalise du couple : c'est ce que l'analyste a
       devant lui — l'alerte qu'il clot en faux positif, ou celle que la paire
       a laissee s'ouvrir. Il porte la fiche criblee (`watchlist_entity`) et,
       depuis ce suivi, l'empreinte du client (`client_fingerprint`).
    2. Ce qu'il ne porte pas — couple jamais crible en production (Good Guys
       du cahier de tests), ou criblage anterieur au suivi — est lu dans les
       donnees en base : la version la plus recente de la fiche, en production
       OU en homologation (le cahier de tests blanchit des fiches qui n'y sont
       pas encore), et la fiche client la plus recente, referentiel ou panel.

    Rend {"entity_checksum", "client_fingerprint", "audit_id", "manque"} :
    `manque` dit, en clair, le cote qui n'a pas pu etre etabli. L'appelant
    refuse alors la pose — une paire qui ne sait pas ce qu'elle couvre
    couvrirait tout.
    """
    from fiskr.backtest import PANEL_FILE_TYPES
    from fiskr.database import (AuditTrail, ClientEntity, Snapshot, WatchlistEntity,
                                WATCHLIST_FILE_TYPES)

    fiche_vue, client_vu, audit_id = None, None, None
    ligne = db.query(AuditTrail.id, AuditTrail.decision_tree).filter(
        AuditTrail.client_id == client_id,
        AuditTrail.watchlist_id == entity_id,
    ).order_by(AuditTrail.id.desc()).first()
    if ligne is not None:
        arbre = ligne[1] or {}
        fiche_vue = (arbre.get("watchlist_entity") or {}).get("entity_checksum")
        client_vu = arbre.get("client_fingerprint")
        if fiche_vue or client_vu:
            audit_id = ligne[0]

    if not fiche_vue:
        trouvee = db.query(WatchlistEntity.entity_checksum).join(
            Snapshot, Snapshot.snapshot_id == WatchlistEntity.snapshot_id
        ).filter(
            WatchlistEntity.entity_id == entity_id,
            WatchlistEntity.excluded.isnot(True),
            Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
            Snapshot.status.in_(("READY", "PENDING_REVIEW")),
        ).order_by(Snapshot.uploaded_at.desc()).first()
        fiche_vue = trouvee[0] if trouvee else None

    if not client_vu:
        rangee = db.query(ClientEntity).join(
            Snapshot, Snapshot.snapshot_id == ClientEntity.snapshot_id
        ).filter(
            ClientEntity.client_id == client_id,
            Snapshot.file_type.in_(PANEL_FILE_TYPES),
            Snapshot.status == "READY",
        ).order_by(Snapshot.uploaded_at.desc()).first()
        if rangee is not None:
            client_vu = empreinte_client(
                {c.name: getattr(rangee, c.name) for c in rangee.__table__.columns})

    manque = []
    if not fiche_vue:
        manque.append(f"la fiche listée {entity_id} n'est ni en production ni en homologation, "
                      f"et aucun criblage journalisé de ce couple ne dit quelle version a été vue")
    if not client_vu:
        manque.append(f"le client {client_id} n'a jamais été criblé contre cette fiche et ne "
                      f"figure dans aucun référentiel : rien ne dit quelle identité la décision "
                      f"couvre — criblez-le d'abord")
    return {"entity_checksum": fiche_vue, "client_fingerprint": client_vu,
            "audit_id": audit_id, "manque": manque}


def poser_les_empreintes(paire, reference: Dict[str, Any]) -> None:
    """Pose les empreintes d'une decision et leve le reexamen eventuel."""
    paire.entity_checksum = reference["entity_checksum"]
    paire.client_fingerprint = reference["client_fingerprint"]
    paire.fingerprint_audit_id = reference.get("audit_id")
    paire.fingerprinted_at = datetime.utcnow()
    paire.stale_since = None
    paire.stale_reason = None

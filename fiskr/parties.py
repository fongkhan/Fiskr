"""
Les parties liees d'un client : beneficiaires effectifs, mandataires,
dirigeants, associes.

POURQUOI CE MODULE EXISTE
-------------------------
Le README annoncait que Fiskr « confronte le referentiel tiers (clients,
mandataires, beneficiaires effectifs) » aux listes. Rien ne pouvait porter ce
lien : `ClientEntity` n'en avait aucun, l'import du referentiel non plus, et
aucune occurrence de « ubo » ou de « mandataire » n'existait dans le code. La
regle des 50 % (fiskr/api.py, `compute_inherited_risk`) ne parcourt que des
liens ENTRE FICHES LISTEES : une societe cliente ACME, non listee, detenue a
60 % par une personne inscrite au registre national des gels, ne ressortait
d'aucun criblage.

Les lignes directrices conjointes DGT/ACPR sur le gel des avoirs (18 mars
2026, « LD » ci-dessous) le demandent pourtant en toutes lettres :
  - §117 : filtrer, quand l'information est disponible, les beneficiaires
    effectifs des clients et toute personne pretendant agir ou autorisee a
    agir en leur nom ;
  - §141 : filtrer les beneficiaires effectifs (CMF L.561-2-2, R.561-1), les
    mandataires, les associes figurant a un registre officiel et les
    dirigeants sociaux (C. com. R.123-54, 1° et 2°), a l'entree en relation
    ET « a chaque mise a jour des elements d'identification » ;
  - §142-143 : sur une alerte portant sur l'une de ces personnes, determiner
    si elle agit pour le compte d'une personne designee ou la detient ou la
    controle, et si les fonds peuvent lui etre mis a disposition ; si oui, ne
    pas executer l'operation et en informer la DG Tresor ;
  - §144 : quand le beneficiaire effectif d'une personne morale est designe,
    verifier s'il peut ENGAGER LES FONDS de la personne morale (representant
    legal, statuts, delegation de signature) ; dans ce cas « il s'abstient
    d'executer l'operation et interroge la DG Tresor » ; sinon (actionnaire
    minoritaire par exemple) les fonds de la personne morale n'ont pas a etre
    geles ; en cas de doute, il s'abstient et interroge ;
  - §131 : detenir une entite, c'est posseder 50 % ou plus de ses droits de
    propriete, et la propriete s'AGREGE : 30 % a une personne designee et
    25 % a une autre, l'entite « devrait etre gelee en consequence » ;
  - §133 : le controle couvre aussi le pouvoir d'administrer ou de disposer
    des fonds sans l'accord du proprietaire.
La Commission des sanctions de l'ACPR (9 fevrier 2022, OPT-NC, n° 2020-11,
§27-28) a retenu le grief : « le dispositif de detection des personnes
soumises a une mesure de gel des avoirs n'etait pas exhaustif, aucun filtrage
des mandataires et des beneficiaires effectifs n'etant realise » — une
obligation de resultat (LD, note 69).

CE QUI EST FAIT
---------------
- Une table `parties_liees` (fiskr/database.py, `PartieLiee`) rattachee au
  client par `client_id`, cle stable (client, partie). L'IDENTITE d'une partie
  ne porte que les elements d'identification d'un client — ceux que le moteur
  lit, releves par fiskr/identification.py (`champs_d_identite`). Pas une
  liste recopiee : le jour ou le moteur compare un champ de plus, une partie
  peut le porter.
- Chaque partie est criblee par le MEME cœur que son client (journal d'audit,
  liste blanche, regles, alertes), a chaque fois que le client l'est : temps
  reel, re-criblage apres mise a jour de liste, lookback, clients nouveaux ou
  modifies. L'alerte est celle du CLIENT — c'est sa relation qui est en cause
  — et nomme la partie : « via le beneficiaire effectif X (35 %) du client
  ACME ».
- Les parties confirmees (vrai positif sur le perimetre SANCTION) sont lues
  pour dire si le client est REPUTE DETENU (cumul >= 50 %, §131) ou CONTROLE
  (une partie designee peut engager ses fonds, §133, §144) par une personne
  designee (`evaluer_la_detention`). Le registre des gels (fiskr/gels.py) en
  tire la mesure.

CE QUI N'EST PAS FAIT
---------------------
- La chaine capitalistique au-dela du premier niveau (§131 : « identifier
  toute la chaine ») : une partie personne morale est criblee pour elle-meme,
  mais ses propres beneficiaires ne sont pas parcourus — le SI qui les connait
  les declare directement comme beneficiaires effectifs du client.
- Les campagnes de criblage batch ne criblent pas les parties : elles criblent
  des profils deposes par fichier, sans referentiel derriere eux.
"""
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fiskr.database import Alert, PartieLiee

logger = logging.getLogger("fiskr.parties")

ROLE_BENEFICIAIRE_EFFECTIF = "BENEFICIAIRE_EFFECTIF"
ROLE_MANDATAIRE = "MANDATAIRE"
ROLE_DIRIGEANT = "DIRIGEANT"
ROLE_ASSOCIE = "ASSOCIE"

# Les quatre qualites que les LD demandent de filtrer (§141). Libelles rendus
# tels quels par l'API et traduits par fiskr/static/i18n.js (une ligne par
# libelle) ; la chaine d'une alerte les reprend en minuscules.
ROLES = {
    ROLE_BENEFICIAIRE_EFFECTIF: "Bénéficiaire effectif",
    ROLE_MANDATAIRE: "Mandataire",
    ROLE_DIRIGEANT: "Dirigeant",
    ROLE_ASSOCIE: "Associé",
}

# Ce qu'un fichier ou un SI ecrit pour dire un role, ramene a son code.
_ALIAS_DE_ROLE = {
    "BE": ROLE_BENEFICIAIRE_EFFECTIF, "UBO": ROLE_BENEFICIAIRE_EFFECTIF,
    "BENEFICIAIRE_EFFECTIF": ROLE_BENEFICIAIRE_EFFECTIF,
    "BENEFICIAL_OWNER": ROLE_BENEFICIAIRE_EFFECTIF,
    "MANDATAIRE": ROLE_MANDATAIRE, "PROCURATION": ROLE_MANDATAIRE,
    "REPRESENTANT": ROLE_MANDATAIRE, "ATTORNEY": ROLE_MANDATAIRE,
    "DIRIGEANT": ROLE_DIRIGEANT, "DIRIGEANT_SOCIAL": ROLE_DIRIGEANT,
    "GERANT": ROLE_DIRIGEANT, "DIRECTOR": ROLE_DIRIGEANT,
    "ASSOCIE": ROLE_ASSOCIE, "ACTIONNAIRE": ROLE_ASSOCIE, "SHAREHOLDER": ROLE_ASSOCIE,
}

# LD §131 : « 50 % ou plus des droits de propriete ». Une seule definition :
# la regle des 50 % entre fiches listees (fiskr/api.py) la lit aussi.
SEUIL_DE_DETENTION_PCT = 50.0

FONDEMENT_DETENTION = "DETENTION_REPUTEE"
FONDEMENT_CONTROLE = "CONTROLE"
FONDEMENTS = {
    FONDEMENT_DETENTION: "Réputé détenu par une personne désignée (50 % ou plus cumulés, LD §131)",
    FONDEMENT_CONTROLE: "Réputé contrôlé par une personne désignée qui peut engager ses fonds (LD §133, §144)",
}

# Cle d'un profil de partie qui porte son contexte (fiskr/rescreen.py) : la
# partie, son client, la chaine. Jamais lue par le moteur.
CLE_DU_CONTEXTE = "_partie"

SOURCE_IMPORT = "IMPORT"
SOURCE_WEBHOOK = "WEBHOOK"

_TRANCHE = 800   # SQLite plafonne les parametres d'une requete


class RefusDePartie(ValueError):
    """Une partie que l'on ne peut pas enregistrer telle quelle : refus franc,
    en nommant pourquoi — jamais une partie a moitie lue."""


# ------------------------------------------------------------ vocabulaire

def champs_d_identite() -> Tuple[str, ...]:
    """Les champs d'identite d'une partie : les elements d'identification d'un
    profil client, releves sur le moteur (fiskr/identification.py)."""
    from fiskr.identification import champs_du_profil_lus_par_le_moteur
    return champs_du_profil_lus_par_le_moteur()


def _forme(texte: Any) -> str:
    t = unicodedata.normalize("NFKD", str(texte or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).upper()
    return re.sub(r"[^A-Z0-9]+", "_", t).strip("_")


def lire_roles(valeur: Any) -> List[str]:
    """« BE, dirigeant » ou ["UBO", "DIRIGEANT"] -> codes, dans l'ordre de
    ROLES. Un role inconnu est refuse : enregistre a moitie, il ne serait ni
    affiche ni compte."""
    if valeur is None:
        brut: List[Any] = []
    elif isinstance(valeur, (list, tuple, set)):
        brut = list(valeur)
    else:
        brut = re.split(r"[,;|/+]", str(valeur))
    codes = set()
    for element in brut:
        forme = _forme(element)
        if not forme:
            continue
        code = _ALIAS_DE_ROLE.get(forme) or next(
            (c for c, libelle in ROLES.items() if _forme(libelle) == forme), None)
        if code is None:
            raise RefusDePartie(f"rôle inconnu « {element} » (attendus : {', '.join(ROLES)}).")
        codes.add(code)
    if not codes:
        raise RefusDePartie(f"aucun rôle : précisez {', '.join(ROLES)}.")
    return [c for c in ROLES if c in codes]


def lire_pourcentage(valeur: Any) -> Optional[float]:
    """« 35 », « 35,5 », « 35 % » -> 35.5 ; vide -> None. Hors de ]0, 100] :
    refus — une detention de 150 % n'est pas une donnee, c'est une erreur."""
    if valeur is None or (isinstance(valeur, str) and not valeur.strip()):
        return None
    if isinstance(valeur, bool):
        raise RefusDePartie("pourcentage de détention illisible.")
    if isinstance(valeur, (int, float)):
        nombre = float(valeur)
    else:
        texte = str(valeur).replace("%", "").replace(" ", "").replace(" ", "").replace(",", ".")
        try:
            nombre = float(texte)
        except ValueError:
            raise RefusDePartie(f"pourcentage de détention illisible : « {valeur} ».")
    if not (0 < nombre <= 100):
        raise RefusDePartie(f"pourcentage de détention hors de ]0, 100] : {valeur}.")
    return round(nombre, 4)


def lire_drapeau(valeur: Any) -> Optional[bool]:
    """oui/non, true/false, 1/0, x ; vide -> None (non renseigne)."""
    if valeur is None:
        return None
    if isinstance(valeur, bool):
        return valeur
    forme = _forme(valeur)
    if not forme:
        return None
    if forme in ("OUI", "O", "TRUE", "VRAI", "YES", "Y", "1", "X"):
        return True
    if forme in ("NON", "N", "FALSE", "FAUX", "NO", "0"):
        return False
    raise RefusDePartie(f"« peut engager les fonds » illisible : « {valeur} » (oui ou non).")


def nom_d_affichage(profil: Dict[str, Any]) -> str:
    """Le nom d'un profil client ou de partie, tel qu'un humain le lit."""
    if (profil.get("client_type") or "PP") in ("PP", "I"):
        nom = f"{profil.get('client_first_name') or ''} {profil.get('client_last_name') or ''}".strip()
        return nom or (profil.get("client_company_name") or "")
    return profil.get("client_company_name") or ""


def _pourcentage_lisible(valeur: Optional[float]) -> str:
    return f"{valeur:g}".replace(".", ",") + " %"


def chaine(roles: Sequence[str], nom: str, pourcentage: Optional[float],
           peut_engager: bool, client_name: str) -> str:
    """« via le bénéficiaire effectif X (35 %) du client ACME » : ce qui
    rattache la correspondance au client, en une phrase."""
    qualite = " et ".join(ROLES.get(r, r).lower() for r in roles) or "partie liée"
    article = "l'" if qualite[:1] in "aeiouéèêh" else "le "
    precisions = []
    if pourcentage is not None:
        precisions.append(_pourcentage_lisible(pourcentage))
    if peut_engager:
        precisions.append("peut engager les fonds")
    suite = f" ({', '.join(precisions)})" if precisions else ""
    return f"via {article}{qualite} {nom}{suite} du client {client_name}"


# ------------------------------------------------------------ lecture

def lire_une_partie(identite: Dict[str, Any], *, roles: Any, pourcentage: Any = None,
                    peut_engager: Any = None, partie_id: Any = None,
                    champs_inconnus_refuses: bool = True) -> Dict[str, Any]:
    """
    Une partie telle qu'elle sera enregistree, ou un refus qui dit pourquoi.

    L'identite passe le MEME contrôle qualite qu'un client : une partie que le
    moteur refuserait de cribler ne s'enregistre pas — elle laisserait croire
    que la relation est criblee. Un champ d'identite inconnu est refuse
    (`champs_inconnus_refuses`) : il ne serait jamais lu, et l'appelant
    croirait l'avoir transmis.

    `peut_engager` non renseigne vaut NON, sauf pour un mandataire : etre
    « autorise a agir au nom » du client (LD §117), c'est le pouvoir d'engager
    ses fonds (procuration, §132). Le doute penche vers ce qui ne fait rien
    perdre ; un « non » explicite est respecte.
    """
    from fiskr.identification import identifiant_calcule
    from fiskr.quality import evaluate_and_clean

    champs = set(champs_d_identite())
    inconnus = sorted(k for k in identite if k not in champs)
    if inconnus and champs_inconnus_refuses:
        raise RefusDePartie(
            f"champ(s) d'identité inconnu(s) : {', '.join(inconnus)}. Une partie "
            f"s'identifie avec les éléments d'identification d'un client, ceux que "
            f"le moteur lit.")
    propre = {k: v for k, v in identite.items()
              if k in champs and v not in (None, "", [], {})}
    propre["client_type"] = (str(propre.get("client_type") or "PP")).strip().upper()
    if propre["client_type"] == "I":
        propre["client_type"] = "PP"
    elif propre["client_type"] == "E":
        propre["client_type"] = "PM"
    if propre["client_type"] not in ("PP", "PM"):
        raise RefusDePartie("client_type d'une partie : PP ou PM.")
    codes = lire_roles(roles)
    rapport = evaluate_and_clean({**propre, "client_id": "partie"})
    if not rapport.get("is_valid"):
        raise RefusDePartie("identité refusée par le contrôle qualité : "
                            + " ; ".join(str(e) for e in rapport.get("errors") or []))
    engage = lire_drapeau(peut_engager)
    if engage is None:
        engage = ROLE_MANDATAIRE in codes
    reference = str(partie_id or "").strip() or identifiant_calcule(propre)
    if not reference:
        raise RefusDePartie("partie sans identifiant ni nom.")
    return {
        "partie_id": reference[:100],
        "roles": codes,
        "identite": propre,
        "nom": nom_d_affichage(propre)[:1000] or reference,
        "pourcentage_detention": lire_pourcentage(pourcentage),
        "peut_engager_les_fonds": bool(engage),
    }


def _identite_comparable(identite: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {k: v for k, v in (identite or {}).items() if v not in (None, "", [], {})}


def remplacer_les_parties(db, client_id: str, lues: Sequence[Dict[str, Any]], *,
                          source: str, source_ref: Optional[str], username: str,
                          maintenant: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Le jeu COMPLET des parties d'un client, tel que la source le declare : les
    parties nouvelles sont creees, les connues mises a jour EN PLACE (meme
    identifiant, donc memes alertes), celles que la source ne declare plus sont
    RETIREES — datees, jamais supprimees. Une partie retiree qui revient est
    reactivee.

    Rend le bilan, dont `identite_changee` : vrai si une identite de partie a
    ete creee, modifiee, retiree ou reactivee — c'est ce qui demande de
    recribler la relation (LD §141). Un pourcentage ou un role modifie ne
    change aucun criblage. Commit par l'appelant.
    """
    vus = [l["partie_id"] for l in lues]
    doublons = sorted({p for p in vus if vus.count(p) > 1})
    if doublons:
        raise RefusDePartie(
            f"deux déclarations pour la même partie ({', '.join(doublons)}) : déclarez "
            f"ses rôles ensemble, sur une seule ligne.")
    now = maintenant or datetime.utcnow()
    existantes = {p.partie_id: p for p in db.query(PartieLiee).filter(
        PartieLiee.client_id == client_id).all()}
    bilan = {"creees": 0, "modifiees": 0, "inchangees": 0, "retirees": 0,
             "reactivees": 0, "identite_changee": False}
    for lue in lues:
        partie = existantes.get(lue["partie_id"])
        if partie is None:
            db.add(PartieLiee(
                client_id=client_id, partie_id=lue["partie_id"], roles=lue["roles"],
                identite=lue["identite"], nom=lue["nom"],
                pourcentage_detention=lue["pourcentage_detention"],
                peut_engager_les_fonds=lue["peut_engager_les_fonds"],
                source=source, source_ref=source_ref, created_at=now, created_by=username,
                updated_at=now, updated_by=username))
            bilan["creees"] += 1
            bilan["identite_changee"] = True
            continue
        identite_differe = _identite_comparable(partie.identite) != _identite_comparable(lue["identite"])
        reactivee = partie.retiree_le is not None
        differe = (identite_differe or reactivee
                   or list(partie.roles or []) != lue["roles"]
                   or partie.nom != lue["nom"]
                   or partie.pourcentage_detention != lue["pourcentage_detention"]
                   or bool(partie.peut_engager_les_fonds) != lue["peut_engager_les_fonds"])
        if not differe:
            bilan["inchangees"] += 1
            continue
        partie.roles = lue["roles"]
        partie.identite = lue["identite"]
        partie.nom = lue["nom"]
        partie.pourcentage_detention = lue["pourcentage_detention"]
        partie.peut_engager_les_fonds = lue["peut_engager_les_fonds"]
        partie.source, partie.source_ref = source, source_ref
        partie.updated_at, partie.updated_by = now, username
        if reactivee:
            partie.retiree_le = partie.retiree_par = None
            bilan["reactivees"] += 1
        else:
            bilan["modifiees"] += 1
        if identite_differe or reactivee:
            bilan["identite_changee"] = True
    declarees = set(vus)
    for reference, partie in existantes.items():
        if reference in declarees or partie.retiree_le is not None:
            continue
        partie.retiree_le, partie.retiree_par = now, username
        partie.updated_at, partie.updated_by = now, username
        bilan["retirees"] += 1
        bilan["identite_changee"] = True
    db.flush()
    return bilan


def parties_actives(db, client_ids: Iterable[str]) -> Dict[str, List[PartieLiee]]:
    """Parties non retirees de ces clients, par client, dans l'ordre de leur
    creation."""
    ids = sorted({c for c in client_ids if c})
    sortie: Dict[str, List[PartieLiee]] = {}
    if len(ids) > 4 * _TRANCHE:
        # Passage sur tout le referentiel (750 000 clients en production) : un
        # IN par tranche ferait pres de mille requetes ; la table des parties,
        # elle, ne porte que les clients qui en ont. Une lecture, un filtre.
        voulus = set(ids)
        for partie in db.query(PartieLiee).filter(PartieLiee.retiree_le.is_(None)) \
                .order_by(PartieLiee.id.asc()).yield_per(5000):
            if partie.client_id in voulus:
                sortie.setdefault(partie.client_id, []).append(partie)
        return sortie
    for debut in range(0, len(ids), _TRANCHE):
        for partie in db.query(PartieLiee).filter(
                PartieLiee.client_id.in_(ids[debut:debut + _TRANCHE]),
                PartieLiee.retiree_le.is_(None)).order_by(PartieLiee.id.asc()).all():
            sortie.setdefault(partie.client_id, []).append(partie)
    return sortie


def contexte(partie: PartieLiee, client_name: str) -> Dict[str, Any]:
    """Ce que la decision et l'alerte figent de la partie : qui, a quel
    titre, pour quel client — et la chaine qui le dit."""
    return {
        "id": partie.id,
        "partie_id": partie.partie_id,
        "client_id": partie.client_id,
        "client_name": client_name or partie.client_id,
        "roles": list(partie.roles or []),
        "nom": partie.nom,
        "pourcentage_detention": partie.pourcentage_detention,
        "peut_engager_les_fonds": bool(partie.peut_engager_les_fonds),
        "chaine": chaine(partie.roles or [], partie.nom, partie.pourcentage_detention,
                         bool(partie.peut_engager_les_fonds), client_name or partie.client_id),
    }


def profil_de_criblage(partie: PartieLiee, ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Le profil que le moteur crible : l'identite de la partie, sous
    l'identifiant de son CLIENT — la liste blanche, la deduplication des
    alertes et la vue 360 sont celles de la relation."""
    profil = dict(partie.identite or {})
    profil["client_id"] = partie.client_id
    if ctx is not None:
        profil[CLE_DU_CONTEXTE] = ctx
    return profil


def joindre_les_parties(db, fiches: List[Dict[str, Any]]) -> None:
    """Attache a chaque fiche client du referentiel les profils de ses
    parties (`identification.CLE_DES_PARTIES`) : le re-criblage les crible
    avec elle, et l'empreinte d'identification de la relation les couvre."""
    from fiskr.identification import CLE_DES_PARTIES
    par_client = parties_actives(db, (f.get("client_id") for f in fiches))
    if not par_client:
        return
    for fiche in fiches:
        parties = par_client.get(fiche.get("client_id"))
        if not parties:
            continue
        nom = nom_d_affichage(fiche) or fiche.get("client_id")
        fiche[CLE_DES_PARTIES] = [profil_de_criblage(p, contexte(p, nom)) for p in parties]


def profils_des_parties(db, client_id: Optional[str]) -> List[Dict[str, Any]]:
    """Les profils des parties actives d'UN client (sans contexte) : ce qui
    entre dans l'empreinte d'identification de sa relation."""
    if not client_id:
        return []
    return [profil_de_criblage(p) for p in parties_actives(db, [client_id]).get(client_id, [])]


def vue(partie: PartieLiee) -> Dict[str, Any]:
    """Une partie telle que l'ecran la montre."""
    identite = partie.identite or {}
    pays = identite.get("client_countries") or {}
    return {
        "id": partie.id,
        "partie_id": partie.partie_id,
        "client_id": partie.client_id,
        "roles": [{"code": r, "libelle": ROLES.get(r, r)} for r in (partie.roles or [])],
        "nom": partie.nom,
        "client_type": identite.get("client_type"),
        "dob": identite.get("client_dob"),
        "nationalites": list(pays.get("nationality") or []) if isinstance(pays, dict) else [],
        "pourcentage_detention": partie.pourcentage_detention,
        "peut_engager_les_fonds": bool(partie.peut_engager_les_fonds),
        "source": partie.source,
        "mise_a_jour_le": partie.updated_at.isoformat() if partie.updated_at else None,
        "retiree_le": partie.retiree_le.isoformat() if partie.retiree_le else None,
    }


# ------------------------------------------------------------ detention reputee

def evaluer_la_detention(db, client_id: Optional[str],
                         surcharges: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    Le client est-il repute detenu ou controle par une personne designee, au
    vu de ses parties CONFIRMEES ?

    Ne comptent que les parties dont une alerte est CONFIRMEE (vrai positif)
    sur une liste du perimetre SANCTION (fiskr/perimeters.py, surcharges
    comprises) : une homonymie non instruite n'est pas une personne designee,
    et un PEP n'en est pas une non plus. Une partie retiree ne compte plus —
    la source ne la declare plus.

    - DETENTION (§131) : les parts des personnes designees s'agregent ; une
      seule part par personne designee — deux declarations de la meme
      detention ne se somment pas (meme regle que les liens entre fiches
      listees, fiskr/api.py). Seuil : 50 % ou plus.
    - CONTROLE (§133, §144) : une partie designee peut engager les fonds du
      client (representant legal, statuts, delegation de signature,
      procuration).

    Rend {"reputee", "fondement", "cumul_pct", "seuil_pct", "controle",
    "parties"} — `parties` detaille chaque partie retenue et son alerte : un
    reviseur doit pouvoir refaire le calcul.
    """
    from fiskr.perimeters import est_sanction
    evaluation = {"reputee": False, "fondement": None, "cumul_pct": 0.0,
                  "seuil_pct": SEUIL_DE_DETENTION_PCT, "controle": False, "parties": []}
    if not client_id:
        return evaluation
    confirmees = [a for a in db.query(Alert).filter(
        Alert.client_id == client_id, Alert.partie_liee_id.isnot(None),
        Alert.status == "CLOSED_CONFIRMED").order_by(Alert.id.asc()).all()
        if est_sanction(a.list_type, surcharges)]
    if not confirmees:
        return evaluation
    ids = sorted({a.partie_liee_id for a in confirmees})
    parties = {p.id: p for p in db.query(PartieLiee).filter(PartieLiee.id.in_(ids)).all()}
    parts: Dict[str, float] = {}
    retenues: Dict[int, Dict[str, Any]] = {}
    for alerte in confirmees:
        partie = parties.get(alerte.partie_liee_id)
        if partie is None or partie.retiree_le is not None:
            continue
        if partie.pourcentage_detention:
            parts[alerte.watchlist_entity_id] = max(parts.get(alerte.watchlist_entity_id, 0.0),
                                                    float(partie.pourcentage_detention))
        if partie.peut_engager_les_fonds:
            evaluation["controle"] = True
        entree = retenues.setdefault(partie.id, {
            "partie_liee_id": partie.id, "nom": partie.nom, "roles": list(partie.roles or []),
            "pourcentage_detention": partie.pourcentage_detention,
            "peut_engager_les_fonds": bool(partie.peut_engager_les_fonds),
            "alertes": [], "personnes_designees": []})
        entree["alertes"].append(alerte.id)
        if alerte.watchlist_name not in entree["personnes_designees"]:
            entree["personnes_designees"].append(alerte.watchlist_name)
    cumul = round(sum(parts.values()), 4)
    evaluation["cumul_pct"] = cumul
    evaluation["parties"] = list(retenues.values())
    if cumul >= SEUIL_DE_DETENTION_PCT:
        evaluation["fondement"] = FONDEMENT_DETENTION
    elif evaluation["controle"]:
        evaluation["fondement"] = FONDEMENT_CONTROLE
    evaluation["reputee"] = evaluation["fondement"] is not None
    return evaluation


def phrase_de_detention(evaluation: Dict[str, Any], client_name: str) -> str:
    """Ce que l'evaluation etablit, en une phrase qui se relit : le cumul, le
    seuil, et les parties qui le forment."""
    detail = ", ".join(
        f"{p['nom']}"
        + (f" {_pourcentage_lisible(p['pourcentage_detention'])}" if p.get("pourcentage_detention") else "")
        + (" (peut engager les fonds)" if p.get("peut_engager_les_fonds") else "")
        + f" — alerte(s) #{', #'.join(str(a) for a in p['alertes'])}"
        for p in evaluation.get("parties") or []) or "aucune"
    cumul = _pourcentage_lisible(evaluation.get("cumul_pct") or 0.0)
    seuil = _pourcentage_lisible(evaluation.get("seuil_pct") or SEUIL_DE_DETENTION_PCT)
    if evaluation.get("fondement") == FONDEMENT_DETENTION:
        return (f"Client {client_name} réputé détenu par une ou des personnes désignées : "
                f"{cumul} cumulés par les parties confirmées ({detail}), seuil {seuil} "
                f"(LD DGT/ACPR §131).")
    if evaluation.get("fondement") == FONDEMENT_CONTROLE:
        return (f"Client {client_name} réputé contrôlé par une personne désignée qui peut "
                f"engager ses fonds ({detail}) ; détention cumulée {cumul}, sous le seuil de "
                f"{seuil} (LD DGT/ACPR §133, §144).")
    return (f"Client {client_name} ni réputé détenu ni réputé contrôlé : {cumul} cumulés par "
            f"les parties confirmées ({detail}), sous le seuil de {seuil}, et aucune partie "
            f"confirmée ne peut engager ses fonds (LD DGT/ACPR §131, §144).")

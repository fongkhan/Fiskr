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
  designee (`evaluer_la_detention`), directement ou par la chaine de
  detention (ci-dessous). Le registre des gels (fiskr/gels.py) en tire la
  mesure.

LA CHAINE DE DETENTION (au-dela du premier niveau)
--------------------------------------------------
Le premier lot ne lisait que le premier niveau : ACME <- 60 % HOLDCO <- 100 %
d'une personne designee n'etait pas reputee detenue — HOLDCO n'est pas
designee, et la personne designee n'est pas une partie d'ACME. Or §131 :
« il appartient aux organismes financiers d'identifier toute la chaine
capitalistique et de determiner si la personne [...] designee detient, au sens
defini ci-dessus, une ou plusieurs personnes, groupes ou entites ».

D'ou viennent les maillons (deux portes, facultatives : sans elles, le jeu
d'un client est exactement celui du premier lot) :
  - `partie_de` : la ligne est la partie d'une personne morale du MEME jeu
    (« X, associe a 70 % de HOLDCO ») — sa detention et son pouvoir d'engager
    les fonds portent sur HOLDCO. C'est la porte du cas courant : la holding
    n'est pas cliente, et le SI qui a calcule les beneficiaires effectifs
    (CMF R.561-1 : detention « directe ou indirecte ») connait sa chaine.
    Ces personnes sont criblees avec le client, comme ses autres parties.
  - `partie_client_id` : la partie EST ce client du referentiel (la holding
    est cliente) ; ses propres parties, criblees avec elle, continuent la
    chaine, et une alerte confirmee sur elle-meme la rend designee.
Les liens entre fiches LISTEES (EntityRelationship, regle des 50 % de
fiskr/api.py) ne sont PAS relies a cette chaine : ils ne portent que des
fiches de listes ; une partie confirmee comme l'une d'elles est deja
designee (premier niveau), et ce graphe ne peut rien dire d'une holding non
listee — rapprocher les deux par le nom fabriquerait des liens.

La regle de calcul, LUE (et ou elle ne dit rien) :
  - LD §131 et meilleures pratiques de l'UE (doc. du Conseil 11623/24,
    3 juillet 2024, §63) : detenir, c'est 50 % ou plus des droits de
    propriete ou une participation majoritaire, et la propriete s'AGREGE
    (30 % + 25 %). Commission europeenne, FAQ « Asset freeze and prohibition
    to make funds available » (reglement 269/2014, version du 25 septembre
    2026), n° 8 : meme agregation ; n° 9 : le controle etabli sur une entite
    « would impact its own subsidiaries ». Meilleures pratiques §62 : les
    avoirs des entites detenues ou controlees par une personne listee sont
    geles — dont les parts qu'elles detiennent.
  - Donc, en CASCADE : une entite reputee detenue (ou controlee) par des
    personnes designees compte pour sa participation ENTIERE dans celle
    qu'elle detient — ACME <- 60 % HOLDCO <- 70 % X : HOLDCO est detenue
    (70 %), ses 60 % comptent, ACME est reputee detenue. Une participation
    portee par une entite qui ne l'est pas ne compte pas. Les participations
    d'entites differentes dans la meme societe s'additionnent (deux chaines
    de 30 % vers le meme designe : 60 %) ; la meme personne designee
    declaree deux fois dans la meme societe ne compte qu'une fois.
  - Le CONTROLE se propage de meme (FAQ n° 9, meilleures pratiques §64 c :
    la majorite des droits de vote) : une entite controlee — une partie
    designee peut engager ses fonds (§133, §144) — compte pour le controle,
    pas pour la detention. En aval, c'est la suspension et l'interrogation
    de la DG Tresor (§144), pas le gel.
  - AMBIGU, dit et non tranche en silence : aucun texte lu ne prescrit ni
    n'ecarte la MULTIPLICATION le long d'une chaine (60 % x 70 % = 42 %).
    Elle est calculee et montree ; quand elle atteint le seuil sans que la
    cascade l'atteigne (30 % en direct + 60 % x 40 % = 54 %), la decision le
    nomme comme un doute — « en cas de doute, il s'abstient [...] et
    interroge la DG Tresor » (§144). L'OFAC, qui ne lie pas un etablissement
    francais, ecrit la meme cascade sans multiplication (FAQ 401, exemples
    1 a 5). Les participations croisees (cycles) et la profondeur ne sont
    traitees par aucun texte : le calcul s'arrete au cycle et a
    `PROFONDEUR_MAXIMALE` niveaux, et le DIT — jamais tronque en silence.
    Le capital et les droits de vote ne sont pas distingues :
    `pourcentage_detention` vaut pour les deux.

CE QUI N'EST PAS FAIT
---------------------
- Les campagnes de criblage batch ne criblent pas les parties : elles criblent
  des profils deposes par fichier, sans referentiel derriere eux.
- Une chaine declaree APRES la confirmation de la personne designee n'ouvre
  aucune mesure d'elle-meme : le point de decision reste la confirmation
  (fiskr/gels.py, `ouvrir_si_due`). La vue 360 calcule en direct et le dit.
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

# Chaine de detention (« LA CHAINE DE DETENTION » ci-dessus). Les textes ne
# bornent pas la profondeur ; une chaine plus longue n'est plus une chaine
# qu'un humain relit, et le parcours doit finir quoi que declare la source.
# Au-dela, le calcul s'arrete et le DIT (signalement PROFONDEUR).
PROFONDEUR_MAXIMALE = 10
# Liens parcourus au plus pour une evaluation : un graphe tres ramifie (la
# meme holding atteinte par des centaines de chemins) ne fige pas une
# confirmation. Au-dela, meme signalement que la profondeur.
_LIENS_MAXIMAUX = 5000

# Ce que la chaine etablit d'un maillon. Libelles rendus tels quels par l'API
# et traduits par fiskr/static/i18n.js.
QUALIFICATION_DESIGNEE = "DESIGNEE"
QUALIFICATION_DETENUE = "DETENUE"
QUALIFICATION_CONTROLEE = "CONTROLEE"
QUALIFICATIONS = {
    QUALIFICATION_DESIGNEE: "personne désignée",
    QUALIFICATION_DETENUE: "réputée détenue",
    QUALIFICATION_CONTROLEE: "réputée contrôlée",
}

# Ce que le calcul ne peut pas trancher seul : nomme, jamais tu.
SIGNAL_CYCLE = "CYCLE"
SIGNAL_PROFONDEUR = "PROFONDEUR"
SIGNAL_HORS_REFERENTIEL = "HORS_REFERENTIEL"
SIGNAL_MULTIPLICATION = "MULTIPLICATION"
SIGNALEMENTS = {
    SIGNAL_CYCLE: "Participation circulaire : le calcul s'arrête au cycle",
    SIGNAL_PROFONDEUR: "Chaîne non parcourue au-delà de la profondeur maximale : calcul incomplet",
    SIGNAL_HORS_REFERENTIEL: "Client lié absent du référentiel en production : ses parties ne sont pas criblées",
    SIGNAL_MULTIPLICATION: "Par multiplication le long des chaînes, le seuil serait atteint : en cas de doute, suspendre et interroger la DG Trésor",
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


def _qualite(roles: Sequence[str]) -> str:
    return " et ".join(ROLES.get(r, r).lower() for r in roles) or "partie liée"


def _precisions(pourcentage: Optional[float], peut_engager: bool) -> str:
    precisions = []
    if pourcentage is not None:
        precisions.append(_pourcentage_lisible(pourcentage))
    if peut_engager:
        precisions.append("peut engager les fonds")
    return f" ({', '.join(precisions)})" if precisions else ""


def chaine(roles: Sequence[str], nom: str, pourcentage: Optional[float],
           peut_engager: bool, client_name: str,
           intermediaires: Sequence["PartieLiee"] = ()) -> str:
    """« via le bénéficiaire effectif X (35 %) du client ACME » : ce qui
    rattache la correspondance au client, en une phrase. Une partie d'une
    holding du client nomme la holding : « via l'associé X (70 %) de HOLDCO,
    associé (60 %) du client ACME » — `intermediaires` va de la personne
    morale dont elle est la partie jusqu'au client exclu."""
    qualite = _qualite(roles)
    article = "l'" if qualite[:1] in "aeiouéèêh" else "le "
    suite = _precisions(pourcentage, peut_engager)
    entre = "".join(
        f" de {p.nom}, {_qualite(p.roles or [])}"
        f"{_precisions(p.pourcentage_detention, bool(p.peut_engager_les_fonds))}"
        for p in intermediaires)
    return f"via {article}{qualite} {nom}{suite}{entre} du client {client_name}"


# ------------------------------------------------------------ lecture

def _reference(valeur: Any) -> Optional[str]:
    texte = str(valeur or "").strip()
    return texte[:100] or None


def lire_une_partie(identite: Dict[str, Any], *, roles: Any, pourcentage: Any = None,
                    peut_engager: Any = None, partie_id: Any = None,
                    partie_de: Any = None, partie_client_id: Any = None,
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

    Chaine de detention : `partie_de` (partie_id d'une personne morale du meme
    jeu : la detention et le pouvoir d'engager portent sur elle) et
    `partie_client_id` (la partie EST ce client du referentiel). Leur
    coherence se juge sur le jeu entier (`verifier_la_structure`).
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
        "partie_de": _reference(partie_de),
        "partie_client_id": _reference(partie_client_id),
    }


def verifier_la_structure(client_id: str, lues: Sequence[Dict[str, Any]]) -> None:
    """
    La chaine que le jeu COMPLET d'un client declare tient-elle debout ? Refus
    franc sinon — une chaine a moitie lue serait un calcul faux qui se presente
    comme juste :
      - `partie_de` doit nommer une personne morale DU JEU, autre que la ligne
        elle-meme : une personne physique ne se detient pas, et une reference
        absente laisserait une detention suspendue a rien ;
      - les `partie_de` d'un jeu ne tournent pas en rond : une ligne porte une
        seule detention, donc un cycle ne mene jamais au client — c'est une
        declaration incoherente, pas une participation croisee (celle-ci se
        declare entre clients, par `partie_client_id`, et se signale au calcul) ;
      - une partie liee a un client (`partie_client_id`) a SES detenteurs dans
        le jeu de ce client : les declarer aussi ici les compterait deux fois ;
        deux parties ne designent pas le meme client, et aucune n'est le client
        lui-meme.
    """
    par_id = {l["partie_id"]: l for l in lues}
    for lue in lues:
        parent = lue.get("partie_de")
        if parent is None:
            continue
        if parent == lue["partie_id"]:
            raise RefusDePartie(f"la partie {lue['partie_id']} se déclare partie d'elle-même.")
        cible = par_id.get(parent)
        if cible is None:
            raise RefusDePartie(
                f"la partie {lue['partie_id']} est déclarée partie de « {parent} », absente du "
                f"jeu déclaré pour ce client : déclarez cette personne morale dans le même jeu.")
        if (cible["identite"].get("client_type") or "PP") != "PM":
            raise RefusDePartie(
                f"la partie {lue['partie_id']} est déclarée partie de {parent}, une personne "
                f"physique : seule une personne morale a des associés ou des dirigeants.")
        if cible.get("partie_client_id"):
            raise RefusDePartie(
                f"{parent} est le client {cible['partie_client_id']} du référentiel : ses parties "
                f"sont celles de ce client — les déclarer aussi ici ({lue['partie_id']}) les "
                f"compterait deux fois.")
    for lue in lues:
        vus = [lue["partie_id"]]
        courant = lue.get("partie_de")
        while courant is not None:
            if courant in vus:
                raise RefusDePartie(
                    "détentions circulaires dans le jeu déclaré : " + " → ".join(vus + [courant])
                    + " — aucune ne mène au client.")
            vus.append(courant)
            courant = par_id[courant].get("partie_de")
    lies = [l["partie_client_id"] for l in lues if l.get("partie_client_id")]
    if client_id in lies:
        raise RefusDePartie(f"une partie est déclarée être le client {client_id} lui-même.")
    doublons = sorted({c for c in lies if lies.count(c) > 1})
    if doublons:
        raise RefusDePartie(
            f"plusieurs parties déclarées être le même client ({', '.join(doublons)}) : sa "
            f"détention compterait plusieurs fois.")


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
    recribler la relation (LD §141). Un pourcentage, un role ou un maillon de
    la chaine modifie ne change aucun criblage. Commit par l'appelant.
    """
    vus = [l["partie_id"] for l in lues]
    doublons = sorted({p for p in vus if vus.count(p) > 1})
    if doublons:
        raise RefusDePartie(
            f"deux déclarations pour la même partie ({', '.join(doublons)}) : déclarez "
            f"ses rôles ensemble, sur une seule ligne.")
    verifier_la_structure(client_id, lues)
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
                partie_de=lue.get("partie_de"), partie_client_id=lue.get("partie_client_id"),
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
                   or bool(partie.peut_engager_les_fonds) != lue["peut_engager_les_fonds"]
                   or partie.partie_de != lue.get("partie_de")
                   or partie.partie_client_id != lue.get("partie_client_id"))
        if not differe:
            bilan["inchangees"] += 1
            continue
        partie.roles = lue["roles"]
        partie.identite = lue["identite"]
        partie.nom = lue["nom"]
        partie.pourcentage_detention = lue["pourcentage_detention"]
        partie.peut_engager_les_fonds = lue["peut_engager_les_fonds"]
        partie.partie_de = lue.get("partie_de")
        partie.partie_client_id = lue.get("partie_client_id")
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


def intermediaires(partie: PartieLiee,
                   parties_du_client: Optional[Iterable[PartieLiee]]) -> List[PartieLiee]:
    """Les personnes morales du jeu entre la partie et son client (`partie_de`
    remonte), de la plus proche a la plus lointaine. Borne et sans boucle
    quoi que porte la base : `verifier_la_structure` refuse les cycles a
    l'entree, ce parcours ne leur fait pas confiance pour terminer."""
    if not partie.partie_de or parties_du_client is None:
        return []
    par_reference = {p.partie_id: p for p in parties_du_client}
    sortie: List[PartieLiee] = []
    courant = par_reference.get(partie.partie_de)
    while courant is not None and courant not in sortie and len(sortie) < PROFONDEUR_MAXIMALE:
        sortie.append(courant)
        courant = par_reference.get(courant.partie_de) if courant.partie_de else None
    return sortie


def contexte(partie: PartieLiee, client_name: str,
             parties_du_client: Optional[Iterable[PartieLiee]] = None) -> Dict[str, Any]:
    """Ce que la decision et l'alerte figent de la partie : qui, a quel
    titre, pour quel client — et la chaine qui le dit. `parties_du_client`
    (les parties actives du meme client) nomme les holdings entre la partie et
    le client ; sans elles, une partie de holding ne se dirait pas « du
    client » : la holding est nommee par sa reference."""
    entre = intermediaires(partie, parties_du_client)
    texte = chaine(partie.roles or [], partie.nom, partie.pourcentage_detention,
                   bool(partie.peut_engager_les_fonds), client_name or partie.client_id, entre)
    if partie.partie_de and not entre:
        texte = texte.replace(" du client ", f" de la partie {partie.partie_de} du client ", 1)
    return {
        "id": partie.id,
        "partie_id": partie.partie_id,
        "client_id": partie.client_id,
        "client_name": client_name or partie.client_id,
        "roles": list(partie.roles or []),
        "nom": partie.nom,
        "pourcentage_detention": partie.pourcentage_detention,
        "peut_engager_les_fonds": bool(partie.peut_engager_les_fonds),
        "partie_de": partie.partie_de,
        "partie_client_id": partie.partie_client_id,
        "chaine": texte,
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
        fiche[CLE_DES_PARTIES] = [profil_de_criblage(p, contexte(p, nom, parties)) for p in parties]


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
        # Chaine de detention : la detention porte sur cette personne morale
        # du jeu (NULL : le client) ; la partie est ce client du referentiel
        "partie_de": partie.partie_de,
        "partie_client_id": partie.partie_client_id,
        "source": partie.source,
        "mise_a_jour_le": partie.updated_at.isoformat() if partie.updated_at else None,
        "retiree_le": partie.retiree_le.isoformat() if partie.retiree_le else None,
    }


# ------------------------------------------------------------ detention reputee

def _par_tranches(ids: Iterable[Any]) -> Iterable[List[Any]]:
    tries = sorted(set(ids))
    for debut in range(0, len(tries), _TRANCHE):
        yield tries[debut:debut + _TRANCHE]


def _clients_au_referentiel(db, client_ids: Iterable[str]) -> set:
    """Ceux de ces clients que le referentiel en production porte : les
    seuls dont les parties sont criblees (fiskr/rescreen.py)."""
    from fiskr.database import ClientEntity, Snapshot
    presents: set = set()
    for tranche in _par_tranches(c for c in client_ids if c):
        presents.update(cid for (cid,) in db.query(ClientEntity.client_id).join(
            Snapshot, Snapshot.snapshot_id == ClientEntity.snapshot_id).filter(
            Snapshot.file_type == "CLIENT_BASE", Snapshot.status == "READY",
            ClientEntity.client_id.in_(tranche)).distinct().all())
    return presents


def nom_du_client(db, client_id: Optional[str]) -> str:
    """Le nom d'un client tel que sa ligne la plus recente du referentiel en
    production le porte ; son identifiant s'il n'y est pas."""
    from fiskr.database import ClientEntity, Snapshot
    if not client_id:
        return ""
    ligne = db.query(ClientEntity).join(
        Snapshot, Snapshot.snapshot_id == ClientEntity.snapshot_id).filter(
        ClientEntity.client_id == client_id, Snapshot.file_type == "CLIENT_BASE",
        Snapshot.status == "READY").order_by(ClientEntity.id.desc()).first()
    if ligne is None:
        return client_id
    return nom_d_affichage({
        "client_type": ligne.client_type, "client_first_name": ligne.client_first_name,
        "client_last_name": ligne.client_last_name,
        "client_company_name": ligne.client_company_name}) or client_id


def clients_detenus_par(db, client_id: Optional[str]) -> List[str]:
    """Les clients dont la chaine de detention passe par ce client — une de
    leurs parties EST ce client (`partie_client_id`) —, de proche en proche :
    ceux que sa designation, ou celle d'une de ses parties, peut rendre
    reputes detenus. Borne a `PROFONDEUR_MAXIMALE`, chaque client une fois."""
    if not client_id:
        return []
    sortie: List[str] = []
    vus = {client_id}
    front = [client_id]
    for _ in range(PROFONDEUR_MAXIMALE):
        if not front:
            break
        suivants = set()
        for tranche in _par_tranches(front):
            suivants.update(cid for (cid,) in db.query(PartieLiee.client_id).filter(
                PartieLiee.partie_client_id.in_(tranche),
                PartieLiee.retiree_le.is_(None)).distinct().all() if cid not in vus)
        front = sorted(suivants)
        vus.update(front)
        sortie.extend(front)
    return sortie


def _est_personne_morale(partie: PartieLiee) -> bool:
    return ((partie.identite or {}).get("client_type") or "PP") == "PM"


class _Parcours:
    """
    UNE evaluation de la chaine d'un client : les jeux de parties charges une
    fois (le client, puis les clients que ses parties SONT, de proche en
    proche), les designations relevees une fois, puis la cascade.

    Un noeud est le client lui-meme (`("C", client_id)`) ou une partie
    (`PartieLiee`). Les detenteurs d'un noeud : les parties de premier niveau
    du client ; pour une partie personne morale, les parties du jeu declarees
    `partie_de` elle et, si elle EST un client du referentiel, les parties de
    premier niveau de ce client. Un noeud deja sur le chemin est un CYCLE : le
    lien n'est pas suivi, et c'est dit.
    """

    def __init__(self, db, client_id: str, surcharges: Optional[Dict[str, str]],
                 client_name: Optional[str]):
        self.db = db
        self.racine = ("C", client_id)
        self.surcharges = surcharges
        self._nom_du_client = client_name
        self.jeux: Dict[str, List[PartieLiee]] = {}
        self.signalements: List[Dict[str, Any]] = []
        self._signales: set = set()
        self.liens = 0
        self.chaines: List[List[Any]] = []
        self.structure: List[Dict[str, Any]] = []
        self._vus_structure: set = set()
        self.qualifications: Dict[Tuple[Any, ...], Optional[str]] = {}
        self._charger(client_id)
        self._designer()

    # -------------------------------------------------- graphe et designations

    def _charger(self, client_id: str) -> None:
        a_charger = [client_id]
        for _ in range(PROFONDEUR_MAXIMALE + 1):
            if not a_charger:
                break
            lus = parties_actives(self.db, a_charger)
            for cid in a_charger:
                self.jeux[cid] = lus.get(cid, [])
            a_charger = sorted({p.partie_client_id for cid in a_charger for p in self.jeux[cid]
                                if p.partie_client_id and p.partie_client_id not in self.jeux})
        self.lies = sorted({p.partie_client_id for jeu in self.jeux.values() for p in jeu
                            if p.partie_client_id})
        self.au_referentiel = _clients_au_referentiel(self.db, self.lies) if self.lies else set()

    def _designer(self) -> None:
        """Les parties designees : une alerte CONFIRMEE (vrai positif) sur une
        liste du perimetre SANCTION, surcharges comprises — sur la partie, ou,
        pour une partie qui EST un client, sur ce client lui-meme. Une
        homonymie non instruite n'est pas une personne designee, un PEP non
        plus."""
        from fiskr.perimeters import est_sanction
        self.alertes_de_partie: Dict[int, List[Alert]] = {}
        ids = [p.id for jeu in self.jeux.values() for p in jeu]
        for tranche in _par_tranches(ids):
            for a in self.db.query(Alert).filter(
                    Alert.partie_liee_id.in_(tranche), Alert.status == "CLOSED_CONFIRMED") \
                    .order_by(Alert.id.asc()).all():
                if est_sanction(a.list_type, self.surcharges):
                    self.alertes_de_partie.setdefault(a.partie_liee_id, []).append(a)
        self.alertes_de_client: Dict[str, List[Alert]] = {}
        for tranche in _par_tranches(self.lies):
            for a in self.db.query(Alert).filter(
                    Alert.client_id.in_(tranche), Alert.partie_liee_id.is_(None),
                    Alert.status == "CLOSED_CONFIRMED").order_by(Alert.id.asc()).all():
                if est_sanction(a.list_type, self.surcharges):
                    self.alertes_de_client.setdefault(a.client_id, []).append(a)
        # Une personne designee, UNE part par societe : la meme personne
        # confirmee sur deux listes (deux fiches, une partie) ou declaree deux
        # fois (deux parties, une fiche) ne s'additionne pas a elle-meme. Le
        # premier lot indexait la part sur la fiche : un beneficiaire a 30 %
        # confirme sur la liste DGT et sur la liste UE comptait 60 %.
        parent: Dict[Any, Any] = {}

        def racine(x):
            while parent.setdefault(x, x) != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for jeu in self.jeux.values():
            for p in jeu:
                for a in self.alertes(p):
                    parent[racine(("P", p.id))] = racine(("F", a.watchlist_entity_id))
        self.personne = {p.id: racine(("P", p.id)) for jeu in self.jeux.values() for p in jeu
                         if self.alertes(p)}

    def alertes(self, partie: PartieLiee) -> List[Alert]:
        return (self.alertes_de_partie.get(partie.id, [])
                + (self.alertes_de_client.get(partie.partie_client_id, [])
                   if partie.partie_client_id else []))

    # -------------------------------------------------- parcours

    def _cles(self, noeud: Any) -> set:
        if isinstance(noeud, tuple):
            return {noeud}
        cles = {("P", noeud.id)}
        if noeud.partie_client_id:
            cles.add(("C", noeud.partie_client_id))
        return cles

    def _nom(self, noeud: Any) -> str:
        if not isinstance(noeud, tuple):
            return noeud.nom
        # Lu seulement quand un texte le demande : un client sans partie
        # s'evalue sans requete de plus.
        if self._nom_du_client is None:
            self._nom_du_client = nom_du_client(self.db, self.racine[1])
        return self._nom_du_client

    def _cle_de_chemin(self, chemin: Sequence[Any]) -> Tuple[Any, ...]:
        return tuple(n if isinstance(n, tuple) else ("P", n.id) for n in chemin)

    def _detenteurs(self, noeud: Any) -> List[PartieLiee]:
        if isinstance(noeud, tuple):
            return [p for p in self.jeux.get(noeud[1], []) if not p.partie_de]
        sortie = [p for p in self.jeux.get(noeud.client_id, []) if p.partie_de == noeud.partie_id]
        if noeud.partie_client_id:
            sortie += [p for p in self.jeux.get(noeud.partie_client_id, []) if not p.partie_de]
        return sortie

    def _a_des_detenteurs(self, partie: PartieLiee) -> bool:
        if not _est_personne_morale(partie):
            return False
        return bool(partie.partie_client_id) or any(
            p.partie_de == partie.partie_id for p in self.jeux.get(partie.client_id, []))

    def texte_du_chemin(self, chemin: Sequence[Any]) -> str:
        """« ACME ← 60 % HOLDCO ← 70 % X » : du client vers ses detenteurs."""
        morceaux = [self._nom(chemin[0])]
        for n in chemin[1:]:
            pct = f"{_pourcentage_lisible(n.pourcentage_detention)} " \
                if n.pourcentage_detention is not None else ""
            engage = " (peut engager les fonds)" if n.peut_engager_les_fonds else ""
            morceaux.append(f"{pct}{n.nom}{engage}")
        return " ← ".join(morceaux)

    def _signaler(self, code: str, chemin: Sequence[Any], suite: str = "") -> None:
        texte = self.texte_du_chemin(chemin) + suite
        if (code, texte) in self._signales:
            return
        self._signales.add((code, texte))
        self.signalements.append({"type": code, "libelle": SIGNALEMENTS[code],
                                  "chemin": [self._nom(n) for n in chemin], "texte": texte})

    def _noter_structure(self, noeud: Any, partie: PartieLiee, niveau: int) -> Dict[str, Any]:
        """Le lien, note AVANT de descendre (l'ordre de lecture d'une
        arborescence) ; sa qualification est posee au retour. Un lien deja
        note (la meme holding atteinte par deux chemins) ne se repete pas."""
        cle = (tuple(sorted(self._cles(noeud))), partie.id)
        if cle in self._vus_structure:
            return {}
        self._vus_structure.add(cle)
        entree = {
            "niveau": niveau, "partie_liee_id": partie.id, "client_id": partie.client_id,
            "nom": partie.nom, "roles": list(partie.roles or []),
            "pourcentage_detention": partie.pourcentage_detention,
            "peut_engager_les_fonds": bool(partie.peut_engager_les_fonds),
            "de": self._nom(noeud), "partie_client_id": partie.partie_client_id,
            "qualification": None, "qualification_label": None,
        }
        self.structure.append(entree)
        return entree

    def evaluer(self, noeud: Any, chemin: List[Any], niveau: int) -> Dict[str, Any]:
        """
        La cascade sur un noeud (au niveau `niveau`, le client a 0) : ses
        detenteurs designes comptent pour leur part ; un detenteur personne
        morale compte pour sa part ENTIERE s'il est lui-meme repute detenu
        (detention) ou controle (controle), et pour rien sinon. Rend la
        qualification du noeud, ses cumuls et l'interet par multiplication.
        """
        sur_le_chemin = set().union(*(self._cles(n) for n in chemin))
        if (not isinstance(noeud, tuple) and noeud.partie_client_id
                and noeud.partie_client_id not in self.au_referentiel):
            self._signaler(SIGNAL_HORS_REFERENTIEL, chemin,
                           f" — client {noeud.partie_client_id}")
        detention: Dict[Any, float] = {}
        controle: Dict[Any, float] = {}
        designees: Dict[Any, float] = {}
        interet: Dict[Any, float] = {}
        engage = False
        for partie in self._detenteurs(noeud):
            self.liens += 1
            if self.liens > _LIENS_MAXIMAUX:
                self._signaler(SIGNAL_PROFONDEUR, chemin + [partie],
                               f" — plus de {_LIENS_MAXIMAUX} liens parcourus")
                break
            entree = self._noter_structure(noeud, partie, niveau + 1)
            if self._cles(partie) & sur_le_chemin:
                self._signaler(SIGNAL_CYCLE, chemin + [partie])
                continue
            pct = float(partie.pourcentage_detention or 0.0)
            qualification, sous_interet = None, 0.0
            if partie.id in self.personne:
                qualification, sous_interet = QUALIFICATION_DESIGNEE, 1.0
                cle = ("personne", self.personne[partie.id])
                designees[cle] = max(designees.get(cle, 0.0), pct)
                self.chaines.append(chemin + [partie])
            else:
                cle = ("entite", partie.id)
                if self._a_des_detenteurs(partie):
                    lie_non_charge = bool(partie.partie_client_id) and partie.partie_client_id not in self.jeux
                    if niveau + 2 > PROFONDEUR_MAXIMALE or lie_non_charge:
                        self._signaler(SIGNAL_PROFONDEUR, chemin + [partie],
                                       f" ← … (au-delà de {PROFONDEUR_MAXIMALE} niveaux)")
                    else:
                        sous = self.evaluer(partie, chemin + [partie], niveau + 1)
                        qualification, sous_interet = sous["qualification"], sous["interet"]
            if entree:
                entree["qualification"] = qualification
                entree["qualification_label"] = QUALIFICATIONS.get(qualification or "")
            if qualification in (QUALIFICATION_DESIGNEE, QUALIFICATION_DETENUE):
                detention[cle] = max(detention.get(cle, 0.0), pct)
            if qualification is not None:
                controle[cle] = max(controle.get(cle, 0.0), pct)
                engage = engage or bool(partie.peut_engager_les_fonds)
            interet[cle] = max(interet.get(cle, 0.0), pct / 100.0 * sous_interet)
        cumul = round(float(sum(detention.values())), 4)
        cumul_controle = round(float(sum(controle.values())), 4)
        if cumul >= SEUIL_DE_DETENTION_PCT:
            qualification = QUALIFICATION_DETENUE
        elif cumul_controle >= SEUIL_DE_DETENTION_PCT or engage:
            qualification = QUALIFICATION_CONTROLEE
        else:
            qualification = None
        self.qualifications[self._cle_de_chemin(chemin)] = qualification
        return {"qualification": qualification, "cumul": cumul, "cumul_controle": cumul_controle,
                "direct": round(float(sum(designees.values())), 4), "engage": engage,
                "interet": min(1.0, sum(interet.values()))}

    def chaines_lisibles(self) -> List[Dict[str, Any]]:
        """Chaque chemin du client vers une partie designee, avec ce qu'il
        etablit : il compte si chaque societe intermediaire est reputee
        detenue ou controlee ; sinon, la premiere qui ne l'est pas est nommee."""
        sortie = []
        for chemin in self.chaines:
            designee = chemin[-1]
            etapes, motif, produit = [], None, 1.0
            for i, n in enumerate(chemin[1:], start=1):
                qualification = (QUALIFICATION_DESIGNEE if n is designee
                                 else self.qualifications.get(self._cle_de_chemin(chemin[:i + 1])))
                if n is not designee and qualification is None and motif is None:
                    motif = f"{n.nom} : ni réputée détenue ni réputée contrôlée"
                produit = None if produit is None or n.pourcentage_detention is None \
                    else produit * float(n.pourcentage_detention) / 100.0
                etapes.append({"nom": n.nom, "partie_liee_id": n.id, "client_id": n.client_id,
                               "partie_client_id": n.partie_client_id,
                               "pourcentage_detention": n.pourcentage_detention,
                               "peut_engager_les_fonds": bool(n.peut_engager_les_fonds),
                               "qualification": qualification,
                               "qualification_label": QUALIFICATIONS.get(qualification or "")})
            alertes = self.alertes(designee)
            sortie.append({
                "racine": self._nom(self.racine), "etapes": etapes, "niveau": len(etapes),
                "texte": self.texte_du_chemin(chemin) + " (personne désignée)",
                "compte": motif is None, "motif": motif,
                "produit_pct": round(produit * 100.0, 4) if produit is not None else None,
                "alertes": [a.id for a in alertes],
                "personnes_designees": list(dict.fromkeys(a.watchlist_name for a in alertes)),
            })
        return sortie


def evaluer_la_detention(db, client_id: Optional[str],
                         surcharges: Optional[Dict[str, str]] = None,
                         client_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Le client est-il repute detenu ou controle par une personne designee, au
    vu de ses parties CONFIRMEES — directement ou par la chaine de detention ?

    Ne comptent que les parties dont une alerte est CONFIRMEE (vrai positif)
    sur une liste du perimetre SANCTION (fiskr/perimeters.py, surcharges
    comprises) : une homonymie non instruite n'est pas une personne designee,
    et un PEP n'en est pas une non plus. Une partie retiree ne compte plus —
    la source ne la declare plus.

    - DETENTION (§131) : les parts des personnes designees s'agregent ; une
      seule part par personne designee dans une meme societe — deux
      declarations de la meme detention ne se somment pas (meme regle que les
      liens entre fiches listees, fiskr/api.py). Une societe intermediaire
      reputee detenue compte pour sa participation entiere (cascade : « LA
      CHAINE DE DETENTION » en tete de module). Seuil : 50 % ou plus.
    - CONTROLE (§133, §144) : une partie designee — ou une societe reputee
      detenue ou controlee — peut engager les fonds du client (representant
      legal, statuts, delegation de signature, procuration), ou 50 % ou plus
      de ses parts sont portes par des societes reputees controlees.

    Rend {"reputee", "fondement", "cumul_pct", "seuil_pct", "controle",
    "parties"} comme au premier niveau — `parties` detaille chaque partie
    designee retenue et son alerte : un reviseur doit pouvoir refaire le
    calcul — et, pour la chaine : `cumul_direct_pct`, `cumul_indirect_pct`,
    `cumul_avec_controle_pct`, `multiplication_pct` (interet economique par
    produit des pourcentages, montre et non retenu : les textes ne le fixent
    pas), `chaines` (chaque chemin vers une partie designee, et s'il compte),
    `structure` (les liens parcourus) et `signalements` (cycle, profondeur,
    client lie hors referentiel, multiplication qui franchirait le seuil).
    """
    evaluation: Dict[str, Any] = {
        "reputee": False, "fondement": None, "cumul_pct": 0.0,
        "seuil_pct": SEUIL_DE_DETENTION_PCT, "controle": False, "parties": [],
        "cumul_direct_pct": 0.0, "cumul_indirect_pct": 0.0, "cumul_avec_controle_pct": 0.0,
        "multiplication_pct": 0.0, "chaines": [], "structure": [], "signalements": [],
        "profondeur_maximale": PROFONDEUR_MAXIMALE}
    if not client_id:
        return evaluation
    parcours = _Parcours(db, client_id, surcharges, client_name)
    racine = parcours.evaluer(parcours.racine, [parcours.racine], 0)
    chaines = parcours.chaines_lisibles()
    multiplication = round(racine["interet"] * 100.0, 4)
    if racine["qualification"] is None and multiplication >= SEUIL_DE_DETENTION_PCT:
        parcours.signalements.append({
            "type": SIGNAL_MULTIPLICATION, "libelle": SIGNALEMENTS[SIGNAL_MULTIPLICATION],
            "chemin": [], "texte": f"{_pourcentage_lisible(multiplication)} par multiplication"})
    retenues: Dict[int, Dict[str, Any]] = {}
    references = {(p.client_id, p.partie_id): p.nom for jeu in parcours.jeux.values() for p in jeu}
    for chaine_lue, chemin in zip(chaines, parcours.chaines):
        partie = chemin[-1]
        if partie.id in retenues:
            continue
        retenues[partie.id] = {
            "partie_liee_id": partie.id, "nom": partie.nom,
            "roles": list(partie.roles or []), "client_id": partie.client_id,
            "pourcentage_detention": partie.pourcentage_detention,
            "peut_engager_les_fonds": bool(partie.peut_engager_les_fonds),
            # La societe dont elle est la partie (NULL : le client evalue)
            "partie_de": (references.get((partie.client_id, partie.partie_de))
                          if partie.partie_de else
                          (None if partie.client_id == client_id else
                           parcours._nom(chemin[-2]))),
            "niveau": len(chemin) - 1, "chemin": chaine_lue["texte"],
            "alertes": chaine_lue["alertes"],
            "personnes_designees": chaine_lue["personnes_designees"]}
    cumul = racine["cumul"]
    evaluation.update({
        "cumul_pct": cumul, "cumul_direct_pct": racine["direct"],
        "cumul_indirect_pct": round(cumul - racine["direct"], 4),
        "cumul_avec_controle_pct": racine["cumul_controle"],
        "multiplication_pct": multiplication,
        "controle": bool(racine["engage"]
                         or racine["cumul_controle"] >= SEUIL_DE_DETENTION_PCT),
        "parties": list(retenues.values()), "chaines": chaines,
        "structure": parcours.structure, "signalements": parcours.signalements})
    if racine["qualification"] == QUALIFICATION_DETENUE:
        evaluation["fondement"] = FONDEMENT_DETENTION
    elif racine["qualification"] == QUALIFICATION_CONTROLEE:
        evaluation["fondement"] = FONDEMENT_CONTROLE
    evaluation["reputee"] = evaluation["fondement"] is not None
    return evaluation


def phrase_de_detention(evaluation: Dict[str, Any], client_name: str) -> str:
    """Ce que l'evaluation etablit, en une phrase qui se relit : le cumul, le
    seuil, les parties qui le forment — et, au-dela du premier niveau, les
    chaines qui y menent et ce que le calcul n'a pas pu trancher."""
    detail = ", ".join(
        f"{p['nom']}"
        + (f" {_pourcentage_lisible(p['pourcentage_detention'])}" if p.get("pourcentage_detention") else "")
        + (f" de {p['partie_de']}" if p.get("partie_de") else "")
        + (" (peut engager les fonds)" if p.get("peut_engager_les_fonds") else "")
        + f" — alerte(s) #{', #'.join(str(a) for a in p['alertes'])}"
        for p in evaluation.get("parties") or []) or "aucune"
    cumul = _pourcentage_lisible(evaluation.get("cumul_pct") or 0.0)
    seuil = _pourcentage_lisible(evaluation.get("seuil_pct") or SEUIL_DE_DETENTION_PCT)
    indirect = evaluation.get("cumul_indirect_pct") or 0.0
    dont = f", dont {_pourcentage_lisible(indirect)} par la chaîne de détention" if indirect else ""
    indirectes = [c for c in evaluation.get("chaines") or [] if c.get("niveau", 1) > 1]
    chaines = (" Chaîne(s) : " + " ; ".join(
        c["texte"] + (" — retenue" if c.get("compte") else f" — non retenue ({c.get('motif')})")
        + (f", {_pourcentage_lisible(c['produit_pct'])} par multiplication"
           if c.get("produit_pct") is not None else "")
        for c in indirectes) + " (cascade : une société réputée détenue compte pour sa "
        "participation entière ; aucun texte lu ne fixe la multiplication).") if indirectes else ""
    signaux = (" À vérifier : " + " ; ".join(
        f"{s['libelle']} — {s['texte']}" for s in evaluation.get("signalements") or []) + ".") \
        if evaluation.get("signalements") else ""
    if evaluation.get("fondement") == FONDEMENT_DETENTION:
        return (f"Client {client_name} réputé détenu par une ou des personnes désignées : "
                f"{cumul} cumulés par les parties confirmées{dont} ({detail}), seuil {seuil} "
                f"(LD DGT/ACPR §131).{chaines}{signaux}")
    if evaluation.get("fondement") == FONDEMENT_CONTROLE:
        controle = evaluation.get("cumul_avec_controle_pct") or 0.0
        if controle >= (evaluation.get("seuil_pct") or SEUIL_DE_DETENTION_PCT):
            par = (f"{_pourcentage_lisible(controle)} de ses parts sont portés par des sociétés "
                   f"réputées contrôlées par des personnes désignées ({detail})")
        else:
            par = f"une personne désignée peut engager ses fonds ({detail})"
        return (f"Client {client_name} réputé contrôlé par une ou des personnes désignées : "
                f"{par} ; détention cumulée {cumul}, sous le seuil de {seuil} "
                f"(LD DGT/ACPR §133, §144).{chaines}{signaux}")
    return (f"Client {client_name} ni réputé détenu ni réputé contrôlé : {cumul} cumulés par "
            f"les parties confirmées{dont} ({detail}), sous le seuil de {seuil}, et aucune partie "
            f"confirmée ne peut engager ses fonds (LD DGT/ACPR §131, §144).{chaines}{signaux}")

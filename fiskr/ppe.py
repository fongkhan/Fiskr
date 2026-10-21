"""
Le dossier PPE : ce qui suit une personne politiquement exposee confirmee.

POURQUOI CE MODULE EXISTE
-------------------------
Lecture du code (enquete de conformite, octobre 2026) : `_close_alert` closait
une alerte PEP confirmee exactement comme les autres — rien ne suivait. Le
registre des gels (fiskr/gels.py) avait donne une suite au perimetre SANCTION ;
une PPE n'est pas gelee, elle n'en relevait pas. Or une correspondance PPE
confirmee n'est pas la fin d'une instruction : c'est le debut de mesures de
vigilance complementaires (Code monetaire et financier, texte consolide de
codes.droit.org edition du 2 octobre 2026 — Legifrance repondait 403) :

  - L.561-10 1° (version de la loi n° 2025-1249 du 22 decembre 2025) : le
    client, ou son beneficiaire effectif, « est une personne qui est exposee
    a des risques particuliers en raison des fonctions politiques,
    juridictionnelles ou administratives qu'elle exerce ou a exercees ou de
    celles qu'exercent ou ont exercees des membres directs de sa famille ou
    des personnes connues pour lui etre etroitement associees ou le devient
    en cours de relation d'affaires ». (C'est le 1° depuis l'ordonnance
    n° 2016-1635 ; le 2° vise aujourd'hui les produits favorisant l'anonymat.)
  - R.561-18 : I. les fonctions, exercees « ou [dont la personne] a cesse
    d'exercer depuis moins d'un an » ; II. les membres directs de la famille
    (conjoint ou concubin notoire, partenaire de PACS, enfants et leur
    conjoint ou partenaire, ascendants au premier degre) ; III. les personnes
    etroitement associees (beneficiaires effectifs conjoints, beneficiaire
    effectif seul d'une structure etablie au profit de la PPE, liens
    d'affaires etroits).
  - R.561-20-2 : 1° « la decision de nouer ou maintenir une relation
    d'affaires avec cette personne ne peut etre prise que par un membre de
    l'organe executif ou toute personne habilitee a cet effet par l'organe
    executif » ; 2° rechercher « l'origine du patrimoine et des fonds
    impliques dans la relation d'affaires ou la transaction » ; 3° renforcer
    la vigilance constante de R.561-12-1.

Ce module tient ces trois obligations, sur le modele du registre des gels :
la confirmation ouvre le dossier dans la meme transaction que la cloture, et
le dossier ne passe « decide » que sur une decision prise par une personne
habilitee qui n'a pas instruit l'alerte.

QUI DECIDE
----------
Le role `reviewer` (l'administrateur passe toujours, cf. fiskr/auth.py). Il
porte deja dans Fiskr les actes que l'etablissement opposera a un controleur :
validation 4-yeux, homologation des listes, declaration des gels. Habiliter
une personne a decider des relations PPE est un acte de l'organe executif que
Fiskr ne peut pas constater ; lui attribuer le role reviewer en est la
traduction dans le produit. La regle que Fiskr PEUT tenir, il la tient : ni
l'analyste qui a propose la confirmation, ni celui qui l'a validee ne decident
de la relation.

QU'EST-CE QU'UNE LISTE PEP
--------------------------
`fiskr.perimeters.est_ppe` — jamais une liste de types recopiee ici.

CE QUI N'EST PAS FAIT
---------------------
Une partie de paiement (« TXN:… ») n'ouvre pas de dossier : il n'y a pas de
relation d'affaires avec elle, et R.561-20-2 vise le client ou son
beneficiaire effectif. Le dossier le dit au lieu de se taire.

LA REVUE DE VIGILANCE RENFORCEE
-------------------------------
Datee a la decision, elle etait signalee echue a l'ecran et c'etait tout. Elle
leve desormais une notification immediate `ppe_revue_echue`, une fois par
echeance ; echue depuis plus que le delai de decision, elle bloque le controle
de mise en service (rejoue par la sonde, sans nom de personne). Elle se tient
par un geste (`enregistrer_revue`) : maintien avec la revue suivante datee, ou
fin de la relation — par une personne habilitee qui n'a pas instruit l'alerte.
La revue n'exige pas de piece nouvelle : son motif dit ce qui a ete revu.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fiskr.database import Alert, AlertAttachment, AlertEvent, DossierPPE

logger = logging.getLogger("fiskr.ppe")

# ------------------------------------------------------------ vocabulaire

# Qualite d'une fiche PEP, LUE a la source par le lecteur (fiskr/ingest.py)
# puis retenue par l'instruction. Une seule definition : le lecteur, le
# dossier et l'ecran la lisent ici.
QUALITE_PPE = "PPE"
QUALITE_FAMILLE = "FAMILLE"
QUALITE_ASSOCIE = "ASSOCIE"
QUALITE_PROCHE = "PROCHE"
QUALITE_NON_PRECISEE = "NON_PRECISEE"
QUALITES = {
    QUALITE_PPE: "Personne politiquement exposée",
    QUALITE_FAMILLE: "Membre de la famille d'une PPE",
    QUALITE_ASSOCIE: "Personne étroitement associée à une PPE",
    QUALITE_PROCHE: "Proche d'une PPE (famille ou associé, non distingué par la source)",
    QUALITE_NON_PRECISEE: "Qualité non précisée par la source",
}
# Ce qu'une instruction peut retenir : les trois categories du texte
# (R.561-18 I, II et III). « Proche » et « non precisee » disent ce que la
# source a pu dire ; une decision, elle, porte sur une qualite etablie.
QUALITES_RETENUES = (QUALITE_PPE, QUALITE_FAMILLE, QUALITE_ASSOCIE)

# Motif ecrit sur la fiche (`designation_reasons`). Le lecteur ecrivait
# « Personne Politiquement Exposée (PEP) » sur CHAQUE fiche du jeu, proche
# compris, sans avoir rien lu qui le dise. Une chaine par ligne : l'ecran les
# traduit telles quelles (fiskr/static/i18n.js).
MOTIFS_DE_FICHE = {
    QUALITE_PPE: "Personne politiquement exposée (qualité lue à la source)",
    QUALITE_FAMILLE: "Membre de la famille d'une personne politiquement exposée (qualité lue à la source)",
    QUALITE_ASSOCIE: "Personne étroitement associée à une personne politiquement exposée (qualité lue à la source)",
    QUALITE_PROCHE: "Proche d'une personne politiquement exposée : membre de la famille ou personne étroitement associée, la source ne distingue pas",
    QUALITE_NON_PRECISEE: "Liste de personnes politiquement exposées — qualité non précisée par la source : PPE, membre de la famille ou personne étroitement associée",
}
# Le motif constant d'avant : reconnu par le delta comme une forme ancienne
# du lecteur (fiskr/ingest.py, `qualite_lue_par_le_lecteur`), jamais ecrit.
ANCIEN_MOTIF_PEP = "Personne Politiquement Exposée (PEP)"


def motif_de_fiche(qualite: Optional[str]) -> str:
    return MOTIFS_DE_FICHE.get(qualite or QUALITE_NON_PRECISEE,
                               MOTIFS_DE_FICHE[QUALITE_NON_PRECISEE])


STATUT_A_DECIDER = "A_DECIDER"
STATUT_DECIDE = "DECIDE"
STATUTS = (STATUT_A_DECIDER, STATUT_DECIDE)
STATUT_LABELS = {STATUT_A_DECIDER: "Décision à prendre", STATUT_DECIDE: "Décidé"}

# La decision de R.561-20-2 1° : « nouer ou maintenir », et leurs contraires.
DECISIONS = {
    "ENTREE_EN_RELATION": "Entrée en relation autorisée",
    "MAINTIEN": "Maintien de la relation autorisé",
    "REFUS": "Refus d'entrer en relation",
    "FIN_DE_RELATION": "Fin de la relation décidée",
}
# Une relation qui s'ouvre ou continue engage la vigilance renforcee : elle
# exige l'origine des fonds et du patrimoine, une piece, et une revue datee.
DECISIONS_QUI_ACCEPTENT = ("ENTREE_EN_RELATION", "MAINTIEN")

# Filtres du registre (en plus des statuts)
FILTRE_EN_RETARD = "EN_RETARD"
FILTRE_NON_DECLAREE = "NON_DECLAREE"
FILTRE_REVUE_ECHUE = "REVUE_ECHUE"

_AVANCE_TOLEREE = timedelta(minutes=5)
_MAX_TEXTE = 4000


class RefusPPE(Exception):
    """Refus franc d'une operation du dossier, avec le code HTTP a rendre."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


# ------------------------------------------------------------ la regle des douze mois

def _date_iso(valeur: Optional[str]) -> Optional[date]:
    if not valeur:
        return None
    try:
        return date.fromisoformat(str(valeur).strip()[:10])
    except ValueError:
        return None


def _un_an_apres(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:          # 29 fevrier
        return d.replace(year=d.year + 1, day=28)


def exposition_echue(fin_de_fonction: Optional[str],
                     maintenant: Optional[datetime] = None) -> bool:
    """
    La fonction s'est-elle terminee il y a UN AN OU PLUS ? R.561-18 I : est
    PPE la personne qui exerce « ou a cesse d'exercer depuis moins d'un an »
    l'une des fonctions. Au-dela, la qualite n'est plus etablie par cette
    seule fonction — c'est un constat a DIRE, pas une cloture : rien n'est
    ferme automatiquement, la personne habilitee decide si la vigilance
    complementaire est maintenue (le risque, lui, ne s'eteint pas a date fixe).
    Une fin inconnue n'est pas une fin.
    """
    fin = _date_iso(fin_de_fonction)
    if fin is None:
        return False
    aujourd_hui = (maintenant or datetime.utcnow()).date()
    return aujourd_hui >= _un_an_apres(fin)


# ------------------------------------------------------------ ouverture

def est_du(alert: Alert) -> bool:
    """Un dossier est du quand une alerte est CONFIRMEE sur une liste PEP et
    porte sur un client (pas une partie de paiement). C'est un du, pas une
    option : rien ne le desactive."""
    from fiskr.perimeters import est_ppe
    return (alert.status == "CLOSED_CONFIRMED" and est_ppe(alert.list_type)
            and not _est_partie_de_paiement(alert.client_id))


def _est_partie_de_paiement(client_id: Optional[str]) -> bool:
    return str(client_id or "").startswith("TXN:")


def motif_sans_dossier(alert: Alert) -> Optional[str]:
    """Pourquoi une PPE confirmee n'a PAS de dossier, quand c'est voulu : dit
    a l'ecran plutot que tu."""
    from fiskr.perimeters import est_ppe
    if alert.status == "CLOSED_CONFIRMED" and est_ppe(alert.list_type) \
            and _est_partie_de_paiement(alert.client_id):
        # Une seule chaine : l'ecran la traduit telle quelle (fiskr/static/i18n.js)
        return "Partie d'un paiement : aucune relation d'affaires avec elle, donc pas de dossier PPE (R.561-20-2 vise le client ou son bénéficiaire effectif)."
    return None


def _classification_du_client(db, client_id: Optional[str]) -> Tuple[Optional[bool], str]:
    """
    Ce que le SI amont dit du client (`client_pep_flag` de sa fiche la plus
    recente au referentiel en production) : (True | False | None, constat).
    Memes fiches que celles que le registre des gels lit — une definition.
    """
    from fiskr.gels import _lignes_client
    lignes = _lignes_client(db, client_id)
    if not lignes:
        return None, ("absent du référentiel en production : sa classification PPE n'a pas "
                      "pu être lue")
    valeur = lignes[0].client_pep_flag
    if valeur is True:
        return True, "que le SI amont déclare PPE"
    if valeur is False:
        return False, "que le SI amont déclare non PPE (client_pep_flag = faux)"
    return None, "dont la fiche ne porte aucune classification PPE (client_pep_flag vide)"


def _journal(db, dossier: DossierPPE, username: str, action: str, detail: str) -> None:
    db.add(AlertEvent(alert_id=dossier.alert_id, username=username, action=action, detail=detail))


def _ouvrir(db, alert: Alert, username: str, depuis: Optional[datetime]) -> DossierPPE:
    from fiskr.gels import fiche_en_production
    fiche = fiche_en_production(db, alert.watchlist_entity_id)
    qualite = (fiche.pep_qualite if fiche is not None else None) or QUALITE_NON_PRECISEE
    declare, constat = _classification_du_client(db, alert.client_id)
    dossier = DossierPPE(
        alert_id=alert.id, statut=STATUT_A_DECIDER,
        client_id=alert.client_id, client_name=alert.client_name,
        watchlist_entity_id=alert.watchlist_entity_id,
        watchlist_name=alert.watchlist_name, list_type=alert.list_type,
        qualite_source=qualite,
        fonction_source=(fiche.pep_role if fiche is not None else None),
        fin_de_fonction_source=(fiche.pep_fin_de_fonction if fiche is not None else None),
        liens_source=(fiche.pep_liens if fiche is not None else None),
        # L'instruction part de ce que la source a etabli — jamais d'une
        # qualite qu'elle n'a pas dite
        qualite_retenue=qualite if qualite in QUALITES_RETENUES else None,
        fonction=(fiche.pep_role if fiche is not None else None),
        fin_de_fonction=(fiche.pep_fin_de_fonction if fiche is not None else None),
        client_pep_declare=declare, ppe_non_declaree=declare is not True,
        ouvert_le=depuis or datetime.utcnow(), ouvert_par=username,
        proposee_par=alert.proposed_by, confirmee_par=alert.decided_by,
    )
    db.add(dossier)
    db.flush()
    _journal(db, dossier, username, "PPE_DOSSIER_OUVERT",
             f"Dossier PPE #{dossier.id} ouvert ({QUALITES[qualite]}) : décision d'entrer ou de "
             f"rester en relation à prendre par une personne habilitée, origine du patrimoine "
             f"et des fonds à rechercher (CMF L.561-10 1°, R.561-20-2).")
    if dossier.ppe_non_declaree:
        _journal(db, dossier, username, "PPE_NON_DECLAREE",
                 f"Dossier PPE #{dossier.id} : PPE confirmée chez un client {constat}. La "
                 f"classification du client est à corriger dans le SI amont — Fiskr ne la "
                 f"corrige pas lui-même.")
    return dossier


def ouvrir_si_du(db, alert: Alert, username: str) -> Optional[DossierPPE]:
    """
    Appele a CHAQUE confirmation d'alerte (avant le commit de la cloture),
    depuis `_close_alert` : ouvre le dossier si la liste est une liste PEP.
    Idempotent — une alerte deja pourvue n'en recoit pas un second. Le delai
    court depuis la confirmation (`decided_at`).
    """
    if not est_du(alert):
        return None
    if db.query(DossierPPE.id).filter(DossierPPE.alert_id == alert.id).first():
        return None
    return _ouvrir(db, alert, username, alert.decided_at)


def ouvrir(db, alert: Alert, username: str) -> DossierPPE:
    """Dossier d'une PPE confirmee AVANT le dossier PPE. Son delai court
    depuis la confirmation, pas depuis aujourd'hui : le dossier ne rajeunit
    pas une decision qui manque depuis ce jour-la."""
    if not est_du(alert):
        if alert.status != "CLOSED_CONFIRMED":
            raise RefusPPE("Seule une alerte confirmée (vrai positif) ouvre un dossier PPE.", 409)
        raise RefusPPE(motif_sans_dossier(alert)
                       or "Cette liste n'est pas une liste de personnes politiquement exposées.", 409)
    if db.query(DossierPPE.id).filter(DossierPPE.alert_id == alert.id).first():
        raise RefusPPE("Cette alerte a déjà son dossier PPE.", 409)
    return _ouvrir(db, alert, username, alert.decided_at)


def alertes_confirmees_sans_dossier(db, plafond: int = 2000) -> List[Alert]:
    """PPE confirmees qui n'ont aucun dossier — typiquement celles confirmees
    avant qu'il n'existe. Filtre SQL sur les types PEP (derives de
    fiskr/perimeters.py) : la sonde de conformite le rejoue chaque minute."""
    from fiskr.perimeters import types_ppe
    avec_dossier = db.query(DossierPPE.alert_id)
    candidates = db.query(Alert).filter(
        Alert.status == "CLOSED_CONFIRMED", Alert.list_type.in_(sorted(types_ppe())),
        ~Alert.id.in_(avec_dossier)).order_by(Alert.decided_at.asc()).limit(plafond).all()
    return [a for a in candidates if est_du(a)]


# ------------------------------------------------------------ instruction

def _date_saisie(valeur: Any, champ: str, futur: bool = False) -> Optional[datetime]:
    """ISO 8601 -> datetime UTC naif. Une date passee est refusee la ou l'on
    attend l'avenir (prochaine revue), une date future la ou l'on trace ce
    qui a eu lieu (decision)."""
    if valeur in (None, ""):
        return None
    if isinstance(valeur, datetime):
        d = valeur
    else:
        try:
            d = datetime.fromisoformat(str(valeur).strip().replace("Z", "+00:00"))
        except ValueError:
            raise RefusPPE(f"{champ} illisible (ISO 8601 attendu).")
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    maintenant = datetime.utcnow()
    if futur and d <= maintenant:
        raise RefusPPE(f"{champ} : une date à venir est attendue.")
    if not futur and d > maintenant + _AVANCE_TOLEREE:
        raise RefusPPE(f"{champ} dans le futur : on ne trace que ce qui a eu lieu.")
    return d


def _date_de_fin(valeur: Optional[str]) -> Optional[str]:
    if valeur in (None, ""):
        return None
    d = _date_iso(valeur)
    if d is None:
        raise RefusPPE("Fin de fonction illisible (AAAA-MM-JJ attendu).")
    if d > datetime.utcnow().date():
        raise RefusPPE("Fin de fonction dans le futur : une fonction en cours n'a pas de fin.")
    return d.isoformat()


def _refuser_si_decide(dossier: DossierPPE) -> None:
    if dossier.statut != STATUT_A_DECIDER:
        raise RefusPPE("Dossier déjà décidé : il ne se réécrit plus. Ce qui a été décidé l'a "
                       "été sur ces pièces.", 409)


def enregistrer_instruction(db, dossier: DossierPPE, *, username: str,
                            qualite_retenue: Optional[str] = None,
                            fonction: Optional[str] = None,
                            fin_de_fonction: Optional[str] = None,
                            origine_fonds: Optional[str] = None,
                            origine_patrimoine: Optional[str] = None) -> None:
    """Ce que l'instruction etablit : qualite retenue, fonction et sa fin,
    origine des fonds et du patrimoine (R.561-20-2 2°). Champ absent : inchange ;
    chaine vide : efface."""
    _refuser_si_decide(dossier)
    changements = []
    if qualite_retenue is not None:
        code = qualite_retenue.strip().upper() or None
        if code is not None and code not in QUALITES_RETENUES:
            raise RefusPPE(f"Qualité inconnue ({', '.join(QUALITES_RETENUES)}).")
        if code != dossier.qualite_retenue:
            changements.append(f"qualité retenue : {QUALITES.get(code, '—') if code else '—'}")
            dossier.qualite_retenue = code
    if fin_de_fonction is not None:
        fin = _date_de_fin(fin_de_fonction)
        if fin != dossier.fin_de_fonction:
            changements.append(f"fin de fonction : {fin or '—'}")
            dossier.fin_de_fonction = fin
    for champ, valeur, libelle in (("fonction", fonction, "fonction"),
                                   ("origine_fonds", origine_fonds, "origine des fonds"),
                                   ("origine_patrimoine", origine_patrimoine, "origine du patrimoine")):
        if valeur is None:
            continue
        texte = valeur.strip()[:_MAX_TEXTE] or None
        if texte != getattr(dossier, champ):
            setattr(dossier, champ, texte)
            changements.append(libelle)
    if changements:
        _journal(db, dossier, username, "PPE_DOSSIER_INSTRUIT",
                 f"Dossier PPE #{dossier.id} : " + " ; ".join(changements) + ".")


def ajouter_piece(db, dossier: DossierPPE, piece: AlertAttachment, username: str) -> None:
    """Une piece de l'origine des fonds ou du patrimoine : piece jointe de
    l'alerte (donc dans l'inventaire des pieces probantes, fiskr/preuves.py),
    referencee par le dossier."""
    _refuser_si_decide(dossier)
    dossier.pieces_origine = list(dossier.pieces_origine or []) + [piece.id]
    _journal(db, dossier, username, "PPE_PIECE_ORIGINE",
             f"Dossier PPE #{dossier.id} : pièce de l'origine des fonds ou du patrimoine "
             f"jointe ({piece.file_name}).")


def verifier_avant_piece(dossier: DossierPPE) -> None:
    """Tout ce qui peut refuser une piece, verifie AVANT que le fichier ne soit
    ecrit sur le disque : un refus apres coup laisserait un fichier qu'aucune
    ligne ne reference."""
    _refuser_si_decide(dossier)


# ------------------------------------------------------------ decision

def instruit_par(dossier: DossierPPE) -> List[str]:
    """Ceux qui ont instruit la confirmation (proposition, validation) : ils
    ne decident pas de la relation."""
    return sorted({u for u in (dossier.proposee_par, dossier.confirmee_par) if u})


def enregistrer_decision(db, dossier: DossierPPE, *, username: str, decision: str,
                         motif: str, reference: Optional[str] = None,
                         decidee_le: Any = None, prochaine_revue_le: Any = None) -> None:
    """
    La decision de nouer ou maintenir la relation (R.561-20-2 1°), par une
    personne habilitee (role verifie par l'endpoint) qui n'a PAS instruit
    l'alerte. Accepter une relation exige ce que le texte attache a la
    vigilance complementaire : qualite retenue, origine des fonds ET du
    patrimoine, au moins une piece, et une prochaine revue datee — sans eux,
    « decide » serait une affirmation que le dossier ne porte pas.
    """
    _refuser_si_decide(dossier)
    if username in instruit_par(dossier):
        raise RefusPPE("La décision d'entrer ou de rester en relation revient à une personne "
                       "habilitée qui n'a pas instruit l'alerte : vous en avez proposé ou validé "
                       "la confirmation.", 403)
    code = (decision or "").strip().upper()
    if code not in DECISIONS:
        raise RefusPPE(f"Décision inconnue ({', '.join(DECISIONS)}).")
    texte = (motif or "").strip()
    if not texte:
        raise RefusPPE("Le motif de la décision est obligatoire.")
    if not dossier.qualite_retenue:
        raise RefusPPE("Renseignez d'abord la qualité retenue (PPE, membre de la famille ou "
                       "personne étroitement associée) : la décision porte sur elle.", 409)
    date_decision = _date_saisie(decidee_le, "Date de la décision") or datetime.utcnow()
    revue = None
    if code in DECISIONS_QUI_ACCEPTENT:
        manquants = [libelle for champ, libelle in (("origine_fonds", "l'origine des fonds"),
                                                    ("origine_patrimoine", "l'origine du patrimoine"))
                     if not getattr(dossier, champ)]
        if not dossier.pieces_origine:
            manquants.append("au moins une pièce qui l'établit")
        if manquants:
            raise RefusPPE("Une relation ne s'ouvre ni ne se maintient sans " + ", ".join(manquants)
                           + " (R.561-20-2 2°).", 409)
        revue = _date_saisie(prochaine_revue_le, "Prochaine revue", futur=True)
        if revue is None:
            raise RefusPPE("Datez la prochaine revue du dossier : la vigilance renforcée "
                           "(R.561-20-2 3°) se tient à une date, pas à une intention.")
    dossier.statut = STATUT_DECIDE
    dossier.decision = code
    dossier.decision_motif = texte[:_MAX_TEXTE]
    dossier.decision_reference = (reference or "").strip()[:255] or None
    dossier.decidee_le = date_decision
    dossier.decidee_par = username
    dossier.prochaine_revue_le = revue
    from fiskr.kpi import delai_de_decision_ppe_secondes
    from fiskr.gels import _duree
    delai = delai_de_decision_ppe_secondes(dossier.ouvert_le, date_decision)
    _journal(db, dossier, username, "PPE_DECISION",
             f"Dossier PPE #{dossier.id} : {DECISIONS[code]} le "
             f"{date_decision.strftime('%d/%m/%Y')} par @{username}"
             + (f" (réf. {dossier.decision_reference})" if dossier.decision_reference else "")
             + f" — {texte}"
             + (f" — prochaine revue le {revue.strftime('%d/%m/%Y')}" if revue else "")
             + f" — délai depuis la confirmation : {_duree(delai)}.")


# ------------------------------------------------------------ delai

def dossiers_en_retard(db, maintenant: Optional[datetime] = None) -> List[DossierPPE]:
    """Dossiers encore sans decision au-dela du seuil — la definition du
    retard est celle de fiskr/kpi.py, lue ici telle quelle."""
    from fiskr.kpi import decision_ppe_en_retard
    from fiskr.settings import ppe_decision_delai_jours
    now = maintenant or datetime.utcnow()
    seuil = ppe_decision_delai_jours(db)
    candidats = db.query(DossierPPE).filter(
        DossierPPE.statut == STATUT_A_DECIDER,
        DossierPPE.ouvert_le < now - timedelta(days=seuil)).all()
    return [d for d in candidats
            if decision_ppe_en_retard(d.statut, d.ouvert_le, d.decidee_le, seuil, now)]


def detecter_retards(db, maintenant: Optional[datetime] = None) -> int:
    """
    Signale UNE fois chaque dossier sans decision au-dela du seuil : evenement
    immediat `ppe_decision_en_retard` et trace au journal de l'alerte. Rien
    n'est emis pour un dossier dans le delai.
    """
    from fiskr.gels import _duree
    from fiskr.kpi import delai_de_decision_ppe_secondes
    from fiskr.notifier import emit
    from fiskr.settings import ppe_decision_delai_jours
    now = maintenant or datetime.utcnow()
    seuil = ppe_decision_delai_jours(db)
    signales = 0
    for dossier in dossiers_en_retard(db, now):
        if dossier.retard_signale_le is not None:
            continue
        delai = delai_de_decision_ppe_secondes(dossier.ouvert_le, None, now)
        dossier.retard_signale_le = now
        _journal(db, dossier, "systeme", "PPE_DECISION_EN_RETARD",
                 f"Dossier PPE #{dossier.id} sans décision d'entrée ou de maintien en relation "
                 f"{_duree(delai)} après la confirmation (seuil : {seuil} j).")
        db.commit()
        emit(db, "ppe_decision_en_retard", {
            "Dossier": f"#{dossier.id}", "Alerte": dossier.alert_id,
            "Client": dossier.client_name, "Fiche listée": dossier.watchlist_name,
            "Qualité": QUALITES.get(dossier.qualite_retenue or dossier.qualite_source or "", "—"),
            "PPE non déclarée par le SI amont": "oui" if dossier.ppe_non_declaree else "non",
            "Confirmée le": dossier.ouvert_le.strftime("%d/%m/%Y %H:%M") + " UTC",
            "Délai écoulé": _duree(delai), "Seuil": f"{seuil} j",
            "Obligation": ("Décision de nouer ou maintenir la relation par un membre de l'organe "
                           "exécutif ou une personne habilitée (CMF R.561-20-2 1°)."),
        })
        signales += 1
    return signales


def revue_echue(dossier: DossierPPE, maintenant: Optional[datetime] = None) -> bool:
    return (dossier.statut == STATUT_DECIDE and dossier.prochaine_revue_le is not None
            and dossier.prochaine_revue_le <= (maintenant or datetime.utcnow()))


# ------------------------------------------------------------ revue de vigilance renforcee
#
# La revue datee a la decision (R.561-20-2 3°) etait signalee echue a l'ecran,
# et c'est tout : aucune notification, aucun controle. Un dossier dont la revue
# a passe sa date ne se voyait qu'en ouvrant le registre, filtre « revue
# echue » — la vigilance renforcee se tenait a une date que personne n'etait
# prevenu d'atteindre. Et rien ne permettait de la tenir : un dossier decide
# ne se reecrit plus, la revue n'avait aucun geste.

CONCLUSION_MAINTIEN = "MAINTIEN"
CONCLUSION_FIN = "FIN_DE_RELATION"
CONCLUSIONS_DE_REVUE = {
    CONCLUSION_MAINTIEN: "Relation maintenue après revue",
    CONCLUSION_FIN: "Fin de la relation décidée à la revue",
}


def revue_en_retard(dossier: DossierPPE, seuil_jours: float,
                    maintenant: Optional[datetime] = None) -> bool:
    """Une revue echue DEPUIS plus que le delai de decision PPE : la meme
    tolerance que la decision, parce que c'est le meme travail — interroger le
    client, renouveler les pieces de l'origine des fonds, presenter le tout a
    la personne habilitee. Au-dela, la revue n'attend plus une instruction,
    elle attend que quelqu'un s'en saisisse. La seule definition : le
    controle de mise en service, l'indicateur et l'ecran la lisent."""
    now = maintenant or datetime.utcnow()
    return (revue_echue(dossier, now)
            and dossier.prochaine_revue_le < now - timedelta(days=seuil_jours))


def revues_echues(db, maintenant: Optional[datetime] = None) -> List[DossierPPE]:
    """Dossiers decides dont la revue a atteint sa date (meme definition que
    `revue_echue`, en SQL pour la detection de chaque minute)."""
    now = maintenant or datetime.utcnow()
    return db.query(DossierPPE).filter(
        DossierPPE.statut == STATUT_DECIDE, DossierPPE.prochaine_revue_le.isnot(None),
        DossierPPE.prochaine_revue_le <= now).order_by(DossierPPE.prochaine_revue_le.asc()).all()


def revues_en_retard(db, maintenant: Optional[datetime] = None) -> List[DossierPPE]:
    from fiskr.settings import ppe_decision_delai_jours
    now = maintenant or datetime.utcnow()
    seuil = ppe_decision_delai_jours(db)
    return [d for d in revues_echues(db, now) if revue_en_retard(d, seuil, now)]


def detecter_revues_echues(db, maintenant: Optional[datetime] = None) -> int:
    """
    Signale UNE fois chaque revue echue : evenement immediat `ppe_revue_echue`
    et trace au journal de l'alerte. Une fois par ECHEANCE, pas a chaque tic :
    la marque (`revue_signalee_le`) n'est levee que par la revue suivante, qui
    fixe une nouvelle date.
    """
    from fiskr.gels import _duree
    from fiskr.notifier import emit
    now = maintenant or datetime.utcnow()
    signales = 0
    for dossier in revues_echues(db, now):
        if dossier.revue_signalee_le is not None:
            continue
        dossier.revue_signalee_le = now
        echeance = dossier.prochaine_revue_le
        _journal(db, dossier, "systeme", "PPE_REVUE_ECHUE",
                 f"Dossier PPE #{dossier.id} : revue de vigilance renforcée échue depuis le "
                 f"{echeance.strftime('%d/%m/%Y')} ({_duree(int((now - echeance).total_seconds()))}). "
                 f"La relation court sans la revue datée à la décision (CMF R.561-20-2 3°).")
        db.commit()
        emit(db, "ppe_revue_echue", {
            "Dossier": f"#{dossier.id}", "Alerte": dossier.alert_id,
            "Client": dossier.client_name, "Fiche listée": dossier.watchlist_name,
            "Qualité": QUALITES.get(dossier.qualite_retenue or dossier.qualite_source or "", "—"),
            "Décision": DECISIONS.get(dossier.decision or "", "—"),
            "Revue prévue le": echeance.strftime("%d/%m/%Y"),
            "Obligation": ("Vigilance renforcée de la relation avec une personne politiquement "
                           "exposée : revue à tenir et maintien à décider par une personne "
                           "habilitée (CMF R.561-20-2 1° et 3°)."),
        })
        signales += 1
    return signales


def enregistrer_revue(db, dossier: DossierPPE, *, username: str, conclusion: str, motif: str,
                      reference: Optional[str] = None, revue_le: Any = None,
                      prochaine_revue_le: Any = None) -> None:
    """
    La revue de vigilance renforcee d'une relation acceptee. Maintenir la
    relation, c'est la decision de R.561-20-2 1° : memes regles que la
    decision — une personne habilitee (role verifie par l'endpoint) qui n'a pas
    instruit l'alerte, un motif, et la revue suivante datee. Mettre fin a la
    relation n'a pas de revue suivante. La decision d'origine ne se reecrit
    pas : la revue s'ajoute a la suite (`revues`), et leve le signalement de
    l'echeance qu'elle tient.
    """
    if dossier.statut != STATUT_DECIDE or dossier.decision not in DECISIONS_QUI_ACCEPTENT:
        raise RefusPPE("Seule une relation acceptée (entrée ou maintien) se revoit : décidez "
                       "d'abord le dossier.", 409)
    if dossier.prochaine_revue_le is None:
        raise RefusPPE("La relation a pris fin à une revue précédente : il n'y a plus de "
                       "revue à tenir.", 409)
    if username in instruit_par(dossier):
        raise RefusPPE("Le maintien de la relation revient à une personne habilitée qui n'a pas "
                       "instruit l'alerte : vous en avez proposé ou validé la confirmation.", 403)
    code = (conclusion or "").strip().upper()
    if code not in CONCLUSIONS_DE_REVUE:
        raise RefusPPE(f"Conclusion de revue inconnue ({', '.join(CONCLUSIONS_DE_REVUE)}).")
    texte = (motif or "").strip()
    if not texte:
        raise RefusPPE("Le motif de la revue est obligatoire : ce qui a été revu, et sur quelles "
                       "pièces.")
    date_revue = _date_saisie(revue_le, "Date de la revue") or datetime.utcnow()
    suivante = None
    if code == CONCLUSION_MAINTIEN:
        suivante = _date_saisie(prochaine_revue_le, "Prochaine revue", futur=True)
        if suivante is None:
            raise RefusPPE("Datez la prochaine revue : la vigilance renforcée (R.561-20-2 3°) "
                           "se tient à une date, pas à une intention.")
    echeance = dossier.prochaine_revue_le
    dossier.revues = list(dossier.revues or []) + [{
        "le": date_revue.isoformat(), "par": username, "conclusion": code,
        "motif": texte[:_MAX_TEXTE], "reference": (reference or "").strip()[:255] or None,
        "echeance_tenue": echeance.isoformat() if echeance else None,
        "prochaine_revue_le": suivante.isoformat() if suivante else None,
    }]
    dossier.prochaine_revue_le = suivante
    dossier.revue_signalee_le = None
    _journal(db, dossier, username, "PPE_REVUE",
             f"Dossier PPE #{dossier.id} : revue de vigilance renforcée du "
             f"{date_revue.strftime('%d/%m/%Y')} par @{username} — {CONCLUSIONS_DE_REVUE[code]}"
             + (f" (réf. {reference.strip()[:255]})" if (reference or "").strip() else "")
             + f" — {texte}"
             + (f" — prochaine revue le {suivante.strftime('%d/%m/%Y')}" if suivante else "")
             + (f" — échéance tenue : {echeance.strftime('%d/%m/%Y')}." if echeance else "."))


# ------------------------------------------------------------ lecture

def serialiser(db, dossier: DossierPPE, seuil_jours: Optional[int] = None,
               maintenant: Optional[datetime] = None,
               pieces: Optional[Dict[int, AlertAttachment]] = None,
               avec_client_actuel: bool = False) -> Dict[str, Any]:
    from fiskr.kpi import decision_ppe_en_retard, delai_de_decision_ppe_secondes
    from fiskr.preuves import piece_presente
    from fiskr.settings import ppe_decision_delai_jours
    now = maintenant or datetime.utcnow()
    seuil = seuil_jours if seuil_jours is not None else ppe_decision_delai_jours(db)
    ids = list(dossier.pieces_origine or [])
    connues = dict(pieces or {})
    manquantes = [i for i in ids if i not in connues]
    if manquantes:
        connues.update({p.id: p for p in db.query(AlertAttachment).filter(
            AlertAttachment.id.in_(manquantes)).all()})

    def iso(d):
        return d.isoformat() if d else None

    fin = dossier.fin_de_fonction
    corps = {
        "id": dossier.id, "alert_id": dossier.alert_id,
        "statut": dossier.statut, "statut_label": STATUT_LABELS.get(dossier.statut, dossier.statut),
        "client_id": dossier.client_id, "client_name": dossier.client_name,
        "watchlist_entity_id": dossier.watchlist_entity_id,
        "watchlist_name": dossier.watchlist_name, "list_type": dossier.list_type,
        "qualite_source": dossier.qualite_source,
        "qualite_source_label": QUALITES.get(dossier.qualite_source or "", QUALITES[QUALITE_NON_PRECISEE]),
        "fonction_source": dossier.fonction_source,
        "fin_de_fonction_source": dossier.fin_de_fonction_source,
        "liens_source": dossier.liens_source or [],
        "qualite_retenue": dossier.qualite_retenue,
        "qualite_retenue_label": QUALITES.get(dossier.qualite_retenue or ""),
        "fonction": dossier.fonction, "fin_de_fonction": fin,
        # R.561-18 I : « a cesse d'exercer depuis moins d'un an ». Dit, jamais
        # tranche : rien n'est ferme par le produit.
        "exposition_echue": exposition_echue(fin, now),
        "origine_fonds": dossier.origine_fonds, "origine_patrimoine": dossier.origine_patrimoine,
        "pieces_origine": [
            {"id": i, "file_name": connues[i].file_name,
             "file_present": piece_presente(connues[i].file_path)} if i in connues
            else {"id": i, "file_name": None, "file_present": False}
            for i in ids
        ],
        "client_pep_declare": dossier.client_pep_declare,
        "ppe_non_declaree": bool(dossier.ppe_non_declaree),
        "ouvert_le": iso(dossier.ouvert_le), "ouvert_par": dossier.ouvert_par,
        "instruit_par": instruit_par(dossier),
        "decision": dossier.decision, "decision_label": DECISIONS.get(dossier.decision or ""),
        "decision_motif": dossier.decision_motif, "decision_reference": dossier.decision_reference,
        "decidee_le": iso(dossier.decidee_le), "decidee_par": dossier.decidee_par,
        "prochaine_revue_le": iso(dossier.prochaine_revue_le),
        "revue_echue": revue_echue(dossier, now),
        # Echue depuis plus que le delai de decision : le controle de mise en
        # service la compte (`revue_en_retard`, une seule definition)
        "revue_en_retard": revue_en_retard(dossier, seuil, now),
        "revue_signalee_le": iso(dossier.revue_signalee_le),
        "revues": [{**r, "conclusion_label": CONCLUSIONS_DE_REVUE.get(r.get("conclusion"))}
                   for r in (dossier.revues or [])],
        "delai_decision_secondes": delai_de_decision_ppe_secondes(
            dossier.ouvert_le, dossier.decidee_le, now),
        "en_retard": decision_ppe_en_retard(dossier.statut, dossier.ouvert_le,
                                            dossier.decidee_le, seuil, now),
        "seuil_jours": seuil,
    }
    if avec_client_actuel and dossier.client_id:
        # Ce que le SI amont dit AUJOURD'HUI : la correction se constate a
        # l'import suivant du referentiel, le dossier ne la presume pas.
        actuel, constat = _classification_du_client(db, dossier.client_id)
        corps["client_pep_declare_actuel"] = actuel
        corps["client_classification_actuelle"] = constat
    return corps


def vocabulaire() -> Dict[str, Any]:
    """Codes et libelles, pour l'ecran : une seule definition, ici."""
    return {
        "statuts": [{"code": c, "label": STATUT_LABELS[c]} for c in STATUTS],
        "qualites": [{"code": c, "label": QUALITES[c]} for c in QUALITES_RETENUES],
        "decisions": [{"code": c, "label": l, "accepte": c in DECISIONS_QUI_ACCEPTENT}
                      for c, l in DECISIONS.items()],
        "conclusions_de_revue": [{"code": c, "label": l} for c, l in CONCLUSIONS_DE_REVUE.items()],
    }

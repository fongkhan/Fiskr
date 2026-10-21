"""
La declaration d'homonymie a la DG Tresor : l'alerte qu'on ne peut ni lever
ni confirmer.

POURQUOI CE MODULE EXISTE
-------------------------
Une alerte se closait en faux positif ou en vrai positif ; le cas « je ne peux
pas trancher » n'existait pas. Le registre des gels (fiskr/gels.py) l'avait
laisse de cote a dessein. Or les lignes directrices conjointes DGT/ACPR sur le
gel des avoirs du 18 mars 2026 (« LD ») le prevoient en toutes lettres :

  §167 « lorsque les organismes financiers ont effectue toutes les diligences
  necessaires pour lever l'alerte et que cette alerte ne peut toujours pas
  etre levee, ces derniers effectuent dans les plus brefs delais une
  "declaration d'homonymie" a la DG Tresor en faisant etat des elements de
  traitement de l'analyse (copie de pieces d'identite, message SWIFT, etc.)
  et de l'ensemble des recherches effectuees ».

La DG Tresor, « au regard des elements fournis par l'organisme et de ceux
qu'elle detient », a trois reponses (§167), et chacune mene l'alerte a son
etat final par le chemin qui existe deja (`_close_alert`, fiskr/api.py) :
  - HOMONYME : « confirmer sans reserve qu'il ne s'agit pas de la personne ou
    l'entite designee. Dans ce cas, l'alerte est levee » — cloture en faux
    positif, motivee par la reponse, sa piece citee ;
  - MEME_PERSONNE : « confirmer qu'il s'agit de la personne [...], l'organisme
    met alors en oeuvre immediatement la mesure de gel » — confirmation, qui
    ouvre la mesure « a declarer » du registre des gels ;
  - DOUTE_PERSISTANT : « il revient a l'organisme de prendre la decision
    d'executer ou non la transaction au regard des elements dont il dispose
    [...]. Si l'organisme decide de ne pas geler les fonds, il adapte son
    niveau de vigilance [...]. Dans tous les cas, il procede a une declaration
    a Tracfin si les conditions visees a l'article L. 561-15 du CMF sont
    remplies. Il informe la DG Tresor de sa decision. » — un humain decide,
    et la decision prend l'un des deux chemins : geler (confirmation, mesure
    de gel — dont la declaration informe la DG Tresor, §180-182) ou ne pas
    geler (cloture sans suite de gel, motif, vigilance, Tracfin, et la piece
    qui prouve que la DG Tresor a ete informee).
Rien n'est automatique : un reviseur enregistre la reponse, piece a l'appui.
Et « dans tous les cas », l'organisme « formalise et conserve [...] les
echanges avec la DG Tresor » (§168) : chaque echange est une piece jointe de
l'alerte, datee, au registre.

PENDANT LE DOUTE : CE QUE LES LD DISENT, ET CE QU'ELLES NE DISENT PAS
---------------------------------------------------------------------
Une seule mesure est imposee, et sur un FLUX : « En cas d'alerte sur un flux,
les organismes financiers suspendent l'execution de l'operation au profit ou
en provenance d'une personne ou entite qui pourrait etre designee, jusqu'au
traitement complet de l'alerte » (§162). Le traitement n'est complet qu'a la
reponse — le troisieme cas de §167 parle encore de « decider d'executer ou non
la transaction » : elle n'a donc pas ete executee entre-temps. D'ou l'etat
PENDING_DGT : l'alerte reste OUVERTE, rien ne la clot sans reponse
enregistree, et aucun signal de cloture ne contredit le verdict HIT rendu au
SI amont.
Sur une alerte de la base clients, les LD ne prescrivent AUCUN gel
conservatoire : le gel suit l'identite etablie (§166, §180, §182), et la
conduite a tenir pendant le doute est renvoyee a la procedure ecrite de
l'organisme — §87 y range « les mesures a prendre a la suite de l'envoi d'une
declaration d'homonymie a la DG Tresor ou a la reponse de celle-ci ». Fiskr ne
marque donc pas le client « gele » ; il trace les mesures conservatoires que
l'organisme declare avoir prises.

UN REGISTRE FRERE, PAS UNE ACTION DE GEL DE PLUS
------------------------------------------------
La declaration de gel « est effectuee seulement lorsque l'organisme s'est
assure que la personne [...] est bien celle qui est visee par une mesure de
gel. Elle se distingue des echanges qu'un organisme peut avoir avec la DG
Tresor pendant le traitement d'une alerte » (§180). Ranger le doute parmi les
actions de mise en oeuvre (§181) aurait fait de chaque homonyme une ligne du
registre des GELS : compte dans son delai confirmation -> declaration, dans
« vrais positifs sans mesure », dans le KPI `asset_freezes`, inscriptible
« GELE » au referentiel. Et rien n'y correspond : un autre formulaire (classeur
« Formulaire de declaration d'homonymie - DGT.xlsx »), une autre boite
(homonymies-gels@dgtresor.gouv.fr), un autre cycle (une reponse, trois
issues). Ce module est donc un registre FRERE, qui reutilise ce qui est commun :
la piece jointe de l'alerte comme preuve, la saisie des dates et le refus franc
(fiskr/gels.py), le journal de l'alerte, la personne concernee et la fiche en
production des projets, la definition du delai (fiskr/kpi.py), la cloture
unique — et la mesure de gel elle-meme quand la DG Tresor confirme.

DELAIS
------
Preparee mais non transmise au-dela de `homonymie.transmission_delai_heures`
(24 h par defaut) : un DEFAUT de l'organisme — notification immediate, une
fois, et controle de mise en service BLOQUANT. En attente de la reponse :
rien ne sonne. La DG Tresor a son rythme, et un signal qui crierait chaque
semaine sur une attente que l'organisme ne peut pas abreger finirait ignore —
y compris le jour ou il compterait. L'anciennete de l'attente se lit au
registre (indicateur et filtre « attente longue ») ; le formulaire prevoit la
relance (« nouvelle saisine »).
"""
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from fiskr.database import (
    ALERT_CLOSED_STATUSES, ALERT_STATUS_ATTENTE_DGT, Alert, AlertAttachment, AlertEvent,
    DeclarationHomonymie,
)
from fiskr.gels import RefusDeGel, _date_saisie, _duree

logger = logging.getLogger("fiskr.homonymie")

STATUT_A_TRANSMETTRE = "A_TRANSMETTRE"
STATUT_TRANSMISE = "TRANSMISE"
STATUT_A_DECIDER = "A_DECIDER"
STATUT_TERMINEE = "TERMINEE"
STATUT_ABANDONNEE = "ABANDONNEE"
STATUTS = (STATUT_A_TRANSMETTRE, STATUT_TRANSMISE, STATUT_A_DECIDER, STATUT_TERMINEE,
           STATUT_ABANDONNEE)
STATUT_LABELS = {
    STATUT_A_TRANSMETTRE: "À transmettre",
    STATUT_TRANSMISE: "Transmise, réponse attendue",
    STATUT_A_DECIDER: "Doute persistant : décision à prendre",
    STATUT_TERMINEE: "Terminée",
    STATUT_ABANDONNEE: "Abandonnée avant transmission",
}
# Une declaration EN COURS tient son alerte en attente de la DG Tresor
STATUTS_EN_COURS = (STATUT_A_TRANSMETTRE, STATUT_TRANSMISE, STATUT_A_DECIDER)

# Les trois reponses de la DG Tresor (LD §167), dans l'ordre du texte
ISSUE_HOMONYME = "HOMONYME"
ISSUE_MEME_PERSONNE = "MEME_PERSONNE"
ISSUE_DOUTE_PERSISTANT = "DOUTE_PERSISTANT"
# (une chaine par ligne, meme longue : l'ecran les traduit par correspondance
# exacte, et le releve des libelles lit le source tel qu'il est ecrit)
ISSUES = {
    ISSUE_HOMONYME:
        "Homonyme : la DG Trésor confirme sans réserve qu'il ne s'agit pas de la personne désignée (l'alerte est levée)",
    ISSUE_MEME_PERSONNE:
        "Même personne : la DG Trésor confirme qu'il s'agit de la personne désignée (gel immédiat)",
    ISSUE_DOUTE_PERSISTANT:
        "Doute persistant : la DG Trésor ne peut exclure avec certitude qu'il s'agisse de la personne désignée",
}
# Les memes issues, en un mot, pour une colonne de registre
ISSUES_COURTES = {
    ISSUE_HOMONYME: "Homonyme",
    ISSUE_MEME_PERSONNE: "Même personne",
    ISSUE_DOUTE_PERSISTANT: "Doute persistant",
}

# Doute persistant : la decision revient a l'organisme (§167)
DECISION_GELER = "GELER"
DECISION_NE_PAS_GELER = "NE_PAS_GELER"
DECISIONS = {
    DECISION_GELER: "Geler : mettre en œuvre la mesure de gel",
    DECISION_NE_PAS_GELER: "Ne pas geler : vigilance adaptée",
}
# « Dans tous les cas, il procede a une declaration a Tracfin si les
# conditions visees a l'article L. 561-15 du CMF sont remplies » (§167)
TRACFIN = {
    "DECLAREE": "Déclaration de soupçon effectuée (CMF L.561-15)",
    "NON_REQUISE": "Conditions de L.561-15 non remplies",
}

# Rubriques du formulaire de la DG Tresor que l'organisme renseigne, avec les
# valeurs que le classeur propose (onglet « Consignes » et listes deroulantes)
ROLES_OPERATEUR = {
    "RELATION_AFFAIRES": "Relation d'affaires",
    "CONTREPARTIE": "Contrepartie de la relation d'affaires (donneur d'ordre, bénéficiaire final…)",
    "AUTRE": "Autre (à préciser en commentaire)",
}
OBJETS = {
    "NOM": "Nom et prénom (personne physique) ou dénomination sociale (personne morale)",
    "ALIAS": "Alias",
}
SAISINES = {
    "INITIALE": "Saisine initiale",
    "NOUVELLE": "Nouvelle saisine (cas déjà déclaré dont le doute n'a pu être levé)",
}
# « Sur quelle operation porte l'alerte ? » — les rubriques du formulaire,
# dans son ordre ; texte libre, tel que le message ou le dossier les porte
CHAMPS_OPERATION = {
    "type": "Type d'opération",
    "objet": "Objet de la transaction",
    "emetteur": "Identité de l'émetteur",
    "beneficiaire": "Identité du bénéficiaire",
    "montant": "Montant de l'opération",
    "date": "Date de la transaction",
}
SENS_D_ECHANGE = {"RECU": "Reçu de la DG Trésor", "ENVOYE": "Envoyé à la DG Trésor"}

# Ou et comment la DG Tresor recoit la declaration — page « Sanctions
# economiques », rubrique « Vous voulez signaler un cas d'homonymie non
# resolu » (consultee le 20 octobre 2026). Le classeur s'envoie « en format
# .xlsx » ; Fiskr n'ecrit pas ce classeur, il en prepare le contenu.
ADRESSE_DGT = "homonymies-gels@dgtresor.gouv.fr"
FORMULAIRE = {
    "titre": "Formulaire de déclaration d'homonymie - DGT.xlsx",
    "url": ("https://www.tresor.economie.gouv.fr/Institutionnel/Niveau2/Pages/"
            "f3234489-26a1-48f7-8a05-f31d34551f13/files/9c7bb937-b538-42ad-9117-fc070ad737ca"),
    "page": "https://www.tresor.economie.gouv.fr/services-aux-entreprises/sanctions-economiques",
}

# Filtres du registre qui ne sont pas des statuts
FILTRE_NON_TRANSMISE_HORS_DELAI = "NON_TRANSMISE_HORS_DELAI"
FILTRE_ATTENTE_LONGUE = "ATTENTE_LONGUE"
# Au-dela, l'attente de la reponse est « longue » : un indicateur et un filtre,
# jamais une alarme (cf. en-tete). Les LD ne fixent aucun delai de reponse a
# la DG Tresor ; un mois est le moment ou une relance (« nouvelle saisine »)
# merite d'etre envisagee. Constante, pas reglage : elle ne declenche rien.
ATTENTE_LONGUE_JOURS = 30

_MAX_TEXTE = 8000
_MAX_ECHANGES = 100


class RefusDHomonymie(RefusDeGel):
    """Refus franc d'une operation du registre (meme contrat que les gels :
    message et code HTTP — l'API les rend de la meme facon)."""


# ------------------------------------------------------------ lecture d'etat

def en_cours(db, alert_id: int) -> Optional[DeclarationHomonymie]:
    return db.query(DeclarationHomonymie).filter(
        DeclarationHomonymie.alert_id == alert_id,
        DeclarationHomonymie.statut.in_(STATUTS_EN_COURS)).order_by(
        DeclarationHomonymie.id.desc()).first()


def refus_en_attente(alert: Alert) -> Optional[str]:
    """Une alerte en attente de la DG Tresor ne se propose, ne s'escalade et ne
    se clot pas : seule la reponse enregistree l'en fait sortir. Rend le
    motif du refus, ou None."""
    if alert.status != ALERT_STATUS_ATTENTE_DGT:
        return None
    return ("Alerte en attente de la DG Trésor (doute d'homonymie déclaré, LD DGT/ACPR §167) : "
            "elle ne se clôt qu'à l'enregistrement de sa réponse, depuis la déclaration "
            "d'homonymie.")


def motif_d_impossibilite(alert: Alert, surcharges: Optional[Dict[str, str]] = None) -> Optional[str]:
    """Pourquoi CETTE alerte ne peut pas etre declaree en homonymie, ou None.

    Le perimetre vient de fiskr/perimeters.py, surcharges comprises : une liste
    hors SANCTION n'impose aucun gel, donc rien a demander a la DG Tresor."""
    from fiskr.perimeters import est_sanction
    if alert.status in ALERT_CLOSED_STATUSES:
        return "Alerte déjà close : le doute se déclare pendant l'instruction."
    if alert.status == ALERT_STATUS_ATTENTE_DGT:
        return "Un doute d'homonymie est déjà déclaré sur cette alerte."
    if alert.status == "PENDING_VALIDATION":
        return ("Une décision est en attente de validation : faites-la renvoyer en analyse "
                "avant de déclarer un doute.")
    if not est_sanction(alert.list_type, surcharges):
        return ("Cette liste est hors du périmètre SANCTION : elle n'impose aucun gel, donc "
                "aucune déclaration d'homonymie à la DG Trésor.")
    return None


def attente_longue(statut: Optional[str], transmise_le: Optional[datetime],
                   maintenant: Optional[datetime] = None) -> bool:
    """Transmise, sans reponse, depuis plus de ATTENTE_LONGUE_JOURS : a voir,
    pas a sonner."""
    if statut != STATUT_TRANSMISE or transmise_le is None:
        return False
    return (maintenant or datetime.utcnow()) - transmise_le > timedelta(days=ATTENTE_LONGUE_JOURS)


def refus_de_liste_blanche(db, client_id: Optional[str], entity_id: Optional[str]) -> Optional[str]:
    """
    Une liste blanche ne vaut que pour une personne « dont l'analyse a etabli
    qu'il ne s'agissait pas de la personne [...] designee » (LD §169). Un
    couple que la DG Tresor examine encore, sur lequel elle n'a pu exclure le
    doute, ou qu'elle a confirme, ne l'est pas : le memoriser ferait taire les
    criblages suivants sur une personne peut-etre designee. Rend le motif du
    refus, ou None. Apres une reponse « homonyme », la paire est legitime.
    """
    if not client_id or not entity_id:
        return None
    lignes = db.query(DeclarationHomonymie).filter(
        DeclarationHomonymie.client_id == client_id,
        DeclarationHomonymie.watchlist_entity_id == entity_id,
        DeclarationHomonymie.statut != STATUT_ABANDONNEE).order_by(
        DeclarationHomonymie.id.desc()).all()
    for d in lignes:
        if d.statut in STATUTS_EN_COURS:
            return (f"Déclaration d'homonymie #{d.id} en cours auprès de la DG Trésor : ce couple "
                    f"n'est pas établi homonyme, il ne se met pas en liste blanche (LD §169).")
        if d.issue == ISSUE_DOUTE_PERSISTANT:
            return (f"Doute persistant après la réponse de la DG Trésor (déclaration #{d.id}) : ce "
                    f"couple n'est pas établi homonyme, il ne se met pas en liste blanche (LD §169).")
        if d.issue == ISSUE_MEME_PERSONNE:
            return (f"La DG Trésor a confirmé qu'il s'agit de la personne désignée (déclaration "
                    f"#{d.id}) : ce couple ne se met pas en liste blanche.")
        if d.issue == ISSUE_HOMONYME:
            return None
    return None


# ------------------------------------------------------------ saisie

def _texte(valeur: Optional[str], limite: int = _MAX_TEXTE) -> Optional[str]:
    return (valeur or "").strip()[:limite] or None


def _code(valeur: Optional[str], table: Dict[str, str], champ: str) -> Optional[str]:
    code = (valeur or "").strip().upper()
    if not code:
        return None
    if code not in table:
        raise RefusDHomonymie(f"Valeur inconnue pour « {champ} » ({', '.join(table)}).")
    return code


def _anterieure(date: datetime, reference: Optional[datetime]) -> bool:
    """La date saisie precede-t-elle la reference ? A la minute pres : l'ecran
    saisit a la minute (champ datetime-local), et une reponse enregistree dans
    la minute de la transmission se lisait « anterieure a la transmission » —
    constate en navigateur, refus d'une reponse legitime."""
    return reference is not None and date < reference.replace(second=0, microsecond=0)


def _journal(db, declaration: DeclarationHomonymie, username: str, action: str, detail: str) -> None:
    db.add(AlertEvent(alert_id=declaration.alert_id, username=username, action=action,
                      detail=detail))


def _mesure_pendant_le_doute(alert: Alert, declarees: Optional[str]) -> str:
    """Ce que les LD imposent pendant le doute — dit au journal, pour que le
    dossier porte la regle appliquee et pas seulement l'etat."""
    mesures = (declarees or "").strip().rstrip(".") or None
    if (alert.channel or "SCREENING") == "FILTERING":
        return ("opération suspendue jusqu'au traitement complet de l'alerte, réponse de la DG "
                "Trésor comprise (LD §162) : l'alerte reste ouverte, aucun signal de clôture n'est "
                "émis." + (f" Mesures déclarées : {mesures}." if mesures else ""))
    return ("les LD n'imposent pas de gel conservatoire sur une alerte de la base (le gel suit "
            "l'identité établie, §166, §180) ; la conduite à tenir relève de la procédure de "
            "l'organisme (§87). Mesures déclarées : " + (mesures or "aucune") + ".")


def declarer(db, alert: Alert, *, username: str, diligences: Optional[str],
             mesures_conservatoires: Optional[str] = None, commentaire: Optional[str] = None,
             role_operateur: Optional[str] = None, objet_homonymie: Optional[str] = None,
             operation: Optional[Dict[str, Any]] = None) -> DeclarationHomonymie:
    """
    Le doute : l'alerte passe en attente de la DG Tresor, la declaration
    s'ouvre « a transmettre ». Les diligences sont exigees — §167 ne connait
    le doute qu'« apres toutes les diligences necessaires », et le formulaire
    demande leur bilan. L'appelant valide la transaction.
    """
    from fiskr.settings import perimeter_overrides
    motif = motif_d_impossibilite(alert, perimeter_overrides(db))
    if motif:
        raise RefusDHomonymie(motif, 409)
    texte = _texte(diligences)
    if not texte:
        raise RefusDHomonymie("Le bilan des recherches effectuées est obligatoire : la DG Trésor "
                              "le demande, et le doute ne se déclare qu'après toutes les "
                              "diligences (LD §167).")
    # Pas de valeur devinee : un criblage manuel vise aussi bien un prospect
    # qu'un client, et le projet nomme ce qui reste a renseigner
    role = _code(role_operateur, ROLES_OPERATEUR, "Rôle de l'opérateur")
    objet = _code(objet_homonymie, OBJETS, "Objet de l'homonymie")
    champs = {k: str((operation or {}).get(k) or "").strip()[:500] for k in CHAMPS_OPERATION}
    champs = {k: v for k, v in champs.items() if v}
    # Une declaration anterieure sur le meme couple fait de celle-ci une
    # relance (« nouvelle saisine ») : derive du registre, pas demande.
    anterieure = db.query(DeclarationHomonymie.id).filter(
        DeclarationHomonymie.client_id == alert.client_id,
        DeclarationHomonymie.watchlist_entity_id == alert.watchlist_entity_id,
        DeclarationHomonymie.statut != STATUT_ABANDONNEE).first() if alert.client_id else None
    mesures = _texte(mesures_conservatoires, 4000)
    declaration = DeclarationHomonymie(
        alert_id=alert.id, statut=STATUT_A_TRANSMETTRE,
        saisine="NOUVELLE" if anterieure else "INITIALE",
        client_id=alert.client_id, client_name=alert.client_name,
        watchlist_entity_id=alert.watchlist_entity_id, watchlist_name=alert.watchlist_name,
        list_type=alert.list_type, channel=alert.channel or "SCREENING",
        statut_alerte_avant=alert.status,
        preparee_le=datetime.utcnow(), preparee_par=username,
        diligences=texte, mesures_conservatoires=mesures,
        commentaire=_texte(commentaire, 4000), role_operateur=role, objet_homonymie=objet,
        operation=champs or None,
    )
    db.add(declaration)
    db.flush()
    alert.status = ALERT_STATUS_ATTENTE_DGT
    # Le travail de l'analyste est fait : un report encore actif ne veut plus
    # rien dire et fausserait le classement de la file
    alert.snoozed_until = None
    from fiskr.settings import homonymie_transmission_delai_heures
    _journal(db, declaration, username, "HOMONYMIE_DECLAREE",
             f"Doute d'homonymie : déclaration #{declaration.id} "
             f"({SAISINES[declaration.saisine].lower()}) à transmettre à la DG Trésor dans les "
             f"plus brefs délais (seuil {homonymie_transmission_delai_heures(db)} h) — LD DGT/ACPR "
             f"§167. L'alerte ne se clôt qu'à sa réponse. Pendant le doute : "
             + _mesure_pendant_le_doute(alert, mesures))
    return declaration


def verifier_transmission(db, declaration: DeclarationHomonymie, *,
                          transmise_le: Any = None) -> Dict[str, Any]:
    """Tout ce qui peut refuser la transmission, AVANT que la piece ne soit
    ecrite sur le disque (meme ordre que la declaration de gel)."""
    if declaration.statut != STATUT_A_TRANSMETTRE:
        raise RefusDHomonymie(f"Déclaration {STATUT_LABELS[declaration.statut].lower()} : sa "
                              f"transmission n'est plus à enregistrer.", 409)
    date = _date_saisie(transmise_le, "Date de transmission") or datetime.utcnow()
    creee = db.query(Alert.created_at).filter(Alert.id == declaration.alert_id).scalar()
    if _anterieure(date, creee):
        raise RefusDHomonymie("Date de transmission antérieure à l'alerte elle-même.")
    return {"transmise_le": date}


def enregistrer_transmission(db, declaration: DeclarationHomonymie, *, username: str,
                             piece: Optional[AlertAttachment], verifiee: Dict[str, Any],
                             reference: Optional[str] = None) -> None:
    """
    La transmission, faite par un humain hors de Fiskr (le classeur .xlsx de la
    DG Tresor, par courriel), et la piece qui la prouve : copie du courriel
    envoye, ou accuse de reception. Exigee : sans elle, « transmise »
    affirmerait un envoi que le produit n'a pas constate.
    """
    if piece is None:
        raise RefusDHomonymie("La pièce de transmission est obligatoire (copie du courriel envoyé "
                              "à la DG Trésor avec le formulaire, ou accusé de réception).")
    from fiskr.kpi import delai_de_declaration_secondes
    date = verifiee["transmise_le"]
    declaration.statut = STATUT_TRANSMISE
    declaration.transmise_le = date
    declaration.transmise_par = username
    declaration.reference = _texte(reference, 255)
    declaration.piece_transmission_id = piece.id
    _journal(db, declaration, username, "HOMONYMIE_TRANSMISE",
             f"Déclaration d'homonymie #{declaration.id} transmise à la DG Trésor le "
             f"{date.strftime('%d/%m/%Y %H:%M')} UTC"
             + (f", réf. {declaration.reference}" if declaration.reference else "")
             + f" — pièce : {piece.file_name} — délai depuis le doute : "
             + _duree(delai_de_declaration_secondes(declaration.preparee_le, date))
             + ". En attente de la réponse de la DG Trésor.")


def verifier_echange(declaration: DeclarationHomonymie, *, sens: Optional[str],
                     objet: Optional[str], le: Any = None) -> Dict[str, Any]:
    if declaration.statut not in (STATUT_TRANSMISE, STATUT_A_DECIDER):
        raise RefusDHomonymie("Un échange se consigne entre la transmission et la clôture de la "
                              "déclaration.", 409)
    code = _code(sens, SENS_D_ECHANGE, "Sens de l'échange")
    if code is None:
        raise RefusDHomonymie(f"Sens de l'échange obligatoire ({', '.join(SENS_D_ECHANGE)}).")
    texte = _texte(objet, 1000)
    if not texte:
        raise RefusDHomonymie("L'objet de l'échange est obligatoire (demande de la DG Trésor, "
                              "pièce complémentaire envoyée, relance…).")
    date = _date_saisie(le, "Date de l'échange") or datetime.utcnow()
    if _anterieure(date, declaration.transmise_le):
        raise RefusDHomonymie("Échange antérieur à la transmission de la déclaration.")
    if len(declaration.echanges or []) >= _MAX_ECHANGES:
        raise RefusDHomonymie(f"Au plus {_MAX_ECHANGES} échanges par déclaration.")
    return {"sens": code, "objet": texte, "le": date}


def consigner_echange(db, declaration: DeclarationHomonymie, *, username: str,
                      piece: Optional[AlertAttachment], verifie: Dict[str, Any]) -> None:
    """Un echange avec la DG Tresor pendant l'attente (demande de complement,
    relance) : conserve, date, appuye de sa piece (§168)."""
    if piece is None:
        raise RefusDHomonymie("La pièce de l'échange est obligatoire (le courriel reçu ou envoyé) : "
                              "un échange sans pièce ne se conserve pas (LD §168).")
    # Liste reaffectee, pas modifiee en place : la colonne JSON ne suit pas
    # les mutations
    declaration.echanges = list(declaration.echanges or []) + [{
        "le": verifie["le"].isoformat(), "sens": verifie["sens"], "objet": verifie["objet"],
        "piece_id": piece.id, "par": username,
    }]
    _journal(db, declaration, username, "HOMONYMIE_ECHANGE",
             f"Déclaration d'homonymie #{declaration.id} — {SENS_D_ECHANGE[verifie['sens']].lower()} "
             f"le {verifie['le'].strftime('%d/%m/%Y %H:%M')} UTC : {verifie['objet']} "
             f"— pièce : {piece.file_name}.")


def verifier_reponse(db, declaration: DeclarationHomonymie, *, issue: Optional[str],
                     recue_le: Any = None) -> Dict[str, Any]:
    if declaration.statut != STATUT_TRANSMISE:
        if declaration.statut == STATUT_A_TRANSMETTRE:
            raise RefusDHomonymie("La déclaration n'a pas été transmise : enregistrez d'abord sa "
                                  "transmission, avec sa pièce.", 409)
        raise RefusDHomonymie("La réponse de la DG Trésor est déjà enregistrée.", 409)
    code = _code(issue, ISSUES, "Issue")
    if code is None:
        raise RefusDHomonymie(f"L'issue de la réponse est obligatoire ({', '.join(ISSUES)}).")
    date = _date_saisie(recue_le, "Date de la réponse") or datetime.utcnow()
    if _anterieure(date, declaration.transmise_le):
        raise RefusDHomonymie("Réponse antérieure à la transmission de la déclaration.")
    return {"issue": code, "recue_le": date}


def enregistrer_reponse(db, declaration: DeclarationHomonymie, *, username: str,
                        piece: Optional[AlertAttachment], verifiee: Dict[str, Any],
                        resume: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """
    La reponse de la DG Tresor, piece a l'appui. Rend la cloture que la
    reponse commande — (decision, motivation) pour `_close_alert` — ou None
    sur un doute persistant, ou la decision revient a l'organisme
    (`verifier_decision`). L'appelant clot, puis appelle `terminer`.
    """
    if piece is None:
        raise RefusDHomonymie("La pièce de la réponse est obligatoire (courriel de la DG Trésor) : "
                              "sans elle, l'alerte serait close sur une réponse que personne n'a "
                              "vue (LD §168).")
    issue, date = verifiee["issue"], verifiee["recue_le"]
    declaration.issue = issue
    declaration.reponse_le = date
    declaration.reponse_par = username
    declaration.reponse_resume = _texte(resume, 4000)
    declaration.piece_reponse_id = piece.id
    from fiskr.kpi import delai_de_declaration_secondes
    _journal(db, declaration, username, "HOMONYMIE_REPONSE",
             f"Réponse de la DG Trésor à la déclaration d'homonymie #{declaration.id}, reçue le "
             f"{date.strftime('%d/%m/%Y %H:%M')} UTC : {ISSUES[issue]}"
             + (f" — {declaration.reponse_resume}" if declaration.reponse_resume else "")
             + f" — pièce : {piece.file_name} — attente : "
             + _duree(delai_de_declaration_secondes(declaration.transmise_le, date)) + ".")
    entete = (f"Réponse de la DG Trésor du {date.strftime('%d/%m/%Y')} à la déclaration "
              f"d'homonymie #{declaration.id} (pièce : {piece.file_name})")
    precision = f" {declaration.reponse_resume}" if declaration.reponse_resume else ""
    if issue == ISSUE_HOMONYME:
        return ("FALSE_POSITIVE", f"{entete} : elle confirme sans réserve qu'il ne s'agit pas de "
                                  f"la personne désignée — l'alerte est levée (LD DGT/ACPR §167)."
                                  f"{precision}")
    if issue == ISSUE_MEME_PERSONNE:
        return ("CONFIRMED", f"{entete} : elle confirme qu'il s'agit de la personne désignée — "
                             f"mise en œuvre immédiate du gel (LD DGT/ACPR §167, CMF L.562-4)."
                             f"{precision}")
    declaration.statut = STATUT_A_DECIDER
    return None


def verifier_decision(declaration: DeclarationHomonymie, *, decision: Optional[str],
                      motif: Optional[str], vigilance: Optional[str], tracfin: Optional[str],
                      tracfin_detail: Optional[str], piece_fournie: bool) -> Dict[str, Any]:
    """
    Doute persistant : ce que §167 attend de l'organisme, verifie AVANT
    d'ecrire la piece. Un motif (« au regard des elements dont il dispose »),
    la position Tracfin (« dans tous les cas »), et, s'il ne gele pas, la
    vigilance adaptee et la piece qui prouve que la DG Tresor est informee —
    s'il gele, c'est la declaration de la mesure de gel qui l'informe
    (§180-182), avec sa propre piece et son propre delai.
    """
    if declaration.statut != STATUT_A_DECIDER:
        raise RefusDHomonymie("Aucune décision à prendre : elle suit une réponse « doute "
                              "persistant » de la DG Trésor.", 409)
    code = _code(decision, DECISIONS, "Décision")
    if code is None:
        raise RefusDHomonymie(f"La décision est obligatoire ({', '.join(DECISIONS)}).")
    texte = _texte(motif, 4000)
    if not texte:
        raise RefusDHomonymie("Le motif de la décision est obligatoire : elle se prend « au regard "
                              "des éléments dont [l'organisme] dispose » (LD §167).")
    position = _code(tracfin, TRACFIN, "Position Tracfin")
    detail = _texte(tracfin_detail, 1000)
    if position is None or not detail:
        raise RefusDHomonymie("La position Tracfin est obligatoire, avec sa référence ou sa "
                              "justification : « dans tous les cas », déclaration de soupçon si "
                              "L.561-15 l'exige (LD §167).")
    vigilance_texte = _texte(vigilance, 4000)
    if code == DECISION_NE_PAS_GELER:
        if not vigilance_texte:
            raise RefusDHomonymie("Sans gel, la vigilance adaptée est obligatoire : « il adapte son "
                                  "niveau de vigilance et le cas échéant, réévalue le profil de sa "
                                  "relation d'affaires » (LD §167).")
        if not piece_fournie:
            raise RefusDHomonymie("La pièce qui prouve que la DG Trésor est informée de la décision "
                                  "est obligatoire (LD §167 : « il informe la DG Trésor de sa "
                                  "décision »).")
    return {"decision": code, "motif": texte, "tracfin": position, "tracfin_detail": detail,
            "vigilance": vigilance_texte}


def enregistrer_decision(db, declaration: DeclarationHomonymie, *, username: str,
                         verifiee: Dict[str, Any],
                         piece: Optional[AlertAttachment] = None) -> Tuple[str, str]:
    """Ecrit la decision de l'organisme et rend la cloture qu'elle commande
    (decision, motivation) pour `_close_alert`."""
    code = verifiee["decision"]
    declaration.decision = code
    declaration.decision_motif = verifiee["motif"]
    declaration.vigilance = verifiee["vigilance"]
    declaration.tracfin = verifiee["tracfin"]
    declaration.tracfin_detail = verifiee["tracfin_detail"]
    declaration.piece_information_id = piece.id if piece is not None else None
    declaration.decidee_le = datetime.utcnow()
    declaration.decidee_par = username
    tracfin = f"Tracfin : {TRACFIN[verifiee['tracfin']]} — {verifiee['tracfin_detail']}."
    information = (f" DG Trésor informée de la décision — pièce : {piece.file_name}."
                   if piece is not None else "")
    _journal(db, declaration, username, "HOMONYMIE_DECISION",
             f"Déclaration d'homonymie #{declaration.id}, doute persistant : {DECISIONS[code]} — "
             f"{verifiee['motif']}"
             + (f" Vigilance : {verifiee['vigilance']}." if verifiee["vigilance"] else "")
             + f" {tracfin}{information}")
    entete = (f"Doute persistant après la réponse de la DG Trésor du "
              f"{declaration.reponse_le.strftime('%d/%m/%Y')} (déclaration d'homonymie "
              f"#{declaration.id}) : décision de l'organisme (LD DGT/ACPR §167)")
    if code == DECISION_GELER:
        return ("CONFIRMED", f"{entete} de geler — {verifiee['motif']} {tracfin} La DG Trésor "
                             f"est informée par la déclaration de la mesure de gel (§180-182).")
    return ("FALSE_POSITIVE", f"{entete} de ne pas geler — {verifiee['motif']} Vigilance adaptée : "
                              f"{verifiee['vigilance']} {tracfin}{information} Le couple n'est pas "
                              f"établi homonyme : pas de liste blanche (LD §169).")


def terminer(db, declaration: DeclarationHomonymie, *, username: str,
             mesure_id: Optional[int] = None) -> None:
    """L'alerte vient d'etre close par le chemin existant : la declaration se
    termine, et le journal dit ce que la cloture a ouvert."""
    declaration.statut = STATUT_TERMINEE
    declaration.terminee_le = datetime.utcnow()
    _journal(db, declaration, username, "HOMONYMIE_TERMINEE",
             f"Déclaration d'homonymie #{declaration.id} terminée : alerte close"
             + (f", mesure de gel #{mesure_id} ouverte — déclaration à la DG Trésor due "
                f"immédiatement (CMF L.562-4, R.562-3)." if mesure_id else "."))


def abandonner(db, declaration: DeclarationHomonymie, alert: Alert, *, username: str,
               motif: Optional[str]) -> None:
    """
    Renoncer a une declaration AVANT sa transmission (doute declare par
    erreur, element nouveau qui leve l'alerte) : l'alerte retourne a
    l'instruction, la ligne reste au registre avec son motif. Une declaration
    TRANSMISE ne s'abandonne pas : la DG Tresor est saisie, seule sa reponse
    la termine — sinon l'abandon deviendrait le moyen d'eteindre l'alarme de
    transmission ou de clore sans reponse.
    """
    if declaration.statut != STATUT_A_TRANSMETTRE:
        raise RefusDHomonymie("Seule une déclaration non transmise s'abandonne : la DG Trésor "
                              "saisie, seule sa réponse termine la déclaration.", 409)
    texte = _texte(motif, 4000)
    if not texte:
        raise RefusDHomonymie("Le motif de l'abandon est obligatoire.")
    declaration.statut = STATUT_ABANDONNEE
    declaration.abandonnee_le = datetime.utcnow()
    declaration.abandonnee_par = username
    declaration.motif_abandon = texte
    avant = declaration.statut_alerte_avant
    alert.status = avant if avant in ("OPEN", "IN_PROGRESS", "ESCALATED") else "IN_PROGRESS"
    _journal(db, declaration, username, "HOMONYMIE_ABANDONNEE",
             f"Déclaration d'homonymie #{declaration.id} abandonnée avant transmission : {texte} "
             f"L'alerte retourne à l'instruction.")


# ------------------------------------------------------------ delai

def non_transmises_en_retard(db, maintenant: Optional[datetime] = None) -> List[DeclarationHomonymie]:
    """Declarations encore a transmettre au-dela du seuil — la definition est
    celle de fiskr/kpi.py, lue ici telle quelle."""
    from fiskr.kpi import homonymie_non_transmise_en_retard
    from fiskr.settings import homonymie_transmission_delai_heures
    now = maintenant or datetime.utcnow()
    seuil = homonymie_transmission_delai_heures(db)
    candidates = db.query(DeclarationHomonymie).filter(
        DeclarationHomonymie.statut == STATUT_A_TRANSMETTRE,
        DeclarationHomonymie.preparee_le < now - timedelta(hours=seuil)).all()
    return [d for d in candidates
            if homonymie_non_transmise_en_retard(d.statut, d.preparee_le, d.transmise_le, seuil, now)]


def detecter_retards(db, maintenant: Optional[datetime] = None) -> int:
    """
    Signale UNE fois chaque declaration preparee et non transmise au-dela du
    seuil : evenement immediat `homonymie_non_transmise` et trace au journal
    de l'alerte. Rien n'est emis pour une declaration dans le delai, ni pour
    une attente de reponse, si longue soit-elle.
    """
    from fiskr.kpi import delai_de_declaration_secondes
    from fiskr.notifier import emit
    from fiskr.settings import homonymie_transmission_delai_heures
    now = maintenant or datetime.utcnow()
    seuil = homonymie_transmission_delai_heures(db)
    signalees = 0
    for d in non_transmises_en_retard(db, now):
        if d.retard_signale_le is not None:
            continue
        delai = delai_de_declaration_secondes(d.preparee_le, None, now)
        d.retard_signale_le = now
        _journal(db, d, "systeme", "HOMONYMIE_NON_TRANSMISE",
                 f"Déclaration d'homonymie #{d.id} non transmise à la DG Trésor {_duree(delai)} "
                 f"après le doute (seuil : {seuil} h).")
        db.commit()
        emit(db, "homonymie_non_transmise", {
            "Déclaration": f"#{d.id}", "Alerte": d.alert_id,
            "Client": d.client_name, "Fiche listée": d.watchlist_name,
            "Liste": d.list_type or "—",
            "Doute déclaré le": d.preparee_le.strftime("%d/%m/%Y %H:%M") + " UTC",
            "Délai écoulé": _duree(delai), "Seuil": f"{seuil} h",
            "Obligation": ("Déclaration d'homonymie « dans les plus brefs délais » à la DG Trésor "
                           f"({ADRESSE_DGT}) — LD DGT/ACPR §167."),
        })
        signalees += 1
    return signalees


# ------------------------------------------------------------ lecture

def _piece(db, piece_id: Optional[int], pieces: Optional[Dict[int, AlertAttachment]]):
    from fiskr.preuves import piece_presente
    if not piece_id:
        return None
    piece = (pieces or {}).get(piece_id) or db.query(AlertAttachment).filter(
        AlertAttachment.id == piece_id).first()
    if piece is None:
        return {"id": piece_id, "file_name": None, "file_present": False}
    return {"id": piece.id, "file_name": piece.file_name,
            "file_present": piece_presente(piece.file_path)}


def serialiser(db, d: DeclarationHomonymie, seuil_heures: Optional[int] = None,
               maintenant: Optional[datetime] = None,
               pieces: Optional[Dict[int, AlertAttachment]] = None) -> Dict[str, Any]:
    from fiskr.kpi import delai_de_declaration_secondes, homonymie_non_transmise_en_retard
    from fiskr.settings import homonymie_transmission_delai_heures
    now = maintenant or datetime.utcnow()
    seuil = seuil_heures if seuil_heures is not None else homonymie_transmission_delai_heures(db)

    def iso(x):
        return x.isoformat() if x else None

    return {
        "id": d.id, "alert_id": d.alert_id,
        "statut": d.statut, "statut_label": STATUT_LABELS.get(d.statut, d.statut),
        "saisine": d.saisine, "saisine_label": SAISINES.get(d.saisine, d.saisine),
        "client_id": d.client_id, "client_name": d.client_name,
        "watchlist_entity_id": d.watchlist_entity_id, "watchlist_name": d.watchlist_name,
        "list_type": d.list_type, "channel": d.channel,
        "preparee_le": iso(d.preparee_le), "preparee_par": d.preparee_par,
        "diligences": d.diligences, "mesures_conservatoires": d.mesures_conservatoires,
        "commentaire": d.commentaire,
        "role_operateur": d.role_operateur,
        "role_operateur_label": ROLES_OPERATEUR.get(d.role_operateur or ""),
        "objet_homonymie": d.objet_homonymie,
        "objet_homonymie_label": OBJETS.get(d.objet_homonymie or ""),
        "operation": d.operation or {},
        "transmise_le": iso(d.transmise_le), "transmise_par": d.transmise_par,
        "reference": d.reference,
        "piece_transmission": _piece(db, d.piece_transmission_id, pieces),
        "delai_transmission_secondes": delai_de_declaration_secondes(
            d.preparee_le, d.transmise_le, now),
        "non_transmise_en_retard": homonymie_non_transmise_en_retard(
            d.statut, d.preparee_le, d.transmise_le, seuil, now),
        "seuil_heures": seuil,
        # Attente de la reponse : un indicateur, jamais une alarme
        "attente_secondes": (delai_de_declaration_secondes(d.transmise_le, d.reponse_le, now)
                             if d.transmise_le else None),
        "attente_longue": attente_longue(d.statut, d.transmise_le, now),
        "attente_longue_jours": ATTENTE_LONGUE_JOURS,
        "echanges": [{**e, "sens_label": SENS_D_ECHANGE.get(e.get("sens"), e.get("sens")),
                      "piece": _piece(db, e.get("piece_id"), pieces)}
                     for e in (d.echanges or [])],
        "reponse_le": iso(d.reponse_le), "reponse_par": d.reponse_par,
        "issue": d.issue, "issue_label": ISSUES.get(d.issue or ""),
        "issue_court": ISSUES_COURTES.get(d.issue or ""),
        "reponse_resume": d.reponse_resume,
        "piece_reponse": _piece(db, d.piece_reponse_id, pieces),
        "decision": d.decision, "decision_label": DECISIONS.get(d.decision or ""),
        "decision_motif": d.decision_motif, "vigilance": d.vigilance,
        "tracfin": d.tracfin, "tracfin_label": TRACFIN.get(d.tracfin or ""),
        "tracfin_detail": d.tracfin_detail,
        "piece_information": _piece(db, d.piece_information_id, pieces),
        "decidee_le": iso(d.decidee_le), "decidee_par": d.decidee_par,
        "terminee_le": iso(d.terminee_le),
        "abandonnee_le": iso(d.abandonnee_le), "abandonnee_par": d.abandonnee_par,
        "motif_abandon": d.motif_abandon,
    }


def pieces_de(declarations: List[DeclarationHomonymie]) -> List[int]:
    """Identifiants des pieces que ces declarations citent (une requete pour
    toute une page du registre)."""
    ids = []
    for d in declarations:
        ids += [d.piece_transmission_id, d.piece_reponse_id, d.piece_information_id]
        ids += [e.get("piece_id") for e in (d.echanges or [])]
    return sorted({i for i in ids if i})


def vocabulaire() -> Dict[str, Any]:
    """Codes et libelles, pour l'ecran : une seule definition, ici."""
    def liste(table):
        return [{"code": c, "label": l} for c, l in table.items()]
    return {
        "statuts": liste(STATUT_LABELS), "issues": liste(ISSUES), "decisions": liste(DECISIONS),
        "tracfin": liste(TRACFIN), "roles_operateur": liste(ROLES_OPERATEUR),
        "objets": liste(OBJETS), "sens": liste(SENS_D_ECHANGE),
        "champs_operation": liste(CHAMPS_OPERATION),
        "adresse_dgt": ADRESSE_DGT, "formulaire": FORMULAIRE,
        "attente_longue_jours": ATTENTE_LONGUE_JOURS,
    }

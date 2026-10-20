"""
Filtrage transactionnel ISO 20022 (roadmap P3-1, a la Fircosoft).

Parse les messages de paiement pain.001 (ordre de virement client) et
pacs.008 (virement interbancaire) de maniere agnostique de la version
(correspondance par nom local des balises), extrait toutes les parties
(donneur d'ordre, beneficiaire, ultimes, agents bancaires, partie initiante)
et crible chacune contre les listes en production.

Difference volontaire avec le criblage du referentiel clients : les donnees
d'un message de paiement sont pauvres (souvent un simple nom, parfois un
pays), donc la recherche de candidats ignore le pays de blocking — seule la
cle phonetique filtre — pour ne manquer aucun hit. Le verdict global est
HIT des qu'une partie declenche une alerte ; chaque partie criblee laisse
une ligne dans le journal d'audit immuable et les hits ouvrent des alertes
de travail adjudicables dans l'onglet Alertes.

PASS ne sort que de ce qui a ete REELLEMENT compare. Une partie sans nom ni
identifiant exact (BIC, LEI) n'est pas « sans correspondance » : elle est
NON CRIBLEE, tracee comme telle, et interdit le PASS — de meme qu'un message
auquel manque une information obligatoire du reglement (UE) 2023/1113. Le
verdict est alors INCOMPLETE, avec la liste nommee de ce qui manque.
"""
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from fiskr import capabilities as caps
from fiskr.config import config
from fiskr.database import log_compliance_decision, log_compliance_decisions
from fiskr.names import parse_individual_name
from fiskr.scoring import match_entities, resolve_cut_off
from fiskr.alerts import open_or_redetect_alerts

logger = logging.getLogger("fiskr.transactions")

# Roles de parties criblees, par element conteneur ISO 20022
PARTY_ROLES = {
    "InitgPty": "Partie initiante",
    "Dbtr": "Donneur d'ordre",
    "UltmtDbtr": "Donneur d'ordre ultime",
    "Cdtr": "Bénéficiaire",
    "UltmtCdtr": "Bénéficiaire ultime",
}
# Compte de paiement d'une partie : porte A COTE d'elle, dans le meme bloc
# (pain.001 : Dbtr/DbtrAcct dans PmtInf, Cdtr/CdtrAcct dans CdtTrfTxInf).
COMPTE_DE = {"Dbtr": "DbtrAcct", "Cdtr": "CdtrAcct"}
# Les intermediaires de la chaine sont criblés au meme titre que les banques
# des deux bouts : les lignes directrices conjointes DGT/ACPR sur le gel des
# avoirs (18 mars 2026, §118) demandent de filtrer les intermediaires. Seul le
# premier l'etait ; un pacs.008 en porte jusqu'a trois, plus trois agents
# instructeurs anterieurs.
AGENT_ROLES = {
    "DbtrAgt": "Banque du donneur d'ordre",
    "CdtrAgt": "Banque du bénéficiaire",
    "InstgAgt": "Agent instructeur",
    "InstdAgt": "Agent instruit",
    "IntrmyAgt1": "Banque intermédiaire",
    "IntrmyAgt2": "Banque intermédiaire 2",
    "IntrmyAgt3": "Banque intermédiaire 3",
    "PrvsInstgAgt1": "Agent instructeur antérieur 1",
    "PrvsInstgAgt2": "Agent instructeur antérieur 2",
    "PrvsInstgAgt3": "Agent instructeur antérieur 3",
}
ROLE_TEXTE_LIBRE = "Informations de remise (texte libre)"

MESSAGE_TYPES = {
    "CstmrCdtTrfInitn": "pain.001",
    "FIToFICstmrCdtTrf": "pacs.008",
}


def _local(elem) -> str:
    return elem.tag.rsplit("}", 1)[-1] if "}" in elem.tag else elem.tag


def _child(elem, *path: str):
    """Descend une chaine de noms locaux (premier enfant correspondant a chaque niveau)."""
    current = elem
    for name in path:
        if current is None:
            return None
        current = next((c for c in current if _local(c) == name), None)
    return current


def _child_text(elem, *path: str) -> str:
    node = _child(elem, *path)
    return (node.text or "").strip() if node is not None else ""


# Longueur maximale d'un nom de partie retenu pour le criblage.
#
# ISO 20022 plafonne <Nm> a 140 caracteres (Max140Text) ; la base de Fiskr
# stocke 1 000. Un message peut malgre tout en porter davantage — rien ne
# valide le XML entrant — et la distance de Damerau-Levenshtein est LINEAIRE en
# cette longueur : 82,55 ms pour un score de base a 20 000 caracteres contre
# 1,19 ms a 100, multiplie par les 415 candidats d'un seau moyen de la
# production. Un seul nom de 20 000 caracteres vaut 34 s de calcul.
#
# La partie n'est pas REJETEE — refuser le message entier le laisserait non
# crible, ce qui est pire — mais son nom est ramene a ce que la base sait
# stocker, sept fois le plafond de la norme.
LONGUEUR_MAX_NOM_PARTIE = 1000


def _nom_borne(nom: str) -> str:
    return nom[:LONGUEUR_MAX_NOM_PARTIE] if nom and len(nom) > LONGUEUR_MAX_NOM_PARTIE else nom


_HORS_ALPHANUM = re.compile(r"[^A-Z0-9]+")


def _alphanum(valeur: str) -> str:
    # Voie rapide : l'immense majorite des fiches listees n'a ni BIC ni LEI,
    # et ce test est fait pour chacune a la construction des index derives.
    return _HORS_ALPHANUM.sub("", str(valeur).upper()) if valeur else ""


def bic_comparable(bic: str) -> str:
    """BIC sous la forme que compare le hard match (8 ou 11 caracteres), ou ''."""
    propre = _alphanum(bic)
    return propre if len(propre) in (8, 11) else ""


def lei_comparable(lei: str) -> str:
    """LEI sous la forme que compare le hard match (20 caracteres), ou ''."""
    propre = _alphanum(lei)
    return propre if len(propre) == 20 else ""


def _adresse(elem) -> Dict[str, str]:
    """Adresse postale (PstlAdr) : pays, ville, lignes libres."""
    postal = _child(elem, "PstlAdr")
    if postal is None:
        return {"country": "", "town": "", "address": ""}
    lines = [(c.text or "").strip() for c in postal if _local(c) == "AdrLine"]
    return {
        "country": _child_text(postal, "Ctry").upper(),
        "town": _child_text(postal, "TwnNm"),
        "address": ", ".join(l for l in lines if l),
    }


def _autres_identifiants(*blocs) -> List[str]:
    """Identifiants que le moteur ne sait pas rapprocher (Othr/Id d'OrgId ou
    PrvtId, ClrSysMmbId d'un agent) : ils ne criblent rien, mais ils disent
    QUI est la partie — sans eux, une partie non criblee serait anonyme dans
    le journal."""
    autres: List[str] = []
    for bloc in blocs:
        if bloc is None:
            continue
        for othr in (c for c in bloc if _local(c) == "Othr"):
            ident = _child_text(othr, "Id")
            if ident:
                schema = (_child_text(othr, "SchmeNm", "Cd")
                          or _child_text(othr, "SchmeNm", "Prtry"))
                autres.append(f"{_local(bloc)}/Othr {ident}" + (f" ({schema})" if schema else ""))
        membre = _child_text(bloc, "ClrSysMmbId", "MmbId")
        if membre:
            autres.append(f"ClrSysMmbId {membre}")
    return autres


def _compte(container, role_tag: str) -> str:
    """Numero de compte de paiement porte a cote de la partie (IBAN ou
    Othr/Id), ou ''. Un alias (Prxy) n'est pas un numero de compte."""
    tag = COMPTE_DE.get(role_tag)
    acct = _child(container, tag) if (tag and container is not None) else None
    if acct is None:
        return ""
    return _child_text(acct, "Id", "IBAN") or _child_text(acct, "Id", "Othr", "Id")


def _extract_party(elem, role_tag: str, container=None) -> Optional[Dict[str, Any]]:
    """
    Extrait une partie : nom, adresse, identifiants, naissance, compte.

    Une partie SANS <Nm> est extraite quand meme. Elle etait ecartee ici,
    avant d'atteindre le criblage : un donneur d'ordre identifie par son seul
    <AnyBIC> ou <LEI> disparaissait sans une ligne d'audit, et le message
    rendait PASS sur zero partie criblee. Elle est desormais rapprochee sur son
    identifiant exact, ou restituee NON CRIBLEE avec le motif.
    """
    if elem is None:
        return None
    name = _child_text(elem, "Nm")
    adresse = _adresse(elem)
    org = _child(elem, "Id", "OrgId")
    prive = _child(elem, "Id", "PrvtId")
    birth = _child(prive, "DtAndPlcOfBirth")
    return {
        "role_tag": role_tag,
        "role": PARTY_ROLES.get(role_tag, role_tag),
        "name": _nom_borne(name),
        "has_name": bool(name),
        "country": adresse["country"],
        "town": adresse["town"],
        "address": adresse["address"],
        # AnyBIC depuis pain.001.001.04 ; BICOrBEI dans les versions anterieures
        "bic": _child_text(org, "AnyBIC") or _child_text(org, "BICOrBEI"),
        "lei": _child_text(org, "LEI"),
        "other_ids": _autres_identifiants(org, prive),
        "account": _compte(container, role_tag),
        "birth_date": _child_text(birth, "BirthDt"),
        "birth_city": _child_text(birth, "CityOfBirth"),
        "birth_country": _child_text(birth, "CtryOfBirth").upper(),
        "is_agent": False,
    }


def _extract_agent(elem, role_tag: str) -> Optional[Dict[str, Any]]:
    """
    Extrait un agent financier (FinInstnId : BICFI/BIC, LEI, Nm, ClrSysMmbId).

    Un agent identifie autrement que par un BIC ou un nom (LEI seul, code de
    chambre de compensation) etait ecarte sans trace ; il est extrait, et le
    criblage dit s'il a pu le comparer a quoi que ce soit.
    """
    if elem is None:
        return None
    fin = _child(elem, "FinInstnId")
    bic = _child_text(fin, "BICFI") or _child_text(fin, "BIC")
    name = _child_text(fin, "Nm")
    country = _child_text(fin, "PstlAdr", "Ctry") or (bic[4:6] if len(bic) >= 6 else "")
    return {
        "role_tag": role_tag,
        "role": AGENT_ROLES.get(role_tag, role_tag),
        # Sans <Nm>, le BIC tient lieu de nom affiche (et de nom compare,
        # comme avant) ; `has_name` dit si un nom a reellement ete fourni.
        "name": _nom_borne(name or bic),
        "has_name": bool(name),
        "country": country.upper(),
        "town": "",
        "address": "",
        "bic": bic,
        "lei": _child_text(fin, "LEI"),
        "other_ids": _autres_identifiants(fin),
        "account": "",
        "birth_date": "",
        "birth_city": "",
        "birth_country": "",
        "is_agent": True,
    }


def _collect_parties(container, into: List[Dict[str, Any]]) -> None:
    """Parcourt les enfants directs d'un bloc (PmtInf ou CdtTrfTxInf) pour en extraire les parties."""
    for child in container:
        tag = _local(child)
        if tag in PARTY_ROLES:
            party = _extract_party(child, tag, container)
            if party:
                into.append(party)
        elif tag in AGENT_ROLES:
            agent = _extract_agent(child, tag)
            if agent:
                into.append(agent)


def _remise(tx) -> str:
    """Informations de remise non structurees (RmtInf/Ustrd), toutes
    occurrences jointes : la norme en admet plusieurs, et un nom coupe entre
    deux lignes doit rester lisible d'un seul tenant."""
    rmt = _child(tx, "RmtInf")
    if rmt is None:
        return ""
    return " ".join(t for t in ((c.text or "").strip() for c in rmt if _local(c) == "Ustrd") if t)


def _extract_amount(tx) -> Tuple[str, str]:
    """Montant + devise d'une transaction (InstdAmt pain.001 / IntrBkSttlmAmt pacs.008)."""
    for path in (("Amt", "InstdAmt"), ("IntrBkSttlmAmt",), ("Amt", "EqvtAmt", "Amt")):
        node = _child(tx, *path)
        if node is not None and (node.text or "").strip():
            return (node.text or "").strip(), (node.get("Ccy") or "").strip()
    return "", ""


def parse_iso20022_payment(content: bytes) -> Dict[str, Any]:
    """
    Parse un message de paiement ISO 20022 (pain.001 ou pacs.008, toute
    version mineure) et retourne les metadonnees + les transactions avec
    leurs parties. Leve ValueError si le message n'est pas reconnu.
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise ValueError(f"XML invalide : {e}")

    # Racine <Document>, premier enfant = type de message
    message_elem = None
    message_type = None
    for candidate in ([root] + list(root)):
        if _local(candidate) in MESSAGE_TYPES:
            message_elem = candidate
            message_type = MESSAGE_TYPES[_local(candidate)]
            break
    if message_elem is None:
        raise ValueError(
            "Message ISO 20022 non reconnu : types supportés pain.001 (CstmrCdtTrfInitn) "
            "et pacs.008 (FIToFICstmrCdtTrf)."
        )

    grp_hdr = _child(message_elem, "GrpHdr")
    result: Dict[str, Any] = {
        "message_type": message_type,
        "msg_id": _child_text(grp_hdr, "MsgId") if grp_hdr is not None else "",
        "creation_datetime": _child_text(grp_hdr, "CreDtTm") if grp_hdr is not None else "",
        "number_of_txs": _child_text(grp_hdr, "NbOfTxs") if grp_hdr is not None else "",
        "control_sum": _child_text(grp_hdr, "CtrlSum") if grp_hdr is not None else "",
        "transactions": [],
    }

    header_parties: List[Dict[str, Any]] = []
    if grp_hdr is not None:
        _collect_parties(grp_hdr, header_parties)

    if message_type == "pain.001":
        for pmt_inf in (c for c in message_elem if _local(c) == "PmtInf"):
            batch_parties: List[Dict[str, Any]] = list(header_parties)
            _collect_parties(pmt_inf, batch_parties)
            for tx in (c for c in pmt_inf if _local(c) == "CdtTrfTxInf"):
                parties = list(batch_parties)
                _collect_parties(tx, parties)
                amount, currency = _extract_amount(tx)
                result["transactions"].append({
                    "end_to_end_id": _child_text(tx, "PmtId", "EndToEndId"),
                    "amount": amount,
                    "currency": currency,
                    "remittance": _remise(tx),
                    "parties": parties,
                })
    else:  # pacs.008 : les parties sont toutes portees par chaque CdtTrfTxInf
        for tx in (c for c in message_elem if _local(c) == "CdtTrfTxInf"):
            parties = list(header_parties)
            _collect_parties(tx, parties)
            amount, currency = _extract_amount(tx)
            result["transactions"].append({
                "end_to_end_id": _child_text(tx, "PmtId", "EndToEndId") or _child_text(tx, "PmtId", "TxId"),
                "amount": amount,
                "currency": currency,
                "remittance": _remise(tx),
                "parties": parties,
            })

    if not result["transactions"]:
        raise ValueError("Aucune transaction (CdtTrfTxInf) trouvée dans le message.")
    return result


# Nombre maximal de parties DISTINCTES criblees dans un seul message.
#
# Sans borne, un message accepte jusqu'au plafond de televersement (8 Mo)
# contient 56 678 transactions — mesure sur un pain.001 minimal reel, 148 o par
# transaction — donc autant de parties distinctes. Chacune interroge l'index de
# filtrage et compare ses candidats : sur la production, un seau phonetique
# porte 415 fiches en moyenne (25 906 pour le plus gros), a ~180 us la
# comparaison. Soit environ 75 ms par partie, et plus d'une heure de calcul
# pour un seul message — dans une requete HTTP synchrone, avec autant de lignes
# ecrites au journal d'audit.
#
# Cette requete echoue DEJA aujourd'hui, sur le delai d'attente du serveur,
# mais apres avoir brule ce temps et ecrit ces lignes. Un refus immediat et
# explicite vaut mieux. La borne est posee a 500 parties : au cout moyen
# mesure, c'est ~37 s de criblage, deja le maximum raisonnable pour une
# reponse synchrone.
MAX_PARTIES_PAR_MESSAGE = 500


class MessageTropVolumineux(ValueError):
    """Le message porte plus de parties que le criblage synchrone n'en prend."""

    def __init__(self, parties: int, plafond: int):
        self.parties = parties
        self.plafond = plafond
        super().__init__(
            f"Ce message porte {parties} parties distinctes à cribler, au-delà "
            f"de la limite de {plafond} d'un filtrage synchrone. Découpez-le en "
            f"plusieurs messages : chacun sera criblé et tracé normalement."
        )


class MessageSansPartieCriblable(ValueError):
    """Aucune partie du message ne porte rien que le moteur sache comparer."""

    def __init__(self, parties: int):
        self.parties = parties
        super().__init__(
            f"Aucune des {parties} partie(s) de ce message ne porte de nom ni "
            f"d'identifiant exact (BIC, LEI) : rien ne peut être comparé aux "
            f"listes. Le message est refusé plutôt que rendu sans correspondance "
            f"— un verdict sur un criblage qui n'a pas eu lieu serait une fausse "
            f"pièce de conformité."
        )


# ------------------ VERDICT ------------------
#
# UNE definition du vocabulaire : la reponse, l'ecran et le journal la lisent.
# INCOMPLETE est la seule notion nouvelle, et elle couvre les deux cas ou le
# filtrage ne PEUT PAS etablir un PASS : une partie qu'il n'a pas pu comparer
# aux listes, une information obligatoire absente du message. Dans les deux
# cas le paiement ne doit pas partir sur la foi du filtrage — c'est ce que
# l'operateur a besoin de savoir, et la liste nommee dit quoi regarder.
VERDICT_HIT = "HIT"
VERDICT_INCOMPLET = "INCOMPLETE"
VERDICT_PASS = "PASS"
# Libelles d'un seul tenant (pas de concatenation) : l'ecran les affiche et
# fiskr/static/i18n.js les traduit par correspondance exacte.
VERDICTS = {
    VERDICT_HIT: "Au moins une correspondance en alerte : alerte(s) ouverte(s).",
    VERDICT_INCOMPLET: "Filtrage incomplet : partie non criblée ou information obligatoire manquante. À revoir avant exécution.",
    VERDICT_PASS: "Toutes les parties criblées sans correspondance, message complet.",
}

STATUT_NON_CRIBLEE = "NOT_SCREENED"
MOTIFS_NON_CRIBLEE = {
    "SANS_ELEMENT_COMPARABLE": "Ni nom (Nm) ni identifiant exact (BIC, LEI) : rien à comparer aux listes.",
    "IDENTIFIANT_DESACTIVE": "Identifiant exact seul (BIC, LEI), dont le rapprochement est désactivé sur le canal filtrage.",
}


def elements_compares(party: Dict[str, Any]) -> List[str]:
    """
    Ce que le moteur compare REELLEMENT aux listes pour cette partie.

    Le nom, s'il est fourni ; le BIC et le LEI, s'ils sont bien formes ET si
    leur hard match est actif sur le canal filtrage — un identifiant que la
    capacite coupee ne compare plus ne crible rien. Un agent sans <Nm> garde
    son BIC comme nom affiche, mais ce n'est pas un nom : il ne compte pas.
    """
    elements = []
    if party.get("has_name", bool(party.get("name"))):
        elements.append("NOM")
    if bic_comparable(party.get("bic")) and caps.is_active(caps.CAP_HARD_BIC, caps.CHANNEL_FILTERING):
        elements.append("BIC")
    if lei_comparable(party.get("lei")) and caps.is_active(caps.CAP_HARD_LEI, caps.CHANNEL_FILTERING):
        elements.append("LEI")
    return elements


def motif_non_criblee(party: Dict[str, Any]) -> Optional[str]:
    """Code du motif pour lequel la partie n'est pas criblable, ou None."""
    if elements_compares(party):
        return None
    if bic_comparable(party.get("bic")) or lei_comparable(party.get("lei")):
        return "IDENTIFIANT_DESACTIVE"
    return "SANS_ELEMENT_COMPARABLE"


def libelle_partie(party: Dict[str, Any]) -> str:
    """Nom affiche d'une partie : son nom, a defaut ce qui l'identifie."""
    if party.get("name"):
        return party["name"]
    for prefixe, valeur in (("BIC", party.get("bic")), ("LEI", party.get("lei"))):
        if valeur:
            return f"{prefixe} {valeur}"
    autres = party.get("other_ids") or []
    return autres[0] if autres else "(sans nom ni identifiant)"


# ------------------ COMPLETUDE : REGLEMENT (UE) 2023/1113 ------------------
#
# Art. 4 : informations qui accompagnent un transfert de fonds ; art. 7 §2 b) :
# le prestataire du beneficiaire detecte l'ABSENCE de celles de l'art. 4 §1
# a), b), c) et §2 a), b) — cas general, celui ou le prestataire du donneur
# d'ordre est etabli hors de l'Union. Seul ce cas general est code, pour les
# deux types de message. Les derogations ne sont PAS appliquees :
#   - art. 5 et art. 7 §2 a) (prestataires etablis dans l'Union : les numeros
#     de compte suffisent) — un virement intra-UE sans adresse du donneur
#     d'ordre est donc signale, alors que l'art. 5 l'admet ;
#   - art. 6 §2 (au plus 1 000 EUR vers l'exterieur : noms et comptes
#     suffisent) ;
#   - art. 4 §3 (transfert hors compte de paiement : identifiant unique de
#     transaction au lieu du compte) — le message ne dit pas si le transfert
#     part d'un compte, l'absence de compte reste donc signalee.
# Une derogation reduit ce qui est EXIGE : ne pas l'appliquer fait signaler
# davantage, jamais moins, et le signal reste nomme, donc tranchable. Le LEI
# (art. 4 §1 d) et §2 c), « s'il est fourni ») n'est jamais un manque.
MANQUES = {
    "DBTR_NM": ("Nom du donneur d'ordre (Dbtr/Nm)", "2023/1113 art. 4 §1 a)"),
    "DBTR_ACCT": ("Numéro de compte du donneur d'ordre (DbtrAcct)", "2023/1113 art. 4 §1 b)"),
    "DBTR_ADR": ("Adresse du donneur d'ordre avec pays et ville (PstlAdr), ou identifiant officiel ou client, ou date et lieu de naissance",
                 "2023/1113 art. 4 §1 c)"),
    "CDTR_NM": ("Nom du bénéficiaire (Cdtr/Nm)", "2023/1113 art. 4 §2 a)"),
    "CDTR_ACCT": ("Numéro de compte du bénéficiaire (CdtrAcct)", "2023/1113 art. 4 §2 b)"),
}
# Identifiants de transaction rendus par manque : au-dela, le compte suffit.
MAX_TRANSACTIONS_PAR_MANQUE = 20


def _manques_transaction(tx: Dict[str, Any]) -> List[str]:
    """Codes des informations obligatoires absentes d'une transaction."""
    roles = {p["role_tag"]: p for p in tx["parties"] if not p.get("is_agent")}
    dbtr, cdtr = roles.get("Dbtr") or {}, roles.get("Cdtr") or {}
    adresse = bool(dbtr.get("country") and (dbtr.get("town") or dbtr.get("address")))
    identifiant = bool(dbtr.get("bic") or dbtr.get("lei") or dbtr.get("other_ids"))
    naissance = bool(dbtr.get("birth_date") and (dbtr.get("birth_city") or dbtr.get("birth_country")))
    manques = []
    if not dbtr.get("has_name"):
        manques.append("DBTR_NM")
    if not dbtr.get("account"):
        manques.append("DBTR_ACCT")
    if not (adresse or identifiant or naissance):
        manques.append("DBTR_ADR")
    if not cdtr.get("has_name"):
        manques.append("CDTR_NM")
    if not cdtr.get("account"):
        manques.append("CDTR_ACCT")
    return manques


def manques_de_completude(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Informations obligatoires absentes du message, nommees, avec les
    transactions concernees. Vide = message complet au sens de l'art. 4."""
    par_code: Dict[str, List[str]] = {}
    for rang, tx in enumerate(parsed["transactions"], start=1):
        for code in _manques_transaction(tx):
            par_code.setdefault(code, []).append(tx.get("end_to_end_id") or f"#{rang}")
    return [{
        "code": code,
        "label": MANQUES[code][0],
        "reference": MANQUES[code][1],
        "transactions_count": len(par_code[code]),
        "transactions": par_code[code][:MAX_TRANSACTIONS_PAR_MANQUE],
    } for code in MANQUES if code in par_code]


def verdict_du_message(en_alerte: bool, non_criblees: int, manques: List[Dict[str, Any]]) -> str:
    """HIT l'emporte (une alerte est ouverte, le paiement est arrete de toute
    facon) ; puis INCOMPLETE ; PASS seulement quand tout a ete compare et que
    rien d'obligatoire ne manque."""
    if en_alerte:
        return VERDICT_HIT
    if non_criblees or manques:
        return VERDICT_INCOMPLET
    return VERDICT_PASS


def _distinct_parties(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Deduplique les parties du message par identite criblee (nom, pays,
    BIC, LEI, autres identifiants), roles agreges. Les identifiants entrent
    dans la cle : deux parties sans nom ne sont pas la meme partie."""
    seen: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for tx in parsed["transactions"]:
        for party in tx["parties"]:
            key = (party["name"].upper(), party["country"], party["bic"],
                   party.get("lei", ""), tuple(party.get("other_ids") or ()))
            if key in seen:
                if party["role"] not in seen[key]["roles"]:
                    seen[key]["roles"].append(party["role"])
            else:
                entry = dict(party)
                entry["roles"] = [party["role"]]
                seen[key] = entry
    return list(seen.values())


def _name_rotations(name: str) -> List[str]:
    """
    Variantes du nom ou chaque mot passe en tete, a tour de role.

    L'ordre des mots d'un champ libre de paiement n'est pas fiable : « PUTIN
    VLADIMIR », « VLADIMIR PUTIN », « MR V PUTIN » designent la meme personne.
    `generate_blocking_keys` fonde sa cle phonetique sur le PREMIER mot ; les
    rotations garantissent donc que chaque mot du champ est vu au moins une
    fois comme premier mot, ce qui reproduit la propriete de l'ancienne
    implantation (phonetique de TOUS les mots) sans en dupliquer le code.
    """
    words = [w for w in re.split(r"[\s\-]+", (name or "").strip()) if w]
    if len(words) <= 1:
        return [" ".join(words)]
    return [" ".join([words[i]] + words[:i] + words[i + 1:]) for i in range(len(words))]


def _filtering_index(entities: List[Dict[str, Any]], filtering_cfg: Dict[str, Any],
                     allowed_lists: Optional[List[str]] = None
                     ) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Index de blocking local du filtrage, construit UNE SEULE FOIS par message
    avec le layout du canal FILTERING (parametrable a chaud, defaut phonetique
    seule — les donnees de paiement sont trop pauvres pour filtrer sur le pays
    ou le type). `allowed_lists` restreint l'univers aux types de listes.
    """
    from fiskr.blocking import generate_blocking_keys
    index: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for entity in entities:
        if allowed_lists and entity.get("_list_type") not in allowed_lists:
            continue
        for key in generate_blocking_keys(entity, filtering_cfg):
            index.setdefault(key, {})[entity["entity_id"]] = entity
    return index


# Index de filtrage du PROCESSUS. Il etait reconstruit A CHAQUE MESSAGE :
# aplatir l'index de criblage, dedupliquer par entity_id, puis regenerer les
# cles de blocage de TOUT l'univers. Mesure sur un corpus aux proportions de la
# production (832 470 fiches) : 17,0 s de generation de cles et 1,7 s
# d'aplatissement — dix-neuf secondes payees dans la requete HTTP, sur le canal
# dont l'exigence premiere est le temps de reponse.
#
# La signature couvre TOUT ce qui change les cles : l'empreinte de l'univers
# en production, le layout du canal, les capacites du moteur (translitteration,
# phonetique) et les equivalences linguistiques actives. La restriction de
# listes n'y figure PAS : elle s'applique a la selection des candidats, pas a
# la construction de l'index.
#
# L'empreinte est celle de fiskr/univers.py : tous les lots en production,
# leurs exclusions et le journal des corrections. Elle etait le hash du SEUL
# lot depose le plus recemment — reproduit : une DGT deposee a T+1h,
# homologuee apres une OFAC deposee a T+2h, ne changeait pas la signature ;
# l'index n'etait pas reconstruit, et un paiement a la personne qu'elle
# designait passait en PASS, zero candidat.
_CACHE_INDEX_FILTRAGE: Dict[str, Any] = {"signature": None, "index": None}
_VERROU_INDEX_FILTRAGE = threading.Lock()


def _signature_index_filtrage(watchlist_hash: str, filtering_cfg: Dict[str, Any]):
    from fiskr import capabilities as caps
    from fiskr import resources
    contexte = resources.current_context() or {}
    index_res = contexte.get("index")
    return (
        watchlist_hash,
        tuple((filtering_cfg.get("blocking") or {}).get("custom_key_layout") or ()),
        caps.current_context("FILTERING"),
        tuple(sorted(contexte.get("fields") or ())),
        getattr(index_res, "content_hash", None),
    )


def index_de_filtrage(watchlist_index: Dict[str, List[Dict[str, Any]]],
                      filtering_cfg: Dict[str, Any],
                      watchlist_hash: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Index de blocage du canal filtrage, construit une fois par parametrage."""
    signature = _signature_index_filtrage(watchlist_hash, filtering_cfg)
    if (_CACHE_INDEX_FILTRAGE["signature"] == signature
            and _CACHE_INDEX_FILTRAGE["index"] is not None):
        return _CACHE_INDEX_FILTRAGE["index"]
    with _VERROU_INDEX_FILTRAGE:
        # Re-teste sous le verrou : pendant l'attente, un autre fil a pu batir
        # exactement le meme index. On ne le refait pas.
        if (_CACHE_INDEX_FILTRAGE["signature"] == signature
                and _CACHE_INDEX_FILTRAGE["index"] is not None):
            return _CACHE_INDEX_FILTRAGE["index"]
        depart = time.perf_counter()
        uniques: Dict[str, Dict[str, Any]] = {}
        for seau in watchlist_index.values():
            for entite in seau:
                uniques.setdefault(entite.get("entity_id"), entite)
        index = _filtering_index(list(uniques.values()), filtering_cfg)
        logger.info(
            f"Index de filtrage construit : {len(uniques)} fiches, {len(index)} seaux, "
            f"{time.perf_counter() - depart:.1f} s. Réutilisé jusqu'au prochain "
            f"changement de listes ou de paramétrage."
        )
        _CACHE_INDEX_FILTRAGE["index"] = index
        _CACHE_INDEX_FILTRAGE["signature"] = signature
        return index


def invalider_index_de_filtrage() -> None:
    """Jette l'index du processus (rechargement de listes, changement de
    reglage). La signature suffit en regime normal ; ceci sert aux tests et aux
    chemins qui savent, eux, que l'univers vient de changer."""
    with _VERROU_INDEX_FILTRAGE:
        _CACHE_INDEX_FILTRAGE["signature"] = None
        _CACHE_INDEX_FILTRAGE["index"] = None


# ------------------ INDEX DERIVES : IDENTIFIANTS EXACTS ET TEXTE LIBRE ------------------
#
# Deux recherches que l'index phonetique ne sait pas faire :
#   - par IDENTIFIANT EXACT (BIC, LEI). Le hard match existait (scoring.py),
#     mais il ne voyait que les fiches deja candidates par le NOM : une partie
#     identifiee par son seul BIC ou LEI n'etait candidate de rien ;
#   - par NOM LISTE DANS UN TEXTE LIBRE (RmtInf/Ustrd), extrait puis ignore.
#
# Ils sont DERIVES de l'objet index de filtrage, et rebatis quand il l'est :
# tout ce qui le fait reconstruire (listes, layout, capacites, equivalences —
# sa signature) change l'univers qu'ils decrivent. Le classement SANCTION /
# HORS_SANCTION, reglable a chaud, entre dans leur propre cle.
_CACHE_DERIVES: Dict[str, Any] = {"source": None, "surcharges": None, "derives": None}
_VERROU_DERIVES = threading.Lock()


def _mots_texte(texte: str) -> List[str]:
    """Mots normalises d'un texte, dans la forme que compare le moteur
    (translitteration et diacritiques selon les capacites du canal filtrage),
    ponctuation retiree : « PUTIN, » et « PUTIN » sont le meme mot."""
    from fiskr.quality import strip_accents_for_matching
    normalise = strip_accents_for_matching(str(texte or ""), caps.CHANNEL_FILTERING).upper()
    if normalise.isascii():
        # Meme decoupe que l'expression reguliere, quatre fois moins cher : ce
        # chemin est pris pour chaque nom liste a la construction de l'index.
        return normalise.translate(_PONCTUATION_ASCII).split()
    return [m for m in _HORS_ALPHANUM.split(normalise) if m]


_PONCTUATION_ASCII = str.maketrans({chr(c): " " for c in range(128) if not chr(c).isalnum()})


def _sequence_de_mots(mots: List[str]) -> Optional[str]:
    """Forme indexee d'un nom liste pour le texte libre, ou None s'il porte
    moins de DEUX mots significatifs (2 lettres ou plus). Un nom d'un seul mot
    (« HAMAS », « ROSNEFT ») rapproche dans un texte libre crierait au loup sur
    tout mot du dictionnaire qui se trouve etre un alias : il n'est pas indexe."""
    if sum(1 for m in mots if len(m) >= 2) < 2:
        return None
    return " ".join(mots)


def _construire_derives(index: Dict[str, Dict[str, Dict[str, Any]]],
                        surcharges: Dict[str, str]) -> Dict[str, Any]:
    from fiskr.perimeters import est_sanction
    from fiskr.quality import MOTS_BRUIT_JURIDIQUES
    from fiskr.rarete import noms_de_fiche
    sans_bruit = caps.is_active(caps.CAP_NOISE_WORDS, caps.CHANNEL_FILTERING)
    depart = time.perf_counter()
    par_bic: Dict[str, Dict[str, Dict[str, Any]]] = {}
    par_lei: Dict[str, Dict[str, Dict[str, Any]]] = {}
    noms: Dict[str, Dict[str, Dict[str, Any]]] = {}
    # Perimetre resolu une fois par TYPE de liste, pas par fiche : la table
    # par defaut est rebatie a chaque appel de `perimetre_de`.
    sanction_par_type: Dict[Any, bool] = {}
    # Une fiche figure dans plusieurs seaux : la fusion des seaux (faite en C)
    # la deduplique bien moins cher qu'une boucle tenant un ensemble.
    uniques: Dict[str, Dict[str, Any]] = {}
    for seau in index.values():
        uniques.update(seau)
    for entity_id, entite in uniques.items():
        bic = bic_comparable(entite.get("bic_swift"))
        if bic:
            par_bic.setdefault(bic[:8], {})[entity_id] = entite
        lei = lei_comparable(entite.get("lei_number"))
        if lei:
            par_lei.setdefault(lei, {})[entity_id] = entite
        # Texte libre : perimetre SANCTION seulement. C'est la que manquer un
        # nom est un manquement au gel des avoirs ; un PEP cite dans un
        # libelle de virement n'appelle pas d'arret du paiement.
        type_liste = entite.get("_list_type")
        if type_liste not in sanction_par_type:
            sanction_par_type[type_liste] = est_sanction(type_liste, surcharges)
        if not sanction_par_type[type_liste]:
            continue
        for nom in noms_de_fiche(entite):
            mots = _mots_texte(nom)
            sequence = _sequence_de_mots(mots)
            if not sequence:
                continue
            noms.setdefault(sequence, {})[entity_id] = entite
            # La forme sans suffixe juridique aussi (meme capacite que le
            # moteur, cf. quality.clean_noise_words) : un libelle ecrit
            # « Volga Trading », pas « Volga Trading LLC ».
            if sans_bruit and not MOTS_BRUIT_JURIDIQUES.isdisjoint(mots):
                variante = _sequence_de_mots([m for m in mots if m not in MOTS_BRUIT_JURIDIQUES])
                if variante:
                    noms.setdefault(variante, {})[entity_id] = entite
    derives = {
        "bic": par_bic, "lei": par_lei, "noms": noms,
        "mots_max": max((s.count(" ") + 1 for s in noms), default=0),
    }
    logger.info(
        f"Index dérivés du filtrage : {len(uniques)} fiches, {len(par_bic)} BIC, {len(par_lei)} LEI, "
        f"{len(noms)} noms SANCTION pour le texte libre, {time.perf_counter() - depart:.1f} s."
    )
    return derives


def index_derives(index: Dict[str, Dict[str, Dict[str, Any]]],
                  surcharges: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Index par identifiant exact et par nom SANCTION, derives de `index`."""
    cle = tuple(sorted((surcharges or {}).items()))
    if _CACHE_DERIVES["source"] is index and _CACHE_DERIVES["surcharges"] == cle:
        return _CACHE_DERIVES["derives"]
    with _VERROU_DERIVES:
        if _CACHE_DERIVES["source"] is index and _CACHE_DERIVES["surcharges"] == cle:
            return _CACHE_DERIVES["derives"]
        derives = _construire_derives(index, dict(surcharges or {}))
        _CACHE_DERIVES.update(source=index, surcharges=cle, derives=derives)
        return derives


def formes_criblees(noms: List[str]) -> List[str]:
    """Noms deja criblés comme parties, sous la forme que compare le texte
    libre (bornes d'espaces : on cherche une sequence de MOTS, pas de lettres).
    Calcule une fois par message, pas une fois par transaction."""
    return [f" {' '.join(_mots_texte(n))} " for n in noms if n]


def sequences_listees_du_texte(texte: str, derives: Dict[str, Any],
                               formes_deja_criblees: Optional[List[str]] = None) -> List[str]:
    """
    Noms SANCTION ecrits dans un texte libre, en sequences exactes normalisees.

    HAUTE PRECISION, mesuree : un filtre de texte libre qui crie au loup sur
    chaque virement est debranche. Corpus liste : les 288 209 noms et alias
    du recueil « sanctions » d'OpenSanctions, tous traites comme fiches
    SANCTION (borne haute : les alias faibles y figurent aussi), table de
    rarete construite dessus. Sur 10 000 libelles de remise plausibles sans
    nom de personne (factures, loyers, abonnements, commandes), 11 declenchent
    (0,11 %) : « Wagner Group », et des raisons sociales generiques reellement
    listees (« United Technologies », « Alpha Trading »). Un nom liste glisse
    dans un libelle est retrouve 2 999 fois sur 3 000. Trois regles :
      - le NOM COMPLET (nom principal ou alias haute priorite) d'au moins deux
        mots significatifs, jamais un fragment ;
      - au moins un de ses mots n'est pas REPANDU dans le corpus liste (meme
        definition que fiskr/rarete.py) : « ENGINEERING GROUP », nom reellement
        liste, n'identifie personne ;
      - une sequence deja criblee comme nom d'une partie du message n'est pas
        rapprochee une seconde fois.
    Ce qui reste est l'homonymie, celle du criblage des parties : un libelle
    qui nomme une personne au nom tres courant declenche comme le ferait ce
    nom en donneur d'ordre — 3,45 % des libelles nommant une personne tiree
    des prenoms et noms les plus frequents, 1,28 % sur un melange ou un
    libelle sur trois nomme quelqu'un.
    """
    from fiskr import rarete
    noms = derives.get("noms") or {}
    mots = _mots_texte(texte)
    if len(mots) < 2 or not noms:
        return []
    table = rarete.table_courante()
    deja = formes_deja_criblees or []
    trouvees: List[str] = []
    for debut in range(len(mots)):
        for longueur in range(2, min(derives.get("mots_max", 0), len(mots) - debut) + 1):
            sequence = " ".join(mots[debut:debut + longueur])
            if sequence not in noms or sequence in trouvees:
                continue
            significatifs = [m for m in sequence.split() if len(m) >= 2]
            # Sans table de rarete, aucun mot n'est dit repandu : rien n'est
            # ecarte (meme repos que fiskr/rarete.profil_indisponible).
            if table is not None and table.total > 0 and all(table.repandu(m) for m in significatifs):
                continue
            if any(f" {sequence} " in d for d in deja):
                continue
            trouvees.append(sequence)
    return trouvees


def party_blocking_keys(party: Dict[str, Any], filtering_cfg: Dict[str, Any]) -> set:
    """
    Cles d'interrogation d'une partie de paiement, pour le layout du canal
    FILTERING.

    UNIFIE sur `blocking.lookup_blocking_keys`, comme l'index qu'elle
    interroge. L'implantation precedente reconstruisait les cles a la main et
    divergeait de l'index sur trois points, tous a perte :

    - AUCUNE TRANSLITTERATION. Le double metaphone ne connait que l'alphabet
      latin : sur « ВЛАДИМИР ПУТИН » il rendait une cle vide, la partie
      tombait dans le seau « XX » et n'etait candidate de RIEN. L'index, lui,
      translitterait. Une partie de paiement ecrite en cyrillique, en arabe ou
      en chinois etait donc structurellement inatteignable au filtrage — le
      meme defaut de nature que le trou « pays inconnu » corrige precedemment.
    - AUCUNE CLE D'EQUIVALENCE. Les tables linguistiques etaient inertes sur
      ce canal, quel que soit leur reglage.
    - AUCUNE CAPACITE DU MOTEUR. Une bascule posee dans blocking.py restait
      sans effet ici, ce qui aurait vide de sens le reglage « par canal ».

    Les proprietes propres au filtrage sont conservees : les deux natures PP
    et PM sont interrogees (la nature d'une partie est inconnue), et l'ordre
    des mots n'est pas suppose fiable — cf. `_name_rotations`.
    """
    from fiskr.blocking import lookup_blocking_keys
    keys: set = set()
    for variante in _name_rotations(party.get("name", "")):
        sonde = dict(party)
        sonde["name"] = variante
        for as_individual in (True, False):
            keys |= lookup_blocking_keys(
                _party_client_dict(sonde, as_individual, "filtrage"), filtering_cfg)
    return keys


def _party_candidates(party: Dict[str, Any],
                      index: Dict[str, Dict[str, Dict[str, Any]]],
                      filtering_cfg: Dict[str, Any],
                      allowed_lists: Optional[List[str]] = None,
                      derives: Optional[Dict[str, Any]] = None
                      ) -> Dict[str, Dict[str, Any]]:
    """
    Candidats de la watchlist pour une partie de paiement (index du filtrage).

    La restriction de listes s'applique ICI, sur les quelques candidats d'un
    seau, et non a la construction de l'index : celui-ci est bati une fois pour
    l'univers entier et partage par tous les messages. La restreindre en amont
    obligerait a reconstruire un index complet par combinaison de listes.

    Avec `derives`, les fiches qui portent le BIC ou le LEI de la partie sont
    candidates aussi, quel que soit leur nom : c'est le hard match du moteur
    qui tranche, et il ne peut trancher que ce qu'on lui presente.
    """
    candidates: Dict[str, Dict[str, Any]] = {}
    if party.get("name"):
        for key in party_blocking_keys(party, filtering_cfg):
            candidates.update(index.get(key, {}))
    if derives:
        compares = elements_compares(party)
        if "BIC" in compares:
            candidates.update(derives["bic"].get(bic_comparable(party["bic"])[:8], {}))
        if "LEI" in compares:
            candidates.update(derives["lei"].get(lei_comparable(party["lei"]), {}))
    if allowed_lists:
        permises = set(allowed_lists)
        candidates = {i: e for i, e in candidates.items()
                      if e.get("_list_type") in permises}
    return candidates


def _party_client_dict(party: Dict[str, Any], as_individual: bool, client_id: str) -> Dict[str, Any]:
    """Profil de criblage synthetique d'une partie de paiement (variante PP ou PM)."""
    country = [party["country"]] if party["country"] else []
    client: Dict[str, Any] = {
        "client_id": client_id,
        "client_gender": "U",
        # BIC (agents bancaires, AnyBIC des parties) et LEI : hard match
        # direct contre le bic_swift et le lei_number des fiches listees
        "client_bic": party.get("bic") or None,
        "client_lei_number": party.get("lei") or None,
        "client_countries": {
            "nationality": [], "residence": country,
            "birth_country": [party["birth_country"]] if party["birth_country"] else [],
            "registration_country": country,
        },
    }
    if as_individual:
        parsed = parse_individual_name(party["name"])
        client["client_type"] = "PP"
        client["client_first_name"] = parsed["first_name"]
        client["client_last_name"] = parsed["last_name"]
        client["client_maiden_name"] = ""
        if party["birth_date"]:
            client["client_dob"] = party["birth_date"]
    else:
        client["client_type"] = "PM"
        client["client_company_name"] = party["name"]
    return client


def _client_journal(client: Dict[str, Any], party: Dict[str, Any]) -> Dict[str, Any]:
    """Profil tel qu'ecrit au journal : une partie sans nom y figure sous ce
    qui l'identifie, pas sous « Inconnu ». Copie : `primary_name` est aussi un
    nom que le moteur compare, il ne doit pas entrer dans le profil crible."""
    if party.get("name"):
        return client
    return dict(client, primary_name=libelle_partie(party))


def _identifiants_affiches(party: Dict[str, Any]) -> List[str]:
    """Ce que la partie porte pour l'identifier, tel que l'operateur le relit."""
    return ([f"BIC {party['bic']}"] if party.get("bic") else []) \
        + ([f"LEI {party['lei']}"] if party.get("lei") else []) \
        + list(party.get("other_ids") or [])


_SANS_AJUSTEMENT = {
    "dob": {"score": 0.0, "description": "N/A"},
    "gender": {"score": 0.0, "description": "N/A"},
    "geography": {"score": 0.0, "description": "N/A"},
}


def _parties_texte_libre(parsed: Dict[str, Any], parties: List[Dict[str, Any]],
                         derives: Dict[str, Any],
                         allowed_lists: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """
    Une « partie » par nom SANCTION trouve dans les informations de remise.

    Elle suit le meme chemin qu'une partie du message — regles anti-faux
    positifs, journal, alerte adjudicable — parce qu'elle appelle la meme
    decision : un virement libelle « pour le compte de <liste> » met des fonds
    a disposition de ce liste aussi surement qu'un beneficiaire nomme.
    """
    deja = formes_criblees([p["name"] for p in parties if p.get("has_name")])
    par_sequence: Dict[str, Dict[str, Any]] = {}
    for rang, tx in enumerate(parsed["transactions"], start=1):
        texte = tx.get("remittance") or ""
        if not texte:
            continue
        for sequence in sequences_listees_du_texte(texte, derives, deja):
            candidats = {eid: e for eid, e in derives["noms"][sequence].items()
                         if not allowed_lists or e.get("_list_type") in allowed_lists}
            if not candidats:
                continue
            entree = par_sequence.setdefault(sequence, {
                "role_tag": "RmtInf", "role": ROLE_TEXTE_LIBRE, "roles": [ROLE_TEXTE_LIBRE],
                "name": sequence, "has_name": True, "country": "", "town": "", "address": "",
                "bic": "", "lei": "", "other_ids": [], "account": "",
                "birth_date": "", "birth_city": "", "birth_country": "", "is_agent": False,
                "free_text": {"field": "RmtInf/Ustrd", "sequence": sequence,
                              "excerpt": texte[:140], "transactions": [],
                              "candidates": candidats},
            })
            entree["free_text"]["transactions"].append(tx.get("end_to_end_id") or f"#{rang}")
    return list(par_sequence.values())


def _correspondance_texte_libre(party: Dict[str, Any], candidate: Dict[str, Any],
                                scoring_config: Dict[str, Any]) -> Dict[str, Any]:
    """Rapprochement d'une sequence du texte libre : EXACT par construction
    (nom complet liste, normalise), donc en alerte quel que soit le seuil."""
    texte = party["free_text"]
    return {
        "status": "ALERT", "base_score": 100.0, "final_score": 100.0,
        "best_client_name": party["name"],
        "best_watchlist_name": candidate.get("primary_name", ""),
        "adjustments": {k: {"score": 0.0, "description": "N/A (texte libre)"} for k in _SANS_AJUSTEMENT},
        "hard_match_triggered": False,
        "free_text_match": {"field": texte["field"], "sequence": texte["sequence"],
                            "excerpt": texte["excerpt"], "transactions": texte["transactions"]},
        "cut_off_applied": resolve_cut_off(scoring_config, candidate),
    }


def screen_payment_message(db, parsed: Dict[str, Any],
                           watchlist_index: Dict[str, List[Dict[str, Any]]],
                           watchlist_version: str, watchlist_hash: str,
                           username: str,
                           screening_lists: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Crible toutes les parties distinctes d'un message de paiement contre les
    listes en production (ou le sous-ensemble `screening_lists`, restriction
    tracee dans chaque ligne d'audit), ainsi que ses informations de remise.
    Chaque partie laisse une ligne d'audit — criblee, ou NON CRIBLEE avec le
    motif ; chaque hit ALERT ouvre une alerte de travail. Verdict global :
    `verdict_du_message` (HIT, INCOMPLETE ou PASS).

    Leve `MessageTropVolumineux` AVANT toute lecture de reglage et tout calcul
    quand le message porte plus de `MAX_PARTIES_PAR_MESSAGE` parties : rien de
    ce qui suit ne doit etre paye pour un message qui ne sera pas crible. Leve
    `MessageSansPartieCriblable` quand aucune partie ne porte rien de
    comparable : un verdict n'aurait alors porte sur rien.
    """
    parties = _distinct_parties(parsed)
    if len(parties) > MAX_PARTIES_PAR_MESSAGE:
        raise MessageTropVolumineux(len(parties), MAX_PARTIES_PAR_MESSAGE)
    if not any(motif_non_criblee(p) is None for p in parties):
        raise MessageSansPartieCriblable(len(parties))

    msg_id = parsed.get("msg_id") or "SANS-ID"
    party_results: List[Dict[str, Any]] = []
    en_alerte = False
    restriction = screening_lists or "ALL"
    # Layout de blocking du canal FILTERING (parametrable a chaud, defaut
    # phonetique seule) + index local construit une seule fois par message
    from fiskr.settings import (blocking_layout, blocking_config_for, scoring_config_with_thresholds,
                                perimeter_overrides)
    from fiskr.fprules import evaluate_fp_rules, build_filtering_ctx, annotate_suppression
    filtering_cfg = blocking_config_for(blocking_layout(db, "FILTERING"), channel="FILTERING")
    # Seuils de cut-off a chaud (reglage > config.yaml), memes regles qu'au criblage
    scoring_config = scoring_config_with_thresholds(db, channel="FILTERING")
    index = index_de_filtrage(watchlist_index, filtering_cfg, watchlist_hash)
    classement = perimeter_overrides(db)
    derives = index_derives(index, classement)
    remises = sum(1 for tx in parsed["transactions"] if tx.get("remittance"))
    a_cribler = parties + _parties_texte_libre(parsed, parties, derives, screening_lists)

    for idx, party in enumerate(a_cribler):
        client_id = f"TXN:{msg_id}:{idx}"
        texte_libre = party.get("free_text")
        motif = None if texte_libre else motif_non_criblee(party)
        if motif:
            # NON CRIBLEE, et dit : une partie absente du journal est une
            # partie dont personne ne saura qu'elle n'a pas ete regardee.
            non_criblee = {
                "status": STATUT_NON_CRIBLEE, "base_score": 0.0, "final_score": 0.0,
                "hard_match_triggered": False,
                "best_client_name": libelle_partie(party),
                "best_watchlist_name": "Non criblée",
                "not_screened": {"code": motif, "reason": MOTIFS_NON_CRIBLEE[motif],
                                 "identifiers": _identifiants_affiches(party),
                                 "roles": party["roles"]},
                "adjustments": _SANS_AJUSTEMENT,
                "cut_off_applied": resolve_cut_off(scoring_config),
                "screening_lists_restriction": restriction,
            }
            audit = log_compliance_decision(
                db, _client_journal(_party_client_dict(party, False, client_id), party),
                {"entity_id": "NONE", "primary_name": "Non criblée"},
                non_criblee, watchlist_version, watchlist_hash)
            party_results.append({
                "name": party["name"], "label": libelle_partie(party), "roles": party["roles"],
                "country": party["country"], "bic": party["bic"], "lei": party.get("lei", ""),
                "is_agent": party["is_agent"], "identifiers": _identifiants_affiches(party),
                "screened_on": [], "free_text": None, "candidates_count": 0,
                "status": STATUT_NON_CRIBLEE,
                "not_screened_reason": motif, "not_screened_label": MOTIFS_NON_CRIBLEE[motif],
                "final_score": 0.0, "best_watchlist_name": None, "best_watchlist_id": None,
                "list_type": None, "hard_match": False,
                "audit_id": audit.id, "alert_id": None,
            })
            continue
        if texte_libre:
            candidates = texte_libre["candidates"]
        else:
            candidates = _party_candidates(party, index, filtering_cfg, screening_lists, derives)

        best: Optional[Dict[str, Any]] = None
        best_client: Optional[Dict[str, Any]] = None
        # TOUTES les correspondances au-dessus du seuil, pas seulement la
        # meilleure : une partie de paiement porte rarement une date de
        # naissance ou un pays, donc l'homonymie y est la norme — et un hit
        # perdu reste un hit perdu, quel que soit le canal qui l'a produit.
        hits: List[Dict[str, Any]] = []
        for candidate in candidates.values():
            # Variante de profil alignee sur le type du candidat : les parties
            # d'un paiement ne portent pas leur nature PP/PM.
            as_individual = candidate.get("entity_type") == "I"
            client = _party_client_dict(party, as_individual, client_id)
            score = (_correspondance_texte_libre(party, candidate, scoring_config) if texte_libre
                     else match_entities(client, candidate, scoring_config))
            score["watchlist_entity"] = candidate
            if score.get("status") == "ALERT":
                hits.append((score, client))
            if best is None or score["final_score"] > best["final_score"]:
                best = score
                best_client = client

        alert_id = None
        suppressed_by_rule = None
        if best is not None:
            hits.sort(key=lambda t: -t[0]["final_score"])
            from fiskr.fprules import active_rules
            regles_actives = active_rules(db, "FILTERING") if hits else []

            a_journaliser = []
            for position, (hit, hit_client) in enumerate(hits, start=1):
                hit["screening_lists_restriction"] = restriction
                # Regles anti-faux positifs du canal FILTERING : appliquees
                # avant de tracer, pour marquer la decision dans le journal
                ctx = build_filtering_ctx(party, hit["watchlist_entity"], hit, parsed,
                                          client_id, hits_count=len(hits), hit_rank=position,
                                          perimeter_overrides=classement)
                hit["screening_perimeter"] = ctx["perimeter"]
                regle = evaluate_fp_rules(db, "FILTERING", ctx, rules=regles_actives)
                if regle is not None:
                    annotate_suppression(hit, regle)
                    if hit is best:
                        suppressed_by_rule = regle
                a_journaliser.append((hit, hit_client, regle))
            if not hits:
                best["screening_lists_restriction"] = restriction
                a_journaliser.append((best, best_client, None))

            lignes = log_compliance_decisions(
                db, _client_journal(best_client, party),
                [(m.get("watchlist_entity") or {}, m) for m, _c, _r in a_journaliser],
                watchlist_version, watchlist_hash, commit=False)
            audit = next((l for (m, _c, _r), l in zip(a_journaliser, lignes) if m is best),
                         lignes[0] if lignes else None)

            a_alerter = [{"audit": ligne, "match": m, "rule": r}
                         for (m, _c, r), ligne in zip(a_journaliser, lignes)
                         if m.get("status") == "ALERT"]
            if not a_alerter:
                db.commit()
            if a_alerter:
                en_alerte = True
                resultat_alertes = open_or_redetect_alerts(
                    db, a_alerter, client_id=client_id, username=username,
                    channel="FILTERING",
                    detail_suffix=(
                        f" [Filtrage transactionnel {parsed['message_type']} {msg_id} — "
                        f"rôle(s) : {', '.join(party['roles'])}]"
                        + (f" [Texte libre {texte_libre['field']} : « {texte_libre['excerpt']} »]"
                           if texte_libre else "")
                        + (f" [Criblage restreint aux listes : {', '.join(screening_lists)}]"
                           if screening_lists else "")
                    ),
                )
                for entree, identifiant in zip(a_alerter, resultat_alertes["alert_ids"]):
                    if entree["match"] is best:
                        alert_id = identifiant
                        break
        else:
            # Aucune partie n'echappe a la piste d'audit : prouver qu'une
            # partie A ETE criblee importe autant que le resultat (meme motif
            # que le criblage unitaire sans candidat). La ligne dit SUR QUOI :
            # une partie rapprochee sur son seul BIC n'a pas ete criblee sur
            # un nom qu'elle ne porte pas.
            no_match = {
                "status": "NO_MATCH", "base_score": 0.0, "final_score": 0.0,
                "hard_match_triggered": False,
                "best_client_name": libelle_partie(party),
                "best_watchlist_name": "Aucun candidat trouvé (Bloqué)",
                "compared_elements": elements_compares(party),
                "adjustments": _SANS_AJUSTEMENT,
                "cut_off_applied": resolve_cut_off(scoring_config),
                "screening_lists_restriction": restriction,
            }
            audit = log_compliance_decision(
                db, _client_journal(_party_client_dict(party, False, client_id), party),
                {"entity_id": "NONE", "primary_name": "Aucun match"},
                no_match, watchlist_version, watchlist_hash
            )
        audit_id = audit.id

        party_results.append({
            "name": party["name"],
            "label": libelle_partie(party),
            "roles": party["roles"],
            "country": party["country"],
            "bic": party["bic"],
            "lei": party.get("lei", ""),
            "is_agent": party["is_agent"],
            "identifiers": _identifiants_affiches(party),
            "screened_on": ["TEXTE_LIBRE"] if texte_libre else elements_compares(party),
            "free_text": ({k: v for k, v in texte_libre.items() if k != "candidates"}
                          if texte_libre else None),
            "candidates_count": len(candidates),
            "status": (best or {}).get("status", "NO_MATCH"),
            "final_score": (best or {}).get("final_score", 0.0),
            "best_watchlist_name": ((best or {}).get("watchlist_entity") or {}).get("primary_name"),
            "best_watchlist_id": ((best or {}).get("watchlist_entity") or {}).get("entity_id"),
            "list_type": ((best or {}).get("watchlist_entity") or {}).get("_list_type"),
            "hard_match": (best or {}).get("hard_match_triggered", False),
            "audit_id": audit_id,
            "alert_id": alert_id,
        })

    hits = [p for p in party_results if p["status"] == "ALERT"]
    non_criblees = sum(1 for p in party_results if p["status"] == STATUT_NON_CRIBLEE)
    manques = manques_de_completude(parsed)
    verdict = verdict_du_message(en_alerte, non_criblees, manques)
    logger.info(
        f"Filtrage transactionnel {parsed['message_type']} {msg_id} : "
        f"{len(party_results) - non_criblees} partie(s) criblée(s), {non_criblees} non criblée(s), "
        f"{len(manques)} information(s) obligatoire(s) manquante(s), {len(hits)} hit(s) — verdict {verdict}."
    )
    return {
        "verdict": verdict,
        "verdict_label": VERDICTS[verdict],
        "message": {k: v for k, v in parsed.items() if k != "transactions"},
        "transactions_count": len(parsed["transactions"]),
        "parties": party_results,
        "hits_count": len(hits),
        "not_screened_count": non_criblees,
        "missing_information": manques,
        "free_text": {"screened": remises,
                      "hits": sum(1 for p in hits if p.get("free_text"))},
        "screening_lists": restriction,
    }

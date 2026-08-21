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
"""
import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

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
AGENT_ROLES = {
    "DbtrAgt": "Banque du donneur d'ordre",
    "CdtrAgt": "Banque du bénéficiaire",
    "InstgAgt": "Agent instructeur",
    "InstdAgt": "Agent instruit",
    "IntrmyAgt1": "Banque intermédiaire",
}

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


def _extract_party(elem, role_tag: str) -> Optional[Dict[str, Any]]:
    """Extrait une partie (Nm, adresse, pays, date/pays de naissance)."""
    if elem is None:
        return None
    name = _child_text(elem, "Nm")
    if not name:
        return None
    postal = _child(elem, "PstlAdr")
    country = _child_text(postal, "Ctry") if postal is not None else ""
    address = ""
    if postal is not None:
        lines = [(c.text or "").strip() for c in postal if _local(c) == "AdrLine"]
        address = ", ".join(l for l in lines if l)
    birth = _child(elem, "Id", "PrvtId", "DtAndPlcOfBirth")
    return {
        "role_tag": role_tag,
        "role": PARTY_ROLES.get(role_tag, role_tag),
        "name": _nom_borne(name),
        "country": country.upper(),
        "address": address,
        "bic": "",
        "birth_date": _child_text(birth, "BirthDt") if birth is not None else "",
        "birth_country": (_child_text(birth, "CtryOfBirth") if birth is not None else "").upper(),
        "is_agent": False,
    }


def _extract_agent(elem, role_tag: str) -> Optional[Dict[str, Any]]:
    """Extrait un agent financier (FinInstnId : BICFI/BIC + Nm)."""
    if elem is None:
        return None
    fin = _child(elem, "FinInstnId")
    if fin is None:
        return None
    bic = _child_text(fin, "BICFI") or _child_text(fin, "BIC")
    name = _child_text(fin, "Nm")
    if not bic and not name:
        return None
    country = _child_text(fin, "PstlAdr", "Ctry") or (bic[4:6] if len(bic) >= 6 else "")
    return {
        "role_tag": role_tag,
        "role": AGENT_ROLES.get(role_tag, role_tag),
        "name": _nom_borne(name or bic),
        "country": country.upper(),
        "address": "",
        "bic": bic,
        "birth_date": "",
        "birth_country": "",
        "is_agent": True,
    }


def _collect_parties(container, into: List[Dict[str, Any]]) -> None:
    """Parcourt les enfants directs d'un bloc (PmtInf ou CdtTrfTxInf) pour en extraire les parties."""
    for child in container:
        tag = _local(child)
        if tag in PARTY_ROLES:
            party = _extract_party(child, tag)
            if party:
                into.append(party)
        elif tag in AGENT_ROLES:
            agent = _extract_agent(child, tag)
            if agent:
                into.append(agent)


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
                    "remittance": _child_text(tx, "RmtInf", "Ustrd"),
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
                "remittance": _child_text(tx, "RmtInf", "Ustrd"),
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


def _distinct_parties(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Deduplique les parties du message par (nom, pays, BIC), roles agreges."""
    seen: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for tx in parsed["transactions"]:
        for party in tx["parties"]:
            key = (party["name"].upper(), party["country"], party["bic"])
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
# La signature couvre TOUT ce qui change les cles : l'empreinte des listes en
# production, le layout du canal, les capacites du moteur (translitteration,
# phonetique) et les equivalences linguistiques actives. La restriction de
# listes n'y figure PAS : elle s'applique a la selection des candidats, pas a
# la construction de l'index.
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
                      allowed_lists: Optional[List[str]] = None
                      ) -> Dict[str, Dict[str, Any]]:
    """
    Candidats de la watchlist pour une partie de paiement (index du filtrage).

    La restriction de listes s'applique ICI, sur les quelques candidats d'un
    seau, et non a la construction de l'index : celui-ci est bati une fois pour
    l'univers entier et partage par tous les messages. La restreindre en amont
    obligerait a reconstruire un index complet par combinaison de listes.
    """
    candidates: Dict[str, Dict[str, Any]] = {}
    for key in party_blocking_keys(party, filtering_cfg):
        candidates.update(index.get(key, {}))
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
        # BIC de la partie (agents bancaires) : hard match direct contre le
        # champ bic_swift des institutions financieres sanctionnees
        "client_bic": party.get("bic") or None,
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


def screen_payment_message(db, parsed: Dict[str, Any],
                           watchlist_index: Dict[str, List[Dict[str, Any]]],
                           watchlist_version: str, watchlist_hash: str,
                           username: str,
                           screening_lists: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Crible toutes les parties distinctes d'un message de paiement contre les
    listes en production (ou le sous-ensemble `screening_lists`, restriction
    tracee dans chaque ligne d'audit). Chaque partie criblee laisse une ligne
    d'audit ; chaque hit ALERT ouvre une alerte de travail. Verdict global :
    HIT des qu'une partie est en alerte, PASS sinon.

    Leve `MessageTropVolumineux` AVANT toute lecture de reglage et tout calcul
    quand le message porte plus de `MAX_PARTIES_PAR_MESSAGE` parties : rien de
    ce qui suit ne doit etre paye pour un message qui ne sera pas crible.
    """
    parties = _distinct_parties(parsed)
    if len(parties) > MAX_PARTIES_PAR_MESSAGE:
        raise MessageTropVolumineux(len(parties), MAX_PARTIES_PAR_MESSAGE)

    msg_id = parsed.get("msg_id") or "SANS-ID"
    party_results: List[Dict[str, Any]] = []
    verdict = "PASS"
    restriction = screening_lists or "ALL"
    # Layout de blocking du canal FILTERING (parametrable a chaud, defaut
    # phonetique seule) + index local construit une seule fois par message
    from fiskr.settings import blocking_layout, blocking_config_for, scoring_config_with_thresholds
    from fiskr.fprules import evaluate_fp_rules, build_filtering_ctx, annotate_suppression
    filtering_cfg = blocking_config_for(blocking_layout(db, "FILTERING"), channel="FILTERING")
    # Seuils de cut-off a chaud (reglage > config.yaml), memes regles qu'au criblage
    scoring_config = scoring_config_with_thresholds(db, channel="FILTERING")
    index = index_de_filtrage(watchlist_index, filtering_cfg, watchlist_hash)

    for idx, party in enumerate(parties):
        client_id = f"TXN:{msg_id}:{idx}"
        candidates = _party_candidates(party, index, filtering_cfg, screening_lists)

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
            score = match_entities(client, candidate, scoring_config)
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
            from fiskr.settings import perimeter_overrides
            regles_actives = active_rules(db, "FILTERING") if hits else []
            classement = perimeter_overrides(db) if hits else {}

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
                db, best_client,
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
                verdict = "HIT"
                resultat_alertes = open_or_redetect_alerts(
                    db, a_alerter, client_id=client_id, username=username,
                    channel="FILTERING",
                    detail_suffix=(
                        f" [Filtrage transactionnel {parsed['message_type']} {msg_id} — "
                        f"rôle(s) : {', '.join(party['roles'])}]"
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
            # que le criblage unitaire sans candidat).
            no_match = {
                "status": "NO_MATCH", "base_score": 0.0, "final_score": 0.0,
                "hard_match_triggered": False,
                "best_client_name": party["name"],
                "best_watchlist_name": "Aucun candidat trouvé (Bloqué)",
                "adjustments": {
                    "dob": {"score": 0.0, "description": "N/A"},
                    "gender": {"score": 0.0, "description": "N/A"},
                    "geography": {"score": 0.0, "description": "N/A"},
                },
                "cut_off_applied": resolve_cut_off(scoring_config),
                "screening_lists_restriction": restriction,
            }
            audit = log_compliance_decision(
                db, _party_client_dict(party, False, client_id),
                {"entity_id": "NONE", "primary_name": "Aucun match"},
                no_match, watchlist_version, watchlist_hash
            )
        audit_id = audit.id

        party_results.append({
            "name": party["name"],
            "roles": party["roles"],
            "country": party["country"],
            "bic": party["bic"],
            "is_agent": party["is_agent"],
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
    logger.info(
        f"Filtrage transactionnel {parsed['message_type']} {msg_id} : "
        f"{len(party_results)} partie(s) criblée(s), {len(hits)} hit(s) — verdict {verdict}."
    )
    return {
        "verdict": verdict,
        "message": {k: v for k, v in parsed.items() if k != "transactions"},
        "transactions_count": len(parsed["transactions"]),
        "parties": party_results,
        "hits_count": len(hits),
        "screening_lists": restriction,
    }

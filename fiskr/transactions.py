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
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from fiskr.config import config
from fiskr.database import log_compliance_decision
from fiskr.names import parse_individual_name
from fiskr.phonetics import double_metaphone
from fiskr.scoring import match_entities, resolve_cut_off
from fiskr.alerts import open_or_redetect_alert

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
        "name": name,
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
        "name": name or bic,
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


def _phonetic_keys(name: str) -> set:
    keys = set()
    for word in re.split(r"[\s\-]+", (name or "").strip()):
        if not word:
            continue
        p_key, s_key = double_metaphone(word)
        if p_key:
            keys.add(p_key)
        if s_key:
            keys.add(s_key)
    return keys


def _phonetic_entity_map(watchlist_index: Dict[str, List[Dict[str, Any]]]
                         ) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """
    Inverse l'index de blocking UNE SEULE FOIS par message : les cles de
    blocking sont de la forme PAYS_TYPE_PHONETIQUE ; le pays et le type sont
    ignores (donnees de paiement trop pauvres pour filtrer dessus), seule la
    composante phonetique sert de cle de recherche.
    """
    phon_map: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for key, items in watchlist_index.items():
        parts = key.split("_")
        if len(parts) != 3:
            continue
        bucket = phon_map.setdefault(parts[2], {})
        for item in items:
            bucket[item["entity_id"]] = item
    return phon_map


def _party_candidates(party: Dict[str, Any],
                      phon_map: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """
    Candidats de la watchlist pour une partie de paiement : la phonetique est
    comparee a TOUS les mots du nom de la partie, l'ordre des mots d'un champ
    libre de paiement n'etant pas fiable.
    """
    candidates: Dict[str, Dict[str, Any]] = {}
    for phon in _phonetic_keys(party["name"]):
        candidates.update(phon_map.get(phon, {}))
    return candidates


def _party_client_dict(party: Dict[str, Any], as_individual: bool, client_id: str) -> Dict[str, Any]:
    """Profil de criblage synthetique d'une partie de paiement (variante PP ou PM)."""
    country = [party["country"]] if party["country"] else []
    client: Dict[str, Any] = {
        "client_id": client_id,
        "client_gender": "U",
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
                           username: str) -> Dict[str, Any]:
    """
    Crible toutes les parties distinctes d'un message de paiement contre les
    listes en production. Chaque partie criblee laisse une ligne d'audit ;
    chaque hit ALERT ouvre une alerte de travail. Verdict global : HIT des
    qu'une partie est en alerte, PASS sinon.
    """
    msg_id = parsed.get("msg_id") or "SANS-ID"
    party_results: List[Dict[str, Any]] = []
    verdict = "PASS"
    phon_map = _phonetic_entity_map(watchlist_index)

    for idx, party in enumerate(_distinct_parties(parsed)):
        client_id = f"TXN:{msg_id}:{idx}"
        candidates = _party_candidates(party, phon_map)

        best: Optional[Dict[str, Any]] = None
        best_client: Optional[Dict[str, Any]] = None
        for candidate in candidates.values():
            # Variante de profil alignee sur le type du candidat : les parties
            # d'un paiement ne portent pas leur nature PP/PM.
            as_individual = candidate.get("entity_type") == "I"
            client = _party_client_dict(party, as_individual, client_id)
            score = match_entities(client, candidate, config)
            score["watchlist_entity"] = candidate
            if best is None or score["final_score"] > best["final_score"]:
                best = score
                best_client = client

        alert_id = None
        if best is not None:
            audit = log_compliance_decision(db, best_client, best["watchlist_entity"],
                                            best, watchlist_version, watchlist_hash)
            if best.get("status") == "ALERT":
                verdict = "HIT"
                alert_id = open_or_redetect_alert(
                    db, audit, client_id, best, username,
                    detail_suffix=(
                        f" [Filtrage transactionnel {parsed['message_type']} {msg_id} — "
                        f"rôle(s) : {', '.join(party['roles'])}]"
                    ),
                )
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
                "cut_off_applied": resolve_cut_off(config),
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
    }

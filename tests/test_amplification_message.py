"""
Amplification d'entrée du filtrage transactionnel.

Un message ISO 20022 accepté jusqu'au plafond de téléversement (8 Mo) porte
**56 678 transactions** — mesuré sur un pain.001 minimal réel, 148 octets par
transaction — donc autant de parties distinctes à cribler. Chacune interroge
l'index de filtrage et compare ses candidats : sur la production, un seau
phonétique porte **415 fiches en moyenne** (25 906 pour le plus gros) à
~180 µs la comparaison, soit ~75 ms par partie. Plus d'une heure de calcul pour
un seul message, dans une requête HTTP synchrone, avec autant de lignes écrites
au journal d'audit immuable.

Cette requête échouait **déjà** sur le délai d'attente du serveur — mais après
avoir brûlé ce temps et écrit ces lignes. Le refus est désormais immédiat et dit
quoi faire.
"""
import pytest

from fiskr.transactions import (MAX_PARTIES_PAR_MESSAGE, MessageTropVolumineux,
                                _distinct_parties, parse_iso20022_payment,
                                screen_payment_message)

_ENTETE = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09">'
           '<CstmrCdtTrfInitn><GrpHdr><MsgId>M1</MsgId></GrpHdr><PmtInf>'
           '<Dbtr><Nm>IVAN IVANOV</Nm></Dbtr>')
_TX = ('<CdtTrfTxInf><PmtId><EndToEndId>E{i}</EndToEndId></PmtId>'
       '<Amt><InstdAmt Ccy="EUR">1</InstdAmt></Amt>'
       '<Cdtr><Nm>BENEFICIAIRE {i}</Nm></Cdtr></CdtTrfTxInf>')
_PIED = '</PmtInf></CstmrCdtTrfInitn></Document>'


def _message(nb_transactions: int) -> bytes:
    return (_ENTETE + "".join(_TX.format(i=i) for i in range(nb_transactions))
            + _PIED).encode()


def test_la_densite_mesuree_tient_dans_le_plafond_de_televersement():
    """La mesure qui motive la borne : combien de parties tiennent dans 8 Mo."""
    from fiskr.api import TAILLE_MAX_TELEVERSEMENT
    octets_par_tx = len(_TX.format(i=1))
    tiennent = (TAILLE_MAX_TELEVERSEMENT["message"] - len(_ENTETE) - len(_PIED)) // octets_par_tx
    assert tiennent > 50_000, (
        f"{tiennent} transactions dans le plafond : la borne sur les parties "
        "n'a plus lieu d'être, ou le plafond a changé")


def test_un_message_ordinaire_passe():
    parsed = parse_iso20022_payment(_message(20))
    parties = _distinct_parties(parsed)
    assert 0 < len(parties) <= MAX_PARTIES_PAR_MESSAGE


def test_un_message_au_dela_de_la_borne_est_refuse_avant_tout_calcul():
    parsed = parse_iso20022_payment(_message(MAX_PARTIES_PAR_MESSAGE + 50))
    with pytest.raises(MessageTropVolumineux) as exc:
        # `db` et l'index ne sont jamais touchés : le refus tombe avant.
        screen_payment_message(None, parsed, {}, "v", "h", "op")
    assert exc.value.parties > MAX_PARTIES_PAR_MESSAGE
    assert exc.value.plafond == MAX_PARTIES_PAR_MESSAGE
    # Le message dit combien, et quoi faire.
    assert str(exc.value.parties) in str(exc.value)
    assert "Découpez" in str(exc.value)


def test_exactement_a_la_borne_le_message_est_accepte():
    """La borne est un maximum inclusif : refuser à N-1 serait un piège."""
    parsed = parse_iso20022_payment(_message(MAX_PARTIES_PAR_MESSAGE - 1))
    assert len(_distinct_parties(parsed)) == MAX_PARTIES_PAR_MESSAGE
    # Aucun refus : l'appel échoue plus loin, sur la base absente, pas ici.
    with pytest.raises(Exception) as exc:
        screen_payment_message(None, parsed, {}, "v", "h", "op")
    assert not isinstance(exc.value, MessageTropVolumineux)


def test_l_endpoint_refuse_en_413_et_pas_en_500():
    """Un 500 laisserait croire à un défaut du serveur : c'est le message
    qui est hors limites, et l'opérateur peut agir."""
    import uuid
    from fastapi.testclient import TestClient
    from fiskr.api import app
    from fiskr.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": f"amp{uuid.uuid4().hex[:4]}", "full_name": "amp",
        "role": "admin", "roles": ["admin"]}
    try:
        with TestClient(app) as client:
            reponse = client.post(
                "/api/transactions/screen",
                files={"file": ("gros.xml", _message(MAX_PARTIES_PAR_MESSAGE + 50),
                                "application/xml")})
        assert reponse.status_code == 413, reponse.text
        assert "Découpez" in reponse.json()["detail"]
    finally:
        app.dependency_overrides.pop(get_current_user, None)

"""
Un virement lié à un territoire sous embargo de l'Union ne passe plus PASS.

Règlement (UE) n° 692/2014 (Crimée et Sébastopol, 25 juin 2014) ; règlement
(UE) 2022/263 (zones non contrôlées par le gouvernement des oblasts de
Donetsk et de Louhansk, 24 février 2022), étendu à Kherson et Zaporija par le
règlement (UE) 2022/1903 (7 octobre 2022). Ces régimes interdisent des
opérations liées à un TERRITOIRE, sans nom de personne désignée : le filtrage,
qui ne crible que des noms, rendait PASS un virement à une société domiciliée à
Simferopol. Le signal est distinct d'un HIT (verdict TERRITORY), hors du
moteur de score, et lit une table DATÉE.

Haute précision : les homonymes réels (boulevard de Sébastopol, Sebastopol en
Californie, Donetsk de l'oblast de Rostov, Kramatorsk dans l'oblast de
Donetsk sous contrôle du gouvernement, Zaporija ville) ne déclenchent pas.

Écrits contre le comportement (verdict, signaux de la réponse) : sur le code
d'avant, chacun échoue sur son assertion.
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from fiskr import rarete, transactions
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.config import config
from fiskr.transactions import parse_iso20022_payment, screen_payment_message
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fiskr.database import AuditTrail, Base


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'territoires.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def univers_au_repos():
    table = rarete.table_courante()
    transactions.invalider_index_de_filtrage()
    rarete.installer(None)
    yield
    transactions.invalider_index_de_filtrage()
    rarete.installer(table)


PUTIN = {
    "entity_id": "TEST-PUTIN", "entity_type": "I", "primary_name": "VLADIMIR PUTIN",
    "individual_name_parsed": {"first_name": "", "last_name": "VLADIMIR PUTIN", "maiden_name": ""},
    "aliases": {"high_priority": [], "low_priority": []}, "dates_of_birth": [], "gender": "U",
    "countries": {"citizenship": [], "residence": [], "birth_country": [], "jurisdiction_country": []},
    "_list_type": "WATCHLIST_DGT",
}


def _pacs(cdtr_adr="<TwnNm>Hamburg</TwnNm><Ctry>DE</Ctry>", ustrd="Facture 2026-114 loyer octobre",
          date="2026-10-20", cdtr_nm="Qwyjibo Holdings GmbH"):
    """pacs.008 SEPA complet, qui rend PASS par défaut ; on change l'adresse
    du bénéficiaire, la remise ou la date de règlement."""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
 <FIToFICstmrCdtTrf>
  <GrpHdr><MsgId>MSG-{uuid.uuid4().hex[:8]}</MsgId><CreDtTm>{date}T10:00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
  <CdtTrfTxInf>
   <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>
   <IntrBkSttlmAmt Ccy="EUR">12000.00</IntrBkSttlmAmt>
   <IntrBkSttlmDt>{date}</IntrBkSttlmDt>
   <DbtrAgt><FinInstnId><BICFI>BNPAFRPP</BICFI></FinInstnId></DbtrAgt>
   <CdtrAgt><FinInstnId><BICFI>DEUTDEFF</BICFI></FinInstnId></CdtrAgt>
   <Dbtr><Nm>Blorptech Vexatron SARL</Nm><PstlAdr><TwnNm>Lyon</TwnNm><Ctry>FR</Ctry></PstlAdr></Dbtr>
   <DbtrAcct><Id><IBAN>FR7630006000011234567890189</IBAN></Id></DbtrAcct>
   <Cdtr><Nm>{cdtr_nm}</Nm><PstlAdr>{cdtr_adr}</PstlAdr></Cdtr>
   <CdtrAcct><Id><IBAN>DE89370400440532013000</IBAN></Id></CdtrAcct>
   <RmtInf><Ustrd>{ustrd}</Ustrd></RmtInf>
  </CdtTrfTxInf>
 </FIToFICstmrCdtTrf>
</Document>"""


def _filtrer(db, xml):
    parsed = parse_iso20022_payment(xml.encode("utf-8"))
    return screen_payment_message(db, parsed, {"K": [PUTIN]}, "v", f"h-{uuid.uuid4().hex}", "tester")


def _signaux(resultat):
    return resultat.get("territory_signals") or []


def test_un_beneficiaire_domicilie_a_simferopol_rend_territory_et_non_hit(db):
    temoin = _filtrer(db, _pacs())
    assert temoin["verdict"] == "PASS" and _signaux(temoin) == []
    resultat = _filtrer(db, _pacs(cdtr_adr="<TwnNm>Simferopol</TwnNm><Ctry>UA</Ctry>"
                                           "<AdrLine>ul. Kievskaya 10</AdrLine>"))
    assert resultat["verdict"] == "TERRITORY"
    assert "pas une correspondance de sanction" in resultat["verdict_label"]
    [signal] = _signaux(resultat)
    assert signal["territory"] == "CRIMEE_SEBASTOPOL" and signal["place"] == "Simferopol"
    assert signal["regime"].startswith("Règlement (UE) n° 692/2014")
    assert signal["field"] == "Cdtr/PstlAdr/TwnNm" and signal["role"] == "Bénéficiaire"
    assert signal["in_force_since"] == "2014-06-25" and signal["operation_date"] == "2026-10-20"
    # Hors du moteur de score : aucune partie en alerte, aucune alerte, rien au journal
    assert resultat["hits_count"] == 0
    assert all(p["status"] == "NO_MATCH" and p["alert_id"] is None for p in resultat["parties"])
    assert not any("territ" in str(l.decision_tree).lower() for l in db.query(AuditTrail).all())


def test_simferopol_dans_un_libelle_banal_signale_aussi(db):
    """RÈGLE : un lieu couvert écrit dans la remise signale, même quand aucune
    partie n'y est domiciliée. C'est souvent la SEULE trace du lien : une
    société chypriote qui paie des marchandises de Crimée (art. 2), un séjour
    à l'hôtel (services touristiques, art. 2 quinquies). Un don à un
    particulier n'est pas interdit — c'est à l'opérateur de le dire : le signal
    s'instruit, il n'ouvre pas d'alerte. Seules les formes NON ambiguës sont
    lues dans le texte libre."""
    resultat = _filtrer(db, _pacs(ustrd="Cadeau anniversaire Olga, Simferopol"))
    assert resultat["verdict"] == "TERRITORY"
    [signal] = _signaux(resultat)
    assert signal["field"] == "RmtInf/Ustrd" and signal["place"] == "Simferopol"


def test_lugansk_en_libelle_de_facture_signale(db):
    resultat = _filtrer(db, _pacs(ustrd="Facture 2026-31 fournisseur Lugansk, charbon"))
    assert [s["territory"] for s in _signaux(resultat)] == ["DONETSK_LOUHANSK"]


# (piège, adresse et remise ; vrai lien, adresse et remise) : chaque piège est
# un homonyme réel, et son jumeau prouve que la lecture discrimine — un
# lecteur qui ne signale jamais rien passerait le premier volet seul.
@pytest.mark.parametrize("piege, vrai", [
    (("<TwnNm>Paris</TwnNm><Ctry>FR</Ctry><AdrLine>12 boulevard de Sébastopol</AdrLine>", "Loyer bd de Sébastopol"),
     ("<TwnNm>Sébastopol</TwnNm><Ctry>UA</Ctry>", "Loyer octobre")),
    (("<TwnNm>Paris</TwnNm><Ctry>FR</Ctry><AdrLine>45 rue de Crimée</AdrLine>", "Livre : la guerre de Crimée"),
     ("<TwnNm>Paris</TwnNm><Ctry>FR</Ctry>", "Livraison vins de Crimea")),
    (("<TwnNm>Sebastopol</TwnNm><CtrySubDvsn>California</CtrySubDvsn><Ctry>US</Ctry>", "Sebastopol winery order"),
     ("<TwnNm>Sebastopol</TwnNm><Ctry>RU</Ctry>", "Winery order")),
    (("<TwnNm>London</TwnNm><Ctry>GB</Ctry><AdrLine>7 Sebastopol Road</AdrLine>", "Crimean War memorial donation"),
     ("<TwnNm>London</TwnNm><Ctry>GB</Ctry><AdrLine>7 Sevastopol</AdrLine>", "Donation")),
    (("<TwnNm>Donetsk</TwnNm><CtrySubDvsn>Rostovskaya oblast</CtrySubDvsn><Ctry>RU</Ctry>", "Transport Rostov"),
     ("<TwnNm>Donetsk</TwnNm><Ctry>RU</Ctry>", "Transport")),
    (("<TwnNm>Kramatorsk</TwnNm><CtrySubDvsn>Donetsk Oblast</CtrySubDvsn><Ctry>UA</Ctry>", "Oplata za antratsit"),
     ("<TwnNm>Antratsyt</TwnNm><Ctry>UA</Ctry>", "Oplata za ugol")),
    (("<TwnNm>Zaporizhzhia</TwnNm><Ctry>UA</Ctry>", "Sudak fillets 200 kg"),
     ("<TwnNm>Melitopol</TwnNm><Ctry>UA</Ctry>", "Fillets 200 kg")),
    (("<TwnNm>Kherson</TwnNm><Ctry>UA</Ctry>", "Livre : la conférence de Yalta"),
     ("<TwnNm>Kyiv</TwnNm><Ctry>UA</Ctry>", "Séjour hôtel Yalta")),
    (("<TwnNm>Moskva</TwnNm><Ctry>RU</Ctry><AdrLine>Sevastopolsky prospekt 28</AdrLine>", "Facture Q3"),
     ("<TwnNm>Moskva</TwnNm><Ctry>RU</Ctry><AdrLine>Simferopol, prospekt Kirova 28</AdrLine>", "Facture Q3")),
])
def test_les_homonymes_ne_declenchent_pas_et_leur_jumeau_si(db, piege, vrai):
    resultat = _filtrer(db, _pacs(cdtr_adr=piege[0], ustrd=piege[1]))
    assert _signaux(resultat) == [], _signaux(resultat)
    assert resultat["verdict"] == "PASS"
    assert _filtrer(db, _pacs(cdtr_adr=vrai[0], ustrd=vrai[1]))["verdict"] == "TERRITORY"


def test_une_adresse_a_lugansk_avant_l_entree_en_vigueur_ne_signale_pas(db):
    """La table est DATÉE : le règlement 2022/263 s'applique au 24 février
    2022. Le même paiement daté de 2021 n'est pas couvert — et la réponse dit
    ce que la date a écarté, pour qu'une date fausse ne passe pas en silence."""
    adresse = "<TwnNm>Lugansk</TwnNm><Ctry>UA</Ctry>"
    apres = _filtrer(db, _pacs(cdtr_adr=adresse, date="2022-03-01"))
    assert apres["verdict"] == "TERRITORY"
    avant = _filtrer(db, _pacs(cdtr_adr=adresse, date="2021-06-01"))
    assert _signaux(avant) == [] and avant["verdict"] == "PASS"
    assert [(e["place"], e["in_force_since"], e["operation_date"]) for e in avant["territory_set_aside"]] \
        == [("Louhansk", "2022-02-24", "2021-06-01")]


def test_une_ville_reprise_cesse_d_etre_couverte(db):
    """Kherson : zone non contrôlée au 7 octobre 2022 (règlement 2022/1903),
    reprise par le gouvernement le 11 novembre 2022."""
    adresse = "<TwnNm>Kherson</TwnNm><Ctry>UA</Ctry>"
    assert _filtrer(db, _pacs(cdtr_adr=adresse, date="2022-10-20"))["verdict"] == "TERRITORY"
    assert _filtrer(db, _pacs(cdtr_adr=adresse, date="2026-10-20"))["verdict"] == "PASS"


@pytest.mark.parametrize("adresse", [
    "<TwnNm>Симферополь</TwnNm><Ctry>RU</Ctry>",
    "<AdrLine>Respublika Krym, g. Simferopol', ul. Lenina 5</AdrLine>",
    "<TwnNm>Sébastopol</TwnNm><Ctry>UA</Ctry>",
    "<TwnNm>Luhans'k</TwnNm>",
    "<TwnNm>Nova Kakhovka</TwnNm><CtrySubDvsn>Khersonska oblast</CtrySubDvsn><Ctry>UA</Ctry>",
    "<CtrySubDvsn>UA-43</CtrySubDvsn><Ctry>UA</Ctry><TwnNm>Saky</TwnNm>",
])
def test_ecritures_et_translitterations_sont_lues(db, adresse):
    assert _filtrer(db, _pacs(cdtr_adr=adresse))["verdict"] == "TERRITORY"


def test_un_hit_de_sanction_l_emporte_et_le_signal_reste_dit(db):
    resultat = _filtrer(db, _pacs(cdtr_nm="Vladimir Putin", cdtr_adr="<TwnNm>Yalta</TwnNm><Ctry>RU</Ctry>"))
    assert resultat["verdict"] == "HIT"
    assert [s["place"] for s in _signaux(resultat)] == ["Yalta"]


def test_la_table_se_surcharge_a_chaud_et_refuse_ce_qu_elle_ne_sait_pas_lire(db):
    """Comme fiskr/country_risk.py : un bloc de config.yaml remplace un
    territoire par son code. Une entrée illisible est REFUSÉE et nommée —
    jamais lue de travers, jamais remplacée en silence."""
    original = config.get("territoires_sous_embargo")
    try:
        config["territoires_sous_embargo"] = {"as_of": "2027-01-01", "territoires": {
            "KHERSON_ZAPORIJIA": {"nom": "Zones", "regime": "R", "depuis": "2022-10-07",
                                  "lieux": [{"nom": "Zaporija", "variantes": ["Zaporizhzhia"]}]},
            "INVENTE": {"nom": "X", "regime": "Y", "depuis": "07/10/2022", "lieux": []},
        }}
        resultat = _filtrer(db, _pacs(cdtr_adr="<TwnNm>Zaporizhzhia</TwnNm><Ctry>UA</Ctry>"))
        assert resultat["verdict"] == "TERRITORY" and resultat["territory_table_as_of"] == "2027-01-01"
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "username": "a", "role": "auditor",
                                                              "roles": ["auditor"]}
        reference = TestClient(app).get("/api/embargo-territories").json()
        assert [e for e in reference["errors"] if e.startswith("INVENTE")]
        assert "INVENTE" not in {t["code"] for t in reference["territories"]}
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        if original is None:
            config.pop("territoires_sous_embargo", None)
        else:
            config["territoires_sous_embargo"] = original
    assert _filtrer(db, _pacs(cdtr_adr="<TwnNm>Zaporizhzhia</TwnNm><Ctry>UA</Ctry>"))["verdict"] == "PASS"

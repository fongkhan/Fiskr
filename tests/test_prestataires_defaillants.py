"""
Un prestataire qui omet de façon répétée les informations requises est vu.

Règlement (UE) 2023/1113, art. 8 §2 : « lorsqu'un prestataire de services de
paiement omet de manière répétée de fournir les informations requises », le
prestataire du bénéficiaire prend des dispositions (avertissements et
échéances, puis rejet, restriction ou fin de la relation) et « déclare cette
omission et les dispositions prises à l'autorité compétente ». Le filtrage
rendait INCOMPLETE chaque virement incomplet, un par un, sans garder la trace
de QUI omettait : cent virements incomplets du même correspondant étaient
cent incidents sans lien, et la déclaration ne pouvait pas être écrite.

Les tests passent par le comportement — verdict, réponse du filtrage,
endpoints du suivi et du signalement —, pas par les noms des nouveaux
modules : sur le code d'avant, chacun échoue sur son assertion.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from fiskr import rarete, transactions
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import Base, get_db
from fiskr.settings import set_setting
from fiskr.transactions import parse_iso20022_payment, screen_payment_message

ADMIN = {"id": 1, "username": "admin_psp", "role": "admin", "roles": ["admin"]}
INSTRUCTEUR = "QWERUS33XXX"          # prestataire hors Union qui nous transmet
BANQUE_DU_DONNEUR = "ASDFINBBXXX"
CLE_CRITERES = "filtrage.prestataires_defaillants"


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'prestataires.sqlite3'}")
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


@pytest.fixture
def notifications(monkeypatch):
    """Ce que le produit notifie, capturé à la source (fiskr.notifier.emit)."""
    from fiskr import notifier
    emises = []
    monkeypatch.setattr(notifier, "emit", lambda db, cle, charge=None, **k: emises.append((cle, charge)))
    return emises


@pytest.fixture
def api(db):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)


PUTIN = {
    "entity_id": "TEST-PUTIN", "entity_type": "I", "primary_name": "VLADIMIR PUTIN",
    "individual_name_parsed": {"first_name": "", "last_name": "VLADIMIR PUTIN", "maiden_name": ""},
    "aliases": {"high_priority": [], "low_priority": []}, "dates_of_birth": [], "gender": "U",
    "countries": {"citizenship": [], "residence": [], "birth_country": [], "jurisdiction_country": []},
    "_list_type": "WATCHLIST_DGT",
}


def _pacs(complet=True, instructeur=INSTRUCTEUR, banque=BANQUE_DU_DONNEUR, transferts=1):
    """pacs.008 reçu de `instructeur` : complet au sens de l'art. 4, ou sans
    numéro de compte du donneur d'ordre (art. 4 §1 b)."""
    compte = "<DbtrAcct><Id><IBAN>FR7630006000011234567890189</IBAN></Id></DbtrAcct>" if complet else ""
    instg = f"<InstgAgt><FinInstnId><BICFI>{instructeur}</BICFI></FinInstnId></InstgAgt>" if instructeur else ""
    txs = "".join(f"""
  <CdtTrfTxInf>
   <PmtId><EndToEndId>E2E-{uuid.uuid4().hex[:6]}</EndToEndId></PmtId>
   <IntrBkSttlmAmt Ccy="EUR">1500.00</IntrBkSttlmAmt>
   <DbtrAgt><FinInstnId><BICFI>{banque}</BICFI></FinInstnId></DbtrAgt>
   <CdtrAgt><FinInstnId><BICFI>DEUTDEFF</BICFI></FinInstnId></CdtrAgt>
   <Dbtr><Nm>Blorptech Vexatron Ltd</Nm><PstlAdr><TwnNm>Mumbai</TwnNm><Ctry>IN</Ctry></PstlAdr></Dbtr>
   {compte}
   <Cdtr><Nm>Qwyjibo Holdings GmbH</Nm><PstlAdr><TwnNm>Hamburg</TwnNm><Ctry>DE</Ctry></PstlAdr></Cdtr>
   <CdtrAcct><Id><IBAN>DE89370400440532013000</IBAN></Id></CdtrAcct>
  </CdtTrfTxInf>""" for _ in range(transferts))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
 <FIToFICstmrCdtTrf>
  <GrpHdr><MsgId>MSG-{uuid.uuid4().hex[:8]}</MsgId><CreDtTm>2026-10-20T10:00:00</CreDtTm>
   <NbOfTxs>{transferts}</NbOfTxs>{instg}</GrpHdr>{txs}
 </FIToFICstmrCdtTrf>
</Document>"""


def _filtrer(db, xml):
    parsed = parse_iso20022_payment(xml.encode("utf-8"))
    return screen_payment_message(db, parsed, {"K": [PUTIN]}, "v", f"h-{uuid.uuid4().hex}", "tester")


def _suivi(api, bic8):
    reponse = api.get("/api/filtrage/prestataires")
    assert reponse.status_code == 200, reponse.text
    return next((p for p in reponse.json()["items"] if p["identifier"] == bic8), None)


# ------------------ AU-DELA DU SEUIL ------------------

def test_au_dela_du_seuil_le_prestataire_est_defaillant_notifie_et_exportable(db, api, notifications):
    """Seuil par défaut : 5 transferts incomplets ET 10 % sur 90 jours. Le
    quatrième ne suffit pas — « répétée » a un seuil ; le cinquième identifie
    le prestataire, notifie UNE fois, et le signalement porte sa ligne."""
    for _ in range(4):
        assert _filtrer(db, _pacs(complet=False))["verdict"] == "INCOMPLETE"
    assert not _suivi(api, "QWERUS33")["episode_open"]
    assert not [n for n in notifications if n[0] == "prestataire_defaillant"]

    cinquieme = _filtrer(db, _pacs(complet=False))
    psp = cinquieme["payment_service_providers"]["imputed"]
    assert [(p["identifier"], p["role"], p["newly_identified"]) for p in psp] == [("QWERUS33", "InstgAgt", True)]
    # Le message qui fait basculer garde son verdict : la politique vise les SUIVANTS
    assert cinquieme["verdict"] == "INCOMPLETE"

    alertes = [c for k, c in notifications if k == "prestataire_defaillant"]
    assert len(alertes) == 1
    assert alertes[0]["BIC / LEI"] == "QWERUS33" and "5 (100 %)" == alertes[0]["Transferts incomplets"]

    ligne = _suivi(api, "QWERUS33")
    assert ligne["episode_open"] and ligne["failing"]
    assert ligne["window"]["incomplete"] == 5 and ligne["window"]["rate_pct"] == 100.0
    assert ligne["missing"][0]["code"] == "DBTR_ACCT" and ligne["missing"][0]["count"] == 5
    assert ligne["report_before"] > ligne["identified_on"]          # trois mois pour signaler

    export = api.get("/api/export/prestataires-defaillants.csv")
    assert export.status_code == 200
    lignes = export.content.decode("utf-8-sig").strip().splitlines()
    assert lignes[0].startswith("prestataire;bic_ou_lei;pays_du_bic;identifie_le")
    assert len(lignes) == 2 and ";QWERUS33;US;" in lignes[1]
    assert "Numéro de compte du donneur d'ordre (DbtrAcct) : 5" in lignes[1]

    # Un sixième ne renotifie pas : une notification par identification
    _filtrer(db, _pacs(complet=False))
    assert len([n for n in notifications if n[0] == "prestataire_defaillant"]) == 1


def test_des_messages_complets_ne_changent_pas_le_compte_des_omissions(db, api, notifications):
    """Le compteur est celui des OMISSIONS : un transfert complet n'en ajoute
    pas — il compte au dénominateur, et c'est tout (le pourcentage des
    orientations, EBA/GL/2024/11 §68 a)."""
    for _ in range(3):
        _filtrer(db, _pacs(complet=False))
    avant = _suivi(api, "QWERUS33")["window"]
    for _ in range(4):
        assert _filtrer(db, _pacs(complet=True))["verdict"] == "PASS"
    apres = _suivi(api, "QWERUS33")["window"]
    assert apres["incomplete"] == avant["incomplete"] == 3
    assert apres["transfers"] == avant["transfers"] + 4 == 7
    assert apres["rate_pct"] == round(100 * 3 / 7, 1)


def test_sous_le_pourcentage_un_gros_correspondant_n_est_pas_defaillant(db, api, notifications):
    """Cinq omissions sur cent transferts (5 %) : sous le seuil de 10 %. Un
    compteur seul aurait déclaré défaillant le correspondant le plus actif."""
    _filtrer(db, _pacs(complet=True, transferts=95))
    for _ in range(5):
        _filtrer(db, _pacs(complet=False))
    ligne = _suivi(api, "QWERUS33")
    assert ligne["window"]["incomplete"] == 5 and ligne["window"]["transfers"] == 100
    assert not ligne["failing"] and not ligne["episode_open"]
    assert not [n for n in notifications if n[0] == "prestataire_defaillant"]


# ------------------ POLITIQUE POUR LES MESSAGES SUIVANTS ------------------

def _rendre_defaillant(db):
    for _ in range(5):
        _filtrer(db, _pacs(complet=False))


def test_politique_renforcee_le_message_suivant_est_retenu(db, notifications):
    set_setting(db, CLE_CRITERES, {"politique": "RETENIR"})
    _rendre_defaillant(db)
    suivant = _filtrer(db, _pacs(complet=True))
    assert suivant["verdict"] == "FAILING_PSP"
    assert "art. 8 §2" in suivant["verdict_label"]
    # Un autre prestataire n'est pas touché
    assert _filtrer(db, _pacs(complet=True, instructeur="ZXCVGB2L"))["verdict"] == "PASS"


def test_politique_par_defaut_signaler_le_verdict_reste_et_la_reponse_le_dit(db, notifications):
    """Le minimum de l'art. 8 §2 : des dispositions qui « peuvent, dans un
    premier temps, comporter l'émission d'avertissements » — pas le rejet."""
    _rendre_defaillant(db)
    suivant = _filtrer(db, _pacs(complet=True))
    assert suivant["verdict"] == "PASS"
    psp = suivant.get("payment_service_providers") or {}
    assert [p["failing_before"] for p in psp.get("imputed") or []] == [True]
    assert psp.get("policy") == "SIGNALER"


def test_hors_bornes_les_criteres_sont_refuses(api):
    for mauvais in ({"seuil_transferts": 1}, {"fenetre_jours": 2}, {"seuil_taux_pct": 120},
                    {"politique": "REJETER_TOUT"}, {"bic_etablissement": ["ABC"]}, {"inconnu": 1}):
        reponse = api.put("/api/settings/ingestion", json={"prestataires_defaillants": mauvais})
        assert reponse.status_code == 400, mauvais
    ok = api.put("/api/settings/ingestion", json={"prestataires_defaillants": {"seuil_transferts": 3}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["prestataires_defaillants"]["seuil_transferts"] == 3


# ------------------ A QUI L'OMISSION EST IMPUTEE ------------------

def test_l_omission_est_imputee_a_l_agent_instructeur_et_la_banque_du_donneur_est_gardee(db, api):
    _filtrer(db, _pacs(complet=False))
    assert _suivi(api, "QWERUS33") is not None
    assert _suivi(api, "ASDFINBB") is None
    detail = api.get("/api/filtrage/prestataires/QWERUS33/manquements").json()["items"]
    assert detail[0]["originator_psp"] == "ASDFINBB" and detail[0]["role"] == "InstgAgt"


def test_sans_agent_instructeur_l_omission_va_a_la_banque_du_donneur(db, api):
    _filtrer(db, _pacs(complet=False, instructeur=None))
    assert _suivi(api, "ASDFINBB")["window"]["incomplete"] == 1


def test_un_message_sortant_n_est_impute_a_personne(db, api):
    """L'établissement qui filtre son propre pacs.008 sortant n'a pas à se
    signaler lui-même : son omission relève de l'art. 4."""
    set_setting(db, CLE_CRITERES, {"bic_etablissement": ["QWERUS33"]})
    resultat = _filtrer(db, _pacs(complet=False))
    assert _suivi(api, "QWERUS33") is None
    assert [n["code"] for n in resultat["payment_service_providers"]["not_imputed"]] == ["MESSAGE_SORTANT"]


def test_un_ordre_de_client_pain001_n_est_impute_a_aucun_prestataire(db, api):
    pain = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pain.001.001.09"><CstmrCdtTrfInitn>
 <GrpHdr><MsgId>PAIN-1</MsgId><CreDtTm>2026-10-20T10:00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
 <PmtInf><PmtInfId>P1</PmtInfId><ReqdExctnDt><Dt>2026-10-21</Dt></ReqdExctnDt>
  <Dbtr><Nm>Blorptech Vexatron Ltd</Nm></Dbtr>
  <DbtrAgt><FinInstnId><BICFI>QWERUS33XXX</BICFI></FinInstnId></DbtrAgt>
  <CdtTrfTxInf><PmtId><EndToEndId>E1</EndToEndId></PmtId><Amt><InstdAmt Ccy="EUR">10</InstdAmt></Amt>
   <CdtrAgt><FinInstnId><BICFI>DEUTDEFF</BICFI></FinInstnId></CdtrAgt>
   <Cdtr><Nm>Qwyjibo Holdings GmbH</Nm></Cdtr></CdtTrfTxInf>
 </PmtInf></CstmrCdtTrfInitn></Document>"""
    assert _filtrer(db, pain)["verdict"] == "INCOMPLETE"
    assert _suivi(api, "QWERUS33") is None


# ------------------ FENETRE GLISSANTE ET CONSERVATION ------------------

def _vieillir(db, jours):
    db.execute(text("UPDATE prestataires_receptions SET recu_le = :d"),
               {"d": datetime.utcnow() - timedelta(days=jours)})
    db.commit()


def test_les_omissions_sorties_de_la_periode_ne_comptent_plus(db, api, notifications):
    for _ in range(4):
        _filtrer(db, _pacs(complet=False))
    assert _suivi(api, "QWERUS33")["window"]["incomplete"] == 4
    _vieillir(db, 91)
    _filtrer(db, _pacs(complet=False))
    ligne = _suivi(api, "QWERUS33")
    assert ligne["window"]["incomplete"] == 1 and not ligne["episode_open"]


def test_les_receptions_sont_purgees_comme_le_journal_de_criblage(db, tmp_path, monkeypatch):
    """Même famille (audit_trail), même plancher légal de cinq années civiles
    (fiskr/conservation.py) — le règlement fixe la même durée (art. 26 §1)."""
    from fiskr import retention
    monkeypatch.setattr(retention, "ARCHIVE_DIR", tmp_path / "archive")
    resultat = _filtrer(db, _pacs(complet=False))
    assert (resultat.get("payment_service_providers") or {}).get("imputed"), "rien n'a été consigné"
    _vieillir(db, 5 * 366 + 2)
    _filtrer(db, _pacs(complet=False))
    set_setting(db, "retention.policy", {"audit_trail": 365})
    retention.run_retention(db, username="test")
    restantes = db.execute(text("SELECT COUNT(*) FROM prestataires_receptions")).scalar()
    assert restantes == 1
    assert (tmp_path / "archive").exists()

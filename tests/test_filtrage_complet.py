"""
Le filtrage ISO 20022 ne rend PASS que sur ce qu'il a réellement criblé.

Trois défauts mesurés, une même classe — un verdict affirmé sans avoir été
établi :

1. **PASS sur un message dont aucune partie n'a été criblée.** Une partie sans
   `<Nm>` était écartée à l'extraction : un pacs.008 dont le donneur d'ordre
   n'est identifié que par `<AnyBIC>ROSNRUMMXXX`, le bénéficiaire par son
   `<LEI>` et les banques par `ClrSysMmbId` ou `LEI` rendait **200 PASS, zéro
   partie criblée, zéro ligne d'audit**. Le verdict était la valeur initiale de
   la variable, jamais confrontée à ce qui avait été comparé.
2. **Le texte libre n'était jamais criblé.** `RmtInf/Ustrd` était extrait puis
   ignoré ; les lignes directrices conjointes DGT/ACPR sur le gel des avoirs
   (18 mars 2026, §118) demandent de filtrer aussi le texte libre.
3. **Aucun contrôle de complétude** au sens du règlement (UE) 2023/1113
   (art. 4 : informations obligatoires ; art. 7 : le prestataire du
   bénéficiaire en détecte l'absence). Comptes, `OrgId`/`LEI`, `TwnNm`
   n'étaient pas lus.

Les tests sont écrits contre le comportement (verdicts, statuts, lignes
d'audit), pas contre les nouveaux noms du module : sur le code d'avant, chacun
échoue sur son assertion, pas sur un import.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from fiskr import capabilities as caps
from fiskr import rarete
from fiskr import transactions
from fiskr.api import app
from fiskr.auth import get_current_user
from fiskr.database import Alert, AuditTrail, Base
from fiskr.transactions import parse_iso20022_payment, screen_payment_message


# ------------------ FIXTURES ------------------

@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'filtrage_complet.sqlite3'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def univers_au_repos():
    """Index de filtrage et table de rareté du processus remis au repos : un
    test ne doit pas cribler contre l'univers d'un autre."""
    table = rarete.table_courante()
    transactions.invalider_index_de_filtrage()
    rarete.installer(None)
    yield
    transactions.invalider_index_de_filtrage()
    rarete.installer(table)


def _fiche(entity_id, nom, list_type="WATCHLIST_DGT", entity_type="I", **extra):
    fiche = {
        "entity_id": entity_id, "entity_type": entity_type, "primary_name": nom,
        "individual_name_parsed": {"first_name": "", "last_name": nom, "maiden_name": ""},
        "aliases": {"high_priority": [], "low_priority": []},
        "dates_of_birth": [], "gender": "U",
        "countries": {"citizenship": [], "residence": [], "birth_country": [], "jurisdiction_country": []},
        "_list_type": list_type,
    }
    fiche.update(extra)
    return fiche


PUTIN = _fiche("TEST-PUTIN", "VLADIMIR PUTIN")
BANQUE_LISTEE = _fiche("TEST-KRASNY", "KRASNY BANK", list_type="WATCHLIST_OFAC",
                       entity_type="E", bic_swift="KRASRUMM", lei_number="5493001KJTIIGC8Y1R12")
PEP = _fiche("TEST-PEP", "BORISLAV KOLOMOISKYEV", list_type="WATCHLIST_PEP")


def _cribler(db, xml, fiches, **kwargs):
    parsed = parse_iso20022_payment(xml.encode("utf-8"))
    # Empreinte unique par appel : l'index de filtrage est mémorisé par
    # empreinte, et deux tests aux univers différents ne doivent pas partager.
    return screen_payment_message(db, parsed, {"K": list(fiches)}, "v",
                                  f"h-{uuid.uuid4().hex}", "tester", **kwargs)


def _pacs(dbtr="<Nm>Blorptech Vexatron SARL</Nm><PstlAdr><TwnNm>Lyon</TwnNm><Ctry>FR</Ctry></PstlAdr>",
          dbtr_acct="<DbtrAcct><Id><IBAN>FR7630006000011234567890189</IBAN></Id></DbtrAcct>",
          cdtr="<Nm>Qwyjibo Holdings GmbH</Nm><PstlAdr><TwnNm>Hamburg</TwnNm><Ctry>DE</Ctry></PstlAdr>",
          cdtr_acct="<CdtrAcct><Id><IBAN>DE89370400440532013000</IBAN></Id></CdtrAcct>",
          agents=("<DbtrAgt><FinInstnId><BICFI>BNPAFRPP</BICFI></FinInstnId></DbtrAgt>"
                  "<CdtrAgt><FinInstnId><BICFI>DEUTDEFF</BICFI></FinInstnId></CdtrAgt>"),
          ustrd="Facture 2026-114 loyer octobre", msg_id="MSG-COMPLET"):
    """pacs.008 COMPLET au sens de l'art. 4 par défaut ; chaque argument
    retire ou remplace un morceau."""
    rmt = f"<RmtInf><Ustrd>{ustrd}</Ustrd></RmtInf>" if ustrd else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
 <FIToFICstmrCdtTrf>
  <GrpHdr><MsgId>{msg_id}</MsgId><CreDtTm>2026-10-19T10:00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
  <CdtTrfTxInf>
   <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>
   <IntrBkSttlmAmt Ccy="EUR">950000.00</IntrBkSttlmAmt>
   {agents}
   {f"<Dbtr>{dbtr}</Dbtr>" if dbtr is not None else ""}
   {dbtr_acct}
   {f"<Cdtr>{cdtr}</Cdtr>" if cdtr is not None else ""}
   {cdtr_acct}
   {rmt}
  </CdtTrfTxInf>
 </FIToFICstmrCdtTrf>
</Document>"""


# Le message de la reproduction : aucune partie ne porte <Nm>.
PACS_R2 = """<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:pacs.008.001.08">
 <FIToFICstmrCdtTrf>
  <GrpHdr><MsgId>MSG-R2</MsgId><CreDtTm>2026-10-19T10:00:00</CreDtTm><NbOfTxs>1</NbOfTxs></GrpHdr>
  <CdtTrfTxInf>
   <PmtId><EndToEndId>E2E-1</EndToEndId></PmtId>
   <IntrBkSttlmAmt Ccy="EUR">950000.00</IntrBkSttlmAmt>
   <Dbtr><Id><OrgId><AnyBIC>ROSNRUMMXXX</AnyBIC></OrgId></Id></Dbtr>
   <DbtrAgt><FinInstnId><ClrSysMmbId><MmbId>044000037</MmbId></ClrSysMmbId></FinInstnId></DbtrAgt>
   <CdtrAgt><FinInstnId><LEI>529900T8BM49AURSDO55</LEI></FinInstnId></CdtrAgt>
   <Cdtr><Id><OrgId><LEI>253400JT3MQWNDKMJE44</LEI></OrgId></Id></Cdtr>
   <CdtrAcct><Id><IBAN>FR7630006000011234567890189</IBAN></Id></CdtrAcct>
  </CdtTrfTxInf>
 </FIToFICstmrCdtTrf>
</Document>"""


def _par_role(resultat, role):
    return next(p for p in resultat["parties"] if role in p["roles"])


# ------------------ 1. RIEN NE PASSE SANS AVOIR ETE COMPARE ------------------

def test_le_message_r2_ne_rend_pas_pass_et_trace_chaque_partie(db):
    resultat = _cribler(db, PACS_R2, [PUTIN])

    assert resultat["verdict"] != "PASS"
    assert resultat["verdict"] == "INCOMPLETE"
    # Les quatre parties sont restituées, aucune n'a disparu à l'extraction
    assert len(resultat["parties"]) == 4
    # ... et chacune laisse sa ligne au journal, criblée ou non
    assert db.query(AuditTrail).count() == 4
    assert all(p["audit_id"] for p in resultat["parties"])

    # Identifiants exacts : rapprochés, et la réponse dit sur quoi
    assert _par_role(resultat, "Donneur d'ordre")["screened_on"] == ["BIC"]
    assert _par_role(resultat, "Bénéficiaire")["screened_on"] == ["LEI"]
    assert _par_role(resultat, "Banque du bénéficiaire")["screened_on"] == ["LEI"]

    # Un code de chambre de compensation ne se compare à rien : NON CRIBLÉE,
    # avec le motif, et la ligne d'audit le dit aussi
    agent = _par_role(resultat, "Banque du donneur d'ordre")
    assert agent["status"] == "NOT_SCREENED"
    assert "rien à comparer" in agent["not_screened_label"]
    assert "ClrSysMmbId 044000037" in agent["identifiers"]
    ligne = db.query(AuditTrail).filter(AuditTrail.id == agent["audit_id"]).one()
    assert ligne.status == "NOT_SCREENED"
    assert ligne.client_name == "ClrSysMmbId 044000037"
    assert resultat["not_screened_count"] == 1


def test_le_message_r2_par_l_api_ne_rend_plus_pass():
    """La reproduction exacte, par l'endpoint : 200 PASS et zéro ligne d'audit
    avant ; une ligne par partie et pas de PASS après."""
    from fiskr.database import SessionLocal
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "role": "admin", "roles": ["admin"]}
    try:
        with TestClient(app) as client:
            session = SessionLocal()
            avant = session.query(AuditTrail).count()
            reponse = client.post("/api/transactions/screen",
                                  files={"file": ("pacs.xml", PACS_R2.encode(), "application/xml")})
            session.expire_all()
            apres = session.query(AuditTrail).count()
            session.close()
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert reponse.status_code == 200, reponse.text
    donnees = reponse.json()
    assert donnees["verdict"] == "INCOMPLETE"
    assert apres - avant == len(donnees["parties"]) == 4


def test_une_partie_identifiee_par_son_seul_bic_est_rapprochee_sur_ce_bic(db):
    """Avant : écartée sans trace. Le hard match BIC existait, mais il ne
    voyait que les fiches déjà candidates par le NOM."""
    xml = _pacs(dbtr="<Id><OrgId><AnyBIC>KRASRUMM123</AnyBIC></OrgId></Id>"
                     "<PstlAdr><TwnNm>Moscow</TwnNm><Ctry>RU</Ctry></PstlAdr>")
    resultat = _cribler(db, xml, [BANQUE_LISTEE, PUTIN])
    assert resultat["verdict"] == "HIT"
    donneur = _par_role(resultat, "Donneur d'ordre")
    assert donneur["status"] == "ALERT"
    assert donneur["hard_match"] is True
    assert donneur["best_watchlist_id"] == "TEST-KRASNY"
    alerte = db.query(Alert).filter(Alert.id == donneur["alert_id"]).one()
    assert alerte.channel == "FILTERING"


def test_un_lei_seul_est_rapproche_sur_le_lei(db):
    xml = _pacs(cdtr="<Id><OrgId><LEI>5493001KJTIIGC8Y1R12</LEI></OrgId></Id>")
    resultat = _cribler(db, xml, [BANQUE_LISTEE])
    beneficiaire = _par_role(resultat, "Bénéficiaire")
    assert beneficiaire["status"] == "ALERT"
    assert beneficiaire["hard_match"] is True
    assert resultat["verdict"] == "HIT"


def test_un_identifiant_seul_sans_correspondance_dit_sur_quoi_il_a_ete_compare(db):
    resultat = _cribler(db, PACS_R2, [BANQUE_LISTEE])
    donneur = _par_role(resultat, "Donneur d'ordre")
    assert donneur["status"] == "NO_MATCH"
    ligne = db.query(AuditTrail).filter(AuditTrail.id == donneur["audit_id"]).one()
    assert ligne.decision_tree["compared_elements"] == ["BIC"]
    # Une partie sans nom figure au journal sous ce qui l'identifie
    assert ligne.client_name == "BIC ROSNRUMMXXX"


def test_un_hard_match_bic_coupe_rend_l_agent_non_cribble(db):
    """Un identifiant que la capacité coupée ne compare plus ne crible rien :
    l'agent identifié par son seul BIC devient NON CRIBLÉ, et le dit."""
    actives = set(caps.current_context(caps.CHANNEL_FILTERING)) - {caps.CAP_HARD_BIC}
    with caps.use_context(caps.CHANNEL_FILTERING, actives):
        resultat = _cribler(db, _pacs(), [PUTIN])
    agent = _par_role(resultat, "Banque du bénéficiaire")
    assert agent["status"] == "NOT_SCREENED"
    assert "désactivé" in agent["not_screened_label"]
    assert resultat["verdict"] == "INCOMPLETE"


def test_un_message_sans_aucune_partie_exploitable_est_refuse(db):
    xml = _pacs(dbtr="<Id><PrvtId><Othr><Id>CLIENT-778</Id></Othr></PrvtId></Id>",
                cdtr=None, cdtr_acct="",
                agents="<DbtrAgt><FinInstnId><ClrSysMmbId><MmbId>044000037</MmbId></ClrSysMmbId>"
                       "</FinInstnId></DbtrAgt>")
    with pytest.raises(ValueError) as refus:
        _cribler(db, xml, [PUTIN])
    assert "rien ne peut être comparé" in str(refus.value)
    # Refus franc : aucune ligne, aucune alerte — rien n'a été criblé
    assert db.query(AuditTrail).count() == 0


def test_un_message_a_zero_partie_est_refuse(db):
    xml = ('<Document><FIToFICstmrCdtTrf><GrpHdr><MsgId>VIDE</MsgId></GrpHdr>'
           '<CdtTrfTxInf><PmtId><EndToEndId>E1</EndToEndId></PmtId></CdtTrfTxInf>'
           '</FIToFICstmrCdtTrf></Document>')
    with pytest.raises(ValueError):
        _cribler(db, xml, [PUTIN])
    assert db.query(AuditTrail).count() == 0


def test_l_api_refuse_en_422_un_message_sans_partie_exploitable():
    xml = ('<Document><FIToFICstmrCdtTrf><GrpHdr><MsgId>VIDE</MsgId></GrpHdr>'
           '<CdtTrfTxInf><DbtrAgt><FinInstnId><ClrSysMmbId><MmbId>1</MmbId></ClrSysMmbId>'
           '</FinInstnId></DbtrAgt></CdtTrfTxInf></FIToFICstmrCdtTrf></Document>')
    app.dependency_overrides[get_current_user] = lambda: {
        "id": 1, "username": "admin", "role": "admin", "roles": ["admin"]}
    try:
        with TestClient(app) as client:
            reponse = client.post("/api/transactions/screen",
                                  files={"file": ("vide.xml", xml.encode(), "application/xml")})
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert reponse.status_code == 422, reponse.text
    assert "rien ne peut être comparé" in reponse.json()["detail"]


def test_les_intermediaires_2_et_3_sont_cribles(db):
    """§118 : filtrer les intermédiaires. Seul le premier l'était."""
    agents = ("<DbtrAgt><FinInstnId><BICFI>BNPAFRPP</BICFI></FinInstnId></DbtrAgt>"
              "<IntrmyAgt2><FinInstnId><Nm>Krasny Bank</Nm></FinInstnId></IntrmyAgt2>"
              "<CdtrAgt><FinInstnId><BICFI>DEUTDEFF</BICFI></FinInstnId></CdtrAgt>")
    resultat = _cribler(db, _pacs(agents=agents), [BANQUE_LISTEE])
    assert resultat["verdict"] == "HIT"
    assert _par_role(resultat, "Banque intermédiaire 2")["status"] == "ALERT"


# ------------------ 2. LE TEXTE LIBRE ------------------

def test_un_ustrd_qui_nomme_un_liste_declenche_un_hit(db):
    xml = _pacs(ustrd="Paiement pour le compte de Vladimir Putin, facture 2026-114")
    resultat = _cribler(db, xml, [PUTIN])

    assert resultat["verdict"] == "HIT"
    libre = _par_role(resultat, "Informations de remise (texte libre)")
    assert libre["status"] == "ALERT"
    assert libre["best_watchlist_id"] == "TEST-PUTIN"
    assert libre["screened_on"] == ["TEXTE_LIBRE"]
    assert libre["free_text"]["transactions"] == ["E2E-1"]
    # Alerte adjudicable sur le canal filtrage, journal qui dit d'où vient le nom
    alerte = db.query(Alert).filter(Alert.id == libre["alert_id"]).one()
    assert alerte.channel == "FILTERING"
    ligne = db.query(AuditTrail).filter(AuditTrail.id == libre["audit_id"]).one()
    assert ligne.decision_tree["free_text_match"]["sequence"] == "VLADIMIR PUTIN"
    assert "Vladimir Putin" in ligne.decision_tree["free_text_match"]["excerpt"]
    assert resultat["free_text"] == {"screened": 1, "hits": 1}


def test_un_ustrd_banal_est_examine_et_ne_declenche_rien(db):
    resultat = _cribler(db, _pacs(ustrd="Facture 2026-114 loyer octobre"), [PUTIN, BANQUE_LISTEE])
    assert resultat["free_text"] == {"screened": 1, "hits": 0}
    assert resultat["verdict"] == "PASS"
    assert db.query(Alert).count() == 0


def test_un_fragment_de_nom_liste_ne_declenche_pas(db):
    """Le nom COMPLET, jamais un fragment : « Putin » seul, ou « Vladimir »
    dans « Vladimir Ivanov », ne rapprochent rien."""
    resultat = _cribler(db, _pacs(ustrd="Remboursement Vladimir Ivanov et M. Putin"), [PUTIN])
    assert resultat["free_text"]["hits"] == 0
    assert resultat["verdict"] == "PASS"


def test_un_nom_fait_de_mots_repandus_ne_declenche_pas(db):
    """« ENGINEERING GROUP » est un nom RÉELLEMENT listé (recueil
    OpenSanctions) qui déclenchait dans la mesure sur un libellé banal. Deux
    mots que le corpus listé porte partout n'identifient personne."""
    corpus = [_fiche(f"T-{i}", f"ACME{i} ENGINEERING GROUP", entity_type="E") for i in range(40)]
    generique = _fiche("T-GEN", "ENGINEERING GROUP", entity_type="E")
    rarete.installer(rarete.construire(corpus + [generique, PUTIN]))
    resultat = _cribler(db, _pacs(ustrd="Etude de sol Engineering Group, devis 4471"),
                        corpus + [generique, PUTIN])
    assert resultat["free_text"] == {"screened": 1, "hits": 0}
    # La même règle laisse passer un nom porteur d'un mot rare
    resultat = _cribler(db, _pacs(ustrd="Pour Vladimir Putin", msg_id="MSG-2"),
                        corpus + [generique, PUTIN])
    assert resultat["free_text"]["hits"] == 1


def test_un_pep_cite_dans_le_texte_libre_ne_declenche_pas(db):
    """Périmètre SANCTION seulement : un PEP cité dans un libellé n'appelle
    pas l'arrêt du paiement."""
    resultat = _cribler(db, _pacs(ustrd="Don pour Borislav Kolomoiskyev"), [PEP])
    assert resultat["free_text"] == {"screened": 1, "hits": 0}
    assert resultat["verdict"] == "PASS"


def test_le_classement_des_perimetres_regle_aussi_le_texte_libre(db):
    """Le classement SANCTION / HORS_SANCTION se règle à chaud : reclasser la
    liste PEP côté SANCTION doit faire entrer ses noms dans le texte libre."""
    from fiskr.settings import SETTING_PERIMETERS, set_setting
    set_setting(db, SETTING_PERIMETERS, {"WATCHLIST_PEP": "SANCTION"})
    resultat = _cribler(db, _pacs(ustrd="Don pour Borislav Kolomoiskyev"), [PEP])
    assert resultat["free_text"]["hits"] == 1
    assert resultat["verdict"] == "HIT"


def test_un_nom_deja_cribble_comme_partie_n_alerte_pas_deux_fois(db):
    xml = _pacs(cdtr="<Nm>Vladimir PUTIN</Nm><PstlAdr><TwnNm>Moscow</TwnNm><Ctry>RU</Ctry></PstlAdr>",
                ustrd="Salaire Vladimir Putin octobre")
    resultat = _cribler(db, xml, [PUTIN])
    assert resultat["free_text"] == {"screened": 1, "hits": 0}
    assert resultat["verdict"] == "HIT"
    assert db.query(Alert).count() == 1


def test_la_restriction_de_listes_s_applique_au_texte_libre(db):
    resultat = _cribler(db, _pacs(ustrd="Pour Vladimir Putin"), [PUTIN],
                        screening_lists=["WATCHLIST_OFAC"])
    assert resultat["free_text"]["hits"] == 0


# ------------------ 3. COMPLETUDE, REGLEMENT (UE) 2023/1113 ------------------

def test_un_message_complet_et_propre_rend_pass(db):
    resultat = _cribler(db, _pacs(), [PUTIN, BANQUE_LISTEE])
    assert resultat["verdict"] == "PASS"
    assert resultat["missing_information"] == []
    assert resultat["not_screened_count"] == 0
    assert resultat["hits_count"] == 0


def test_un_donneur_d_ordre_sans_compte_est_un_manque_nomme(db):
    resultat = _cribler(db, _pacs(dbtr_acct=""), [PUTIN])
    assert resultat["verdict"] == "INCOMPLETE"
    manques = {m["code"]: m for m in resultat["missing_information"]}
    assert set(manques) == {"DBTR_ACCT"}
    assert "DbtrAcct" in manques["DBTR_ACCT"]["label"]
    assert manques["DBTR_ACCT"]["reference"] == "2023/1113 art. 4 §1 b)"
    assert manques["DBTR_ACCT"]["transactions"] == ["E2E-1"]


def test_les_cinq_informations_de_l_article_4_sont_controlees(db):
    """Sans nom ni compte ni adresse du donneur d'ordre, ni nom ni compte du
    bénéficiaire : les cinq manques sont nommés. Le message garde une partie
    exploitable (les banques, sur leur BIC) : il n'est pas refusé, il est
    INCOMPLETE."""
    resultat = _cribler(db, _pacs(dbtr=None, dbtr_acct="", cdtr=None, cdtr_acct=""), [PUTIN])
    assert resultat["verdict"] == "INCOMPLETE"
    assert [m["code"] for m in resultat["missing_information"]] == [
        "DBTR_NM", "DBTR_ACCT", "DBTR_ADR", "CDTR_NM", "CDTR_ACCT"]


def test_une_adresse_sans_ville_n_est_pas_une_adresse(db):
    """« l'adresse, y compris le nom du pays » : un pays seul n'en est pas une."""
    resultat = _cribler(db, _pacs(dbtr="<Nm>Blorptech Vexatron SARL</Nm><PstlAdr><Ctry>FR</Ctry></PstlAdr>"),
                        [PUTIN])
    assert [m["code"] for m in resultat["missing_information"]] == ["DBTR_ADR"]


def test_la_date_et_le_lieu_de_naissance_remplacent_l_adresse(db):
    """Art. 4 §1 c) : l'adresse, ou un identifiant officiel ou client, ou la
    date et le lieu de naissance."""
    naissance = ("<Nm>Blorptech Vexatron</Nm><Id><PrvtId><DtAndPlcOfBirth><BirthDt>1970-01-01</BirthDt>"
                 "<CityOfBirth>Lyon</CityOfBirth><CtryOfBirth>FR</CtryOfBirth></DtAndPlcOfBirth></PrvtId></Id>")
    resultat = _cribler(db, _pacs(dbtr=naissance), [PUTIN])
    assert resultat["missing_information"] == []
    assert resultat["verdict"] == "PASS"


def test_un_hit_l_emporte_mais_les_manques_restent_nommes(db):
    xml = _pacs(cdtr="<Nm>Vladimir PUTIN</Nm>", cdtr_acct="")
    resultat = _cribler(db, xml, [PUTIN])
    assert resultat["verdict"] == "HIT"
    assert [m["code"] for m in resultat["missing_information"]] == ["CDTR_ACCT"]

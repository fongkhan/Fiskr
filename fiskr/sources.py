"""
Registre declaratif des sources branchees sur le lecteur OpenSanctions
(`targets.simple.csv`). Une source = une entree ici, et TOUT le reste se
derive : configuration, runner de synchronisation, alias d'API, type de
liste, planification, import manuel, sondes de diagnostic, tests.

Pourquoi un module sans dependance : `database.py` (types de listes),
`settings.py` (planification) et `sync.py` (runners) doivent tous lire ce
registre, et ils ne peuvent pas s'importer entre eux sans cycle.

Choix assume, documente dans config.yaml et SOURCES_PREMIUM.md : ces listes
passent par le format plat d'OpenSanctions — un seul chemin de code, teste,
au prix de la base legale et des references officielles que ce format ne
porte pas (meme limite, deja actee, que PEP et la voie de secours SECO).
L'URL officielle native de chaque emetteur est conservee dans le registre a
titre documentaire, pour une future voie « official » (pattern SECO).
Licence : usage non commercial libre ; en production commerciale, licence
OpenSanctions requise (opensanctions.org/licensing).
"""
from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class OpenSanctionsSource:
    run_key: str              # cle interne (config, planification, file de travaux)
    dataset: str              # slug du dataset OpenSanctions (verifiable par la sonde)
    source: str               # nom court des rapports de sync (SyncReport.source ≤ 20)
    file_type: str            # type de liste (colonnes list_type ≤ 30)
    id_prefix: str            # prefixe des entity_id produits
    label: str                # libelle humain (front, docs)
    family: str               # famille d'affichage (regroupe les selecteurs)
    origin: str               # provenance affichee sur les fiches
    designation_reasons: str  # motif de designation par defaut
    official_url: str         # source officielle native (documentaire)


# Familles d'affichage — memes cles que le front (regroupement des selecteurs)
FAMILY_SANCTIONS = "sanctions"        # designations avec obligation de gel
FAMILY_REGULATORY = "regulatory"      # alertes de regulateurs (signal, pas de gel)
FAMILY_DEBARMENT = "debarment"        # exclusions de bailleurs multilateraux
FAMILY_AGGREGATED = "aggregated"      # donnees agregees (PEP, OpenSanctions)

OPENSANCTIONS_SOURCES: Tuple[OpenSanctionsSource, ...] = (
    # --- Exclusions des banques multilaterales de developpement -------------
    # Meme logique que la Banque mondiale deja branchee : une exclusion des
    # marches finances, prononcee pour fraude ou corruption averee — risque
    # de contrepartie sur le financement de projets, pas un gel des avoirs.
    OpenSanctionsSource(
        "adb", "adb_sanctions", "ADB", "WATCHLIST_ADB", "ADB",
        "Banque asiatique de développement — exclusions", FAMILY_DEBARMENT,
        "BAsD (exclusions)", "Exclusion des marchés financés par la BAsD",
        "https://www.adb.org/site/integrity/sanctions"),
    OpenSanctionsSource(
        "iadb", "iadb_sanctions", "IADB", "WATCHLIST_IADB", "IADB",
        "Banque interaméricaine de développement — exclusions", FAMILY_DEBARMENT,
        "BID (exclusions)", "Exclusion des marchés financés par la BID",
        "https://www.iadb.org/en/transparency/sanctioned-firms-and-individuals"),
    OpenSanctionsSource(
        "ebrd", "ebrd_ineligible", "EBRD", "WATCHLIST_EBRD", "EBRD",
        "BERD — entités inéligibles", FAMILY_DEBARMENT,
        "BERD (exclusions)", "Entité inéligible aux financements BERD",
        "https://www.ebrd.com/ineligible-entities.html"),
    OpenSanctionsSource(
        "afdb", "afdb_sanctions", "AFDB", "WATCHLIST_AFDB", "AFDB",
        "Banque africaine de développement — exclusions", FAMILY_DEBARMENT,
        "BAfD (exclusions)", "Exclusion des marchés financés par la BAfD",
        "https://www.afdb.org/en/projects-operations/debarment-and-sanctions-procedures"),

    # --- Asie-Pacifique -----------------------------------------------------
    OpenSanctionsSource(
        "japan_mof", "jp_mof_sanctions", "JAPANMOF", "WATCHLIST_JAPAN_MOF", "JPMOF",
        "Japon — gels des avoirs (MOF)", FAMILY_SANCTIONS,
        "Japon MOF", "Mesure de gel des avoirs (Ministère des finances japonais)",
        "https://www.mof.go.jp/policy/international_policy/gaitame_kawase/gaitame/economic_sanctions/list.html"),
    OpenSanctionsSource(
        "mas", "sg_terrorists", "MAS", "WATCHLIST_MAS", "MAS",
        "Singapour — désignations TSOFA (MAS)", FAMILY_SANCTIONS,
        "Singapour MAS", "Désignation au titre du TSOFA (Singapour)",
        "https://www.mas.gov.sg/regulation/anti-money-laundering/targeted-financial-sanctions"),
    OpenSanctionsSource(
        "nz_russia", "nz_russia_sanctions", "NZRUSSIA", "WATCHLIST_NZ", "NZRU",
        "Nouvelle-Zélande — registre sanctions Russie", FAMILY_SANCTIONS,
        "Nouvelle-Zélande", "Registre néo-zélandais des sanctions (Russia Sanctions Act)",
        "https://www.mfat.govt.nz/en/countries-and-regions/europe/ukraine/russian-invasion-of-ukraine/sanctions/"),

    # --- Listes nationales de gel terrorisme + Israel -----------------------
    # Completent l'ONU/UE pour les designations purement nationales.
    OpenSanctionsSource(
        "nl_terror", "nl_terrorism_list", "NLTERROR", "WATCHLIST_NL_TERROR", "NLT",
        "Pays-Bas — liste nationale terrorisme", FAMILY_SANCTIONS,
        "Pays-Bas (terrorisme)", "Liste nationale néerlandaise de gel (terrorisme)",
        "https://www.government.nl/topics/counterterrorism-and-national-security/national-terrorism-list"),
    OpenSanctionsSource(
        "be_terror", "be_fod_sanctions", "BETERROR", "WATCHLIST_BE_TERROR", "BET",
        "Belgique — liste nationale de gel", FAMILY_SANCTIONS,
        "Belgique (gels)", "Liste nationale belge de gel (terrorisme)",
        "https://finances.belgium.be/fr/tresorerie/sanctions-financieres"),
    # Le slug `il_nbctf_sanctions` n'a JAMAIS existe au catalogue (404
    # NoSuchKey a chaque passage, constate a la verification generale) : le
    # jeu publie est `il_mod_terrorists`.
    OpenSanctionsSource(
        "il_nbctf", "il_mod_terrorists", "ILNBCTF", "WATCHLIST_IL_NBCTF", "ILN",
        "Israël — organisations terroristes", FAMILY_SANCTIONS,
        "Israël NBCTF", "Désignation du bureau national israélien de lutte contre le financement du terrorisme",
        "https://nbctf.mod.gov.il/en/Pages/designation.aspx"),

    # --- Ukraine guerre & sanctions ----------------------------------------
    # Signal riche (flotte fantome, contournement) mais source non
    # occidentale : a croiser, d'ou son type de liste propre et son seuil.
    OpenSanctionsSource(
        "ua_nsdc", "ua_nsdc_sanctions", "UANSDC", "WATCHLIST_UA_NSDC", "UAN",
        "Ukraine — sanctions NSDC", FAMILY_SANCTIONS,
        "Ukraine NSDC", "Sanction du Conseil national de sécurité et de défense ukrainien",
        "https://sanctions.nsdc.gov.ua/"),

    # --- Listes nationales de terrorisme (RCSNU 1373) -----------------------
    # Le coeur du CFT : chaque Etat designe sur son propre sol, et AUCUNE de
    # ces designations ne remonte dans les listes onusiennes ou europeennes.
    # Juridictions retenues pour l'exposition d'un etablissement francais :
    # Golfe et Levant (correspondants bancaires, transferts de fonds),
    # Maghreb, Asie du Sud-Est, Afrique.
    OpenSanctionsSource(
        "ae_terror", "ae_local_terrorists", "AETERROR", "WATCHLIST_AE_TERROR", "AET",
        "Émirats arabes unis — liste nationale de terrorisme", FAMILY_SANCTIONS,
        "EAU (terrorisme)", "Désignation nationale émirienne (RCSNU 1373)",
        "https://www.uaeiec.gov.ae/en-us/un-page"),
    OpenSanctionsSource(
        "sa_terror", "sa_pcct_terrorism_list", "SATERROR", "WATCHLIST_SA_TERROR", "SAT",
        "Arabie saoudite — liste nationale de terrorisme", FAMILY_SANCTIONS,
        "Arabie saoudite (terrorisme)", "Désignation nationale saoudienne (RCSNU 1373)",
        "https://www.spa.gov.sa/"),
    OpenSanctionsSource(
        "qa_nctc", "qa_nctc_sanctions", "QANCTC", "WATCHLIST_QA_NCTC", "QAT",
        "Qatar — registre national des sanctions", FAMILY_SANCTIONS,
        "Qatar NCTC", "Désignation nationale qatarienne (comité national de lutte contre le terrorisme)",
        "https://www.nctc.gov.qa/"),
    OpenSanctionsSource(
        "eg_terror", "eg_terrorists", "EGTERROR", "WATCHLIST_EG_TERROR", "EGT",
        "Égypte — liste nationale de terrorisme", FAMILY_SANCTIONS,
        "Égypte (terrorisme)", "Désignation nationale égyptienne (RCSNU 1373)",
        "https://www.cc.gov.eg/"),
    OpenSanctionsSource(
        "tr_masak", "tr_fcib", "TRMASAK", "WATCHLIST_TR_MASAK", "TRM",
        "Türkiye — gels d'avoirs MASAK", FAMILY_SANCTIONS,
        "Türkiye MASAK", "Gel d'avoirs prononcé par la cellule de renseignement financier turque",
        "https://en.hmb.gov.tr/financial-crimes-investigation-board"),
    OpenSanctionsSource(
        "id_dttot", "id_dttot", "IDDTTOT", "WATCHLIST_ID_DTTOT", "IDT",
        "Indonésie — liste DTTOT", FAMILY_SANCTIONS,
        "Indonésie DTTOT", "Désignation nationale indonésienne (terroristes et organisations terroristes)",
        "https://www.ppatk.go.id/"),
    OpenSanctionsSource(
        "za_fic", "za_fic_sanctions", "ZAFIC", "WATCHLIST_ZA_FIC", "ZAF",
        "Afrique du Sud — sanctions financières ciblées", FAMILY_SANCTIONS,
        "Afrique du Sud FIC", "Sanction financière ciblée sud-africaine (Financial Intelligence Centre)",
        "https://www.fic.gov.za/"),
    OpenSanctionsSource(
        "tn_cnlct", "tn_cnlct", "TNCNLCT", "WATCHLIST_TN_CNLCT", "TNC",
        "Tunisie — liste nationale antiterroriste", FAMILY_SANCTIONS,
        "Tunisie CNLCT", "Désignation nationale tunisienne (commission nationale de lutte contre le terrorisme)",
        "https://www.cnlct.tn/"),

    # --- Voisinage europeen : gels nationaux hors liste UE ------------------
    OpenSanctionsSource(
        "mc_freezes", "mc_fund_freezes", "MCFREEZE", "WATCHLIST_MC_FREEZE", "MCF",
        "Monaco — gels de fonds", FAMILY_SANCTIONS,
        "Monaco (gels)", "Gel de fonds prononcé par la Principauté de Monaco",
        "https://geldesfonds.gouv.mc/"),
    OpenSanctionsSource(
        "cz_terror", "cz_terrorists", "CZTERROR", "WATCHLIST_CZ_TERROR", "CZT",
        "Tchéquie — désignations antiterroristes", FAMILY_SANCTIONS,
        "Tchéquie (terrorisme)", "Désignation nationale tchèque (règlement gouvernemental)",
        "https://www.financnianalytickyurad.cz/"),

    # --- Etats-Unis et Royaume-Uni : ce que nos listes ne portent pas -------
    # L'OFAC porte le gel, PAS la designation d'organisation terroriste
    # etrangere (FTO) du Departement d'Etat ; l'OFSI porte le gel financier,
    # PAS la liste de sanctions du FCDO ni les organisations proscrites.
    OpenSanctionsSource(
        "us_fto", "us_state_terrorist_orgs", "USFTO", "WATCHLIST_US_FTO", "FTO",
        "États-Unis — organisations terroristes étrangères (FTO)", FAMILY_SANCTIONS,
        "US FTO", "Désignation d'organisation terroriste étrangère (Département d'État)",
        "https://www.state.gov/foreign-terrorist-organizations/"),
    OpenSanctionsSource(
        "gb_fcdo", "gb_fcdo_sanctions", "GBFCDO", "WATCHLIST_GB_FCDO", "FCD",
        "Royaume-Uni — liste de sanctions FCDO", FAMILY_SANCTIONS,
        "UK FCDO", "Désignation du Foreign, Commonwealth & Development Office",
        "https://www.gov.uk/government/publications/the-uk-sanctions-list"),
    OpenSanctionsSource(
        "gb_proscribed", "gb_proscribed_orgs", "GBPROSC", "WATCHLIST_GB_PROSCRIBED", "GBP",
        "Royaume-Uni — organisations terroristes proscrites", FAMILY_SANCTIONS,
        "UK organisations proscrites", "Organisation proscrite au titre du Terrorism Act 2000",
        "https://www.gov.uk/government/publications/proscribed-terror-groups-or-organisations"),

    # --- Australie : la voie officielle DFAT ne repond plus ------------------
    # Le CSV et le XLSX de dfat.gov.au ont ete retires (erreur de flux HTTP/2
    # puis 404, constate a la verification generale) : le connecteur natif
    # WATCHLIST_DFAT reste en place pour l'import manuel du fichier, et cette
    # voie-ci prend le relais de la synchronisation automatique.
    OpenSanctionsSource(
        "au_dfat", "au_dfat_sanctions", "AUDFAT", "WATCHLIST_AU_DFAT", "AUD",
        "Australie — liste consolidée DFAT", FAMILY_SANCTIONS,
        "Australie DFAT", "Sanction australienne (ONU transposée + désignations autonomes)",
        "https://www.dfat.gov.au/international-relations/security/sanctions/consolidated-list"),

    # --- Exclusions de bailleurs : la voie Banque mondiale sans cle ---------
    # L'API native de la Banque mondiale exige desormais une cle d'abonnement
    # (401). Cette voie-ci porte le meme contenu, sans cle.
    OpenSanctionsSource(
        "worldbank_os", "worldbank_debarred", "WBDEBAR", "WATCHLIST_WB_DEBARRED", "WBD",
        "Banque mondiale — fournisseurs exclus (voie ouverte)", FAMILY_DEBARMENT,
        "Banque mondiale (exclusions)", "Exclusion des marchés financés par le Groupe de la Banque mondiale",
        "https://www.worldbank.org/en/projects-operations/procurement/debarred-firms"),

    # --- Crypto : portefeuilles designes ------------------------------------
    # Fiskr sait deja faire une correspondance exacte sur adresse crypto : ces
    # portefeuilles designes lui donnent de la matiere.
    OpenSanctionsSource(
        "il_crypto", "il_mod_crypto", "ILCRYPTO", "WATCHLIST_IL_CRYPTO", "ILC",
        "Israël — portefeuilles crypto désignés", FAMILY_SANCTIONS,
        "Israël (crypto)", "Portefeuille crypto désigné par le ministère de la Défense israélien",
        "https://nbctf.mod.gov.il/en/Pages/AdministrativeSeizure.aspx"),
)

OPENSANCTIONS_BY_KEY: Dict[str, OpenSanctionsSource] = {
    s.run_key: s for s in OPENSANCTIONS_SOURCES
}
OPENSANCTIONS_BY_FILE_TYPE: Dict[str, OpenSanctionsSource] = {
    s.file_type: s for s in OPENSANCTIONS_SOURCES
}


def opensanctions_default_url(dataset: str) -> str:
    """URL du format plat `targets.simple.csv` pour un dataset donne."""
    return f"https://data.opensanctions.org/datasets/latest/{dataset}/targets.simple.csv"


# Garde-fous de schema : ces bornes sont des colonnes de base
# (SyncReport.source VARCHAR(20), AuditTrail.list_type VARCHAR(30)) — un
# depassement casserait a l'INSERT, autant echouer a l'import du module.
for _s in OPENSANCTIONS_SOURCES:
    assert len(_s.source) <= 20, f"source trop long : {_s.source}"
    assert len(_s.file_type) <= 30, f"file_type trop long : {_s.file_type}"

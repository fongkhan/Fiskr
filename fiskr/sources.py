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
    OpenSanctionsSource(
        "il_nbctf", "il_nbctf_sanctions", "ILNBCTF", "WATCHLIST_IL_NBCTF", "ILN",
        "Israël — désignations NBCTF", FAMILY_SANCTIONS,
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

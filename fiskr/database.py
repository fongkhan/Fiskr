import json
import hashlib
import logging
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text, JSON, Boolean, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from fiskr.config import config

logger = logging.getLogger("fiskr.database")

Base = declarative_base()

class Snapshot(Base):
    __tablename__ = "snapshots"
    
    snapshot_id = Column(String(50), primary_key=True)
    file_type = Column(String(50), nullable=False) # WATCHLIST_OFAC, WATCHLIST_EU, CLIENT_BASE
    file_name = Column(String(255), nullable=False)
    file_hash = Column(String(64), nullable=False)
    record_count = Column(Integer, default=0)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    status = Column(String(20), default="PROCESSING") # PROCESSING, PENDING_REVIEW, READY, SUPERSEDED, REJECTED, ERROR
    # Homologation (pointage humain avant mise en production)
    reviewed_by = Column(String(100), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    review_comment = Column(Text, nullable=True)
    # Dernier cahier de tests (backtest) execute sur ce snapshot candidat :
    # rapport archive avec le snapshot, auditable apres promotion
    backtest_report = Column(JSON, nullable=True)
    backtest_at = Column(DateTime, nullable=True)
    backtest_by = Column(String(100), nullable=True)
    # Progression d'un import en cours (statut PROCESSING) : nombre de fiches
    # deja persistees, total estime (lignes/octets) et phase courante
    # (DOWNLOAD|HASH|PARSE|PERSIST|DELTA|RELOAD|DONE) — commits periodiques
    processed_count = Column(Integer, nullable=True)
    total_hint = Column(Integer, nullable=True)
    phase = Column(String(30), nullable=True)

class WatchlistEntity(Base):
    __tablename__ = "watchlist_entities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(50), ForeignKey("snapshots.snapshot_id"), nullable=False)
    # Relation declaree pour l'ORDONNANCEMENT du flush : sans elle, l'ORM peut
    # inserer les fiches AVANT leur snapshot dans un meme commit — defaut
    # masque par SQLite (FK non appliquees par defaut), revele par PostgreSQL.
    snapshot = relationship("Snapshot")
    entity_id = Column(String(100), nullable=False)
    entity_type = Column(String(10), nullable=False) # I, E, V, O
    primary_name = Column(String(1000), nullable=False)
    
    # Parsed structure
    individual_name_parsed = Column(JSON, nullable=True) # first_name, last_name, maiden_name
    aliases = Column(JSON, nullable=True) # {"high_priority": [], "low_priority": []}
    dates_of_birth = Column(JSON, nullable=True) # list of YYYY-MM-DD
    date_of_death = Column(String(50), nullable=True)
    is_deceased = Column(Boolean, default=False)
    gender = Column(String(5), default="U")
    countries = Column(JSON, nullable=True) # citizenship, residence, birth_country, jurisdiction_country
    
    # New fields requested
    place_of_birth = Column(String(255), nullable=True)
    address = Column(Text, nullable=True)
    city = Column(String(255), nullable=True)
    state = Column(String(255), nullable=True)
    country = Column(String(100), nullable=True)
    origin = Column(String(255), nullable=True)
    designation = Column(String(500), nullable=True)
    designation_reasons = Column(Text, nullable=True)  # Motifs de la designation (annexes EUR-Lex, notes OFAC)
    additional_informations = Column(Text, nullable=True)
    alternative_addresses = Column(JSON, nullable=True)

    # Identifiers
    imo_number = Column(String(20), nullable=True)
    aircraft_tail_number = Column(String(50), nullable=True)
    lei_number = Column(String(50), nullable=True)
    
    # JSON arrays of objects
    national_registry_ids = Column(JSON, nullable=True) # number, country, registry_name
    other_registration_ids = Column(JSON, nullable=True) # id_type, number
    passport_documents = Column(JSON, nullable=True) # number, issuing_country, expiration_date
    national_id_documents = Column(JSON, nullable=True) # number, issuing_country
    other_id_documents = Column(JSON, nullable=True) # doc_type, number, issuing_country
    
    # Reference officielle de l'emetteur (reglement UE, reference ONU/DGT...),
    # incluant la date de publication/mise a jour quand la source la fournit
    official_reference = Column(String(500), nullable=True)

    # ---- Champs etendus (extraction structuree des sources) ----
    # Identifiants a fort pouvoir de matching
    crypto_wallets = Column(JSON, nullable=True)        # [{"currency", "address"}] (OFAC Digital Currency Address)
    bic_swift = Column(String(20), nullable=True)       # OFAC BIK/SWIFT — croise avec les BIC du filtrage ISO 20022
    tax_id = Column(String(100), nullable=True)         # Tax ID / INN
    duns_number = Column(String(20), nullable=True)     # D-U-N-S
    # Navires / aeronefs (features OFAC structurees)
    vessel_call_sign = Column(String(50), nullable=True)
    vessel_mmsi = Column(String(20), nullable=True)
    vessel_flag = Column(String(100), nullable=True)
    vessel_type = Column(String(100), nullable=True)
    vessel_tonnage = Column(String(50), nullable=True)
    vessel_owner = Column(String(500), nullable=True)
    aircraft_model = Column(String(200), nullable=True)
    aircraft_operator = Column(String(200), nullable=True)
    aircraft_construction_number = Column(String(100), nullable=True)
    # Detection, tri, pilotage
    sanction_programs = Column(JSON, nullable=True)     # ["SDGT", "UKR", ...]
    listed_on = Column(String(20), nullable=True)       # date d'inscription (ISO)
    delisted_on = Column(String(20), nullable=True)     # date de radiation/expiration (ISO)
    name_original_script = Column(String(1000), nullable=True)  # nom en ecriture d'origine
    title = Column(String(255), nullable=True)          # titre honorifique (distinct de designation)
    pep_role = Column(String(500), nullable=True)       # fonction PEP (OpenSanctions)
    secondary_sanctions_risk = Column(Text, nullable=True)  # OFAC CAATSA
    designating_state = Column(String(200), nullable=True)  # Etat a l'origine de l'inscription (ONU)
    # Personnes morales
    organization_established_date = Column(String(20), nullable=True)
    organization_type = Column(String(200), nullable=True)
    # Contact & investigation
    phone_numbers = Column(JSON, nullable=True)
    email_addresses = Column(JSON, nullable=True)
    websites = Column(JSON, nullable=True)

    # Checksum for version comparisons
    entity_checksum = Column(String(64), nullable=False)

    # Derniere modification manuelle (patch de valeurs par un reviseur)
    modified_by = Column(String(100), nullable=True)
    modified_at = Column(DateTime, nullable=True)

    # Exclusion par un reviseur lors de l'homologation (NULL = non exclu, lignes legacy)
    excluded = Column(Boolean, default=False, nullable=True)
    exclusion_justification = Column(Text, nullable=True)
    exclusion_file_name = Column(String(255), nullable=True)
    exclusion_file_path = Column(String(500), nullable=True)
    excluded_by = Column(String(100), nullable=True)
    excluded_at = Column(DateTime, nullable=True)

class WatchlistEntityChange(Base):
    """
    Journal des modifications manuelles des fiches listees : qui, quand,
    quel champ, ancienne -> nouvelle valeur (JSON-serialisees pour les
    champs structurees). Table independante : l'historique survit meme si
    la fiche est remplacee par une synchronisation ulterieure.
    """
    __tablename__ = "watchlist_entity_changes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_pk = Column(Integer, nullable=False, index=True)  # watchlist_entities.id
    entity_id = Column(String(100), nullable=False)
    snapshot_id = Column(String(50), nullable=True)
    field = Column(String(60), nullable=False)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    changed_by = Column(String(100), nullable=False)
    changed_at = Column(DateTime, default=datetime.utcnow)

class ClientEntity(Base):
    __tablename__ = "client_entities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(50), ForeignKey("snapshots.snapshot_id"), nullable=False)
    # Meme motif que WatchlistEntity.snapshot : ordonnancement du flush
    snapshot = relationship("Snapshot")
    client_id = Column(String(100), nullable=False)
    client_type = Column(String(10), nullable=False) # PP, PM
    
    client_first_name = Column(String(100), nullable=True)
    client_last_name = Column(String(100), nullable=True)
    client_maiden_name = Column(String(100), nullable=True)
    # Denominations alternatives declarees au referentiel : nom d'usage, nom de
    # scene, translitteration differente, ancienne raison sociale. Le moteur les
    # criblait deja s'il les recevait (fiskr/scoring.py), mais AUCUN chemin
    # d'entree ne permettait de les porter : la moitie cliente de la
    # correspondance par alias etait donc inatteignable.
    client_aliases = Column(JSON, nullable=True)
    client_company_name = Column(String(1000), nullable=True)
    client_dob = Column(String(50), nullable=True)
    client_gender = Column(String(5), default="U")
    client_is_deceased = Column(Boolean, default=False)
    client_countries = Column(JSON, nullable=True) # nationality, residence, birth_country, registration_country
    
    # New fields requested
    client_place_of_birth = Column(String(255), nullable=True)
    client_address = Column(Text, nullable=True)
    client_city = Column(String(255), nullable=True)
    client_state = Column(String(255), nullable=True)
    client_country = Column(String(100), nullable=True)
    client_origin = Column(String(255), nullable=True)
    client_designation = Column(String(500), nullable=True)
    client_additional_informations = Column(Text, nullable=True)
    client_alternative_addresses = Column(JSON, nullable=True)
    client_date_of_death = Column(String(50), nullable=True)
    
    # Identifiers
    transaction_vessel_imo = Column(String(20), nullable=True)
    transaction_aircraft_registration = Column(String(50), nullable=True)
    client_lei_number = Column(String(50), nullable=True)

    # JSON arrays of objects
    client_national_registry_ids = Column(JSON, nullable=True)
    client_other_registration_ids = Column(JSON, nullable=True)
    client_passport_documents = Column(JSON, nullable=True)
    client_national_id_documents = Column(JSON, nullable=True)
    client_other_id_documents = Column(JSON, nullable=True)

    # ---- Champs etendus KYC ----
    # Miroirs de matching (croisables avec les champs etendus des listes)
    client_iban = Column(String(50), nullable=True)
    client_bic = Column(String(20), nullable=True)
    client_tax_id = Column(String(100), nullable=True)
    client_phone = Column(String(100), nullable=True)
    client_email = Column(String(255), nullable=True)
    client_website = Column(String(255), nullable=True)
    client_crypto_wallets = Column(JSON, nullable=True)  # liste d'adresses
    # Gouvernance & priorisation (exploitables par les regles anti-FP)
    client_risk_rating = Column(String(20), nullable=True)      # FAIBLE / MOYEN / ELEVE
    client_pep_flag = Column(Boolean, nullable=True)            # PEP auto-declare
    client_segment = Column(String(50), nullable=True)          # particulier / PME / corporate...
    client_activity_sector = Column(String(100), nullable=True) # code NAF / secteur
    client_activity_countries = Column(JSON, nullable=True)     # pays d'exposition operationnelle
    client_relationship_start = Column(String(20), nullable=True)
    client_status = Column(String(20), nullable=True)           # actif / cloture / gele

    # Checksum for version comparisons
    entity_checksum = Column(String(64), nullable=False)

class AuditTrail(Base):
    __tablename__ = "compliance_audit_trail"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    client_id = Column(String(100), nullable=True)
    client_name = Column(String(1000), nullable=False)
    client_type = Column(String(10), nullable=False)
    watchlist_id = Column(String(100), nullable=False)
    watchlist_name = Column(String(1000), nullable=False)
    base_score = Column(Float, nullable=False)
    final_score = Column(Float, nullable=False)
    status = Column(String(20), nullable=False)
    decision_tree = Column(JSON, nullable=False)
    config_state = Column(JSON, nullable=False)
    watchlist_version = Column(String(50), nullable=False)
    watchlist_hash = Column(String(64), nullable=False)
    # Type de liste d'origine du liste (WATCHLIST_OFAC, WATCHLIST_UN...) —
    # NULL sur les enregistrements anterieurs a l'ajout (journal immuable,
    # jamais reecrit) et sur les decisions sans candidat
    list_type = Column(String(30), nullable=True)

class ReviewRecord(Base):
    """
    Dossier d'homologation : ce qui a ete decide, sur quelle base, et pourquoi.

    Pourquoi FIGER plutot que recalculer
    ------------------------------------
    Le delta d'une liste candidate se lit « par rapport a la production ». Or
    approuver la liste EN FAIT la production : la comparaison d'origine devient
    irreproductible, et un recalcul apres coup comparerait le snapshot a
    lui-meme. Le rapport du cahier de tests, lui, vit sur le snapshot, mais son
    contexte (base de comparaison, exclusions posees, verdict au moment de la
    decision) se perd des la synchronisation suivante.

    Un dossier est donc constitue AU MOMENT DE LA DECISION et n'est plus jamais
    reecrit : c'est ce qui permet de rouvrir des mois plus tard une
    homologation et d'y retrouver exactement ce que le reviseur avait sous les
    yeux. Meme principe que la piste d'audit de criblage.
    """
    __tablename__ = "review_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_id = Column(String(50), nullable=False, index=True)
    file_type = Column(String(50), nullable=False, index=True)
    file_name = Column(String(255), nullable=True)
    # APPROVED : mis en production · REJECTED : ecarte definitivement
    decision = Column(String(20), nullable=False, index=True)
    decided_by = Column(String(100), nullable=True)
    decided_at = Column(DateTime, default=datetime.utcnow, index=True)
    comment = Column(Text, nullable=True)
    # Base de comparaison : la liste qui etait en production a cet instant
    production_snapshot_id = Column(String(50), nullable=True)
    record_count = Column(Integer, nullable=True)
    excluded_count = Column(Integer, default=0)
    # Delta fige (memes formes que l'ecran d'homologation : resume exact,
    # details bornes par _truncate_delta_details)
    delta_summary = Column(JSON, nullable=True)
    delta_details = Column(JSON, nullable=True)
    # Cahier de tests tel qu'il etait au moment de la decision
    backtest_report = Column(JSON, nullable=True)
    backtest_at = Column(DateTime, nullable=True)
    backtest_by = Column(String(100), nullable=True)


class SyncReport(Base):
    __tablename__ = "sync_reports"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(20), nullable=False)             # OFAC, EURLEX
    executed_at = Column(DateTime, default=datetime.utcnow)
    trigger = Column(String(20), default="MANUAL")          # MANUAL, SCHEDULED
    status = Column(String(30), nullable=False)             # SUCCESS, NO_CHANGE, NO_PUBLICATION, ERROR
    message = Column(Text, nullable=True)
    snapshot_id = Column(String(50), nullable=True)
    previous_snapshot_id = Column(String(50), nullable=True)
    added_count = Column(Integer, default=0)
    modified_count = Column(Integer, default=0)
    removed_count = Column(Integer, default=0)
    delta_report = Column(JSON, nullable=True)              # truncated delta details for the UI
    email_sent = Column(Boolean, default=False)

# Types de snapshots persistes en WatchlistEntity (listes officielles).
# Vit ici (et non dans api.py) pour que le demon travailleur et les modules
# moteur n'aient pas a importer l'application FastAPI ; api.py re-exporte.
# Les sources branchees sur le lecteur OpenSanctions declarent leur type dans
# le registre fiskr/sources.py : une seule source de verite, derivee ici.
from fiskr.sources import OPENSANCTIONS_SOURCES as _OS_SOURCES

WATCHLIST_FILE_TYPES = [
    "WATCHLIST_OFAC", "WATCHLIST_EU", "WATCHLIST_SSIE", "WATCHLIST_DGT",
    "WATCHLIST_UN", "WATCHLIST_PEP", "WATCHLIST_OFSI", "WATCHLIST_SECO",
    "WATCHLIST_OFAC_NONSDN", "WATCHLIST_CSL", "WATCHLIST_CANADA", "WATCHLIST_DFAT",
    "WATCHLIST_HK_SFC", "WATCHLIST_AMF", "WATCHLIST_WORLDBANK",
] + [s.file_type for s in _OS_SOURCES]


def production_watchlist_reference(db) -> tuple:
    """
    (version, empreinte) des listes EN PRODUCTION, derivees de la base.

    Miroir exact de ce que `load_watchlist_cache` pose dans le processus API
    (version constante, empreinte = hash du snapshot READY le plus recent).
    Le re-criblage execute dans le demon travailleur trace cette reference au
    journal d'audit : il ne peut pas lire le cache memoire d'un autre
    processus, et un audit portant « N/A » serait faux.
    """
    snap = db.query(Snapshot).filter(
        Snapshot.file_type.in_(WATCHLIST_FILE_TYPES),
        Snapshot.status == "READY",
    ).order_by(Snapshot.uploaded_at.desc()).first()
    return "Database Active Snapshot", (snap.file_hash if snap else "N/A")


JOB_STATUSES = ("QUEUED", "RUNNING", "DONE", "ERROR", "CANCELLED")

class Job(Base):
    """
    File de travaux persistee : chaque operation longue (cahier de tests,
    synchronisation, import, re-criblage, simulation...) vit ici, du depot a
    la fin. C'est le canal unique entre le processus API et le demon
    travailleur (fiskr/worker.py), et ce qui permet la REPRISE apres un
    redemarrage : un job RUNNING au battement de coeur perime est remis en
    file (relance de zero) tant que `attempts` < `max_attempts`, puis marque
    ERROR, relancable d'un clic.

    La progression (phase/processed/total) y est ecrite par le travailleur
    (commits throttles) : le registre memoire de fiskr/progress.py ne
    traverse pas les processus, cette ligne si. `result` porte le rapport
    final (borne : les listes de details sont deja tronquees en amont).
    """
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Jeton public, celui de GET /api/progress?id= — reprend les formats
    # existants (backtest:<snap>, sync:<source>, uuid d'import...). PAS unique :
    # un meme jeton revient a chaque execution (sync:ofac chaque jour) ; les
    # lectures prennent la ligne la plus recente.
    token = Column(String(100), nullable=False, index=True)
    kind = Column(String(40), nullable=False, index=True)
    label = Column(String(255), nullable=True)
    params = Column(JSON, nullable=True)
    status = Column(String(20), default="QUEUED", index=True)
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=2)
    not_before = Column(DateTime, nullable=True)   # backoff entre tentatives
    priority = Column(Integer, default=100)        # plus petit = plus urgent
    # Exclusivite : deux jobs portant la meme cle ne coexistent pas en file
    # (ex. "sync:ofac" — une meme source ne se synchronise pas deux fois)
    dedupe_key = Column(String(150), nullable=True, index=True)
    # Progression inter-processus (miroir du registre memoire)
    phase = Column(String(30), nullable=True)
    processed = Column(Integer, default=0)
    total = Column(Integer, nullable=True)
    snapshot_id = Column(String(50), nullable=True)
    # Execution
    claimed_by = Column(String(100), nullable=True)    # "host:pid"
    heartbeat_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    result = Column(JSON, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Alert(Base):
    """
    Alerte de criblage : objet de travail avec cycle de vie et decision 4-yeux.
    OPEN -> IN_PROGRESS (assignee) -> PENDING_VALIDATION (decision proposee)
    -> CLOSED_CONFIRMED | CLOSED_FALSE_POSITIVE ; ESCALATED en derivation.
    Le journal immuable reste compliance_audit_trail (audit_id).
    """
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    audit_id = Column(Integer, ForeignKey("compliance_audit_trail.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Canal d'origine : SCREENING (criblage clients) ou FILTERING (transactions)
    channel = Column(String(20), default="SCREENING", index=True)
    # Denormalise pour la file de travail
    client_id = Column(String(100), nullable=True)
    client_name = Column(String(1000), nullable=False)
    watchlist_entity_id = Column(String(100), nullable=False)
    watchlist_name = Column(String(1000), nullable=False)
    final_score = Column(Float, nullable=False)
    # Type de liste d'origine (NULL sur les alertes anterieures a l'ajout)
    list_type = Column(String(30), nullable=True)
    # Cycle de vie
    status = Column(String(30), default="OPEN", index=True)
    assigned_to = Column(String(100), nullable=True)
    # Decision proposee (1er regard)
    proposed_decision = Column(String(30), nullable=True)  # CONFIRMED, FALSE_POSITIVE
    proposed_by = Column(String(100), nullable=True)
    proposed_at = Column(DateTime, nullable=True)
    proposal_comment = Column(Text, nullable=True)
    # Decision finale (2e regard, ou 1er si 4-yeux desactive)
    decided_by = Column(String(100), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_comment = Column(Text, nullable=True)
    # Case management : priorite explicite (calculee a la creation, modifiable)
    # et echeance SLA (due_at = created_at + delai du reglage par priorite)
    priority = Column(String(12), nullable=True, index=True)  # LOW|MEDIUM|HIGH|CRITICAL
    due_at = Column(DateTime, nullable=True)
    # Mise en attente (« attente de pieces, revoir le 12 ») : la file classe
    # l'alerte APRES les actives tant que l'echeance n'est pas passee — jamais
    # masquee, et le SLA (due_at) continue de courir : reporter son travail ne
    # reporte pas l'obligation. Chaque report est trace dans alert_events.
    snoozed_until = Column(DateTime, nullable=True)
    # Checklist d'instruction (dossier) : {"0": {done, by, at}, ...}
    checklist_state = Column(JSON, nullable=True)

ALERT_PRIORITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")

class AlertAttachment(Base):
    """Piece jointe d'une alerte (justificatif d'instruction) : stockage
    fichier + reference en base, meme motif que les preuves whitelist."""
    __tablename__ = "alert_attachments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_id = Column(Integer, ForeignKey("alerts.id"), nullable=False, index=True)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    comment = Column(Text, nullable=True)
    uploaded_by = Column(String(100), nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

class AdminAuditLog(Base):
    """Journal append-only des actions d'administration (utilisateurs,
    reglages, purges, revocations) : qui, quand, quoi, avant -> apres.
    Jamais modifie ni purge — attendu en controle ACPR/FED."""
    __tablename__ = "admin_audit_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    at = Column(DateTime, default=datetime.utcnow, index=True)
    username = Column(String(100), nullable=False)
    action = Column(String(50), nullable=False, index=True)
    target = Column(String(255), nullable=True)
    before = Column(JSON, nullable=True)
    after = Column(JSON, nullable=True)
    detail = Column(Text, nullable=True)


def log_admin_action(db, username, action, target=None,
                     before=None, after=None, detail=None):
    """Trace append-only d'une action d'administration (commit par l'appelant).
    Vit ici, a cote du modele, pour etre appelable des deux processus : l'API
    ET le demon travailleur (la fouille d'homonymes plantait chaque nuit sur
    cet import quand la fonction n'existait que dans fiskr.api)."""
    db.add(AdminAuditLog(username=username, action=action, target=target,
                         before=before, after=after, detail=detail))


ALERT_OPEN_STATUSES = ("OPEN", "IN_PROGRESS", "ESCALATED", "PENDING_VALIDATION")
ALERT_CLOSED_STATUSES = ("CLOSED_CONFIRMED", "CLOSED_FALSE_POSITIVE", "CLOSED_BY_RULE")

# Types de relations entre entites listees (schema pivot). Les types OFAC
# non reconnus sont conserves tels quels (colonne texte libre en plus du code).
RELATION_TYPES = (
    "OWNED_BY",          # detenu ou controle par (support de la regle des 50 %)
    "ACTING_FOR",        # agit pour le compte de
    "ASSOCIATE_OF",      # associe de
    "FAMILY_OF",         # membre de la famille de
    "LEADER_OF",         # dirigeant / role de direction dans
    "PROVIDING_SUPPORT", # apporte un soutien a
    "OTHER",
)

class EntityRelationship(Base):
    """
    Lien entre deux entites listees (graphe de relations / ownership) :
    `from` --[relation]--> `to`, ex. FILIALE --OWNED_BY--> MAISON-MERE.
    References par entity_id (stable a travers les snapshots). Sources :
    extraction OFAC (ProfileRelationships du SDN_ADVANCED, rafraichie a chaque
    ingestion) ou saisie manuelle (avec % de detention pour la regle des 50 %).
    """
    __tablename__ = "entity_relationships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_entity_id = Column(String(100), nullable=False, index=True)
    to_entity_id = Column(String(100), nullable=False, index=True)
    relation_type = Column(String(30), nullable=False)          # code pivot RELATION_TYPES
    relation_label = Column(String(200), nullable=True)         # libelle source (ex. OFAC)
    ownership_pct = Column(Float, nullable=True)                # % de detention (manuel)
    source = Column(String(20), default="MANUAL", index=True)   # OFAC | MANUAL
    comment = Column(Text, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# Index composites/manquants pour les requetes chaudes a volumetrie reelle.
# Declares hors modeles : crees de facon idempotente dans init_db (create_all
# ne rajoute pas d'index sur une table existante).
_PERFORMANCE_INDEXES = (
    Index("ix_alerts_status_channel", Alert.status, Alert.channel),
    Index("ix_alerts_client_entity", Alert.client_id, Alert.watchlist_entity_id),
    # ---- Dates des alertes ----
    # Depuis qu'un criblage ouvre une alerte PAR CORRESPONDANCE, cette table
    # grossit du nombre d'homonymes : un seul « Mohammed Ali » sans pays en
    # ajoute 2 976. Or l'accueil, les exports, les indicateurs et la courbe
    # journaliere filtrent et trient tous sur ces deux dates, dont aucune
    # n'etait indexee — chaque chargement de l'accueil parcourait la table
    # entiere deux fois (« alertes creees 24 h », « decidees 24 h »).
    Index("ix_alerts_created_at", Alert.created_at),
    Index("ix_alerts_decided_at", Alert.decided_at),
    # Cle etrangere vers le journal d'audit. PostgreSQL n'indexe PAS
    # automatiquement le cote referencant : sans cet index, chaque ligne de
    # `compliance_audit_trail` supprimee par la retention declenche un parcours
    # SEQUENTIEL de `alerts` pour verifier l'integrite referentielle. Purger
    # 100 000 lignes d'audit valait 100 000 parcours d'une table qui se compte
    # desormais en millions.
    Index("ix_alerts_audit_id", Alert.audit_id),
    Index("ix_audit_trail_timestamp", AuditTrail.timestamp),
    Index("ix_audit_trail_client_id", AuditTrail.client_id),
    # Filtres du journal d'audit (/api/history) : statut de decision et liste
    Index("ix_audit_trail_status", AuditTrail.status),
    Index("ix_audit_trail_list_type", AuditTrail.list_type),
    Index("ix_wl_entities_snapshot_id", WatchlistEntity.snapshot_id),
    Index("ix_wl_entities_entity_id", WatchlistEntity.entity_id),
    # Couple (lot, identifiant) : c'est la cle de rapprochement du calcul de
    # delta, qui compare deux instantanes fiche par fiche. Les deux index
    # separes ne servent chacun qu'une moitie de la condition ; celui-ci sert
    # l'anti-jointure « present dans l'un, absent de l'autre » et la jointure
    # sur empreintes en une seule lecture.
    Index("ix_wl_entities_snapshot_entity", WatchlistEntity.snapshot_id,
          WatchlistEntity.entity_id),
    Index("ix_client_entities_snapshot_id", ClientEntity.snapshot_id),
    Index("ix_client_entities_client_id", ClientEntity.client_id),
    # File de travaux : le claim parcourt QUEUED par priorite puis anciennete
    Index("ix_jobs_claim", Job.status, Job.priority, Job.id),
    # ---- Consultation des listes (/api/watchlist/db) ----
    # Sans eux, afficher UNE page de 50 fiches lisait la table ENTIERE : le
    # perimetre « production » se definit par des colonnes de `snapshots`
    # (statut, type) et par `excluded`, dont aucune n'etait indexee. Mesure sur
    # un jeu aux proportions reelles (2,79 M lignes, 92 % hors production) :
    # parcours sequentiel de 2,79 M lignes pour rendre 50 — 327 ms pour la page
    # et 263 ms pour le compte ; en production (11,2 M lignes, 9 Go) : 18 s.
    Index("ix_snapshots_status_type", Snapshot.status, Snapshot.file_type),
    Index("ix_snapshots_uploaded_at", Snapshot.uploaded_at),
    # Index PARTIEL : n'indexe que les fiches non exclues, c'est-a-dire
    # exactement le perimetre lu. Il porte (snapshot_id, id) : le planificateur
    # y trouve les fiches d'un lot deja triees par id, ce qui sert aussi le
    # tri par defaut (les lots sont ordonnes par date de televersement).
    # Meme mesure, avec : page 1,8 ms (x182), compte 55 ms (x4,7).
    Index("ix_wl_entities_production", WatchlistEntity.snapshot_id, WatchlistEntity.id,
          postgresql_where=WatchlistEntity.excluded.isnot(True)),
    # Index partiel SYMETRIQUE, pour le perimetre « Exclues ». Celui du dessus
    # ne sert QUE la negation : un index partiel sur `excluded IS NOT TRUE`
    # n'aide en rien la requete `excluded IS TRUE`, qui n'avait donc aucun
    # index et parcourait la table entiere. Mesure en production : le
    # perimetre EXCLUDED mettait 21 a 35 s pour rendre... zero ligne.
    # Celui-ci n'indexe que les fiches effectivement exclues — une poignee,
    # la ou l'autre en indexe des centaines de milliers.
    Index("ix_wl_entities_excluded", WatchlistEntity.snapshot_id, WatchlistEntity.id,
          postgresql_where=WatchlistEntity.excluded.is_(True)),
)

# Index de RECHERCHE plein texte (ILIKE « %terme% »), separes parce qu'ils ont
# un cout d'ecriture : ce sont des index GIN trigramme, qui exigent l'extension
# pg_trgm et ralentissent l'insertion d'environ 78 % (mesure : 50 000 fiches en
# 1,58 s sans, 2,82 s avec). Ils rendent en echange la recherche utilisable :
# 1 270 ms -> 17 ms sur le meme jeu (x73), et la production mesurait 54 s.
# Creation reservee a l'outil dedie (tools/create_perf_indexes.py) : ils ne
# sont PAS crees au demarrage, pour laisser l'exploitant arbitrer ce compromis.
TRIGRAM_SEARCH_INDEXES = (
    ("ix_wl_primary_name_trgm", "watchlist_entities", "primary_name"),
    ("ix_wl_entity_id_trgm", "watchlist_entities", "entity_id"),
)

# Au-dela de ce nombre de lignes, `init_db` NE CREE PAS un index manquant :
# un `CREATE INDEX` ordinaire prend un verrou exclusif pendant toute sa
# construction et figerait la production plusieurs minutes. L'exploitant lance
# alors tools/create_perf_indexes.py, qui construit en CONCURRENTLY, sans
# interruption de service. Les installations neuves (table vide) sont
# indexees automatiquement.
LARGE_TABLE_ROW_GUARD = 200_000

# Index de performance que `init_db` a DELIBEREMENT laisses de cote, faute de
# pouvoir les creer sans verrouiller une grosse table. L'information n'existait
# que dans un WARNING du demarrage — c'est-a-dire nulle part pour qui regarde
# l'application. La liste est relevee ici pour que l'ecran de mise en service
# puisse la montrer, et elle est reconstruite a chaque `init_db` : jamais une
# copie a maintenir, toujours ce que le dernier demarrage a constate.
_INDEX_DIFFERES: list = []


def index_de_performance_manquants() -> list:
    """(nom d'index, table) des index differes au dernier demarrage."""
    return list(_INDEX_DIFFERES)


def _add_missing_nullable_columns(engine, inspector) -> list:
    """
    Aligne les tables existantes sur le modele en AJOUTANT les colonnes
    manquantes. Ne supprime jamais rien.

    POURQUOI CETTE FONCTION EXISTE
    ------------------------------
    Le demarrage portait ceci :

        if "place_of_birth" not in columns:
            Base.metadata.drop_all(bind=engine)   # TOUTES les tables

    Autrement dit : une seule colonne NULLABLE absente — le genre d'ecart
    qu'une migration additive regle en une seconde — et l'application
    DETRUISAIT l'integralite de la base au demarrage, listes homologuees,
    alertes et journal d'audit immuable compris. Le defaut a ete constate en
    conditions reelles : 2,79 millions de fiches effacees par un simple
    demarrage sur un schema incomplet. Aucune sauvegarde n'est demandee,
    aucune confirmation, et le message journalise ne dit pas qu'il y a
    destruction (« Database schema outdated. Dropping and recreating
    tables... » au niveau INFO).

    Une colonne nullable s'AJOUTE : c'est ce que fait cette fonction, pour
    toutes les tables du modele et sans liste a tenir a la main — le type DDL
    est compile par SQLAlchemy pour le dialecte courant, donc valable aussi
    bien sur PostgreSQL que sur SQLite.

    Une colonne NOT NULL sans valeur par defaut, elle, ne peut pas etre
    ajoutee a une table deja peuplee : elle est SIGNALEE (avertissement
    explicite, action manuelle) plutot que traitee par une destruction.

    Retourne la liste des colonnes ajoutees, pour le journal et les tests.
    """
    from sqlalchemy import text as _t

    added = []
    existing_tables = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # table absente : `create_all` la creera complete
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            addable = (column.nullable
                       or column.default is not None
                       or column.server_default is not None)
            if not addable:
                logger.warning(
                    "Colonne %s.%s absente et NON NULL sans valeur par défaut : "
                    "elle ne peut pas être ajoutée à une table déjà peuplée. "
                    "Intervention manuelle requise — AUCUNE donnée n'est touchée.",
                    table.name, column.name,
                )
                continue
            ddl_type = column.type.compile(engine.dialect)
            # Identifiants issus du MODELE (jamais d'une entree utilisateur).
            try:
                with engine.begin() as conn:
                    conn.execute(_t(
                        f'ALTER TABLE {table.name} ADD COLUMN {column.name} {ddl_type}'
                    ))
                added.append(f"{table.name}.{column.name}")
            except Exception as e:
                logger.warning("Ajout de %s.%s impossible : %s",
                               table.name, column.name, e)
    if added:
        logger.info("Schéma mis à niveau — %d colonne(s) ajoutée(s) : %s",
                    len(added), ", ".join(added))
    return added


def _index_exists(engine, index_name: str) -> bool:
    """Vrai si l'index existe deja (tous moteurs). Jamais bloquant."""
    try:
        from sqlalchemy import inspect as _sa_inspect
        insp = _sa_inspect(engine)
        for table in insp.get_table_names():
            if any(ix.get("name") == index_name for ix in insp.get_indexes(table)):
                return True
    except Exception:
        pass
    return False


def _table_row_estimate(engine, table_name: str) -> int:
    """
    Estimation du nombre de lignes — JAMAIS un COUNT(*) : sur une table de
    plusieurs millions de lignes il coute lui-meme des secondes, au demarrage.
    PostgreSQL : statistiques du planificateur (`pg_class.reltuples`, gratuit).
    Ailleurs (SQLite de developpement) : 0, donc aucun garde-fou — les bases de
    test sont petites et doivent recevoir leurs index.
    """
    try:
        if engine.dialect.name != "postgresql":
            return 0
        from sqlalchemy import text as _t
        with engine.connect() as conn:
            row = conn.execute(_t(
                "SELECT COALESCE(reltuples, 0)::bigint FROM pg_class WHERE relname = :n"
            ), {"n": table_name}).first()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def refresh_source_relationships(db, source: str, relations) -> int:
    """
    Remplace l'integralite des relations d'une source (ex. OFAC) par le jeu
    fourni — rafraichissement idempotent a chaque ingestion de la liste.
    Les relations MANUAL ne sont jamais touchees. Commit par l'appelant.
    """
    db.query(EntityRelationship).filter(EntityRelationship.source == source) \
      .delete(synchronize_session=False)
    seen = set()
    count = 0
    for rel in relations or []:
        key = (rel.get("from_entity_id"), rel.get("to_entity_id"), rel.get("relation_type"))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        db.add(EntityRelationship(
            from_entity_id=rel["from_entity_id"],
            to_entity_id=rel["to_entity_id"],
            relation_type=rel.get("relation_type") or "OTHER",
            relation_label=rel.get("relation_label"),
            ownership_pct=rel.get("ownership_pct"),
            source=source,
        ))
        count += 1
    return count

BATCH_CAMPAIGN_STATUSES = ("RUNNING", "DONE", "ERROR")

class BatchCampaign(Base):
    """
    Campagne de criblage batch persistee : un fichier de clients ad hoc
    (upload manuel ou depot CFT dans l'inbox surveillee) crible cote serveur
    avec les MEMES garanties que le criblage unitaire (journal d'audit,
    alertes, liste blanche, regles anti-faux positifs).
    """
    __tablename__ = "batch_campaigns"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False)
    file_name = Column(String(255), nullable=True)
    trigger = Column(String(20), default="manual")   # manual | inbox (depot CFT)
    status = Column(String(20), default="RUNNING", index=True)
    error_message = Column(Text, nullable=True)
    screening_lists = Column(JSON, nullable=True)    # restriction eventuelle
    total_clients = Column(Integer, default=0)
    processed_clients = Column(Integer, default=0)
    # Trois comptes, trois questions distinctes.
    #
    # `alert_count`  : les CLIENTS qui declenchent au moins une correspondance.
    # `hits_count`   : les CORRESPONDANCES au-dessus du seuil — TOUTES, liste
    #                  blanche et regles anti-FP comprises. Un client homonyme
    #                  d'un nom courant en porte des centaines.
    # `opened_count` : les ALERTES reellement ouvertes.
    #
    # Les deux derniers etaient confondus, et le commentaire de cette colonne
    # affirmait meme que le criblage « ouvre une alerte chacune ». C'est faux
    # d'exactement ce que le dispositif absorbe : une correspondance en liste
    # blanche n'ouvre rien, une correspondance close par regle est ouverte puis
    # refermee. L'ecart entre les deux chiffres EST le travail epargne — le
    # confondre avec le travail restant le rendait invisible.
    #
    # Colonne additive : les campagnes anterieures portent NULL, et un ecran
    # qui ne sait pas ne doit pas inventer.
    alert_count = Column(Integer, default=0)
    hits_count = Column(Integer, nullable=True, default=0)
    opened_count = Column(Integer, nullable=True, default=0)
    no_match_count = Column(Integer, default=0)
    rejected_count = Column(Integer, default=0)      # lignes refusees par le quality gate
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)

class BatchResult(Base):
    """Resultat unitaire d'une campagne batch (lie au journal d'audit immuable)."""
    __tablename__ = "batch_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    campaign_id = Column(Integer, ForeignKey("batch_campaigns.id"), nullable=False, index=True)
    client_id = Column(String(100), nullable=True)
    client_name = Column(String(1000), nullable=True)
    status = Column(String(20), nullable=False)      # ALERT | NO_MATCH | WHITELISTED | REJECTED
    final_score = Column(Float, nullable=True)
    watchlist_entity_id = Column(String(100), nullable=True)
    watchlist_name = Column(String(1000), nullable=True)
    list_type = Column(String(30), nullable=True)
    audit_id = Column(Integer, nullable=True)
    alert_id = Column(Integer, nullable=True)
    error = Column(Text, nullable=True)              # motif de rejet quality gate

class SavedView(Base):
    """
    Vue sauvegardee d'une file d'alertes : un analyste memorise sa combinaison
    de filtres (statuts, priorite, type de liste) sous un nom et la restaure
    en un clic. Propre a chaque utilisateur.
    """
    __tablename__ = "saved_views"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    channel = Column(String(20), nullable=False, default="SCREENING")
    filters = Column(JSON, nullable=False)           # {status, priority, list_type}
    created_at = Column(DateTime, default=datetime.utcnow)

class UserDashboard(Base):
    """
    Disposition personnalisee de la vue d'ensemble : chaque utilisateur
    compose son accueil (choix des panneaux, ordre, tailles). L'absence de
    ligne signifie « disposition par defaut livree avec l'application » —
    la remise a zero supprime simplement la ligne.
    """
    __tablename__ = "user_dashboards"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    widgets = Column(JSON, nullable=False)           # [{id, size}] ordonnes
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

FP_RULE_STATUSES = ("DRAFT", "PENDING_VALIDATION", "ACTIVE", "SUPERSEDED")

class FpRule(Base):
    """
    Regle Python anti-faux positifs, par canal (SCREENING = criblage clients,
    FILTERING = filtrage transactionnel). Le code doit definir rule(ctx) -> bool
    (True = supprimer l'alerte candidate, qui est alors creee puis auto-cloturee
    CLOSED_BY_RULE — jamais de suppression silencieuse, exigence ACPR/FED).

    Cycle de vie facon branche/merge : DRAFT (mode DEV, jamais appliquee en
    production) -> PENDING_VALIDATION (figee, tests unitaires verts exiges)
    -> ACTIVE (validation 4-yeux par un autre utilisateur habilite) ;
    la modification d'une regle ACTIVE cree une NOUVELLE version DRAFT
    (replaces_rule_id) qui, validee, remplace l'ancienne (SUPERSEDED).
    """
    __tablename__ = "fp_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel = Column(String(20), nullable=False, index=True)  # SCREENING | FILTERING
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    code = Column(Text, nullable=False)
    status = Column(String(30), default="DRAFT", index=True)
    enabled = Column(Boolean, default=True)   # interrupteur des regles ACTIVE
    run_order = Column(Integer, default=100)
    # Perimetres ou la regle s'applique (liste JSON : SANCTION, HORS_SANCTION).
    # NULL = tous, ce qui est le comportement des regles ecrites avant cette
    # colonne. Le moteur FILTRE dessus : une regle limitee au hors-sanction ne
    # peut pas cloturer une correspondance de gel d'avoirs, meme si son code
    # l'oublie. Un controleur lit la portee sur la regle, pas dans son code.
    perimeters = Column(JSON, nullable=True)
    hit_count = Column(Integer, default=0)
    version = Column(Integer, default=1)
    replaces_rule_id = Column(Integer, nullable=True)  # version ACTIVE remplacee
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_by = Column(String(100), nullable=True)
    updated_at = Column(DateTime, nullable=True)
    submitted_by = Column(String(100), nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    validated_by = Column(String(100), nullable=True)
    validated_at = Column(DateTime, nullable=True)
    validation_comment = Column(Text, nullable=True)

class FpRuleChange(Base):
    """Journal immuable des modifications de regles (qui, quand, quel code)."""
    __tablename__ = "fp_rule_changes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, nullable=False, index=True)
    rule_name = Column(String(200), nullable=False)
    channel = Column(String(20), nullable=False)
    # CREATED, UPDATED, SUBMITTED, VALIDATED, REJECTED, ENABLED, DISABLED, DELETED
    action = Column(String(20), nullable=False)
    old_code = Column(Text, nullable=True)
    new_code = Column(Text, nullable=True)
    comment = Column(Text, nullable=True)
    changed_by = Column(String(100), nullable=False)
    changed_at = Column(DateTime, default=datetime.utcnow)

class FpRuleTest(Base):
    """
    Test unitaire d'une regle (mode DEV) : contexte d'alerte JSON + resultat
    attendu. La soumission en validation exige 100 % de tests verts.
    """
    __tablename__ = "fp_rule_tests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(Integer, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    ctx = Column(JSON, nullable=False)
    expected = Column(Boolean, nullable=False)  # True = la regle doit supprimer
    last_result = Column(Boolean, nullable=True)
    last_error = Column(Text, nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class AlertFollower(Base):
    """
    Suivi d'un dossier d'alerte sans se l'assigner : le suiveur est notifie
    des actions des autres (categorie criblage, recapitulatif periodique).
    Personnel et reversible — ce n'est PAS un acte d'instruction, donc rien
    n'en va au journal immuable de l'alerte.
    """
    __tablename__ = "alert_followers"
    __table_args__ = (UniqueConstraint("alert_id", "username", name="uq_alert_follower"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_id = Column(Integer, ForeignKey("alerts.id"), nullable=False, index=True)
    username = Column(String(100), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class AlertEvent(Base):
    """Historique append-only des actions sur une alerte (jamais modifie)."""
    __tablename__ = "alert_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    alert_id = Column(Integer, ForeignKey("alerts.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow)
    username = Column(String(100), nullable=False)
    action = Column(String(30), nullable=False)  # CREATED, REDETECTED, ASSIGNED, COMMENT, ESCALATED, PROPOSED, VALIDATED, RETURNED
    detail = Column(Text, nullable=True)

class WhitelistPair(Base):
    """
    Liste blanche client x liste (« Good Guys », guidance Wolfsberg) : supprime
    les alertes recurrentes d'un faux positif avere, avec justification
    gouvernee. Revocation douce uniquement (jamais de suppression physique) ;
    chaque suppression d'alerte reste tracee dans le journal d'audit.
    """
    __tablename__ = "whitelist_pairs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String(100), nullable=False, index=True)
    watchlist_entity_id = Column(String(100), nullable=False, index=True)
    client_name = Column(String(1000), nullable=True)
    watchlist_name = Column(String(1000), nullable=True)
    # Type de liste d'origine du liste (NULL sur les paires anterieures)
    list_type = Column(String(30), nullable=True)
    justification = Column(Text, nullable=True)
    evidence_file_name = Column(String(255), nullable=True)
    evidence_file_path = Column(String(500), nullable=True)
    created_by = Column(String(100), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)  # gouvernance : revue periodique
    revoked_by = Column(String(100), nullable=True)
    revoked_at = Column(DateTime, nullable=True)
    revoke_comment = Column(Text, nullable=True)

class LearnedEquivalence(Base):
    """
    Equivalence linguistique DECOUVERTE par le moteur de fouille, par
    opposition a celles declarees a la main dans les fichiers `resources/`.

    Chaque ligne porte sa PREUVE : les fiches listees ou les alertes qui l'ont
    fait apparaitre. Une equivalence qui elargit le perimetre des alertes doit
    pouvoir etre justifiee devant un controleur — « le moteur l'a trouvee » ne
    suffit pas, il faut pouvoir montrer quoi.

    Cycle de vie : PROPOSED -> APPROVED (appliquee au criblage) | REJECTED.
    Une equivalence approuvee reste revocable : le retour a REJECTED la retire
    de l'index au rechargement suivant.
    """
    __tablename__ = "learned_equivalences"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Type de champ (given_name, surname, city, country, state)
    field = Column(String(30), nullable=False, index=True)
    # Classe canonique visee : celle d'un groupe existant que la paire rejoint,
    # ou une classe neuve nommee d'apres le terme le plus frequent
    class_id = Column(String(100), nullable=False)
    # Termes de la paire decouverte, normalises
    term_a = Column(String(200), nullable=False)
    term_b = Column(String(200), nullable=False)
    # Cle de deduplication : field + les deux termes tries
    signature = Column(String(500), unique=True, nullable=False, index=True)
    # ALIAS (graphe d'alias des listes) | ANALYST (alerte confirmee en revue)
    source = Column(String(30), nullable=False, index=True)
    # Nombre de fiches/alertes DISTINCTES portant la paire
    occurrences = Column(Integer, nullable=False, default=1)
    # Similarite de chaine et concordance phonetique, pour la confiance
    similarity = Column(Float, nullable=False, default=0.0)
    phonetic_match = Column(Boolean, nullable=False, default=False)
    confidence = Column(Float, nullable=False, default=0.0, index=True)
    # Echantillon de preuves : [{entity_id, primary_name, alias, list_type}, ...]
    evidence = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default="PROPOSED", index=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    # « systeme » quand l'auto-approbation a tranche, sinon l'utilisateur
    decided_by = Column(String(100), nullable=True)
    decided_at = Column(DateTime, nullable=True)
    decision_comment = Column(Text, nullable=True)

class AppSetting(Base):
    __tablename__ = "app_settings"

    key = Column(String(100), primary_key=True)
    value = Column(JSON, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(100), nullable=True)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    salt = Column(String(64), nullable=False)
    full_name = Column(String(255), nullable=True)
    # Adresse de notification : les mails d'etape sont routes par ROLE vers les
    # comptes qui ont une adresse renseignee (repli sur NOTIFY_EMAIL_TO sinon)
    email = Column(String(255), nullable=True)
    role = Column(String(50), default="admin")
    created_at = Column(DateTime, default=datetime.utcnow)
    # Anti-brute-force : compteur d'echecs consecutifs + verrouillage temporaire
    failed_login_count = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    # MFA TOTP (RFC 6238) : secret base32 stocke des la phase d'enrolement,
    # actif seulement quand totp_enabled est vrai (confirmation par un code)
    totp_secret = Column(String(64), nullable=True)
    totp_enabled = Column(Boolean, default=False)
    # Delegation d'absence : jusqu'a absent_until, les assignations d'alertes
    # destinees a ce compte sont redirigees vers delegate_to
    absent_until = Column(DateTime, nullable=True)
    delegate_to = Column(String(100), nullable=True)
    # Profil enrichi (espace « Mon compte ») : photo (data-URI compressee cote
    # client), description libre, telephone, fonction. L'identite d'un acteur
    # de conformite ne se resume pas a un pseudo.
    avatar = Column(Text, nullable=True)
    bio = Column(Text, nullable=True)
    phone = Column(String(50), nullable=True)
    job_title = Column(String(150), nullable=True)
    # Categories de notifications coupees par CE compte (["ALL"] = tout couper).
    # Filtre personnel applique par-dessus le routage par role du notifier.
    notification_opt_out = Column(JSON, nullable=True)

class ApiKey(Base):
    """
    Cle d'API technique (compte de service) pour les integrations systemes
    (CFT, ordonnanceurs, SI amont) : la cle complete « fsk_... » n'est montree
    QU'A LA CREATION ; seuls le prefixe (identification) et le hash SHA-256
    (verification) sont stockes. Revocation douce, jamais de suppression.
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    prefix = Column(String(20), unique=True, nullable=False, index=True)
    key_hash = Column(String(64), nullable=False)
    roles = Column(String(50), default="user")
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    revoked_by = Column(String(100), nullable=True)
    revoked_at = Column(DateTime, nullable=True)


class NotificationDelivery(Base):
    """
    Journal des notifications d'etape ET file d'attente du recapitulatif.

    - urgence IMMEDIATE : la ligne est ecrite avec le resultat de l'envoi
      (SENT / FAILED) — le journal repond a « le mail est-il parti ? » sans
      fouiller les logs serveur ;
    - urgence DIGEST : la ligne est creee en QUEUED, puis passee a SENT par la
      boucle de regroupement qui compose un seul mail par destinataire.
    Purge TTL 90 jours par la meme boucle.
    """
    __tablename__ = "notification_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_key = Column(String(60), nullable=False, index=True)
    category = Column(String(40), nullable=True)
    urgency = Column(String(20), nullable=True)          # immediate | digest
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    payload = Column(JSON, nullable=True)
    recipients = Column(Text, nullable=True)             # adresses, separees par des virgules
    status = Column(String(20), default="QUEUED", index=True)  # QUEUED|SENT|FAILED|SKIPPED
    sent_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)


class HookDelivery(Base):
    """
    Livraison d'un webhook entrant (SI amont) : garantit l'idempotence par
    cle X-Idempotency-Key — la reponse d'origine est stockee et rejouee a
    l'identique en cas de retransmission (retries reseau de l'appelant).
    Table auto-nettoyee : les livraisons de plus de 90 jours sont purgees
    opportunistement a chaque appel de webhook.
    """
    __tablename__ = "hook_deliveries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    idempotency_key = Column(String(200), unique=True, nullable=False, index=True)
    endpoint = Column(String(100), nullable=False)      # screening | client-upsert
    caller = Column(String(200), nullable=True)          # apikey:<nom>
    status_code = Column(Integer, nullable=False, default=200)
    response_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


import secrets
import os
import re
from typing import Optional


def safe_upload_filename(filename: Optional[str], default: str = "upload.bin") -> str:
    """
    Reduit un nom de televersement a un basename inoffensif : jamais de
    separateur de chemin (ni « / » ni « \\ »), jamais « .. », jamais de nom
    absolu, jamais de fichier cache. Un nom comme « ../../passenger_wsgi.py »
    ou « /etc/cron.d/x » ne peut donc plus faire ecrire le televersement HORS
    du repertoire prevu (path traversal / ecriture arbitraire). Le nom
    d'origine reste affiche a l'utilisateur ailleurs ; SEUL le chemin disque
    passe par ici.
    """
    base = os.path.basename((filename or "").replace("\\", "/"))
    base = re.sub(r"[^\w.\-]", "_", base).lstrip(".")
    return (base or default)[:200]


def hash_password(password: str, salt_hex: str = None) -> tuple[str, str]:
    """Hashes a password securely using PBKDF2 HMAC SHA-256 and 100,000 iterations."""
    salt = bytes.fromhex(salt_hex) if salt_hex else os.urandom(16)
    hashed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
    return hashed.hex(), salt.hex()

def verify_password(password: str, stored_hash: str, stored_salt: str) -> bool:
    """Verifies a plain-text password against a stored hash and salt."""
    try:
        salt = bytes.fromhex(stored_salt)
        hashed = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 100_000)
        return secrets.compare_digest(hashed.hex(), stored_hash)
    except Exception:
        return False


# Setup Database Engine
db_config = config.get("database", {})
pg_url = db_config.get("url", "postgresql://postgres:postgres@localhost:5432/fiskr")
sqlite_path = db_config.get("sqlite_path", "fiskr.sqlite3")
fallback = db_config.get("fallback_to_sqlite", True)

engine = None
SessionLocal = None


def _tune_sqlite(eng):
    """
    Regle SQLite pour que l'application RESTE DISPONIBLE pendant une operation
    longue (ingestion d'une liste consolidee, mise en production, re-criblage).

    En mode journal par defaut (DELETE), un ecrivain pose un verrou exclusif sur
    tout le fichier : pendant les minutes que dure une ingestion, AUCUNE autre
    requete ne peut lire. Comme toutes les routes touchent la base — la
    dependance d'authentification elle-meme fait une lecture —, c'est l'API
    entiere qui devenait injoignable, y compris /api/progress : l'ecran semblait
    gele et sa progression ne s'affichait plus.

    - journal_mode=WAL : les lecteurs ne sont jamais bloques par l'ecrivain ;
    - busy_timeout : un ecrivain concurrent attend au lieu d'echouer aussitot
      sur « database is locked » ;
    - synchronous=NORMAL : compromis recommande avec WAL (durabilite conservee
      hors coupure d'alimentation, ecritures nettement moins couteuses).

    Sans effet sur PostgreSQL, qui ne connait pas ces pragmas et ne souffre pas
    du probleme (MVCC).
    """
    from sqlalchemy import event

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA synchronous=NORMAL")
        except Exception as exc:  # base en lecture seule, systeme de fichiers reseau...
            logger.warning(f"Impossible d'appliquer les pragmas SQLite (WAL) : {exc}")
        finally:
            cursor.close()


def init_db():
    global engine, SessionLocal
    try:
        if pg_url.startswith("postgresql"):
            logger.info("Attempting to connect to PostgreSQL database...")
            engine = create_engine(pg_url, connect_args={"connect_timeout": 3})
            # Test connection
            with engine.connect() as conn:
                pass
            logger.info("Successfully connected to PostgreSQL.")
        else:
            raise ValueError("Not a PostgreSQL URL")
    except Exception as e:
        if fallback:
            try:
                err_msg = str(e)
            except Exception:
                err_msg = repr(e)
            if "codec" in err_msg.lower() and ("decode" in err_msg.lower() or "utf-8" in err_msg.lower()):
                err_msg = "OperationalError (Connection refused or database unreachable on localhost:5432)"
            logger.warning(f"Failed to connect to PostgreSQL: {err_msg}. Falling back to SQLite.")
            sqlite_url = f"sqlite:///{sqlite_path}"
            engine = create_engine(sqlite_url, connect_args={"check_same_thread": False})
            _tune_sqlite(engine)
        else:
            try:
                err_msg = str(e)
            except Exception:
                err_msg = repr(e)
            if "codec" in err_msg.lower() and ("decode" in err_msg.lower() or "utf-8" in err_msg.lower()):
                err_msg = "OperationalError (Connection refused or database unreachable on localhost:5432)"
            logger.error(f"Failed to connect to database and fallback is disabled: {err_msg}")
            raise e

    from sqlalchemy import inspect, text
    try:
        inspector = inspect(engine)

        # Migrations additives (colonnes nullables) : homologation / exclusions
        _additive_migrations = {
            "snapshots": [
                ("reviewed_by", "VARCHAR(100)"),
                ("reviewed_at", "TIMESTAMP"),
                ("review_comment", "TEXT"),
                ("backtest_report", "JSON"),
                ("backtest_at", "TIMESTAMP"),
                ("backtest_by", "VARCHAR(100)"),
                ("processed_count", "INTEGER"),
                ("total_hint", "INTEGER"),
                ("phase", "VARCHAR(30)"),
            ],
            "watchlist_entities": [
                ("excluded", "BOOLEAN"),
                ("exclusion_justification", "TEXT"),
                ("exclusion_file_name", "VARCHAR(255)"),
                ("exclusion_file_path", "VARCHAR(500)"),
                ("excluded_by", "VARCHAR(100)"),
                ("excluded_at", "TIMESTAMP"),
                ("official_reference", "VARCHAR(500)"),
                ("modified_by", "VARCHAR(100)"),
                ("modified_at", "TIMESTAMP"),
                # Champs etendus (extraction structuree des sources)
                ("crypto_wallets", "JSON"),
                ("bic_swift", "VARCHAR(20)"),
                ("tax_id", "VARCHAR(100)"),
                ("duns_number", "VARCHAR(20)"),
                ("vessel_call_sign", "VARCHAR(50)"),
                ("vessel_mmsi", "VARCHAR(20)"),
                ("vessel_flag", "VARCHAR(100)"),
                ("vessel_type", "VARCHAR(100)"),
                ("vessel_tonnage", "VARCHAR(50)"),
                ("vessel_owner", "VARCHAR(500)"),
                ("aircraft_model", "VARCHAR(200)"),
                ("aircraft_operator", "VARCHAR(200)"),
                ("aircraft_construction_number", "VARCHAR(100)"),
                ("sanction_programs", "JSON"),
                ("listed_on", "VARCHAR(20)"),
                ("delisted_on", "VARCHAR(20)"),
                ("name_original_script", "VARCHAR(1000)"),
                ("title", "VARCHAR(255)"),
                ("pep_role", "VARCHAR(500)"),
                ("secondary_sanctions_risk", "TEXT"),
                ("designating_state", "VARCHAR(200)"),
                ("organization_established_date", "VARCHAR(20)"),
                ("organization_type", "VARCHAR(200)"),
                ("phone_numbers", "JSON"),
                ("email_addresses", "JSON"),
                ("websites", "JSON"),
            ],
            "client_entities": [
                # Champs etendus KYC
                ("client_iban", "VARCHAR(50)"),
                ("client_bic", "VARCHAR(20)"),
                ("client_tax_id", "VARCHAR(100)"),
                ("client_phone", "VARCHAR(100)"),
                ("client_email", "VARCHAR(255)"),
                ("client_website", "VARCHAR(255)"),
                ("client_crypto_wallets", "JSON"),
                ("client_risk_rating", "VARCHAR(20)"),
                ("client_pep_flag", "BOOLEAN"),
                ("client_segment", "VARCHAR(50)"),
                ("client_activity_sector", "VARCHAR(100)"),
                ("client_activity_countries", "JSON"),
                ("client_relationship_start", "VARCHAR(20)"),
                ("client_status", "VARCHAR(20)"),
            ],
            "alerts": [
                ("list_type", "VARCHAR(30)"),
                ("channel", "VARCHAR(20)"),
                ("priority", "VARCHAR(12)"),
                ("due_at", "TIMESTAMP"),
                ("checklist_state", "JSON"),
            ],
            "compliance_audit_trail": [
                ("list_type", "VARCHAR(30)"),
            ],
            "whitelist_pairs": [
                ("list_type", "VARCHAR(30)"),
            ],
            "users": [
                ("failed_login_count", "INTEGER"),
                ("locked_until", "TIMESTAMP"),
                ("totp_secret", "VARCHAR(64)"),
                ("totp_enabled", "BOOLEAN"),
                ("absent_until", "TIMESTAMP"),
                ("delegate_to", "VARCHAR(100)"),
                ("email", "VARCHAR(255)"),
                ("avatar", "TEXT"),
                ("bio", "TEXT"),
                ("phone", "VARCHAR(50)"),
                ("job_title", "VARCHAR(150)"),
                ("notification_opt_out", "TEXT"),
            ],
        }
        inspector = inspect(engine)
        for table_name, cols in _additive_migrations.items():
            if table_name not in inspector.get_table_names():
                continue
            existing_cols = [c["name"] for c in inspector.get_columns(table_name)]
            for col_name, col_type in cols:
                if col_name not in existing_cols:
                    logger.info(f"Adding missing column {table_name}.{col_name}...")
                    with engine.begin() as conn:
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_type}"))

        # Filet GENERIQUE, apres les migrations declarees ci-dessus : toute
        # colonne nullable du modele encore absente est AJOUTEE. C'est ce qui
        # a remplace un `drop_all()` — le demarrage effacait toute la base des
        # qu'une colonne nullable manquait (cf. _add_missing_nullable_columns).
        # Rien n'est jamais supprime ; les colonnes NOT NULL sans defaut sont
        # signalees, pas traitees par une destruction.
        _add_missing_nullable_columns(engine, inspect(engine))

        # Backfill idempotent du canal des alertes existantes : les alertes de
        # filtrage transactionnel sont reconnaissables a leur client_id TXN:
        if "alerts" in inspector.get_table_names():
            with engine.begin() as conn:
                conn.execute(text(
                    "UPDATE alerts SET channel = 'FILTERING' WHERE channel IS NULL AND client_id LIKE 'TXN:%'"
                ))
                conn.execute(text(
                    "UPDATE alerts SET channel = 'SCREENING' WHERE channel IS NULL"
                ))
    except Exception as e:
        logger.warning(f"Failed to inspect database schema: {e}")
    Base.metadata.create_all(bind=engine)
    # Index de performance idempotents (les tables existantes n'en heritent pas).
    # GARDE-FOU : un `CREATE INDEX` ordinaire verrouille la table pendant toute
    # sa construction. Sur une grosse table de production (11 M de lignes
    # constatees), cela figerait le service plusieurs minutes au demarrage. On
    # ne cree donc au demarrage que ce qui est sans risque — table petite ou
    # neuve — et on DIT quoi faire pour le reste.
    _deferred_indexes = []
    for perf_index in _PERFORMANCE_INDEXES:
        try:
            table_name = perf_index.table.name
            if _table_row_estimate(engine, table_name) > LARGE_TABLE_ROW_GUARD:
                if not _index_exists(engine, perf_index.name):
                    _deferred_indexes.append((perf_index.name, table_name))
                continue
            perf_index.create(bind=engine, checkfirst=True)
        except Exception as e:
            logger.warning(f"Index {perf_index.name} non créé : {e}")
    _INDEX_DIFFERES.clear()
    _INDEX_DIFFERES.extend(_deferred_indexes)
    if _deferred_indexes:
        noms = ", ".join(n for n, _ in _deferred_indexes)
        logger.warning(
            "PERFORMANCE : %d index manquant(s) sur des tables volumineuses (%s). "
            "Ils ne sont PAS créés au démarrage — un CREATE INDEX ordinaire "
            "verrouillerait la table plusieurs minutes. Lancez, service allumé : "
            "python tools/create_perf_indexes.py  (construction CONCURRENTLY, "
            "sans interruption).", len(_deferred_indexes), noms,
        )
    
    # Check if we need to alter column lengths (e.g. if we are on postgresql)
    if engine.dialect.name == "postgresql":
        try:
            from sqlalchemy import text
            with engine.begin() as conn:
                conn.execute(text("SET lock_timeout = '2s'"))
                conn.execute(text("ALTER TABLE watchlist_entities ALTER COLUMN primary_name TYPE VARCHAR(1000)"))
                conn.execute(text("ALTER TABLE client_entities ALTER COLUMN client_company_name TYPE VARCHAR(1000)"))
                conn.execute(text("ALTER TABLE compliance_audit_trail ALTER COLUMN client_name TYPE VARCHAR(1000)"))
                conn.execute(text("ALTER TABLE compliance_audit_trail ALTER COLUMN watchlist_name TYPE VARCHAR(1000)"))
            logger.info("Successfully checked and upgraded column lengths in PostgreSQL.")
        except Exception as alter_err:
            logger.warning(f"Could not automatically alter column types in PostgreSQL: {alter_err}")
            
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Seed default admin user if missing
    from fiskr.config import ADMIN_USERNAME, ADMIN_PASSWORD
    db = SessionLocal()
    try:
        admin_user = db.query(User).filter(User.username == ADMIN_USERNAME).first()
        if not admin_user:
            h_pass, salt_str = hash_password(ADMIN_PASSWORD)
            new_admin = User(
                username=ADMIN_USERNAME,
                hashed_password=h_pass,
                salt=salt_str,
                full_name="Administrator",
                role="admin"
            )
            db.add(new_admin)
            db.commit()
            logger.info(f"Seeded default admin user: '{ADMIN_USERNAME}'")
    except Exception as user_err:
        db.rollback()
        logger.warning(f"Failed to seed admin user: {user_err}")
    finally:
        db.close()


def get_db():
    if SessionLocal is None:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def log_compliance_decision(
    db,
    client: dict,
    watchlist_entry: dict,
    scoring_result: dict,
    wl_version: str,
    wl_hash: str
) -> AuditTrail:
    """Inserts a compliance screening decision into the audit trail database."""
    from fiskr.config import config as active_config
    config_audit = {k: v for k, v in active_config.items() if k != "database"}
    
    # Handle the difference in client keys (for client_last_name / primary_name)
    cname = client.get("primary_name", "")
    if not cname:
        fname = client.get("client_first_name", "")
        lname = client.get("client_last_name", "")
        cname = f"{fname} {lname}".strip() or client.get("client_company_name", "")
        
    ctype = client.get("entity_type") or client.get("client_type") or "PP"
    
    db_entry = AuditTrail(
        client_id=client.get("entity_id") or client.get("client_id"),
        client_name=cname or "Inconnu",
        client_type=ctype,
        watchlist_id=watchlist_entry.get("entity_id", "NONE"),
        watchlist_name=watchlist_entry.get("primary_name", "Aucun match"),
        base_score=scoring_result.get("base_score", 0.0),
        final_score=scoring_result.get("final_score", 0.0),
        status=scoring_result.get("status", "NO_MATCH"),
        decision_tree=scoring_result,
        config_state=config_audit,
        watchlist_version=wl_version,
        watchlist_hash=wl_hash,
        list_type=watchlist_entry.get("_list_type")
    )
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)
    return db_entry


def log_compliance_decisions(db, client: dict, decisions, wl_version: str,
                             wl_hash: str, commit: bool = True):
    """
    Journalise PLUSIEURS decisions d'un meme criblage, en un seul commit.

    Un criblage produit autant de decisions qu'il trouve de correspondances
    au-dessus du seuil — jusqu'a plusieurs milliers pour un homonyme d'un nom
    tres courant. Un commit par ligne aurait rendu ce chemin inutilisable.
    Chaque ligne reste COMPLETE et autonome : le journal est immuable et une
    decision doit rester relisible sans dependre des autres.

    `decisions` : sequence de (fiche_listee, resultat_de_score).
    """
    from fiskr.config import config as active_config
    config_audit = {k: v for k, v in active_config.items() if k != "database"}

    cname = client.get("primary_name", "")
    if not cname:
        fname = client.get("client_first_name", "")
        lname = client.get("client_last_name", "")
        cname = f"{fname} {lname}".strip() or client.get("client_company_name", "")
    ctype = client.get("entity_type") or client.get("client_type") or "PP"
    client_ref = client.get("entity_id") or client.get("client_id")

    lignes = []
    for watchlist_entry, scoring_result in decisions:
        lignes.append(AuditTrail(
            client_id=client_ref,
            client_name=cname or "Inconnu",
            client_type=ctype,
            watchlist_id=watchlist_entry.get("entity_id", "NONE"),
            watchlist_name=watchlist_entry.get("primary_name", "Aucun match"),
            base_score=scoring_result.get("base_score", 0.0),
            final_score=scoring_result.get("final_score", 0.0),
            status=scoring_result.get("status", "NO_MATCH"),
            decision_tree=scoring_result,
            config_state=config_audit,
            watchlist_version=wl_version,
            watchlist_hash=wl_hash,
            list_type=watchlist_entry.get("_list_type"),
        ))
    db.add_all(lignes)
    if commit:
        db.commit()
    else:
        # `flush` attribue les identifiants sans clore la transaction. Apres un
        # commit, la session expire les objets et relire `ligne.id` declenche
        # un SELECT PAR LIGNE — le N+1 que ce regroupement vient d'eviter.
        # L'appelant qui enchaine sur les alertes commit une seule fois, et
        # journal et alertes deviennent atomiques.
        db.flush()
    return lignes

# Helper function to compute entity checksums
def compute_checksum(data: dict) -> str:
    """Computes a SHA-256 checksum of normalized fields in a dictionary."""
    # Serialize sorted keys, filtering out metadata keys like id, snapshot_id, entity_checksum
    filtered_data = {k: v for k, v in data.items() if k not in ["id", "snapshot_id", "entity_checksum"]}
    dumped = json.dumps(filtered_data, sort_keys=True, default=str)
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()

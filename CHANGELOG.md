# Changelog

All notable changes to the **Fiskr** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Documentation
- **Competitive benchmark & improvement roadmap** (`Documentation/BENCHMARK_CONCURRENTS.md`): market analysis of sanctions/PEP screening solutions (LSEG World-Check One, Dow Jones, ComplyAdvantage, SymphonyAI Sensa, Fircosoft, Napier, and open-source engines OpenSanctions/yente and Moov Watchman), regulatory framework review (Wolfsberg sanctions screening guidance, ACPR/DGT asset-freeze guidelines, official machine-readable lists), capability comparison matrix, 7-point gap analysis mapped to the codebase, and a prioritized roadmap (P0: DGT national asset-freeze register connector, EU FSF consolidated XML replacing the OJ scraping, UN consolidated list · P1: alert lifecycle with 4-eyes, client×entity whitelist, automatic post-delta rescreening · P2: multi-script transliteration, per-list thresholds, decision-tree rendering, PEP source, KPIs · P3: transaction filtering, adverse media, AI narratives).

---

## [2.7.1] - 2026-07-14

### Fixed
- **OFAC SDN_ADVANCED party types — every listed party came out as "E" (entity)**: pass 1 of `parse_ofac_advanced_xml` cleared the children of `ReferenceValueSets`/`Locations`/`IDRegDocuments` before reading them (iterparse `end` events fire bottom-up), so the PartyType/PartySubType reference sets stayed empty and the real file — which carries the type as a `PartySubTypeID` attribute on `<Profile>` resolved by name lookup — always fell back to "E". Both passes now use a depth-aware multi-target streaming helper (modeled on the SSIE engine) that only frees elements outside target subtrees; individuals/vessels/aircraft are typed correctly again, which also restores individual name splitting, PP blocking partitions and the individual quality rules.
- The same premature clear also emptied addresses, citizenship/residence country codes and every ID-document number/classification on the real file — all restored.

### Added
- **Heuristic type fallback**: when neither the inline mock style nor the reference lookup can type a party, its traits decide (IMO → vessel, tail number → aircraft, gender/DOB/passport/national ID → individual, else entity).
- **Extended OFAC extraction** (previously dropped despite being present in the file): `place_of_birth`, structured addresses (`address`, `alternative_addresses`, `city`, `state`, `country`), `designation` (title/position features), `designation_reasons` (sanctions program names from `SanctionsEntries`), `additional_informations` (non-pivotable features: vessel call sign/flag, aircraft model, websites, emails, phones…), passport/ID `expiration_date` (from `DocumentDate`), and `origin`. ID documents are now classified by reference-set names on real files (hard-coded mock IDs kept for backward compatibility).
- 99 automated tests passing (3 new: real-structure SDN_ADVANCED fixture covering party types, locations/documents/programs extraction, and the heuristic fallback).

### Note
- The first OFAC sync after this upgrade will report most of the list as MODIFIED (checksums change with the corrected types and new fields). This is expected and one-time; with homologation mode enabled it will surface as one large pending snapshot to review.

---

## [2.7.0] - 2026-07-13

### Added
- **Homologation mode (pre-production review environment)** for watchlist ingestion: when enabled, every inbound watchlist snapshot (manual upload, manual sync, scheduled OFAC/EUR-Lex sync) lands in a new `PENDING_REVIEW` status instead of going straight to production. Pending snapshots are invisible to the screening engine; the previous `READY` list stays live until a human approves the new one. Snapshot lifecycle becomes `PROCESSING → PENDING_REVIEW → READY | REJECTED → SUPERSEDED`.
- **Hot-toggleable settings store (`app_settings` table + `fiskr/settings.py`)**: `ingestion.require_approval`, `review.exclusion_justification_required` and `review.exclusion_file_required` are admin-editable at runtime via `GET/PUT /api/settings/ingestion` (no restart needed); `config.yaml` only provides the defaults. Disabling the mode leaves already-pending snapshots reviewable.
- **Review workflow API**: `GET /api/review/pending`, `GET /api/review/snapshots/{id}` (live delta vs the current production list, computed on demand), paginated entity browsing, `POST …/approve` (promotes to `READY`, supersedes previous same-type snapshots, reloads the cache) and `POST …/reject` (comment required, snapshot never enters production, kept for audit). Reviewer identity, timestamp and comment are stored on the snapshot.
- **Per-entity exclusions with modular justification**: a reviewer can exclude individual listed parties from a pending snapshot before approval. Each exclusion action carries a text justification and an evidence file (archived under `exclusion_evidence/` and downloadable via `GET /api/review/exclusion-evidence/{id}`); whether each of the two fields is mandatory is controlled independently by the two settings above. Excluded entities never enter the screening cache but remain in the database for audit, and are not carried forward by the EUR-Lex incremental merge.
- **`reviewer` role with stackable roles**: `User.role` now accepts comma-separated stacked roles (e.g. `user,reviewer`); existing single-role accounts keep working. New `require_roles`/`require_reviewer` dependencies (admin always passes); approve/reject/exclusion endpoints require reviewer or admin.
- **Dashboard — new "Homologation" sub-tab**: pending-snapshot queue with count badge, delta tiles vs production, paginated entity table with exclusion checkboxes, justification/evidence modal (required-field marks follow the live settings), approve/reject actions, and the admin settings card with the three toggles. Snapshot list shows explicit `EN ATTENTE D'HOMOLOGATION` / `REJETÉ` badges; user management supports the stacked roles.
- 96 automated tests passing (15 new: review lifecycle, modular justification, role enforcement, staged syncs).

### Changed
- Sync hash-deduplication now also matches snapshots awaiting review, so a daily sync no longer re-creates a pending duplicate every morning; EUR-Lex gains content-hash deduplication and uses the newest live-or-pending snapshot as its incremental merge base so successive pending days chain without losing amendments.
- Approving a snapshot supersedes previous `READY` snapshots of the same type (manual uploads previously stacked). Manual single-entity additions (`manual-watchlist`) remain immediate — already an explicit human action.
- `POST /api/snapshots/purge` also purges `REJECTED` snapshots, freeing their file hash for re-upload.

---

## [2.6.0] - 2026-07-10

### Changed
- **EUR-Lex switched to the English Official Journal edition** (the regulatory reference retained): default daily-view URL now uses `locale=en` and the act filter keyword is "restrictive measures". The scraping vocabulary (annex column headers, editorial boilerplate, truncated language mentions, amendment instructions) now covers English alongside French, so both editions remain parseable.
- **Entity-type detection now leverages the designation reasons**: personal indicators found anywhere in the annex row — including the Reasons/Motifs column (pronouns "he/she is", roles such as minister, oligarch, businessman/woman, propagandist, birth data, nationality) — take precedence over entity/vessel keywords quoted in the reasons; entity and vessel keyword sets were extended (corporation, subsidiary, registered in, state-owned / tanker, shadow fleet, MMSI, flag of…).

### Added
- **Audit-proof PDF archiving**: for every retained act, the official EUR-Lex PDF (the version that is authentic for audits) is downloaded to `eurlex_archives/` with its SHA-256 integrity hash, recorded in the sync report (`acts[].pdf_file` / `pdf_sha256`). A PDF download failure never interrupts the synchronization.
- New endpoints `GET /api/sync/evidence` (list) and `GET /api/sync/evidence/{filename}` (download, filename-validated) to retrieve archived evidence PDFs.
- Sync report detail panel now lists the archived official PDFs with direct download links and their SHA-256 fingerprints.
- 81 automated tests passing (English-source mocks, PDF archiving assertions).

---

## [2.5.0] - 2026-07-09

### Added
- **Individual Name Detection Engine (`fiskr/names.py`)** — shared by every listed-party import:
  - Official lists (EUR-Lex, UN) write the FAMILY NAME in capitals and given names in mixed case; the engine uses this typographic signal to split names correctly whatever the block order ("Aleksandr Vladimirovich GUTSAN" → given names "Aleksandr Vladimirovich" / family "GUTSAN", previously "Aleksandr" / "Vladimirovich GUTSAN").
  - Handles "FAMILY, Given Names" comma format, family particles attached to the capitalized block (bin LADIN, Le PEN, van der…), initials, single-token names, and falls back to first-token-as-given-name when no case signal exists.
  - `ensure_parsed_name` plugs the engine into all import paths — EUR-Lex scraping, SSIE pivot, OFAC/SSIE/CSV/PDF `/api/ingest` branches, source synchronization, and the manual addition form — without ever overwriting a split provided by the source (OFAC XML name parts) or explicit CSV first/last columns.
  - 10 dedicated tests (`tests/test_names.py`).
- **Amendment-instruction filter in the EUR-Lex scraper**: annex rows that quote list-entry text inside amendment instructions ("la mention suivante est remplacée par…") are no longer registered as listed parties, and typographic quotes are stripped from names.
- 81 automated tests passing.

---

## [2.4.1] - 2026-07-09

### Fixed
- **EUR-Lex sync crash on long act titles (`StringDataRightTruncation`)**: EUR-Lex act titles routinely exceed the 255-character `origin` column (e.g. the OJ of 2026-06-08 Iran decision). `build_watchlist_entity` now clamps every string value to its column's `VARCHAR` length before insertion, so scraped data of any length can no longer fail the snapshot INSERT. Entity checksums are computed on the pivot record before clamping, keeping cross-day deltas stable.
- **Annex scraping noise filters hardened** (observed on the June 2026 Official Journals):
  - Truncated language mentions are stripped from names, with or without parentheses ("Anton USOV en russe : Антон УСОВ" → "Anton USOV").
  - Column headers ("Noms (translittération en caractères latins)", "Lieu d'enregistrement", "Motifs de l'inscription sur une liste", plural "Noms"/"Names") and legal boilerplate ("Sont gelés tous les fonds…", "Limited Liability Company") are no longer registered as listed parties.
  - Records whose name does not survive cleansing (e.g. Cyrillic-only cells) are skipped instead of persisting empty-name entities.
- 3 new regression tests (`tests/test_sync.py`) — 71 passing total.

---

## [2.4.0] - 2026-07-09

### Added
- **Automatic Source Synchronization (OFAC download & EUR-Lex scraping)**:
  - New `fiskr/sync.py` module and **Sources Automatiques** sub-tab under Watchlist Management.
  - **OFAC collector**: streams the official `SDN_ADVANCED.XML` publication, ingests it as a snapshot, computes the delta (ADDED / MODIFIED / REMOVED) against the active OFAC list, then applies it — the new snapshot supersedes the previous one in the screening cache. Unchanged file hashes short-circuit with a `NO_CHANGE` report.
  - **EUR-Lex collector**: fetches the Official Journal (L series) daily view for the requested date, keeps acts whose title mentions "mesures restrictives" (accent-insensitive), and heuristically scrapes their annexes (tables and numbered lists) into pivot-schema entities — Individuals (with DOB extraction), Entities, Vessels (IMO) and Aircraft — using stdlib `html.parser` (no new dependency). Scraped entities are **incrementally merged** with the active EU list (stable `EU-<hash>` entity ids for cross-day deltas); `NO_PUBLICATION` is reported when no relevant act exists.
  - Manual on-the-fly additions (`manual-watchlist` snapshot) are never superseded or merged away by synchronizations.
  - **Follow-up reports**: every run (manual or scheduled) persists a `SyncReport` row (status, delta counts, truncated delta details, acts found) surfaced in the app, and is emailed when SMTP is configured (`SMTP_*` / `SYNC_EMAIL_TO` in `.env`).
  - **Daily scheduler**: optional asyncio background task (`sync.auto_enabled` / `sync.schedule_time` in the new `sync` section of `config.yaml`) running both collectors every morning.
  - New endpoints: `POST /api/sync/run` (admin-only manual trigger, per source, optional date for EUR-Lex), `GET /api/sync/reports`, `GET /api/sync/config`.
  - UI: source cards with "Synchroniser maintenant" buttons (date picker for EUR-Lex), scheduler status line, and a clickable synchronization reports history with delta detail panel.
  - 10 new automated tests (`tests/test_sync.py`) on an isolated SQLite database: daily journal filtering, annex scraping (types, DOB, IMO, word-boundary type detection), OFAC replace flow (initial import → `NO_CHANGE` → full delta with supersede), EUR-Lex incremental merge, email skip without SMTP, and API endpoints — bringing the suite to 68 passing tests.
- **26th Compliance Field — Designation Reasons (« Motifs de la désignation »)**:
  - New nullable `designation_reasons` column on `watchlist_entities`, added through a non-destructive `ALTER TABLE ADD COLUMN` migration in `init_db` (existing data preserved).
  - The EUR-Lex scraper locates the « Motifs » column via the annex header row (FR/EN: motifs / reasons / grounds) and stores each listed party's designation grounds alongside its identity.
  - Plumbed through every ingestion path: OFAC/SSIE/CSV/PDF connectors, JSON seed, source synchronization, and the manual addition form (new « Motifs de la Désignation » textarea).
  - SSIE pivot maps dynamically discovered feature labels containing motif/reason/grounds to the new field.
  - Displayed in the entity details modal (full-width row) and covered by scraping assertions in `tests/test_sync.py`.

---

## [2.3.0] - 2026-07-08

### Added
- **Smart Sanctions Ingestion Engine (SSIE) Integration**:
  - New `fiskr/ssie.py` module porting the SSIE 3-phase pipeline into the watchlist import: Phase 1 **Discovery** (streaming extraction of the feature-type reference dictionary), Phase 2 **Resolution** (dynamic join of listed entities' features against the dictionary), Phase 3 **Restitution** (dynamic pivot of resolved features into Fiskr's 25-field compliance schema).
  - **Structural agnosticism**: pivot tag selectors (`reference_item_tag`, `entity_root_tag`, `entity_feature_tag`, `mapping_id_attr`, `mapping_link_attr`) are externally configured in the new `ssie` section of `config.yaml` and can be overridden per import, supporting OFAC Advanced, SWIFT SLD, or any ID-cross-referenced XML feed without hard-coding.
  - Memory-safe event streaming (`ElementTree.iterparse` with depth-tracked `elem.clear()`) keeping RAM consumption constant on multi-GB Full Dataset files.
  - New `WATCHLIST_SSIE` file type on `POST /api/ingest` accepting optional `ssie_selectors` (JSON) and `ssie_source_format` form fields (HTTP 400 on malformed selectors), feeding the Quality Gate, entity checksums, and the in-memory screening cache like any other watchlist.
  - Unmapped dynamically-discovered features are preserved in `additional_informations` (pivoted `Label: value` pairs); heuristic entity typing (Individual/Entity/Vessel/Other) and `LAST, First` name splitting for individuals.
  - **Import de Liste UI**: new "Smart Sanctions — XML générique (Moteur SSIE)" option in the snapshot ingestion form with an adaptive panel exposing the source format and the pivot selectors JSON; dedicated SSIE XML badge in the snapshot history table.
  - SSIE snapshots are fully integrated with the **Delta Engine** version comparator and the active watchlist cache loader.
  - 6 new automated tests (`tests/test_ssie.py`) covering reference discovery, the full pipeline with default and custom selectors, partial selector merging, and end-to-end API ingestion — bringing the suite to 58 passing tests.

---

## [2.2.0] - 2026-07-02

- **User Management & Role-Based Access Control (RBAC)**:
  - Added full User Management module supporting two privilege levels: `admin` (Administrateur) and `user` (Analyste Conformité).
  - Built self-service endpoints (`PUT /api/users/me/profile`, `PUT /api/users/me/password`) allowing any logged-in user to update their display name, username, or change their password securely.
  - Built administrative CRUD endpoints (`GET /api/users`, `POST /api/users`, `PUT /api/users/{id}`, `DELETE /api/users/{id}`) protected by the `require_admin` dependency (HTTP 403 Forbidden for standard users).
  - Added dedicated **Utilisateurs** tab in the sidebar navigation dynamically visible only for Admin accounts.
  - Added interactive user management table with status pills, edit/delete actions, and modal windows (`#user-modal`, `#profile-modal`).


---

## [2.1.0] - 2026-07-01

### Added
- **UI Consolidation into 3 Primary Tabs**:
  - **Gestion des Watchlists**: Consolidates the Active Watchlist explorer, Snapshot ingestion, and the Delta Engine report.
  - **Criblage**: Groups the real-time screening sandbox and the mass batch screening simulator.
  - **Audit**: Houses the compliance audit trail and detail modal inspector.
- **Manual Entity Insertion On-the-Fly**:
  - API endpoint `POST /api/watchlist/entity` validating new profiles against the Quality Gate, calculating checksums, and rebuilding the screening cache in-memory instantly.
  - Full-featured **Ajout Manuel** sub-tab form in the Watchlist Management section to add individuals, corporate entities, or vessels manually.
- **Performance & UI Rendering Optimization**:
  - Implemented pagination (100 items per page) on the Active Watchlist explorer.
  - Refactored DOM rendering to insert rows using `DocumentFragment`, preventing browser layout lockups and reflow lags when exploring large datasets (such as a full OFAC list).
  - Added click triggers on Active Watchlist table rows to open a details modal (`#details-modal`) displaying all 25 compliance attributes in a structured CSS Grid layout.
- **Browser Compatibility & Cache-Busting**:
  - Addressed caching bugs in Firefox by adding query-string cache-busting version numbers (`?v=2.6`) to static CSS and JS script imports.
  - Leveraged pre-existing `.hidden` styling in HTML and JS to ensure proper tab state visibility.
- **Automated Test Coverage**:
  - Added new integration tests (`test_create_watchlist_entity_success` and `test_create_watchlist_entity_quality_gate_failure`) bringing the automated test suite to 47 passing tests.
- **Full 25-Field Compliance Ingestion, Screening & Manual Addition**:
  - Expanded both `WatchlistEntity` and `ClientEntity` database schemas to support Birth Place, Address, City, State, Country, Origin, Job Designation, Remarks, and Alternate Addresses.
  - Built automatic database table migrator using SQLAlchemy schema inspection to drop and recreate tables if schemas are outdated.
  - Updated Pydantic API schemas (`ScreenClientRequest`, `WatchlistEntityCreate`) and CSV/XML/JSON ingest connectors to parse and map all 25 fields.
  - Extended the geographical matching algorithm in `scoring.py` to evaluate the direct `client_country` and `country` fields.
  - Created type-adaptive form layouts for both **Criblage Temps Réel** and **Ajout Manuel** forms, dynamically tailoring the inputs for Individu (PP), Entité (PM), Navire (Vessel) and Autre.
  - Implemented backend normalization in `/api/screen` to automatically convert client type selectors (e.g., `I` to `PP`) side-stepping potential front-end cache mismatches.

---

## [2.0.0] - 2026-06-16

### Added
- **ETL Ingestion Connectors (Section 2.4)**:
  - **OFAC XML Connector**: Memory-safe sequential parser using `xml.etree.ElementTree.iterparse` mapping `PartyTypeID`, `NamePartTypeID`, and `IDRegistrationDocTypeID` directly from the OFAC Advanced XML format.
  - **CSV Connector**: Dynamic mapping connector supporting configurable CSV delimiter characters and column headers.
  - **PDF Connector**: Text extractor utilizing `pypdf` combined with a regex-based Named Entity Recognition (NER) simulator for parsing European/national sanction publications.
- **Delta Comparison Engine (Section 8.3)**:
  - Dynamic snapshot comparison between any two version instances of the same file type.
  - MD5/SHA checksum comparisons (`entity_checksum`) to identify modified records instantly without full cell-by-cell scans.
  - Recursive nested dictionary diffing tool displaying dot-notation modifications (e.g. `countries.residence`) along with `before` and `after` values.
  - Structured Delta JSON Report output classifying entities as `ADDED`, `REMOVED`, or `MODIFIED`.
- **Sequential Hard Match Sequence (Section 5.5)**:
  - High-priority exact match bypass sequence: 
    1. LEI code comparison.
    2. Passport number & issuing country.
    3. National Registry ID & country.
    4. National ID number & issuing country.
    5. Transport Vessel IMO or Aircraft Tail number.
    6. Other ID type & number.
  - Automatically locks the final score to `100.0%` with status `ALERT`, bypassing fuzzy scoring entirely.
- **Alias Risk Categorization (Section 5.6)**:
  - Ingestion-level classification of aliases into `high_priority` (actively screenable) and `low_priority` (consultation only).
  - Built-in heuristic fallback categorization to filter out single-word, short (<= 4 chars), or noise-word-only aliases from the fuzzy scoring pool.
- **Data Quality Gate Upgrades (Section 3)**:
  - Added new rules: `Rule_B04` (individual missing names), `Rule_B05` (name < 2 chars), `Rule_M04` (vital contradictions), `Rule_M05` (format date), `Rule_M06` (passport format), `Rule_M07` (LEI format), `Rule_M08` (PDF confidence), and `Rule_I03` (multi-gender fallback to `U`).
- **Comprehensive Unit Testing**:
  - Created dedicated test files: `test_hard_matches.py`, `test_alias_risk.py`, `test_delta.py`, and `test_ingestion.py`.
  - Expanded total test suite coverage to 42 automated tests, all passing successfully.
- **Dashboard UI Enhancements**:
  - Added a **Versions & Delta** dashboard tab supporting drag-and-drop uploads for watchlists/client files and visual side-by-side Delta Report analysis cards.
  - Upgraded sandbox inputs with advanced matching identifiers.

---

## [1.0.0] - 2026-06-13

Initial release of the Fiskr Compliance Screening Engine.

### Added
- **Module 1 (Data Quality Gate)**:
  - LEVEL 1 validation checks rejecting empty, short, or untyped profiles.
  - LEVEL 2 warning detection for missing country/DOB, and non-ASCII/non-Latin letters.
  - LEVEL 3 text cleaning (uppercase, accent flattening, corporate PM suffix cleaning via Regex).
- **Module 2 (Phonetic & Blocking Engine)**:
  - Custom blocking layout keys based on config components (e.g. `FR_PP_JN`).
  - Pure Python Philips' **Double Metaphone** algorithm (independent of C binary compilers).
  - Fallback keys (`XX`) and multi-value Cartesian product expansion.
- **Module 3 (Hybrid Scoring & Context)**:
  - String metrics integration (Jaro-Winkler, Damerau-Levenshtein, Token Sort) with configuration weights.
  - Best-Match rule across all aliases.
  - Linear adjustments: DOB exact (+15), DOB gap <= 2 years (+5), DOB gap > 2 years (-15), Gender conflicts (-20), and Geographic contact overlap (+10 / -10).
- **Module 4 (Real-time API)**:
  - **FastAPI** asynchronous application.
  - Startup lifespan loading, validating, and indexing `watchlist.json` into RAM memory blocks (caching).
  - Real-time endpoints `/api/screen`, `/api/watchlist`, `/api/history`, and `/api/config`.
- **Module 5 (Batch Engine)**:
  - PySpark distributed screening script implementing Broadcast Join.
- **Module 6 (Audit Trail)**:
  - **SQLAlchemy** database layer mapping immutable compliance screening decisions.
  - Automatic database failover: targets PostgreSQL and falls back automatically to SQLite for easy local runs.
  - Storage of active watchlist version, file hash, config snapshot, and the exact decision tree.
- **Compliance Dashboard UI**:
  - Interactive SPA web page served by FastAPI.
  - Real-time screening sandbox, mock batch scanner, memory cache explorer, and audit log viewer with detail modals.
- **Testing Suite**:
  - 20 unit and integration tests under `tests/` checking quality gates, blocking layouts, scoring distances, and API flows.
- **Project Documentation**:
  - Global `config.yaml` layout.
  - Detailed `README.md` and `CHANGELOG.md` guides.

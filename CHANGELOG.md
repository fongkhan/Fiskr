# Changelog

All notable changes to the **Fiskr** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
- **Browser Compatibility & Cache-Busting**:
  - Addressed caching bugs in Firefox by adding query-string cache-busting version numbers (`?v=2.4`) to static CSS and JS script imports.
  - Leveraged pre-existing `.hidden` styling in HTML and JS to ensure proper tab state visibility.
- **Automated Test Coverage**:
  - Added new integration tests (`test_create_watchlist_entity_success` and `test_create_watchlist_entity_quality_gate_failure`) bringing the automated test suite to 47 passing tests.

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

# Changelog

All notable changes to the **Fiskr** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

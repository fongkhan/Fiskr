# Changelog

All notable changes to the **Fiskr** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Added
- **Ownership graph & OFAC 50 % rule + persisted batch screening campaigns (CFT-ready).**
  - **Entity relationship graph** (`entity_relationships`): links between listed parties (`OWNED_BY`, `ACTING_FOR`, `ASSOCIATE_OF`, `FAMILY_OF`, `LEADER_OF`, `PROVIDING_SUPPORT`), referenced by stable `entity_id`. **OFAC `ProfileRelationships` are now extracted** from SDN_ADVANCED (resolved via the `RelationType` reference set) and idempotently refreshed at each ingestion/sync; manual relations (reviewer/admin, with ownership percentage and comment, admin-logged) are never touched by syncs and are the only ones deletable.
  - **50 % rule (OFAC)**: inherited-risk computation walks `OWNED_BY` edges that are majority-owned (≥ 50 %) or presumed (OFAC link without percentage), transitively with cycle/depth guards. Surfaced in `GET /api/relationships/{entity_id}`, as a red banner in the entity modal ("Règle des 50 % — détention majoritaire par X"), and **annotated into the screening decision tree** (`ownership_inherited_risk`) so the immutable audit trail carries the ownership context of every match.
  - **Server-side batch campaigns** (`batch_campaigns`/`batch_results`): a client CSV (CLIENT_BASE columns) is screened in a background thread through the **exact same shared core as `/api/screen`** (`screen_client_profile` refactor — quality gate, whitelist, FP rules, immutable audit rows, real work-queue alerts). Endpoints: upload/launch, list with live progress, paginated results with status filter, CSV export. Quality-gate rejects are kept as `REJECTED` rows with their reason (never silent). New "Campagnes de Criblage Batch" screen (progress polling, result drill-down to the alert modal).
  - **CFT-ready watched inbox** (`batch.inbox_dir` in config.yaml): the transfer monitor (CFT/SFTP) simply drops a CSV; a poller detects stable files, archives them (`archive/` with timestamp) and launches a campaign automatically (`trigger: inbox`) — the natural integration point for a banking file-transfer flow, zero development on the sender side.
  - 238 automated tests passing (8 new in `tests/test_ownership_batch.py`: OFAC ProfileRelationships extraction, relation CRUD guards, transitive 50 % rule ignoring minority stakes, OFAC-sourced links undeletable, screening annotation, end-to-end campaign with alert/no-match/reject, oversized/empty file rejection, inbox drop → automatic campaign).
- **Operational compliance (lot B) — case management, exports, admin audit, notifications, global search.**
  - **Alert priorities & SLA deadlines**: every alert gets an explicit `priority` (CRITICAL on hard match, HIGH ≥ 95, MEDIUM/LOW near the threshold — editable via `POST /api/alerts/{id}/priority`, journaled `PRIORITY_CHANGED`, deadline recomputed) and a `due_at` SLA deadline (hot-toggleable hours per priority, `alerts.sla_hours`, defaults 24/72/120/240 h). The work queue is now ordered **CRITICAL → deadline → score**, shows a priority column with an "⏰ EN RETARD" badge (`overdue` computed server-side), and gains a priority filter.
  - **Alert attachments**: `POST /api/alerts/{id}/attachments` (+ download endpoint, listed in the detail and the modal, `ATTACHMENT` event) — same evidence-storage pattern as whitelist proofs.
  - **CSV exports** (`;` separator + UTF-8 BOM, opens directly in Excel FR): `GET /api/export/alerts.csv`, `/api/export/history.csv`, `/api/export/watchlist.csv` — each honouring the active screen filters, with ⬇ CSV buttons on the three screens.
  - **Printable alert report** (`GET /api/alerts/{id}/report`): self-contained HTML (browser print → PDF, zero dependencies) with identities, decision tree, 4-eyes action history and attachments — ACPR/FED-ready, linked from the alert modal.
  - **Admin action log**: new append-only `admin_audit_log` table tracing user CRUD (create/update/delete with before → after), settings changes (ingestion + blocking, value deltas), snapshot purges and whitelist revocations; `GET /api/admin-log` (admin) + new "Actions d'Administration" sub-tab in the Audit screen.
  - **Business notifications** (`fiskr/notify.py`): email (reuses the sync SMTP variables, `NOTIFY_EMAIL_TO` override) + **generic webhooks** (POST JSON to `config.yaml notifications.webhooks`); events alert-created, pending-4-eyes-validation, snapshot-pending-review, sync-error, each hot-toggleable (`notifications.events`); strictly **fire-and-forget in a thread** — a notification failure can never block or fail screening (dedicated never-raises test).
  - **Global search (Ctrl+K)**: command palette searching listed parties (`/api/watchlist/db`, incl. fuzzy), alerts (new `search` param on `GET /api/alerts`) and screen navigation, fully keyboard-driven (↑↓/Enter/Escape).
  - 230 automated tests passing (14 new in `tests/test_lot_b.py`: priority computation & SLA recalc, overdue flag, hot SLA setting driving deadlines, attachments upload/download, 3 CSV exports + BOM, HTML report, admin log writes + admin-only guard, notification settings validation, notify never raises, alert search + priority filter/ordering).
- **UX/UI overhaul (lot A) — dual theme, responsive, home dashboard.**
  - **Light/dark theme**: the whole design system is now token-driven (`styles.css` v3 — surfaces, insets, inputs, shadows exposed as CSS variables with a complete `[data-theme="light"]` override set). Header toggle 🌙/☀️, persisted in `localStorage`, applied before first paint (no flash) on both the dashboard and the login page; hardcoded dark inline colors across `index.html`/`app.js` templates were migrated to the tokens.
  - **Responsive layout**: the fixed 280px sidebar becomes a slide-in drawer under 1024px (hamburger button + overlay, auto-close on navigation); 2-column forms, detail grids and KPI tiles collapse under 768px.
  - **Home dashboard « Vue d'ensemble »** (new default tab): clickable KPI tiles (open alerts per channel, pending 4-eyes, snapshots awaiting review, FP rate, average decision time), **native SVG charts with zero dependencies** — 30-day created/closed alerts line chart (per channel), production entities per list bar chart, alert-status donut — plus the 5 oldest open alerts (deep links into the work queue) and the last sync status.
  - **Richer `GET /api/kpi`**: `timeseries_30d` (created per channel + closed per day, SQLite/PostgreSQL-portable), `open_by_list_type`, `by_analyst` (decided volumes + average decision hours), active FP rules efficiency (`hit_count`, finally exposed), `oldest_open`. The Pilotage tab gains the per-analyst and per-rule tables.
  - **Sortable columns everywhere**: generic client-side sorting on every in-memory table (numeric/text auto-detection, ▲▼ indicators); the server-paginated live database view gets real API sorting (`sort_by`/`sort_dir` on `GET /api/watchlist/db`, strictly validated).
  - **Unified fetch wrapper** (`apiFetch`): consistent network-error toasts and automatic redirect to `/login` on expired session across all ~60 call sites; `formatDate`/`formatDateTime` helpers; **skeleton loading rows and homogeneous empty states** on the main tables.
  - **Accessibility**: Escape closes modals (plus click-on-backdrop), `role="dialog"`/`aria-modal`, `role="tab"`/`aria-selected` on sub-tabs, aria-labels on icon buttons; French status labels via a shared `STATUS_LABELS` map; the missing `.status-badge.warning` style now exists; header renamed « Fiskr — Poste de Contrôle Conformité ».
  - 216 automated tests passing (3 new: extended KPI structure, server-side sort ordering, unknown sort column rejected).
- **Extended data fields: 26 structured columns for listed parties + 14 KYC columns for clients.** The advanced parsers stop dumping structured source data into free text: OFAC SDN_ADVANCED features are now mapped by feature type (digital-currency wallets with currency suffix, SWIFT/BIC, tax ID, D-U-N-S, vessel call sign/MMSI/flag/type/tonnage/owner, aircraft model/operator/construction number, websites, emails, phones, secondary sanctions risk, organization established date — DatePeriod-aware — and organization type), UN adds `title`/`listed_on`/`designating_state`/`name_original_script`/`sanction_programs`, EU FSF adds `title`/`listed_on`/programme list, DGT adds legal-basis programmes + contact TypeChamps, OFSI adds Title/Listed On/Non-Latin script (kept as a matching alias)/passport & NI documents/Regime, PEP adds `pep_role`/`first_seen`/phones/emails. All 26 columns are nullable additive migrations, searchable (field search groups Références/Identifiants/Contact + "Tout champ"), editable via the journaled `PATCH` and the detail modal ("Champs étendus" section), and accepted as generic-CSV columns (list fields split on `;`).
  - **Five new hard-match keys** in the priority sequence: **BIC/SWIFT** (8/11 alphanumeric, branch-tolerant 8-char bank comparison), **tax ID** (normalized), **crypto wallet address** (exact), **vessel MMSI** and **call sign**. `POST /api/screen` accepts the client mirrors (`client_bic`, `client_tax_id`, `client_iban`, `client_crypto_wallets`, `transaction_vessel_mmsi`, `transaction_vessel_call_sign`); **ISO 20022 filtering now hard-matches bank agents by BIC** (`DbtrAgt`/`CdtrAgt` × sanctioned `bic_swift`).
  - **14 client KYC columns** ingested from `CLIENT_BASE` CSVs: IBAN, BIC, tax ID, phone, email, website, crypto wallets (`;`), risk rating, PEP flag, segment, activity sector, activity countries (`,`), relationship start, status.
  - Fixed on the way: OFAC feature types containing the word "address" but not postal ("Digital Currency Address - XBT", "Email Address") were classified as postal-address features and lost. 213 automated tests passing (16 new in `tests/test_extended_fields.py`: BIC/tax/crypto/MMSI/call-sign hard matches, OFAC structured-feature extraction, pacs.008 sanctioned-agent BIC hit, extended CSV ingestion + field search, journaled extended-field PATCH, CLIENT_BASE KYC ingestion; plus extended assertions in the UN/FSF/DGT/OFSI/PEP/OFAC parser tests).
- **Screening / Filtering channel separation** — alerts are split into two distinct queues: **Criblage Clients** (`SCREENING`, client referential × lists) and **Filtrage Transactionnel** (`FILTERING`, ISO 20022 payment parties). New `alerts.channel` column (additive migration + idempotent backfill: `TXN:`-prefixed alerts → FILTERING); `GET /api/alerts?channel=`, per-channel counters (`open_alerts_screening/filtering`) and sidebar sub-tab badges; the filtering queue shows Message/Party instead of Client.
- **Per-channel blocking keys** (`GET/PUT /api/settings/blocking`, new `blocking` role or admin): the ordered key layout (`COUNTRY_ISO`, `ENTITY_TYPE`, `PHONETIC_FIRST`) is now configurable **separately for screening and filtering**. Screening changes reload the production cache immediately (index/probe layout kept in sync via a memorized `watchlist_index_layout`); filtering defaults to phonetic-only. The transaction filter's hard-coded 3-part key assumption is replaced by a proper per-channel local index + `party_blocking_keys` probe (PP/PM both tried, all name words phonetized).
- **Python false-positive rules with a DEV workflow** (`/api/fprules`, new `rules` role or admin) — `fiskr/fprules.py`: rules are `def rule(ctx) -> bool` (True suppresses the alert). Independent rule sets per channel. Suppressed alerts are **never silent**: the alert is created then auto-closed `CLOSED_BY_RULE` (visible via a dedicated filter), with a `RULE_SUPPRESSED` event and `fp_rule_applied {id,name,version}` written to the immutable audit trail (ACPR/FED). Applied after the ALERT decision and whitelist check in `/api/screen`, rescreen, transaction filtering and the homologation backtest; **fail-open** (a rule that raises keeps the alert). Volume control: an existing `CLOSED_BY_RULE` alert for the same pair is re-detected, not duplicated.
  - **Branch → tests → 4-eyes → merge lifecycle**: rules live as `DRAFT` (never applied to production) → `PENDING_VALIDATION` (submission gated on ≥1 unit test and 100% green) → `ACTIVE` (4-eyes validation by someone other than the submitter). Editing an ACTIVE rule creates a **new DRAFT version** (`replaces_rule_id`) that supersedes the old one on validation. Immutable change journal (`fp_rule_changes`), unit tests (`fp_rule_tests`), and a DEV bench: run unit tests, replay real alert history with a **true-positive guardrail** (`CLOSED_CONFIRMED` alerts that would be suppressed, flagged red), or generate alerts from a pseudo-client panel.
- New `Documentation/REGLES_ET_BLOCKING.md` guide. 201 automated tests passing (15 new in `tests/test_fprules.py`: engine compile/run/fail-open, CRUD + role guards, submission gate, 4-eyes validation + branch/merge, reject-to-draft, draft-never-applied, channel independence, per-channel blocking validation, end-to-end `CLOSED_BY_RULE` with audit trace, channel filtering).
- **Typo-tolerant search (fuzzy fallback) in the live database view**: when the exact (substring) search returns results, only those are shown — never fuzzy neighbours; when it returns **nothing**, the view falls back to a fuzzy scan of the selected field (Jaro-Winkler with the engine's accent/case normalization, whole-text and word-by-word, threshold 80), ranked by similarity. The response carries `match_mode: "exact"|"fuzzy"` and a per-item `_fuzzy_score`; the UI shows an amber banner ("Aucun résultat exact — N résultat(s) approché(s)") and a ≈ score badge next to each name. 4 new tests (typo transposition, exact-hides-fuzzy-neighbours, fuzzy honors `search_field`, no-search mode).
- **Search on any field in the live database view** (`GET /api/watchlist/db?search_field=`): a field selector (grouped in French: Identité, Localisation, Références, Identifiants) targets any of the 28 entity columns — JSON columns (aliases, countries, dates of birth, alternative addresses, identity documents…) are searched via `CAST(col AS TEXT)`, valid on SQLite and PostgreSQL — plus a "🔎 Tout champ" option OR-ing everything; the default remains the fast indexed search (name, ID, LEI, IMO). The input placeholder follows the selected field; unknown field → 400. 3 new tests in `tests/test_watchlist_db.py`.

### Fixed
- **Collapsed search input in filter bars**: the global `select { width: 100% }` rule made the two selects of the "Listés — Base de Données" filter bar expand and crush the search input into an unusable tiny square. New reusable `.filter-bar` class (input keeps ≥220px, selects get natural bounded widths), applied to the watchlist view and the audit-trail filter bar.
- **Guided list-production pipeline** — the Homologation detail becomes a 4-step journey (**Delta → Exclusions → Cahier de tests → Décision**), documented in the new `Documentation/PRODUCTION_DES_LISTES.md` guide; after an upload or sync that lands in `PENDING_REVIEW`, the dashboard offers to open the journey directly:
  - **Full delta detail in the review screen**: besides the three counters, the added/removed entities and the modified ones (changed fields with before → after values) are now rendered — the API already returned them, only counts were displayed. Also fixes `POST /api/snapshots/compare` which returned nothing (missing `return`), breaking the snapshot comparator.
  - **Test book / backtest** (`POST /api/review/snapshots/{id}/backtest`, reviewer/admin): **dry-run A/B screening** of a pseudo-client panel against the current production universe AND the candidate universe (the pending snapshot replacing same-type lists, exclusions deducted, manual additions preserved) — same per-list cut-offs and whitelist as production, but **zero alerts or audit rows written**. Reports both **interception rates**, the relative **gap (%)** vs a tolerated threshold, an `OK`/`WARN` verdict, the **new alert pairs** and the resolved ones; the report is **archived on the snapshot** (`backtest_report/at/by` columns, returned by the review detail, auditable after promotion).
  - **Pseudo-client panel generator** (`POST /api/testpanels/generate`, `GET /api/testpanels`): 50–5000 clients derived from candidate+production entities (~10% exact copies, ~10% typos/name inversions, ~10% near-collisions with shifted DOB, ~70% neutral clients from an embedded lexicon, seedable). Stored as `CLIENT_TEST_PANEL` snapshots — **never** picked up by the real client-referential rescreening; real `CLIENT_BASE` uploads remain usable as panels.
  - **Bulk Good Guys** (`POST /api/whitelist/bulk`): multi-select the backtest's new alerts and whitelist them with one shared justification (whitelist governance settings honored, already-active pairs skipped) — then re-run the test to verify the gap closes.
  - **Two hot-toggleable governance settings**: `review.backtest_max_gap_pct` (tolerated interception-rate gap, default 20%) and `review.backtest_required` (hard gate: approval refused without an `OK`-verdict backtest — otherwise the verdict stays advisory, shown at the Décision step).
  - 179 automated tests passing (8 new in `tests/test_backtest.py`: A/B gap detection with strict dry-run proof, bulk Good Guys then gap closing to `OK`, approval gating on missing/`WARN` reports, generated-panel isolation from `rescreen._client_dicts`, size bounds, comparator regression).
- **Value patching for listed parties** (`PATCH /api/watchlist/entity/{id}`, reviewer/admin only): any production entity (READY snapshot, not excluded) can now have its values edited from the detail modal — scalar fields, parsed names, dates of birth, countries, aliases and alternative addresses. Every changed field is journaled in the new `watchlist_entity_changes` table (who, when, old → new value, surviving snapshot supersession) and surfaced in the modal as a "Historique des modifications" section (`GET /api/watchlist/entity/{id}/changes`); the entity's version checksum is recomputed and the screening cache reloads immediately. Editing a synced-source entity shows an explicit warning that the next synchronization will overwrite the patch.
- **Official reference with update date** (`official_reference` column): the UN (`REFERENCE_NUMBER` + `LAST_DAY_UPDATED`/`LISTED_ON`), EU FSF (regulation `numberTitle` + `publicationDate`), DGT (`REFERENCE_UE`/`REFERENCE_ONU` + registry `DatePublication`) and OFSI (`UK Sanctions List Ref` + `Last Updated`) parsers now extract the issuer's official reference suffixed with its update date (e.g. `QDi.430 (maj 2016-08-14)`); also accepted as an optional generic-CSV column and on manual entity creation. When patching an entity, the `touch_official_reference_date` flag replaces the date contained in the reference (the last one, ISO or `DD/MM/YYYY`, keeping its original format) with today's date — offered as a default-checked checkbox in the edit form whenever a date is detected. Existing rows are not backfilled; they pick the reference up on their next sync/import.
- 171 automated tests passing (10 new in `tests/test_entity_patch.py`: journaling with checksum recompute and cache reload, structured-field patches, date-touch in French and ISO formats targeting the last date, no-date no-op, patched-reference touch in the same request, role guard, out-of-production 409, validations; plus `official_reference` assertions in the DGT/EU FSF/UN parser tests).
- **Live database view of listed parties** (`GET /api/watchlist/db`): the "Watchlist Active" sub-tab becomes **"Listés — Base de Données (en direct)"** and now reads the relational database on every display instead of dumping the engine's in-memory cache to the browser. Server-side pagination (100/page, max 500), debounced search (name, entity ID, LEI, IMO), list-type filter, and a new **scope filter**: `production` (default — READY snapshots, excluded entities out, mirroring what the engine screens), `all`, `PENDING_REVIEW`, `SUPERSEDED`, `REJECTED` and `EXCLUDED`. Each row carries a snapshot-status badge (plus an "EXCLUE" badge) and the existing 26-attribute detail modal works unchanged. The engine cache and its sidebar hash are untouched (`GET /api/watchlist` unchanged) — divergence between cache and database becomes visible, which is the point of a live view. 8 new tests in `tests/test_watchlist_db.py`.
- **License — Sustainable Use License (fair-code)**, copyright © 2026 Alexis Vuadelle (`LICENSE.md`): free internal-business and personal use, public source; commercialization (resale, paid hosting for third parties, paid on-premise deployment services and commercial licenses) reserved to the copyright holder, available on paid request via GitHub.
- **Sponsoring**: `.github/FUNDING.yml` pointing to GitHub Sponsors (`fongkhan`), plus license and sponsor badges and a "Licence & Offre Commerciale" section in the README.

## [2.11.0] - 2026-07-16

Business-process/UX overhaul of the dashboard plus list-type scoping across the product.

### Added
- **List-type everywhere (`list_type`)**: additive migrations denormalize the originating list type onto `alerts`, `compliance_audit_trail` and `whitelist_pairs`, populated at write time (`log_compliance_decision`, `open_or_redetect_alert` — with progressive backfill of open alerts on redetection — and server-side derivation on whitelist creation). Old rows are **never rewritten** (immutable audit): `NULL` renders as "Inconnue" and is targetable with the `UNKNOWN` filter value, while `/api/history` falls back to the type stored in the `decision_tree` for display.
- **"Liste" filters and columns on every screen**: active watchlist (new column + combined text/list filter; "Type" is renamed "Type d'entité" to remove the I/E/V/O ambiguity), alerts worklist (`GET /api/alerts?list_type=`), audit trail, whitelist (`GET /api/whitelist?list_type=`) and snapshot history. One shared label map (`LIST_TYPE_LABELS`) is used across snapshots, homologation, KPI, compare selects and badges.
- **Audit trail pagination**: `GET /api/history` now returns a `{total, page, page_size, items}` envelope (default 50, max 200) with `status` and `list_type` filters and explicit serialization, replacing the unbounded ORM dump; the dashboard gains pager controls.
- **Restricted screening (`screening_lists`)**: real-time screening, the batch simulator and ISO 20022 transaction filtering can screen against a subset of lists ("certaines banques n'ont pas besoin de tout utiliser"). Compliance guardrails: absent/empty = **all lists** (default), unknown values → 400, and every restriction is traced — in the immutable `decision_tree` (`screening_lists_restriction`), in the response (`screening_lists`) and in the alert event detail. Checkbox groups (all checked by default) with an explicit audit warning in both screening forms.
- **Lightweight `GET /api/counters`** (open alerts, pending homologations) polled every 60 s to keep the sidebar badges alive without reloading the tables.
- 153 automated tests passing (9 new in `tests/test_list_scope.py`: unrestricted default, restriction excluding/including the matching list with decision-tree tracing, unknown-list 400 on both endpoints, `list_type` persistence and filters on alerts/history/whitelist, whitelist derivation, counters, transaction restriction PASS/HIT).

### Changed — dashboard UX (audit follow-up)
- **Flow continuity**: a screening that opens an alert now shows a direct **"Instruire l'alerte #N"** button (also on batch ALERT rows and transaction hits); the Homologation sub-tab reloads its queue on every opening; sidebar badges refresh automatically.
- **Menu reorganization**: new admin **"⚙️ Paramètres"** tab hosting the 7 hot-toggleable governance settings (previously buried in Watchlists → Homologation); the whitelist becomes an **Alertes sub-tab**; snapshot upload moves to a dedicated **"Import de Fichiers"** sub-tab, separated from the Delta comparator.
- **No more native browser popups**: all 77 `alert()/confirm()/prompt()` call sites replaced with an integrated toast system and Promise-based confirm/prompt modals — regulatory comments (proposal, 4-eyes validation, whitelist revocation, snapshot rejection) are now typed in proper textareas.
- **Label consistency**: fixed the sync-report bug that displayed every non-OFAC source as "EUR-Lex JO" (shared `SYNC_SOURCE_LABELS` map, also used by the KPI page); homologation table no longer shows raw `WATCHLIST_*` codes; French-language pass ("Launch Screening Engine" → "Lancer le criblage", "Genders/Gels" → "État / Genre", delta labels, entity-type badges).
- **Dead code removed**: duplicated and broken early definitions of `fetchAuditHistory`/`renderAuditTable`/`showAuditModal`, duplicate `fetchConfig` and the shadowed `window.onclick` handler (the surviving versions are the correct ones); audit modal display harmonized.

---

## [2.10.1] - 2026-07-16

Documentation-vs-code audit follow-up: every gap found while verifying the implementation against the documentation is fixed, plus two code quick wins.

### Fixed
- **Transaction filtering — parties with no blocking candidate now leave an audit line**: `screen_payment_message` previously only wrote to the immutable audit trail when at least one candidate had been retrieved, contradicting the documented guarantee that *every screened party* is traced. Parties with zero candidates now log a `NO_MATCH` decision ("Aucun candidat trouvé"), mirroring the unit-screening behavior — proving a party *was* screened matters as much as the outcome. `audit_id` is now populated for every party in the response.
- **`GET /api/adverse-media` no longer blocks the event loop**: the endpoint was `async def` but performed a synchronous outbound HTTP call (up to 30 s); it is now a sync `def`, executed by FastAPI's threadpool like `/api/sync/run`.

### Changed
- **Transaction filtering candidate retrieval is O(index) once per message** instead of once per party: the blocking index is inverted into a phonetic→entities map a single time (`_phonetic_entity_map`), then each party is a dictionary lookup — noticeable on large production watchlists.
- Pydantic deprecation cleanup: `Field(..., example=...)` → `json_schema_extra` (removes 8 deprecation warnings from every test run and API startup).
- CI workflow can now be triggered manually (`workflow_dispatch`).

### Documentation
- KPI guide: clarified that the false-positive rate is computed over **all** closed alerts while the average decision time is computed over the **last 500** closed alerts.
- README: CI badge added; the homologation section now lists all seven gated sources (not just OFAC/EUR-Lex); the ingestion section mentions the dedicated official-source parsers (DGT JSON, EU FSF XML, UN XML, PEP/OFSI CSV) beyond the four generic connector families; field 7 "Nationality" now points to its real storage (`countries.citizenship` / `client_countries.nationality` — there is no dedicated `nationality` column).

---

## [2.10.0] - 2026-07-15

Roadmap items **P2 (technical differentiation)** and **P3 (horizon)** — completes the competitive-benchmark roadmap (P0 → P3). Merged via PR #9.

### Added
- **ISO 20022 transaction filtering** (roadmap item P3-1, Fircosoft-like): new `fiskr/transactions.py` parses `pain.001` (customer credit transfer initiation) and `pacs.008` (FI-to-FI credit transfer) payment messages version-agnostically (local-name matching), extracts every party — debtor, creditor, ultimate debtor/creditor, initiating party and financial agents (BICFI/BIC, country derived from the BIC when absent, birth date/country from `PrvtId`) — and screens each distinct party against the production watchlists. Candidate retrieval deliberately ignores the blocking country (payment data is too sparse to filter on it) and matches phonetics on every word of the free-text name; each party is scored with the profile variant (PP/PM) matching the candidate's type. Global verdict **PASS / HIT**; every screened party leaves an immutable audit line and every hit opens a deduplicated work-item alert (`TXN:{msg_id}` client ids). Endpoint `POST /api/transactions/screen`; the dashboard Screening tab gains a third sub-tab with file upload, verdict banner and per-party results (linking straight to the opened alerts).
- **Adverse media search** (roadmap item P3-2): new `fiskr/adverse_media.py` queries the free public Google News RSS feed for the name combined with AML keywords (money laundering, sanctions, fraud, corruption... configurable via `adverse_media.keywords`), with a replaceable provider (`adverse_media.provider`). Strictly informational: results never alter a score or a screening status. Endpoint `GET /api/adverse-media?name=`; the alert investigation modal gains "Presse : client" / "Presse : listé" buttons showing the headlines with sources and dates.
- **Human-in-the-loop alert narratives** (roadmap item P3-3): new `fiskr/narrative.py` composes a French investigation-narrative **draft** exclusively from traced data — the linked audit's `decision_tree` (hard match reason or fuzzy base score, DOB/gender/geography adjustments, applied threshold), party identities, list version, redetections and decision history — so every sentence is justifiable by a database field (EU AI Act explainability). Optional LLM rewrite via the Claude API (`narrative.llm_enabled`, default off; requires `ANTHROPIC_API_KEY` + the `anthropic` package) with strict no-new-facts instructions and silent deterministic fallback on any error. The narrative never closes an alert: proposing and 4-eyes validating remain human acts. Endpoint `POST /api/alerts/{id}/narrative` (traced as a `NARRATIVE` event); the alert modal gains a "Générer un narratif" button with an editable, copyable draft.
- 144 automated tests passing (14 new: pain.001/pacs.008 parsing incl. agents and birth data, unknown-message rejection, party screening HIT opening an alert / PASS, Google News query building, RSS parsing with max_results, injected-fetcher search, deterministic narratives for fuzzy and hard-match/closed alerts, LLM-disabled fallback, transaction endpoint 400/PASS, adverse media endpoint). End-to-end verified: pacs.008 upload → HIT 90% → alert opened and visible in the Alerts tab; narrative generated from a real alert's audit trail.
- **Multi-script transliteration** (roadmap item P2-1): names written in Cyrillic, Arabic, Chinese, Greek and any other non-Latin script are now transliterated to Latin (via the `anyascii` library, ISC license) before normalization in `quality.strip_accents`, so *Владимир Путин* scores 100% against *VLADIMIR PUTIN*. Latin diacritics keep the historical NFKD folding; if `anyascii` is not installed the engine degrades gracefully to the previous behavior.
- **Per-list cut-off thresholds** (roadmap item P2-2): the global `scoring.cut_off_threshold` can be overridden per list type via `scoring.cut_off_overrides` (e.g. a stricter threshold on `WATCHLIST_PEP` than on `WATCHLIST_DGT`). Watchlist entries are annotated with their `_list_type` when loaded into the screening cache and by the rescreen engine; `resolve_cut_off` picks the applicable threshold and every result keeps reporting it in `cut_off_applied` (now surfaced as a tooltip on the screening status badge).
- **PEP source connector — OpenSanctions** (roadmap item P2-5): `run_pep_sync` downloads the consolidated Politically Exposed Persons dataset (`targets.simple.csv`), mapped by `parse_pep_targets_csv` (Person → I / organizations → E, aliases, partial birth dates normalized, ISO2 countries) into a new `WATCHLIST_PEP` list with the shared replacement cycle (delta, supersede, homologation-aware). **Disabled by default**: OpenSanctions data requires a paid license for commercial use (opensanctions.org/licensing) — enable `sync.pep.enabled` only within the terms.
- **UK OFSI consolidated list connector** (roadmap item P2-4, opt-in): `run_ofsi_sync` downloads HM Treasury's `ConList.csv` (2022 format); `parse_ofsi_conlist_csv` skips the preamble, groups rows by Group ID (Primary name vs aka aliases), types Individual → I / Ship → V / else → E, converts `dd/mm/yyyy` dates and normalizes nationalities to ISO2 into a new `WATCHLIST_OFSI` list. Both new sources are wired into the scheduler, `POST /api/sync/run`, manual upload, sync cards, upload options and snapshot badges.
- **Compliance KPI page** (roadmap item P2-6): new **Pilotage** dashboard tab backed by `GET /api/kpi` — open/in-progress/pending-validation/closed alert counts, **false-positive rate** and average decision time (last 500 closed alerts), active whitelist pairs, production entity counts per list type, snapshots per status, screening decision distribution and the 15 most recent sync reports.
- Screening results now render the `WHITELISTED` outcome with a dedicated badge ("Supprimée par liste blanche") instead of falling through to the generic style (roadmap item P2-3 companion; the decision-tree rendering itself shipped with P1-1).
- 130 automated tests passing (8 new: Cyrillic transliteration and cross-script scoring, per-list threshold resolution and ALERT→NO_MATCH flip, PEP CSV mapping, OFSI ConList mapping incl. preamble and multi-row alias groups, PEP+OFSI sync lifecycle with hash dedup, KPI endpoint structure).

### Documentation
- New functional guide `Documentation/ALERTES_ET_SURVEILLANCE_CONTINUE.md` (post-screening workflow: alert lifecycle, 4-eyes, whitelist, rescreening/lookback, narratives, adverse media, transaction filtering, KPIs); the README keeps a compact summary. Benchmark updated: P0 → P3 all marked delivered, capability matrix refreshed.

---

## [2.9.0] - 2026-07-15

Roadmap items **P1 (analyst efficiency)** — alert case management with 4-eyes validation, client×listed-party whitelist, continuous screening. Merged via PR #8.

### Added
- **Client×listed-party whitelist — "Good Guys"** (roadmap item P1-2, Wolfsberg guidance): a reviewer can whitelist a client×entity pair (typically after a validated false-positive), suppressing its recurring alerts. Suppression is **never silent**: every whitelisted hit is still logged in the immutable audit trail with full scores under the explicit `WHITELISTED` status. Creation is governed (justification and evidence file with independently hot-toggleable requirements — `review.whitelist_justification_required` / `review.whitelist_file_required` — evidence archived under `whitelist_evidence/` and downloadable), optionally time-boxed via `expires_at` for periodic review, and revocation is soft-only with a mandatory reason (alerts resume). Endpoints `POST/GET /api/whitelist`, `POST /api/whitelist/{id}/revoke`, `GET /api/whitelist/evidence/{id}`; the dashboard Alerts tab gains a whitelist management card and closed false-positive alerts offer a one-click "Mettre en liste blanche" prefilled modal.
- **Automatic post-delta rescreening** (roadmap item P1-3): whenever a watchlist snapshot goes live — manual sync, scheduled sync, manual upload, or homologation approval — the client base (`CLIENT_BASE` snapshots) is automatically rescreened against **only the new or modified entities** (checksum diff vs the replaced snapshot), using a local blocking index. New hits open work-item alerts through the P1-1 lifecycle (deduplicated; events authored by `rescreen-auto`), and whitelisted pairs are suppressed with an audit trace (counted as `whitelisted_suppressed`). Hot-toggleable via `ingestion.auto_rescreen` (default on); counters returned in sync/upload/approve responses. New shared module `fiskr/alerts.py` (alert dedup + whitelist lookup) reused by the API and the new `fiskr/rescreen.py` engine.
- **Manual lookback** (`POST /api/rescreen/run`, admin): rescreens the whole client base against all production lists (or one list type) — the Wolfsberg lookback capability.
- 122 automated tests passing (8 new: whitelist governance/suppression/revocation/expiry, changed-entities-only rescreen with dedup, whitelist-aware rescreen, lookback permissions, sync-response counters). End-to-end verified: ALERT → whitelist → `WHITELISTED` with no alert → revocation → ALERT again.
- **Alert lifecycle with 4-eyes validation** (roadmap item P1-1): every real-time screening decision with `ALERT` status now opens a work item in the new `alerts` table (deduplicated per client×listed-party pair — re-screenings append a `REDETECTED` event instead of duplicating). Lifecycle: `OPEN → IN_PROGRESS (assigned) → PENDING_VALIDATION (decision proposed) → CLOSED_CONFIRMED | CLOSED_FALSE_POSITIVE`, with `ESCALATED` as a side path. Proposing a decision (true/false positive) requires a comment; **validation requires a reviewer or admin different from the proposer** (HTTP 403 on self-validation), and a refusal returns the alert to analysis with a mandatory reason. The requirement is hot-toggleable (`review.alert_four_eyes_required`, default on): when off, a proposal closes the alert directly. Every action is recorded in the append-only `alert_events` history; the immutable `compliance_audit_trail` stays untouched and linked (`audit_id`).
- New endpoints: `GET /api/alerts` (worklist with status/assignee filters, sorted by risk), `GET /api/alerts/{id}` (detail with the linked audit `decision_tree` and full event history), and actions `assign`, `comment`, `escalate`, `propose`, `validate`. Dashboard gains an **Alertes** sidebar tab with an open-count badge, status filters, and an investigation modal that renders the score explanation (decision-tree adjustments), the action timeline, and role-aware buttons; the admin settings card gains the 4-eyes toggle.
- 114 automated tests passing (7 new: creation/no-match/dedup, full 4-eyes lifecycle incl. self-validation and role rejections, refusal path, toggle-off direct closure, escalation and worklist filters). End-to-end verified over HTTP: screen → alert → propose → self-validate 403 → second reviewer closes.
- **Continuous integration** (`.github/workflows/ci.yml`): GitHub Actions workflow running the full pytest suite (Python 3.11, pip cache) and a dashboard JavaScript syntax check (`node --check`) on every push and pull request to `master`.

### Note
- Alerts are opened by the real-time screening path only; the optional Spark batch engine does not create work items yet.

---

## [2.8.0] - 2026-07-14

Roadmap items **P0 (compliance & quick wins)** — native connectors for the official DGT, EU FSF and UN consolidated lists. Merged via PR #8.

### Added
- **UN consolidated list connector** (roadmap item P0-3): `run_un_sync` downloads the official Security Council consolidated XML (`scsanctions.un.org`, public), parses individuals and entities (`parse_un_consolidated_xml`: names, original-script and Good/Low aliases, birth dates/places, nationalities, documents, designations, UN list type and reference), normalizes English country labels to ISO2 for blocking, and replaces the active `WATCHLIST_UN` list with delta + supersede — homologation-aware like every other source.
- **EU FSF consolidated list connector** (roadmap item P0-2): `run_eu_fsf_sync` downloads the Commission's authoritative consolidated financial-sanctions XML (FSF files, FSD webgate — requires a free registration token, `sync.eu_fsf.token`). `parse_eu_fsf_xml` maps sanctionEntity records (subjectType P/E, name aliases with strong/weak quality, gender/function, birthdates, ISO2 citizenships, identifications, addresses, regulation programme and remarks). Shares the `WATCHLIST_EU` file type so the consolidated snapshot supersedes the scraped OJ list (**removals finally become reliable**) while the daily OJ scraping remains an optional same-day freshness complement merging on top. Disabled by default until a token is configured; a missing token yields an explicit error report instead of a failed download.
- Both sources are wired into the daily scheduler (OFAC → EU FSF → EUR-Lex OJ → DGT → UN), `POST /api/sync/run` (`EUFSF`/`UN`), manual dashboard upload (UN XML file type; an `.xml` file uploaded as `WATCHLIST_EU` is parsed as FSF), sync tab cards and snapshot badges. Shared replacement-cycle helper `_run_list_replacement_sync` (hash dedup incl. pending snapshots, delta, supersede, homologation gating) now backs both connectors.
- 107 automated tests passing (5 new: FSF mapping, UN mapping, UN sync lifecycle, FSF token guard, FSF replacement + homologation staging). End-to-end verified: UN and FSF uploads go live and a matching client raises a 100% ALERT.

- **DGT national asset-freeze register connector** (roadmap item P0-1): new `run_dgt_sync` downloads the official French registre national des gels from the public DGT/ENGEL API (`gels-avoirs.dgtresor.gouv.fr`), ingests it as a `WATCHLIST_DGT` snapshot (Personne physique → I, Personne morale → E, Navire → V), computes the delta against the active DGT list and applies the replacement — with the same hash deduplication, homologation-mode gating (`PENDING_REVIEW`) and sync reporting as the OFAC connector. Implementing national freeze measures is a standalone obligation for French institutions under the ACPR/DGT guidelines.
- New JSON parser `parse_dgt_gels_json` maps every register field to the 26-field pivot schema: names/aliases, gender, dates and places of birth, nationalities, addresses, passports, identifications, title (`designation`), legal grounds and UN/EU references (`additional_informations`), and reasons (`designation_reasons`). French country names and nationality adjectives are normalized to ISO2 codes so blocking keys line up with the client base (verified end-to-end: a client matching a DGT-listed individual raises a 100% ALERT).
- Wired everywhere a source can enter: daily scheduler, manual `POST /api/sync/run` (`source: "DGT"`), manual dashboard upload (new "Registre national des gels — DGT (JSON)" file type), sync tab card, and snapshot type badges.
- 102 automated tests passing (3 new: register parsing/mapping, sync lifecycle with delta and supersede, homologation staging with hash dedup).

### Note
- The first EU FSF sync will report most of the EU list as ADDED/REMOVED against a previously scraped OJ snapshot (different stable identifiers). Expected and one-time; with homologation mode enabled it surfaces as one large pending snapshot to review.

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

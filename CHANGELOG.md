# Changelog

All notable changes to the **Fiskr** project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Fixed — two startup jobs wired to a startup production never runs
Fiskr is served by Passenger through `a2wsgi.ASGIMiddleware`, which builds one `http` scope per request and **does not implement** the ASGI `lifespan` protocol. FastAPI's startup therefore never runs in production. The codebase already says so in four places, each written after a real incident — the "Active hash" badge stuck at N/A, a first screening at 64 s, a screening returning NO MATCH against an empty cache.

That makes a defect class that only exists in production: work wired into `lifespan` runs everywhere — under uvicorn in development, in every `TestClient` in the suite — and nowhere that counts. Two were left.

**The daemon's autostart hook.** `jobs.on_submit_hook = ensure_worker` was set in `lifespan`. On shared hosting there is no systemd, so the application starting its own daemon is, in the words of the comment above `ensure_worker`, *the only way to have one at all* — and it was wired at precisely the point shared hosting does not reach. Verified by loading `fiskr.wsgi` exactly as Passenger does: the hook reads `None` before the first request, and `None` after it. Submitting a job from the application woke nothing, and a dead daemon stayed dead until a human pressed "restart". The assignment now happens at module import, where it costs nothing — no database, no process — and it is gone from `lifespan`, so there is one place, not two.

**The repair of imports stuck in `PROCESSING`.** It fires after one hour; in production a PEP snapshot sat frozen for three days. It has moved to the worker daemon, which does start in production and is unique (flock), so it runs once whatever the number of web processes — and it now sits where it belongs, right beside the requeue of jobs killed by a brutal stop. A job put back in the queue without its snapshot leaves a list out of production with all its records in the database, which is exactly the observed state. It also runs every five minutes, not only at start: a snapshot can freeze *while* the daemon lives — the job dies, the snapshot stays — and waiting for the next restart is what cost three days.

Not everything in `lifespan` is affected, and the difference is worth stating: `init_db` is covered, because `get_db` calls it lazily on the first request (checked: `SessionLocal` goes from unset to set with no `lifespan` in sight), and the engine cache has `_ensure_watchlist_cache` as its documented net. What has no path is what was fixed. The same sweep over every module looking for process-local state answering a user-visible question came back empty.

### Fixed — twelve modals claimed `aria-modal="true"` and left the keyboard outside
A modal that carries `aria-modal="true"` asserts that nothing else exists while it is open. Measured in a real browser, one modal at a time: **twelve out of twelve** left focus where it was — behind the dialog — and Tab walked straight back into the page underneath. For a mouse the modal was open; for a keyboard it had merely appeared.

Three gestures, held in one place: focus enters on open, stays while the dialog is open, and returns where it came from on close. Entering means the first control that is *not* the close cross — a form modal opens on its first field, a reading modal on its own container so the dialog announces itself. Leaving means the element that opened it, and only if that element is still on the page: a dialog opened from a table row the refresh has since replaced has no "where it came from".

**A modal closes one way.** Escape and the backdrop click used to add the `hidden` class directly, which is not what the cross does. `closeAlertModal` resets the current alert id; Escape left a stale one behind. `closeAuditModal` also clears an inline style. The close command is already declared in the markup — the cross carries it — so both now replay it rather than keeping a second list here that will drift.

Two things the measurement caught that no error would have. `.modal` is `position: fixed`, so `offsetParent` is null even wide open: the first visibility test declared all twelve permanently closed and therefore never opened anything. And `[href]` alone matches the `<use href="#icon">` inside an SVG — it satisfies the selector and refuses focus, so two modals out of eleven stayed unreachable in silence. The code now *checks* that focus actually moved instead of assuming it.

Dialogs also announce their own name now: `aria-labelledby` points at the heading already present in the markup, instead of a screen reader saying "dialog" twelve times.

### Removed — a screen that had been replaced, and a rule written twice
The **Mon Profil & Sécurité** modal did profile and password; the **Mon compte** tab does profile, password, MFA, absence and delegation, notifications and display preferences. Nothing had opened the modal since — but it was still there, still translated into six languages, still read by anyone trying to understand how a password gets changed. Removed with its form and its three orphaned translation keys, which the i18n guard flagged the moment the markup went.

The backdrop click had two implementations too: `initA11y`, covering all twelve modals, and a `window.onclick` assignment covering four of them, closing them without their close command — and clobbering any other handler on the way. Removed; one rule, one place.

A guard now states the general form: **no function in `app.js` may be unreachable**. It holds with no exception list, and it fails on the previous file, naming `openProfileModal`. Two more alongside it — every command in the markup resolves to a function that exists, and every id the code looks up is still in the markup. None of those three defects surfaces at runtime: the browser reports nothing, a missing function only throws on click, and `getElementById` simply returns `null`.

### Fixed — seventeen commands the keyboard could not reach
An element that *reacts* to a click must be reachable and triggerable without a mouse. `<button>` and `<a href>` are, natively; a `<li onclick>` or a `<div onclick>` is not — it is skipped by tabbing altogether. Counted on the rendered page: **344 clickable elements, 17 of them unreachable** — the eight modal close crosses, every sortable table header, the rows of the notification centre and of the home screen, the user badge in the sidebar.

The stylesheet had already stated the intent for the crosses: `.close-modal` carried `background: none; border: none; padding: 0` — rules written for a button, applied to a `<span>`. Turning those eight spans into `<button type="button" aria-label="Fermer">` therefore changes not one pixel, and gives back what the markup had promised.

Sorting was mouse-only on **every table in the product**: `<th>` is not a command as far as the browser is concerned. The headers now carry `tabindex="0"` and `role="button"`, and Enter or Space sorts. Space is intercepted only over a header — measured on a text field, `preventDefault` stays false, so typing a space still types a space.

The generated rows are the harder half: the front end rebuilds its lists on every refresh, so marking once at load would not hold. One observer picks up each injected fragment and one delegation handles the key — never a listener per row. Cost measured on 800 injected rows: **3.3 ms, all 804 marked**. Re-counted afterwards: **17 unreachable → 1**, that one being the sidebar overlay, left out on purpose — Escape closes the drawer, and adding it to the tab order would only buy an empty stop.

### Fixed — the mechanism that turned a markup slip into an accumulation
The stray `</div>` was fixed last release; what made it *visible as four stacked screens* was not. `switchSubTab` turned panels off with a query **scoped** to the section and back on by **global** id, so a panel that had escaped its section could be lit and never extinguished.

Both halves now query the section. A panel that exists globally but not in this section is no longer activated — it is reported: `console.error` naming the panel, the section, and what to check. A tab that does not answer is a defect you can find; a tab that lights up and stays lit is a defect that looks like a rendering bug.

Searched across the whole file, function by function: this was the only place mixing the two scopes. Two guards keep it that way — one reads the body of `switchSubTab` and refuses the global form of the *assignment* (while leaving the diagnostic branch alone), the other checks every `switchSubTab('section', 'panel')` call in the HTML and the JS against where that panel actually lives in the markup.

### Checked — two full sweeps of the interface, nothing found
Both run against the real application in a real browser, screen by screen across all **35 sub-tabs**: **zero JavaScript errors** and **zero failing network requests**. Recorded here because a sweep that finds nothing is only worth something if it is written down — otherwise the same ground gets walked again next time, and the fact that it was once clean is lost.

### Added — a commissioning screen that checks *this* installation, on first start
A fresh install starts silent. Nothing says that no list is in production — so screening will answer "no match" for ever without complaining — that the worker daemon is absent, or that the secrets are still the ones in the source code. Those three states lived in a startup WARNING, in a log nobody opens, or nowhere at all.

**Guide → Mise en service** (`GET /api/setup/status`, admin only) reports twelve checks across four families — Foundation, Lists, Screening, Operations — each with what was observed, what to do about it, and a link to the screen that settles it. On an empty base the screen opens by itself; a banner then persists on every screen for as long as a **blocking** point remains.

Three levels, and the difference matters: **Blocking** — the product cannot do its job, or a door is open (default secrets, daemon down, no list in production). **Warning** — it works, but it will be paid for later (SQLite fallback, deferred indexes, silent notifications). **To do** — a normal step of getting started, not a defect.

**Nothing is stored.** Each check queries the same source as the screen it describes: the database for lists, the queue for the daemon, `fiskr.config` for the secrets, the last `init_db` for the indexes. A point that turned false again six months after go-live would say so — that is what separates a check from a box ticked once. A test pins it: the secrets check flips from blocking to verified within the same session, purely from the state changing.

**Configured is not reachable.** The report observes that an SMTP server is *declared*; never that it *answers*. Answering means opening a connection, which a status page has no business doing on every render — hence the explicit probe, triggered by a human, bounded by a short timeout, journalised. The distinction is not theoretical: it was found in production, on a correctly declared SMTP whose every send timed out while the application believed itself able to warn — including about a source's synchronisation failure.

One thing the deferred performance indexes needed on the way: `init_db` decides not to create them (an ordinary `CREATE INDEX` locks the table for minutes) and said so in a startup WARNING only. The list is now readable, rebuilt at every `init_db` — never a copy to maintain, always what the last start observed.

### Fixed — one stray `</div>` put four screens outside their own tab
Reported from production as "a big display problem". Diagnosed by driving a browser against the running application: four panels — Ajout Manuel, Sources, Homologation, Historique — were children of **`<body>`**. A surplus `</div>` closed `#sec-watchlist-mgmt` early, and the parser hoisted everything after it out of the layout entirely.

Three consequences, all of them visible on screen:

- **They showed on every tab.** Outside any `.tab-content`, they escaped `display: none` — Screening, Audit, Settings, whichever tab was selected.
- **They stacked.** `switchSubTab` deactivates panels with a query **scoped** to the section (`section.querySelectorAll`) but activates by **global** id. A panel outside the section could therefore be switched on and never off. Measured: three to four panels rendered at once, a page 6 424 px tall instead of 4 451.
- **They slid under the sidebar.** Outside `.main-content` they lost its `margin-left: 280px` and started at x = 0, beneath a `position: fixed` sidebar that is 95 % opaque — which is why the screenshots showed the form legible *through* the navigation.

That asymmetry in `switchSubTab` — scoped off, global on — is what turned a markup slip into an accumulation. What the browser reports after the fix: one panel at a time, page height back to 4 451 px, content at x = 328 instead of 0.

The guard derives the answer from the markup itself: every `.sub-tab-content` inside a `.tab-content`, everything inside `.main-content`, both served pages structurally balanced, and one panel active per section in the shipped HTML. Verified to bite — it fails on three counts against the previous file. A browser says nothing about a stray `</div>`: it closes the tag, moves the rest, and renders something plausible.

### Fixed — a synchronisation could not say where it was, from another process
Reported from production: two syncs showing "Analysing the file…", indeterminate bar, counter at zero, apparently forever. They were not stuck. PEP finished normally while the diagnosis was under way — **707 951 records in thirteen and a half minutes**. It was invisible, not blocked.

`SyncProgress` published its phases into `fiskr.progress`, an **in-memory registry private to the process that writes it**. A sync runs in the worker daemon; the screen queries an API process that will never see that registry — `/api/diagnostic/jobs` said so plainly, `progress_active: []`. The queue row does cross processes, and it kept the phase stamped at claim time — `PARSE`, rendered as "Analysing the file…" — from the first second to the last, with `processed` at 0.

The bridge already existed and its docstring describes this exact case: *"the registry does not cross processes, the jobs row does"*. The sync simply did not take it. It does now — every phase change goes straight through, a phase that has not changed is rewritten at most every three seconds, and a broken bridge never interrupts a sync. The snapshot row, meanwhile, had known all along: `PERSIST, 593 000 records processed`.

What it cost is not cosmetic: an operator had no way to tell a source that is advancing from a source that has stopped responding. On the very same screen, the DFAT source — whose host accepts the connection and then never sends a single byte — displayed exactly the same thing.

### Fixed — the direct DFAT route has never worked, and cost a work slot every day
Measured on 22/08/2026: `dfat.gov.au` answers 403 to a HEAD, then accepts a GET connection and sends **nothing** — 110 s, zero bytes. So the download never ends on its own: it burns the whole retry budget (4 attempts × 120 s read timeout, plus backoff) before failing, about **nine minutes of a work slot on every run**. In production that source has **never** produced a single snapshot, while the failure notification could not be delivered either (SMTP has been timing out on every message).

With two slots and two long syncs, everything else waits: 13 snapshots had piled up awaiting approval and 11 promotion jobs sat queued. They all drained the moment a slot came free — observed live.

Australia is covered without it by the `au_dfat` source (OpenSanctions aggregate of the same perimeter), which syncs normally — 3 737 records, promoted the same morning. The shipped configuration now says so, next to the URL, rather than leaving a dead host looking like a working default.

### Fixed — three counts that credited the system for work it had not done
Same question as the backtest fix, asked of the rest of the surface: *does each published number count what its label says?* Three did not, all on the alert-volume side, and all in the same direction — the system was credited with more work opened, and less noise absorbed, than reality.

**A re-screening announced alerts it had not opened.** `open_or_redetect_alerts` — the name says it — opens **or** re-detects, and returns the exact breakdown: `opened`, `redetected`, `closed_by_rule`. The re-screening threw that return value away and kept its own tally, incrementing "new alerts" on every match that got past the whitelist and the rules. A match landing on an **already open** alert creates nothing. Measured on the plainest case there is — the same list re-screened twice:

| | announced | actually created |
|---|---:|---:|
| first pass | 1 new alert | 1 |
| second pass | **1 new alert** | **0** |

The case is not marginal, it is the ordinary one: on every list refresh, a listed record that changed but still matches the same client produces a re-detection. And a **lookback** — which re-screens the whole production universe — produces almost nothing else, so it announced as many novelties as it found matches. The number reaches three recipients (the step e-mail after a promotion, the approval e-mail, the end-of-sync message), all labelled "new alerts". It is the number read by whoever just approved a list, to judge what their approval did.

The counts now come from what actually happened. And the report's shape is defined in one place, because it had already drifted: `rule_suppressed` appeared in the result **only if a rule had fired**, and `rescreen_lookback`'s empty return did not carry it at all — a recipient could not tell "no rule fired" from "nobody told me".

**A batch campaign counted whitelisted matches as opened alerts.** `hits_count` counts matches above the cut-off — *all* of them, whitelist and FP rules included — and every screen presented it as "alerts opened", column header included. The model's own comment asserted that screening "opens one alert for each". It does not, and it is wrong by exactly what the system absorbs. Measured, one client, one Good Guy on the matched pair:

| | clients in alert | matches found | alerts opened |
|---|---:|---:|---:|
| without whitelist | 1 | 1 | 1 |
| with one Good Guy | 0 | 1 | **0** |

The screen showed "0 clients in alert" next to "1 alert opened" — contradicting itself in two adjacent columns. `opened_count` now carries the alerts actually opened, and the gap between the two numbers is exactly what the whitelist and the rules removed: the one thing those two mechanisms produce, and it was invisible. Additive column: earlier campaigns carry NULL and the screen shows "—" rather than a zero that would read as a measurement.

**The dashboard's false-positive rate ignored the rules.** `CLOSED_BY_RULE` is a closed status, deliberately outside the rate — an alert closed by a rule was reviewed by nobody and says nothing about the quality of what reaches an analyst. That is defensible, and the tile said "rate on closed alerts", which is not what it measures. It now says *reviewed* alerts, publishes its denominator, and shows the volume the rules absorbed beside it — without which the harder the rules work, the less the screen shows of the noise the system actually produces.

### Fixed — the backtest counted the same client twice, and never split the volume per list
Two defects on the screen that gates a list's approval, both found from the same question: *do alerts and hits actually break down per list delta?*

**A panel of one client could report a 200 % interception rate.** The backtest screens two ways. In **full** mode, one pass over the production universe and one over the candidate. In **delta** mode — the default, and the one that runs in production — one pass over the *shared* universe (everything that does not move), then two tiny passes over the removed and added records. A client matched **both** by an unchanged record and by a record in the delta received an outcome in each pass, and the passes were added together: that client was counted twice. Reproduced with the most ordinary case there is — an official list carrying the same individual twice, under two programmes and two ids, of which the candidate version adds the second:

| | delta mode | single pass |
|---|---:|---:|
| clients intercepted | **2** | 1 |
| interception rate | **200 %** | 100 % |
| matches (hits) | 2 | 2 |

`hits` was right — two passes cover disjoint record sets, so a match cannot appear in both. `alerts` was not, and `gap_pct`, the number that decides the approval, is computed from it.

The fix is one sentence: **one client, one outcome** — the one a single pass would have kept, the best match. The aggregation is rebuilt around a `par_client` map from which every published counter derives, so they cannot contradict each other; combining the delta passes now goes through the very same merge that combines the slices of a parallel screening. Whitelisted outcomes had to start carrying their client id and score, without which two passes could not tell they were talking about the same client. A test pins delta mode to a single pass, number by number, and fails against the previous behaviour.

**The alert volume was never split per list.** A consolidated backtest covers a wave of deltas, and its per-list table gave "alerts gained / lost" — which are *clients newly intercepted*, not alerts. Matches now carry the list of the record that triggered them, all the way to the report: each list shows its own alert volume, before and after. The difference is exactly the point — in the duplicate case above the list intercepts **no new client** and yet opens **one more alert**, and the reviewer could see neither number before. The global counters, meanwhile, cover the *whole* screened universe, untested lists included; they never answered "what does approving this list add?".

Which surfaced a third one: the report's own cards announced `alerts` as "N alerte(s)" while it counts clients, and never showed the volume at all. The vocabulary discipline already applied on the approval file — *clients intercepted* against *alerts opened* — now holds on the report too, and the guard test was widened to catch it.

### Fixed — the README announced 153 tests while the suite held 1 370
A number written once in a document ages exactly like a hand-copied table, and this one was off by a factor of ten. It is corrected, and now derived from `tests/` by a test rather than trusted. `FISKR_JOBS_MODE` is documented too — it **overrides** `config.yaml`'s `jobs.mode`, which is worth knowing before wondering why a freshly edited `jobs.mode` has no effect. Three variables the code reads were missing from `.env.example` altogether, `ANTHROPIC_API_KEY` among them: an operator starts from that file, so a variable absent from it is invisible — the two AI functions declined cleanly without anything ever saying what to fill in. A test now holds every read variable against the template.

Swept in the same pass, with nothing found: every repository file path and internal link cited in the documentation (58 and 0 broken respectively), the 676 element ids of the interface (no duplicates), every `getElementById` target, all 239 front-end API calls against the live route table, the endpoints quoted inside the in-app guide, the client list-type labels against the 42 types the server produces, and the environment variables the code reads against those the documentation names.

The architecture document's §6.4 — *shared vocabularies: derived, never copied* — is extended to say what this batch showed: the rule does not stop at code. Documentation, translation keys and CSS rules are hand-copied tables too, and two of them fail in an instructive way. A **graceful fallback hides the defect**: i18n leaves French in place when a key is missing, which is the right product decision and is precisely why seventy-one labels went untranslated for months without one error message — so a graceful fallback must always come with a test that you are not permanently falling into it. And a **priority that cancels its source**: a CSS rule fully redeclared inline is unreachable, and the symptom is `!important` flags appearing one by one to win the fight; the cure is not another one, it is bringing the values back to a single place.

### Fixed — seventy-one labels showed in French to every non-French user
The i18n engine matches a text node against a French key. Its header states the fallback plainly: *"strings absent from the dictionary stay in French (never a hole)"*. That is the right call — and it is exactly what makes this defect invisible. A key that no longer matches anything raises nothing, logs nothing, and simply leaves French in front of a reader who asked for another language.

When the interface moved from emoji labels to inline SVG icons, the dictionary kept its emoji-prefixed keys. `"🚪 Déconnexion"` no longer matches a page that renders `<svg …/> Déconnexion`. **Seventy-one labels** were affected, and they are not marginal ones: Instruire, Éditer, Supprimer, Commenter, Escalader, M'assigner, Proposer : Faux positif, Proposer : Vrai positif, Valider (4-yeux), Refuser & renvoyer, Rejeter, Approuver & Mettre en Production, Lancer la campagne, Purger le journal — plus every settings section heading (Sécurité des Accès, Rétention des Données, Seuils de Score du Criblage, Clés d'API Techniques…). The whole vocabulary an analyst clicks all day.

Six more keys were stale for a different reason — the wording moved on (`Criblage à blanc — listes en production…` became `Criblage à blanc (passe 1/2) — listes en production…`), and one link's key still carried an emoji the markup had dropped, leaving "Créer une règle" untranslated as well. Every key is re-pointed at the text the interface really renders, and the emoji is stripped from the translations too, since the rendered text no longer has one.

The existing tests could not see any of it: they sample the dictionary, and they check that the dictionary is *complete* — never that a key still corresponds to something on screen. The new guard derives the answer from the interface itself: it collects the text nodes and translatable attributes of both pages, the string literals of `app.js` (re-joining literals split across lines), and the server messages, then asserts that **every** dictionary key can be reached by at least one of them.

### Fixed — a link on the backtest screen did nothing
`onclick="showTab('alerts')"` — a function that exists nowhere, and a tab named `alerts` that exists nowhere either. Clicking "Créer une règle" threw a `ReferenceError` and left the user where they were. The real call is `switchSubTab('screening', 'alerts-rules')`, and the label now names the destination as the interface names it: Criblage → Règles FP. The same sweep checked all 676 element ids (no duplicates), every `getElementById` target, and every `switchTab`/`switchSubTab` destination against the 14 sections and 102 sub-tabs actually declared — this link was the only one broken.

### Fixed — dead CSS was hiding a lost affordance, and a rule nothing could change
Two defects hide behind one another in a stylesheet that has lived.

**A rule nobody names any more.** The markup moves on, the rule stays. Harmless — until it was carrying something the new markup never took back. That is what happened to the notification centre: `.notif-item` held the padding, the pointer cursor and the hover of clickable rows; the rendering moved to bare `<li>`s, the rule was orphaned, and the rows lost every sign that they can be clicked. Nothing broke, so nothing reported it. The proof that the cursor was expected is in the rendering itself: the rows that are *not* clickable ("Nothing to handle.") still give themselves `style="cursor: default;"` — which only means something against a `cursor: pointer` baseline. The sibling list built by the same code, `.home-list li`, kept all of it. Eight orphaned rules are removed and the affordance is restored.

**A rule an inline style had already overruled.** `#notif-panel` was styled twice: a stylesheet rule, and an inline `style` attribute redeclaring every one of its properties with *different* values. The rule was unreachable — you could edit the width, the radius, the z-index and see strictly nothing. The evidence that someone hit this: the mobile rule for the same panel carries four `!important` flags added to win against that inline style, and the one declaration that was **not** flagged, `right: 0.5rem`, loses to the inline `right: 0` — so on a phone the panel sits flush against the right edge instead of keeping its gutter. The inline values are moved into the stylesheet (identical rendering, one place to change it), the `!important` flags are gone, and the gutters are symmetric again.

Both are now held by derived tests, including one that accounts for `!important` — `.hidden { display: none !important }` legitimately beats an inline `display`, and a naive check would have called it a bug.

### Fixed — five translation keys written twice, and the better wording lost
In a JavaScript object literal a repeated key raises nothing: **the second silently overwrites the first**. Five keys appeared twice in the dictionary, and two carried *different* translations — so the wording someone chose deliberately was never displayed, and nobody could learn it. "Campagnes batch" rendered in Chinese as 批量任务 ("batch task") instead of 批量筛查活动 ("batch screening campaign"), the precise term, and the one its own sibling entry `📦 Campagnes batch` still uses on the very same screen.

The existing i18n tests could not see this: they sample ("one key entry", "a probe on one entry"), and a sample cannot find a hole somewhere else. The dictionaries are now parsed and checked in full — 640 label entries, 97 paragraphs, 10 composed rules, each carrying its five target languages, no key written twice, and no `$3` referring to a group its pattern never captures.

### Fixed — two documented endpoints did not exist
`GET /api/clients/quality`, in the client-integration guide, is really `GET /api/quality/clients` — the two segments are the wrong way round, so an integrator following the guide gets a 404. The other was in an entry of this changelog. Documentation is a hand-copied table like any other, and it drifts the same way: every endpoint, repository file path and internal link cited in `README.md` and `Documentation/` is now checked against the live route table and the repository itself. That sweep also found fifteen links pointing at `file:///e:/Program Files/git/Fiskr/...` — the drafting machine of whoever wrote those pages, useless to every other reader, one of them broken even there. They are repository-relative now.

### Fixed — the upload cap closed one door and left the other open
The previous batch capped every upload. But a list enters Fiskr through **two** doors: an operator drops a file on the import screen, or a configured source serves it over https. It is the same artefact, read by the same parser, written to the same working directory — and the second door had no cap at all.

- `download_to_file` wrote to disk for as long as the host kept sending. On shared hosting a full disk is an outage, and a refusal that leaves half a file behind occupies exactly the space it was refusing to grant — so the partial file is now removed.
- `http_get_text` returned `response.text` with **no bound whatsoever**. It serves the negative-press RSS feeds and the EUR-Lex scraping, so the size of what it reads is decided entirely by the far end.

Bounding `http_get_text` meant changing *how* it reads, not adding a check: `client.get()` buffers the entire body before returning, so any measurement taken afterwards protects nothing — by the time you can measure, the memory is already spent. It now streams and refuses at the first chunk past the ceiling; a test asserts the flow really stops there rather than after the fact, because a cap that only reports is not a cap.

The ceilings live in one place, `fiskr/limites.py`, and the download ceiling is **derived** from the list-upload ceiling rather than copied next to it — the same anti-divergence rule the front-end tables above are now held to. A page or an RSS feed is not a data file and gets its own, much lower ceiling: 32 MB, against 12 KB measured for a real Google News feed on a name query. An overflow is deliberately **not** retried — the response will not shrink, and replaying it would pay twice for the download just refused.

### Fixed — a second, dead copy of the alert-opening path
`open_or_redetect_alert` (singular) was imported by three modules and called by none, while duplicating ninety-five lines of the most sensitive compliance path there is: deduplication, priority, SLA due date, closure by rule, event journal, notification. Two implementations of the same regulatory obligation, one of them never exercised by a single test, is a trap with a fuse: the next person to fix a bug in alert opening had one chance in two of fixing the copy nobody runs. It is removed; `open_or_redetect_alerts` (plural, batched) is the only path, and a test now holds that it stays the only one.

### Fixed — the upload cap did not bound what a connector holds in memory
Three official connectors are JSON — the French national freeze registry, the American Consolidated Screening List, World Bank debarment — and the standard library offers no streaming reader: `json.load` builds the whole tree. That tree weighs **more** than the file, and by how much depends on the content. Measured with `tracemalloc`:

| Content | Factor | 512 MB would give |
|---|---:|---:|
| realistic CSL entries | ×4,0 | 2,0 GB |
| distinct short strings | ×6,0 | 3,0 GB |
| tiny objects `{"a":1}` | ×15,1 | 7,5 GB |
| empty lists `[]` | ×16,1 | 8,0 GB |

The upload cap set in the previous batch (512 MB for a list) was therefore not enough: on shared hosting the process dies, and under Passenger the whole web worker goes with it.

`TAILLE_MAX_LECTURE_BLOC` (64 MB) now bounds what a connector **without** a streaming reader accepts. That is more than three times the largest real file — trade.gov's CSL weighs about twenty megabytes — and it bounds the adversarial worst case to roughly one gigabyte. The refusal is explicit and says what to do: a bigger file must come through a streaming connector.

Two readers needed no cap, only to be written as streams: the British ConList did `f.read().splitlines()` — half a gigabyte of text plus the header of several million `str` objects before reading a single useful line — and the regulator alert pages materialised the whole page, where `HTMLParser.feed` accepts chunks.

### Fixed — three front-end tables had drifted from what the server produces
Same class of defect each time: a table copied by hand, a source that moves on, and a screen that shows a raw code or — worse — offers a key that does not exist. All three are now **derived** by a test from what the code actually produces.

**The rule editor's palette offered keys that do not exist.** The false-positive rules screen offers `ctx` keys at a click, their sub-keys, and code templates.

- **Five keys were missing**: `perimeter`, `hits_count`, `hit_rank`, `corroboration`, `rarity` — precisely those that exist *so that* a rule can reason about volumetry, corroboration and how banal a name is. A rule author could not discover them.
- Two `entity` sub-keys **did not exist**: `programs` and `designation_date` (the real columns are `sanction_programs` and `listed_on`). A rule written from those chips always read `None`.
- A shipped template read `adjustments["country_penalty"]`, which does not exist: the comparison was `0 <= -10`, so **the rule never fired**.

That last one is the worst defect possible on this screen: a silently inert rule tells nobody — not its author, not its validator, not the auditor. Two templates are added, one on name-only matches and one on rarity, both written to respect the sanctions perimeter. `RULE_TEMPLATE`, the first thing a rule author reads, enumerated the context without `perimeter` or `rarity` either.

**The administration journal spoke in English capitals.** It is the screen a controller opens first. **Twenty-eight of the thirty-five logged actions** had no French label and showed as raw codes: `RETENTION_PURGE`, `ACCOUNT_LOCKED`, `APIKEY_REVOKED`, `MFA_RESET`, `LOGIN_FAILED`… All thirty-five are now named, grouped by family.

**The progress vocabulary had drifted three ways.** A long operation announces a *phase* and a *kind*. Four emitted phases were declared nowhere, including `QUEUED` — the phase every queued job carries, so the one an operator saw most often, shown as a raw code in the middle of a French interface. `INDEX` was declared on both sides and emitted by nothing. Nine of the fourteen kinds submitted had neither icon nor deep link — and `#batch`, the link on the batch-campaign row, resolved to no screen at all (`applyHashRoute` looks for `sec-<tab>`, and `sec-batch` does not exist), so the click did nothing.

### Fixed — a table in error must not look like an empty table
On a compliance product this is a distinction of substance. An analyst who sees an empty table concludes there is nothing to investigate; if the call actually failed, they have just concluded wrong, and nothing tells them.

Three shapes existed on the list screens, all bad: **no check of the status code** (the error payload was read as data, `data.items` was `undefined`, and the table showed "no snapshot" / "no decision"); **a bare `return`** (the loading skeleton stayed on screen, so a table that seems to load forever); and **the empty state reused to say an error** (right text, wrong colour, indistinguishable at a glance).

`tableError()` now paints a distinct state — red frame, warning icon, and what to do — that `tableEmpty()` cannot imitate. Along the way, one colspan gap: the batch-campaign empty state announced nine columns since a tenth — "alerts opened" — had been added to the table. A test now checks that **every** empty, error or loading state spans the full width of its table.

### Fixed — ten form fields were mute to a screen reader
A `<select>` or `<input>` with no accessible name is announced as "combo box" or "edit", nothing more: the user hears that a control is there, not what it does. On a product analysts use all day that is a real obstacle, and WCAG 2.1 criterion 4.1.2.

The screen already uses all three valid forms (`<label for>`, wrapping `<label>`, `aria-label`); ten fields had none — mostly filter lists standing alone in a toolbar, and file pickers. Twelve accessible names were also missing from the translation dictionary, which the i18n engine explicitly translates: a name left in French is read as-is by a screen reader set to English, German or Arabic.

### Added — what a name is worth: the rarity of its words
Keeping every match above the cut-off is the audit requirement; "Mohammed Ali" without a country produces **2 976** of them in production, dozens of them at 100,00. No string metric separates those records — the names *are* identical, and no metric should. What separates them is elsewhere: in the listed corpus, "MOHAMMED" and "ALI" are borne by thousands of records; "TYURIN" by one. Matching two names on omnipresent words identifies nobody.

`fiskr/rarete.py` counts the **document frequency** of name words over the universe actually screened — primary name *and* high-priority aliases, a record counted once per word — and attaches the count to the alert:

- **Written into the decision tree** of matches in ALERT, not merely displayed: a rarity is re-read months later, at audit, alongside the corpus that produced it.
- **Readable by the false-positive rules**, `ctx["rarity"]`.
- **Shown to the analyst** in all three decision-tree views.
- **Exposed for calibration** — Engine screen and `GET /api/screening/name-rarity`: you see what the corpus holds *before* writing a threshold into a rule.

A shipped rule template uses it, scoped to the **HORS_SANCTION** perimeter: it closes the banal name without corroboration, and **a single rare shared word keeps the alert open**. With no table (a process that has not loaded the cache) every flag is at rest: the rule closes nothing, and it does not crash.

**Rarity moves no score.** Adding a term to the calculation would shift, in one stroke, every calibrated threshold, every rule written against them and every homologated test book. The signal goes to whoever decides — the rule, the analyst — it is not imposed on the engine.

Measured on a real sample of **12 500 production records** (25 pages spread across the 832 470 in production):

| Word | Share of corpus | | Word | Share of corpus |
|---|---:|---|---|---:|
| DE | 3,95 % | | AL | 1,18 % |
| JOSE | 2,25 % | | ALI | 0,88 % |
| MARIA | 1,98 % | | MOHAMMAD | 0,52 % |
| SILVA | 1,82 % | | IBRAHIM | 0,51 % |

| Pair | Verdict |
|---|---|
| « Mohammed Ali » vs MOHAMMED ALI | common — closable |
| « Jose Silva » vs JOSE DA SILVA | common — closable |
| « Vladimir Putin » vs VLADIMIR … PUTIN | PUTIN is rare — **alert kept** |
| « Igor Sechin » vs SECHIN IGOR IVANOVICH | SECHIN is rare — **alert kept** |

By extrapolation (Heaps' law fitted on that sample) the full corpus carries ~584 000 distinct words. The table keeps only the **20 000 most frequent**: the words that decide anything number in the hundreds, and any absent word is *rarer than the last one kept* — an **upper** bound, never zero, so an unknown word never closes anything.

The table is built where the universe is in memory (the API cache) and otherwise **from the database**: re-screening and test books run in the worker daemon, which cannot see the API process's cache. Without that path, a rarity-based rule would close in one process and not in the other.

The screening response **does not grow with the perimeter** — a contract established after the ~240 MB measured in production. Putting rarity on every `all_matches` row broke it (45 366 B for 60 candidates against 50 835 B for 200, where the contract tolerates 10 %). It is returned in full on `best_match` and written to the journal: the database is what counts.

### Fixed — the filtering index was rebuilt on every payment message
`screen_payment_message` flattened the screening index, deduplicated by `entity_id`, then regenerated the blocking keys of **the whole universe** — for every message. Measured on a corpus at production proportions (832 470 records): **17,0 s** of key generation and **1,7 s** of flattening. Nineteen seconds paid inside the HTTP request, on the channel whose first requirement is response time.

The index is now memoised per process under a signature covering everything that changes the keys: production list fingerprint, channel layout, engine capabilities, active linguistic equivalences. A stale index would miss records — a silent false negative — so invalidation is tested change by change.

The list restriction is **not** in the signature: it now applies to **candidate selection**, on the handful of records in a bucket, rather than to index construction. Restricting no longer costs nineteen seconds, and a test verifies the excluded set is exactly what it was.

### Fixed — one payment message could cost more than an hour of computation
The number of parties screened in an ISO 20022 message was unbounded. A message accepted up to the upload cap (8 MB) carries **56 678 transactions** — measured on a real minimal pain.001, 148 bytes per transaction — hence as many distinct parties. Each queries the filtering index and compares its candidates: in production a phonetic bucket holds **415 records on average** (25 906 for the largest) at ~180 µs per comparison, so ~75 ms per party. Over an hour of computation for a single message, inside a synchronous HTTP request, with as many rows written to the immutable audit trail.

That request **already** failed on the server's timeout — but after burning that time and writing those rows. The refusal is now immediate, *before* any setting is read and any computation done, and it says how many parties the message carries and what to do: split it. The bound is 500 parties (~37 s at the measured average cost, already the maximum reasonable for a synchronous response). If real files exceed it, the natural follow-up is to route large messages through the job queue as batch campaigns are — but that changes the endpoint's response contract, so it is a product decision, not a fix.

### Fixed — every file upload was unbounded
None of the six upload endpoints had a size cap. Transaction filtering read the whole message into memory (`await file.read()`); the other five copied to disk with `shutil.copyfileobj` without a limit. A single file was therefore enough to exhaust the worker's memory or the instance's disk — and the monitored CFT inbox is fed by an upstream system, so the input is not always an attentive operator's.

Three bounded helpers replace the raw copies, with caps differentiated by nature of deposit — an official list is bulky by construction (OFAC's `SDN_ADVANCED.XML` weighs 126 MB, measured), an alert attachment is not: **list 512 MB, clients 64 MB, attachment 32 MB, message 8 MB**. Past the cap a 413 is raised and nothing partial is left on disk. An AST test checks that every endpoint function taking an `UploadFile` goes through one of the helpers, so the next endpoint written cannot reintroduce the hole.

### Fixed — a single malformed record made every screening that saw it fail
The multi-valued fields — `countries.citizenship`, `aliases.high_priority`, `dates_of_birth`, `genders` — are JSON columns whose shape no schema guarantees. Two documented doors let a string through where the engine expects a list: `PATCH /api/watchlist/entity/{entity_pk}` declares `countries: Dict[str, Any]` (so `{"citizenship": "FR"}` is **valid** to Pydantic), and the client upsert webhook declares `client_countries: Dict[str, Any]`, fed system-to-system by an upstream nobody controls.

`[] + "FR"` raises. On the client side the request returned 500 — annoying. On the **listed record** side it was far worse: one malformed record made **every** screening whose blocking selects it fail, and a screening that does not complete leaves **no audit line at all**. An invisible false negative. Losing one context field beats losing the screening.

One case was worse than a crash because it was **silent**. An alias written `{"high_priority": "IVAN IVANOV"}` was extended **character by character** — "I", "V", "A", "N"… — so the alias was never compared, and a record listed under its alias went through with nothing to show for it. It is now screened like a list.

### Fixed — no bound on the length of an incoming name
Damerau-Levenshtein is **linear** in the length of the screened name (the other side, the listed record, is short):

| Name length | Damerau-Levenshtein | Full base score |
|---:|---:|---:|
| 100 | 0,33 ms | 1,19 ms |
| 1 000 | 2,67 ms | 4,30 ms |
| 5 000 | 13,85 ms | 20,31 ms |
| 20 000 | 56,02 ms | 82,55 ms |

Multiplied by a bucket's candidates — 415 on average in production — a single 20 000-character name is worth **34 seconds** of computation for one request. And the computation is lost in advance: the database stores `String(1000)`.

The bound is therefore **exactly the database's**, 1 000 characters. The longest *real* name in the production corpus is **310** (a Russian penitentiary institution, measured on the 12 500-record sample), and ISO 20022 caps `<Nm>` at 140. Screening and the client webhook refuse with a 422 before any computation; in a payment message the name is **truncated**, the party is not rejected — refusing the whole message would leave it unscreened, which is worse.

### Fixed — the blocking-component cap existed only on read
`MAX_BLOCKING_FIELDS = 3` exists for a measurable reason: index lookup tries **every combination of wildcards** over the field components — otherwise a listed record that leaves such a field empty becomes structurally unreachable — which is 2^N probes per screening. The cap was applied only when **reading** the setting. On **write**, a layout with four field components was accepted (`200 "Blocking keys mises à jour. Cache de criblage rechargé."`), written to the database, recorded in the administration log — then **silently ignored** by the engine, which fell back to the default layout.

The operator believed they had changed how screening selects candidates; nothing had changed, and nothing said so. Same class as the unreachable `token_set` weight fixed earlier: a setting you can save that never acts. Both sides now count the same set of components, named once.

### Fixed — three missing indexes on `alerts`, and a NULL trap in retention
Since a screening opens an alert **per match**, this table grows by the number of homonyms: one "Mohammed Ali" without a country adds 2 976. Three indexes that cost nothing on a table of a few thousand rows are worth having on one counted in millions.

- `ix_alerts_created_at` / `ix_alerts_decided_at` — the home screen, exports, per-analyst indicators and the daily curve all filter and sort on these two dates. Neither was indexed: every home-screen load scanned the whole table twice ("alerts created 24 h", "decided 24 h"), and `ORDER BY created_at DESC LIMIT 50` read everything to return fifty.
- `ix_alerts_audit_id` — PostgreSQL does **not** automatically index the *referencing* side of a foreign key. Without it, each `compliance_audit_trail` row deleted by retention triggers a **sequential scan** of `alerts` for the referential-integrity check: purging 100 000 audit rows was worth 100 000 scans.

Separately, `_purgeable_audit_query` selects expired audit rows "no longer referenced by any alert" with `~AuditTrail.id.in_(query(Alert.audit_id))`. In SQL, `x NOT IN (…, NULL)` is **never** true. If `alerts.audit_id` became nullable and a single alert carried NULL, the purge would **silently** stop purging — no error, no log, a zero counter that looks normal, and GDPR retention no longer applying. The column is NOT NULL today; a test now holds that, with the rewrite (NOT EXISTS) named in the failure message so that changing the schema is deliberate rather than fatal.

### Fixed — "alert" did not mean "alert" on three screens
Since screening opens an alert **per match**, three places counted *clients* while writing *alerts*: the test book, the engine impact report and batch campaigns. Confusing the two understates the workload by the number of homonyms — a factor of hundreds on a list like PEP. All three now carry both figures, named apart: **clients intercepted** on one side (the rate, unchanged, which makes the verdict), **alerts opened** on the other (the volume of work). `BatchCampaign` gains a nullable `hits_count`, so older campaigns display honestly rather than showing a fabricated zero.

### Fixed — the screening pool returned the whole record for every hit
`_match_chunk` attached the complete listed record to each score before shipping it back to the parent. On a common name, a chunk's hits are counted in thousands, and every one of them travelled through the pickle channel carrying ~1,8 KB of record the parent **already had in memory** (it transmitted it by fork). Children now return the `entity_id` alone and the parent re-attaches the record by identity. An orphaned identifier — impossible unless the index changes mid-run — is logged as an ERROR and dropped rather than silently producing an alert without a record.

### Fixed — CSV formula injection in every export
Every CSV export wrote cell values verbatim. A cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return is interpreted as a **formula** by Excel and LibreOffice: a client name or a decision comment reading `=cmd|'/c calc'!A1` executes on the auditor's machine when they open the export. The content comes from ingested lists, imported client files, payment messages and analyst comments — none of which the product controls. All cells are now neutralised by a leading apostrophe, headers included, on every export.

### Fixed — login timed differently for a known and an unknown account
`POST /api/auth/login` only computed the password hash when the account existed. The two paths were therefore separated by the cost of a key derivation — measurable from outside, and enough to enumerate valid usernames without ever authenticating. The unknown-account path now verifies against a dummy fingerprint computed once at startup, so both paths pay the same computation.

### Changed — ASCII fast path in comparison normalisation
A purely ASCII text passes through `strip_accents_for_matching` **unchanged**, whatever the capabilities: `detect_scripts` finds no non-Latin script in it, and the NFKD decomposition of ASCII is ASCII with no combining character. The demonstration is exhaustive over the 128 code points, and held by a test.

This is the engine's hottest path — taken twice per comparison, over a whole universe of candidates — and **98,3 %** of production listed names are pure ASCII. Measured: 1,01 µs per call with a warm cache against 0,23 µs on the fast path (×4,5); with no cache, 5,39 against 0,34 (×16). End to end: ×1,07 on a full match (the string metrics dominate), ×1,13 on generating a universe's blocking keys, and building the rarity table drops from 12,79 s to 4,43 s over 832 000 distinct names.

### Fixed — comparing two snapshots loaded both of them entirely into memory
The delta engine received two **complete lists of entity dictionaries** — seventy columns each — to publish at most **a hundred rows per category** (`MAX_REPORT_DETAILS`). Measured on 40 000 records against 40 000, half of them modified:

| | Time | Peak memory |
|---|---:|---:|
| loading both snapshots + `calculate_delta` | 5,90 s | +94 MB |
| `calculate_delta_db` | **0,22 s** | **+0 MB** |

Extrapolated to production's largest list — WATCHLIST_PEP, **709 511 records** — the old path asks for **~1,66 GB and ~105 s**, inside a synchronous HTTP request, on shared hosting. The review screen for a manual import of that list simply could not open.

The new path does the work in SQL: an anti-join for additions and removals, a join on **checksums** for modifications, a `COUNT` and a `LIMIT` for each. Only the records actually published in detail are then loaded in full, to derive the field-by-field comparison. The checksum equivalence is not an approximation: `compute_checksum` and `find_differences` exclude **exactly** the same three keys (`id`, `snapshot_id`, `entity_checksum`), so "different checksums" and "at least one compared field differs" are the same statement — and a test asserts the two implementations agree row for row on a mixed dataset.

A new index `ix_wl_entities_snapshot_entity` carries the pair `(snapshot_id, entity_id)`, which is the join key: the two separate indexes each served only half of the condition.

**One behaviour change**: `POST /api/snapshots/compare` used to return the delta in full. It now takes a `limit` (default 1 000, max 5 000) and reports `added_truncated` / `removed_truncated` / `modified_truncated`. The **counts stay exact** — only the sample is cut, and the screen now says so rather than letting a reviewer believe they have seen everything they are approving.

### Fixed — the periodic schedulers ran during the test suite
Every `TestClient(app)` enters the application lifespan. In `eager` mode — the tests' mode — six loops were started there: source scheduler, retention purge, notification digest, KPI digest, homonym mining, CFT inbox polling. All of them wake **on the next full minute**, and all of them work on the **same database** as the tests.

Over a four-minute suite that is roughly four ticks landing on whichever test happens to be running. Hence one failure per run, on a **different test each time**, with rows vanishing under the running test's feet (`ObjectDeletedError`) — a retention purge, a scheduler submitting a real synchronization. Verified on `b3a577e`, before this batch: the lifespan does create the six tasks in eager mode there too, so the defect predates it; a night of repeated runs merely made it visible.

`requeue_stale` is excluded for the same reason: it flips orphaned QUEUED jobs to ERROR, which means nothing when nothing is ever queued — but is enough to break a test that inserts a QUEUED row by hand.

What is removed is the **loop**, not the logic: each scheduler keeps its synchronous tick (`_cron_sync_tick`, `_retention_tick`, `_digest_tick`…), tested one by one and callable without a clock. The `thread` mode (deployment without a daemon) and `worker` mode keep exactly their behaviour.

### Fixed — the list type of a record was resolved by scanning the cache
`next(e["_list_type"] for e in watchlist_store if e["entity_id"] == …)` — 832 470 comparisons per identifier in production. In bulk whitelisting from a test-book report that scan was **inside the loop**: 500 proposed pairs were worth 416 million comparisons for one call. The database already indexes `entity_id`; `_list_types_map(db, ids)` resolves the whole batch in one query (chunks of 800), production winning over a pending or superseded record — the same rule as `_entity_names_map`, of which it is the counterpart.

### Fixed — the test book stopped predicting production, and one of the two gaps was mine
Keeping every match above the cut-off opened two gaps between what the test book announces and what production does. Both were consequences of the previous change, found by asking whether the backtest still measures the thing it claims to.

**The rules were inert during the test book.** `screen_one` called `build_screening_ctx` with its defaults, so `hits_count` was always **1** there. A volumetric rule — "beyond N matches…" — therefore never fired during a test book, while it fires in production. The book announced an interception-rate gap computed with rules that were not applying. It now receives the same volumetry as production, and the best match sits at rank 1 exactly as it does there. A test asserts a volumetric rule fires on twelve homonyms and does not on three.

**The announced volume was a different quantity.** The report counted `(client, listed)` pairs — one per client, the best match. That is the right measure for an *interception rate*, and the verdict rightly rests on it. It is not the number of alerts production will open: production opens one **per match**. The report now carries both, named apart, and the screen shows both:

| | Answers |
|---|---|
| **Clients interceptés** | "What share of the panel is caught?" — the interception rate, unchanged |
| **Alertes ouvertes** | "How much work will this list create?" — one alert per match above the cut-off |

Confusing the two understates the workload by the number of homonyms — a factor of hundreds on a list like PEP. A whitelisted pair still counts its client's other matches: they exist regardless.


### Fixed — `token_set` was unreachable
Found while reviewing the documentation, which is what a documentation review is for. `scoring_weights()` rebuilds the weight dictionary from the keys of `DEFAULT_SCORING_WEIGHTS` alone. `token_set` was not among them, so it was **silently dropped**: setting `token_set: 0.4` through the settings screen had no effect whatsoever, and the metric could never be enabled — whatever value was chosen. The engine did read `weights.get("token_set", 0.0)`, but the key could never reach it.

The metric shipped two rounds ago was therefore dead in practice. It is now in the defaults at **weight zero** — same intent as before, activation still a deliberate act — and two tests cover both ends of the chain: the setting survives, and a weight that survives actually moves the score (70.57 → 110.57 on « Vladimir Putin » vs « Vladimir Vladimirovitch Putin »).

### Changed — the guide and the documentation say what the product now does
The in-app guide described the previous behaviour: a screening opening *one* alert, and a cut-off overridable "per list". Both statements had stopped being true.

- **Guide, Screening chapter**: the decision chain now says that **each** match above the cut-off opens an alert with its own audit line, and a new *Deux périmètres* section explains the sanction / non-sanction split, what each authorises, and that a rule's scope is enforced by the engine rather than by the rule's own code.
- **Guide, Alerts chapter**: a *Quand un criblage en produit beaucoup* section — why thousands of hits on a common name are genuine homonyms rather than a tuning fault, the two levers available on the non-sanction perimeter, and the volumetry notification.
- **ALERTES_ET_SURVEILLANCE_CONTINUE**: same additions, plus a name collision resolved — "périmètre" meant two different things (the sanction split, and the `screening_lists` restriction). They are now two distinctly titled sections. The frozen list of fifteen `WATCHLIST_*` types is replaced by a pointer to the registry that derives it, which now holds more than forty.
- **REGLES_ET_BLOCKING**: the rule context table gains `perimeter`, `hits_count`, `hit_rank` and `corroboration`; the blocking components go from the three documented to the thirteen that exist, with the wildcard mechanism and the cap explained; the screen's location is corrected.
- **ALGORITHMES_DU_MOTEUR**: the section stating that metric weights "stay in `config.yaml`" and that offering them as switches "would be a trap" was **wrong** — they have been hot-settable with an impact simulation for some time. Rewritten to say what they are: a recalibration, not a switch, because the weights are not normalized. `token_set` is documented there with the reason its default is zero.

### Changed — the documentation folder is navigable
`Documentation/` held fifteen reference documents, two stale working notes describing an interface that no longer exists, a 46 KB prototype HTTP server, a Faker script, and no index. Nothing said which was which.

- **`Documentation/README.md`**: an index classed **by question** — "je veux comprendre comment Fiskr décide", "je veux exploiter la plateforme" — where each document carries its **nature**: *Référence* (kept current), *Parcours* (read in order), *Relevé* (dated measurement, true at its date), *Étude* (settled question). Knowing that `VERIFICATION_DES_SOURCES` is a dated snapshot rather than a living reference changes how it is read.
- **`Documentation/archives/`**: the two working notes and the prototype server, with a README saying what each one is and why it is no longer a reference.
- The Faker script moves to `tools/`, where the other operational tools live, with a real page instead of a seven-line stub.
- `Document Architecture Technique.md` (spaces in the filename, so `%20` in every link) becomes `ARCHITECTURE_TECHNIQUE.md`; the DAT annex follows.
- **README**: a summary table with **explicit anchors** — the headings carry emoji, whose rendering as anchors varies between Markdown engines, so a hand-placed anchor works everywhere. The opening paragraph is corrected: it announced Dow Jones and World-Check among the connected sources (they are not, and `SOURCES_PREMIUM` says so) and presented the optional Spark batch as a core feature. The documentation section stops duplicating the index and points to it. One `file:///e:/Program Files/git/Fiskr/...` link is removed.

Eight tests hold it together: every internal link resolves, every document is reachable from the index, the README's anchors exist, the archives declare themselves, and no executable code sits in the documentation folder. A documentation that points at a renamed file is worse than no documentation — it looks reliable.


### Added — two screening perimeters, because the two risks are not the same
Keeping every hit is a regulatory requirement; the volumetry is its consequence. But the two are not one problem — they are two, and they call for opposite treatment:

**SANCTION** — designations carrying a freezing obligation (OFAC, EU, UN, DGT, OFSI, national counter-terrorism lists). A missed match is a missed terrorist or sanctioned party: observable at audit, financially sanctionable. Everything is generated, nothing is closed by volumetry.

**HORS_SANCTION** — PEPs, regulator alert lists, multilateral debarment. Vigilance signals, not freezing obligations. This perimeter takes a more aggressive closure.

What makes the split worth having, measured on production: **709 511 records out of 895 157 (79 %)** sit on the non-sanction side, almost all of them `WATCHLIST_PEP`. That is exactly where common-name homonymy explodes — and therefore exactly where the cut-off can rise, so the match is never created rather than having to be closed afterwards.

The classification derives from the `family` already declared in the source registry, so a source added there is classified without touching anything. The fifteen list types predating that registry are classified one by one, with the reason. **An unknown type falls on the SANCTION side** — the side that closes nothing: a list wrongly placed on the non-sanction side would have its matches closed in volume, so the default leans toward what loses nothing. Being a compliance call, the whole map is overridable through `screening.perimeters`.

Two levers per perimeter:

- **A cut-off per perimeter**, between the per-list override and the global threshold (`scoring.cut_off_by_perimeter`). The cut is *natural*: below it the match is never created, so there is nothing to close. **Empty by default** — introducing perimeters moves no score, and therefore no already-approved test book.
- **A declared scope on each rule** (`FpRule.perimeters`, nullable, `NULL` = every perimeter, so existing rules behave exactly as before). The **engine** filters on it, not the rule's code: a non-sanction rule cannot close a freezing-obligation match even if its code forgets to check — and a controller reads the scope on the rule instead of hunting through its code. A broken declaration applies **nowhere** rather than everywhere.

The three volumetric templates now declare `HORS_SANCTION`. A fourth, `SANCTION`-scoped, ships deliberately inert: it closes nothing, and exists as the starting point for a rule targeting one identified family of false positives on that perimeter, never a sort by count.

`GET /api/screening/perimeters` serves the classification, the effective cut-off per perimeter and where it comes from. Screening responses carry `hits.by_perimeter`, so a gap between what was found and what stays open is visible on the side where a shortfall is observable at audit.


### Fixed — a screening kept one hit out of 2 976
The engine persisted only the **best** match. Measured on production through `GET /api/screen/preview` (read-only, same engine):

| Profile screened | Candidates | Hits ≥ cut-off | Traces written |
|---|---:|---:|---:|
| Mohammed Ali, no country | 17 649 | 2 976 | **1** |
| Ivan Ivanov, no country | 28 940 | 538 | **1** |
| Ivan Ivanov + RU | 1 223 | 453 | **1** |

And the twelve best for "Mohammed Ali" all sit at **100.00** — "ALI MUHAMMED", "MOHAMMAD ALI", real homonyms, not scoring noise. 2 975 regulatory hits vanished without a written trace, on all four channels: single screening, batch, post-delta re-screening and transaction filtering.

Every match at or above the cut-off is now written: one audit line, one alert, each alert pointing at **its own** audit line. Those a false-positive rule decides are **created and then closed** `CLOSED_BY_RULE`, with the rule's name and version in the alert's decision comment and in its event — never suppressed silently. The whitelist keeps its own path: logged `WHITELISTED`, no alert.

`best_match`, `audit_trail_id` and `alert_id` still designate the top match — the response contract is unchanged, and `hits` now reports what was written (`hits`, `opened`, `closed_by_rule`, `redetected`, `whitelisted`).

### Fixed — writing N hits must not mean reading N times
Turning one hit into thousands exposed four read amplifications, all now measured by a test that compares the **read** count between 11 and 12 hits (writes obviously scale — that is the feature):

- the whitelist was queried per hit → one batched `whitelisted_pairs`;
- the active rules were reloaded per hit → loaded once per screening;
- the SLA setting was read per alert → read once per batch;
- **and the subtle one**: `commit()` expires the session's objects, so re-reading `ligne.id` afterwards fired one `SELECT` per row. The N+1 came back through the back door, after the writes had been grouped. The audit lines are now flushed, ids read, and a single commit covers audit and alerts together — atomically.

Rule code was also being recompiled on every evaluation; it is now memoised on the rule text.

**A bug this branch introduced and its own test caught**: the commit went through alert creation, so a screening with nothing to alert — everything whitelisted, or no hit at all — left its audit lines written but never committed, and therefore lost. Two tests now read the journal back **from a separate session**, which is what a controller does.

### Added — what a rule can now see, and three templates that use it
No string metric separates "MOHAMMED ALI" from "MOHAMMED ALI". What is missing is not precision, it is **identification** — date of birth, country, identity document. The rule context now carries that distinction explicitly:

- `hits_count` and `hit_rank`: the volumetry of the screening that produced this hit, and its rank by descending score;
- `corroboration`: `has_dob`, `has_country`, `has_identity_document`, `name_only`, `corroborated`, plus the three adjustment scores.

`GET /api/fprules/templates` serves three ready-to-install rules built on it — name-only in volume, no corroboration beyond the top hits, and the filtering equivalent where a payment party rarely carries a date of birth. **None is active by default**: these are compliance trade-offs, not comfort settings, so each carries a `loss` field saying plainly what it costs, and a test asserts none of them ever closes a hard match — an identical official identifier is an identification, not a homonymy.

### Changed — a burst of alerts no longer means a burst of notifications
One screening can now open thousands of alerts at once. Individual `alert_created` notifications stop at ten and give way to a single `alert_volume` event carrying the count and the top score. Alerts already closed by a rule no longer get a re-detection event on every pass either: post-delta re-screening replays the whole client base after each list goes live, and one event row per rule-closed alert per pass would grow the journal without teaching anything — the audit line for each screening is still written every time.


### Fixed — a screening returned every candidate it had scored
`POST /api/screen` returned `all_matches`: **every** scored candidate, each carrying its full listed record. Measured on production through `GET /api/screen/preview` (read-only, same engine), the scope a screening actually covers:

| Profile screened | Candidates | Alerts | Response time |
|---|---:|---:|---:|
| Ivan Ivanov + RU | 1 223 | 453 | 1.9 s |
| **Ivan Ivanov, no country** | **28 940** | 538 | **24.2 s** |
| Mohammed Ali, no country | 17 649 | 2 976 | — |
| Li Wei, no country | 15 520 | 268 | — |
| Zzyxwv Qqrstuv, no country | 2 894 | 0 | 3.3 s |
| Bank of Example (E) | 385 | 14 | 1.2 s |

Without a country the profile falls into the "unknown country" block, which gathers every listed record whose source publishes no geography. At ~1.8 KB per record, `all_matches` for "Mohammed Ali" carried some **30 MB** — and that many objects retained in memory — on a field **nobody read**: not a screen, not a test.

The response now keeps the 50 best, held in a bounded heap. What matters for compliance is untouched: `best_match`, the audit trail and the alert are still computed over **all** candidates, and `candidates_count` says how many were actually compared. A test rebuilds the expected top 50 candidate by candidate, outside the endpoint, and asserts the heap returns exactly that list in exactly that order — ties included, since strict comparison preserves what the previous stable sort did.

**Measured and reverted — an early exit in the scoring loop.** Once a name pair reaches the maximum attainable score, no other pair can beat it, so the remaining aliases could be skipped. Provably safe, three lines. Measured on a record with eight aliases: 167 µs versus 173 µs per candidate — inside the noise, because `c_names` and `w_names` are built through `set()` and the exact match is not reached first. Not worth three lines in the most safety-critical module of the product, so it is not in this branch.

**Reported, not changed — the 24 seconds themselves.** Profiling puts the cost in the string metrics, not in redundant normalization: Damerau-Levenshtein 37 %, Jaro 24 %, token sort 14 %; normalization is under 2 %. Production works out to ~0.83 ms per candidate, against 174 µs on a synthetic corpus — the difference is the aliases, each one another full set of metrics. There is no way to make this materially faster except to compare fewer candidates, and that is the blocking layout: a decision with a recall trade-off, which the settings screen already exposes (up to three fields). The numbers above are what that decision needs.


### Fixed — one call to `GET /api/watchlist` would have taken production down
The endpoint returned `watchlist_store` **whole**. Measured on the real cache: ~1.8 KB per record. With 895 157 records in production, that is **over 1.5 GB** serialized in memory inside the web process, for a single authenticated call — on shared hosting, the application with it. The figure is an estimate derived from a local measurement: calling that endpoint against production would have been triggering the very thing being described.

The front end never used it (`fetchWatchlist` has always gone to `/api/watchlist/db`), which is why it went unnoticed — but it is a documented endpoint and the tests reached for it.

The response is now bounded, and it **says so**: `total` stays exact and `truncated` reports the cut, so the number the check relies on is never wrong — only the sample is. `entity_id` returns one record's cached entry, which is what this check actually asks most of the time, without depending on its position in the cache. Browsing the reference data was never this endpoint's job: `GET /api/watchlist/db` is paginated, filterable, and read from the database rather than from one process's memory.

Two existing tests depended on finding their record inside the full dump. They pass unchanged today only because the dev cache holds fewer than a hundred entries — a silent trap. One now queries by `entity_id`; the other raises `limit` and **asserts `truncated` is false**, since concluding "this name is absent" from a sample would be worth nothing.

### Changed — three round trips per page load, for nothing
Pages reference their assets by the **fingerprint of their content** (`app.js?v=<hash>`): the URL changes when the file changes, and never otherwise. But no response carried a cache header, so the browser revalidated all of them on **every** page load. Measured on production:

```
app.js       304, 0 bytes, 1.01 s
i18n.js      304, 0 bytes, 0.66 s
styles.css   304, 0 bytes, 0.70 s
```

**Correction to a first, too-quick reading of my own measurement**: I nearly reported the assets as uncompressed. They are not — the front end serves brotli, and 476 KB of `app.js` arrive as 167 KB. Only the round trips were being paid.

A versioned URL is now `immutable` for a year, so it is not requested again at all. An URL **without** a version keeps revalidating: nothing guarantees a bare URL follows the content, and freezing it for a year would serve a stale file after a deploy. The parameter detection is parsed, not substring-matched — `version=`, `nv=`, `v=` and `v=%20` all correctly count as unversioned.

The HTML page cannot be cached for long: it is the thing carrying the asset version, and served stale it would reference the old assets — exactly the fault the fingerprint was built to fix. It now carries `no-cache` plus an ETag: still requested on every load, but answered with an empty 304 while nothing has moved. Its body does not depend on the user — only access to it does — so one fingerprint serves everyone.

A test ties the two halves together: the assets can only be declared immutable *because* the page rewrites their version, so it asserts the page really does carry the current fingerprint, and that no asset in the markup is referenced without one.

### Changed — a hidden tab no longer polls the server
A Fiskr tab left open questioned the server forever:

| Poll | Cadence | Requests/hour |
|---|---:|---:|
| `/api/progress/active` | 8 s | 450 |
| `/api/worker/status` | 30 s | 120 |
| `/api/counters` | 60 s | 60 |
| `/api/version` | 5 min | 12 |

~640 requests per hour **per tab**, for a screen nobody is looking at — on shared hosting where a request costs ~0.15 s of server time, close to 100 s of server per hour per forgotten tab.

Hidden, the tab stops polling and catches up in one go on return, which makes the figures *fresher* at the moment they are read than a periodic poll that happened to land just before. One exception, and it matters: if an operation **is running**, the cadence is kept even hidden — its completion fires screen callbacks and a toast that must not wait for the user to come back. A test asserts the running branch is taken before the visibility test, not after.

The visibility check is deliberately conservative: without `visibilityState`, it polls as before rather than going silent forever.

### Changed — the snapshot history is paginated (breaking: `GET /api/snapshots` now returns an envelope)
The previous round measured this endpoint and left it alone: the weight was not a field to drop (the row had already lost its backtest report) but the **number of rows**, which grows on its own. Production: **547 snapshots for 282 KB**, one born per source per day across 42 sources, and that response was reloaded after every import, sync, approval and purge.

`GET /api/snapshots` now returns `{total, page, page_size, items}` — 50 rows by default, 500 at most — with server-side `file_type` and `status` filters (comma-separated). This **changes the response shape**, as `/api/history` did before it: a bare list is no longer returned. The screen's list-type filter moved to the server with it, because on a paginated list a client-side filter only ever sees the loaded page.

The comparison drop-downs are the reason this could not be a plain paginate-and-done: comparing two snapshots means picking any pair from the whole history, including an old one. They now have their own source, `GET /api/snapshots/options`, cut to the four columns a drop-down actually displays.

Measured on the production data:

| | |
|---|---:|
| Old response | 283 872 B |
| New — page of 50 | 25 985 B (−90.8 %) |
| New — comparison options | 96 515 B (−66.0 %) |
| **Opening the screen** | **122 500 B (−56.8 %)** |

Then the options became the heavier half, so they are cached client-side: reloaded only when a snapshot is created, approved, rejected or purged — not on every return to the tab. Page turns move 26 KB and nothing else.

**Not done, and why**: denormalizing `backtest_verdict` / `backtest_gap_pct` into real columns, so the serializer stops reading `backtest_report` for two scalars. Pagination already divides that read by eleven (50 rows instead of 547), which leaves a schema change, a backfill and two write sites to save the measured ~18 ms of the previous round.

### Fixed — two tests that were passing for the wrong reason
Widening the front-end call detection to `nom(` instead of `nom()` — needed once pagination gave `fetchSnapshots` an argument — revealed that the previous round's regex matched **nothing** at all after the change, so every test in that file would have passed vacuously. The file now has a guard test asserting the detection finds something, and the allow-list of callers permitted to reload without the visibility guard is explicit: navigation, plus the screen's own controls (sort, filter, pagination, record edit), which the narrow regex had been silently skipping. The guarded code was correct; the test covering it was narrower than it claimed.

The page-load test asserted each loader appears literally in the tab-routing block. Reaching `fetchSnapshots` through a small wrapper is legitimate, so the test now expands one level of `rafraichir*` wrappers instead of matching the bare name — the property checked ("opening the tab loads the screen") is unchanged.


### Fixed — the last N+1 in the application
`GET /api/kpi` computed the average decision time **one query per analyst**, each pulling that analyst's 200 most recent decisions. The cost grew with the team, on a landing screen (~0.67 s of server work on production).

A window function numbers each analyst's decisions from newest to oldest and **one** query returns everyone's first 200. The date subtraction stays in Python — it is not portable in SQL (PostgreSQL `interval` versus SQLite text dates) and the figure has to stay identical.

Two things the regrouping had to preserve, both easy to break and both locked by test:

- the decision **count** filters on closed statuses, the **delay** does not — it takes any alert carrying a decision date. An alert decided then reopened counts in the delay and not in the count;
- the 200 bound is **per analyst**, not global. The fixture makes the two wrong ways fail differently: 250 decisions at 1 h and 50 at 100 h for one analyst — the correct bound gives 1.0, no bound gives 17.5, reversed ordering gives 100.

A query-counting test asserts the cost no longer moves between 3 and 13 analysts.

An AST sweep over the endpoint layer and the rest of the package found no other query inside a loop: what remains is a bounded BFS (relationship graph, depth ≤ 3), chunked bulk reads (re-screening) and a fixed loop over four retention families.

### Changed — background refreshes only reload what is on screen
A sync, an import, an approval or a purge reloaded the snapshot history (**282 KB, 547 rows** on production) *and* the paginated watchlist, whether or not the user was looking at those tables. Doubly pointless: `switchTab` and `switchSubTab` already reload both views on entering their tab, so fresh data is guaranteed on arrival — the background refresh only paid the weight for nobody.

Both are now behind a visibility guard, and one duplicate call is gone: after a manual entity add the code called `fetchWatchlist()` and then `switchSubTab('watchlist-mgmt', 'watchlist-active')`, which reloads it — the same request twice.

The tests read the front end from both sides, because getting it wrong one way breaks navigation and the other way cancels the gain: navigation paths must reload unconditionally, background paths must all go through the guard.

### Measured and left alone — `/api/snapshots`
The list serializer reads `backtest_report` on every row to extract two scalars (`verdict`, `gap_pct`), so PostgreSQL detoasts the whole document 547 times for two numbers — the same shape of waste as the watchlist row. Denormalizing the two scalars into real columns would fix it, at the price of a migration, a backfill and two write sites.

Measured before doing it, at the real production volume (547 snapshots, ~2.6 MB of reports over the ~60 that carry one): **20.3 ms versus 2.1 ms** (×9.7). Roughly 18 ms out of ~1050 ms of server time. The ratio looks impressive and the absolute figure does not justify a schema change — so it was not made. (A first run at 66 KB per report gave ×55; that fixture was 13× heavier than production and its ratio meant nothing.)

What actually weighs on that endpoint is the 547 rows themselves, growing daily across 42 sources. The honest fix is pagination with server-side filters, which is a screen change — the comparison drop-downs need the whole list and the table filters client-side today.

### Fixed — the "Excluded" scope took 21 to 35 seconds to return nothing
`ix_wl_entities_production` is a **partial** index on `excluded IS NOT TRUE`. A partial index only serves queries whose clause its own implies, so `WHERE excluded IS TRUE` was covered by nothing and scanned all 11.2 M rows — to return zero rows, since nothing is excluded today. Measured on production: **21 to 35 s**.

The symmetric `ix_wl_entities_excluded` (same columns, `WHERE excluded IS TRUE`) closes the gap. It indexes only the records actually excluded — a handful, where the other one indexes hundreds of thousands. Like the rest, it is deferred on a large table and created by `tools/create_perf_indexes.py` (now 15 statements).

### Changed — the watchlist screen stopped recounting its universe on every page turn
Measured on production by varying the page size, not by guessing:

| `page_size` | 1 | 10 | 50 | 200 |
|---|---:|---:|---:|---:|
| Response | 4.00 s | 3.73 s | 3.84 s | 4.13 s |

**The cost does not depend on the page** — 2 ms per row beyond the first. What remains is the `COUNT` over the scope: 895 157 records in production, recomputed on every page turn.

That count is now memoised under a **signature of production**: the watchlist epoch (moved by everything that changes the screened universe) plus a direct reading of the `READY` snapshots — count, latest upload, sum of record counts. That reading covers 42 rows and earns its place: approval commits the flip to `READY` and then **defers** the cache reload — and therefore the epoch bump — to a background job. On the epoch alone the count would have lagged by one approval until that job ran. The reading catches the flip at commit time. A test asserts exactly that, and another counts the SQL: three page turns now issue **one** `COUNT` instead of three.

Only the `production` scope is memoised. Exclusions are placed and removed on snapshots **awaiting approval**, with no epoch bump, so `EXCLUDED` and `PENDING_REVIEW` keep counting on every call. It is also why the signature does not count excluded records — that is precisely the query that took 21 to 35 s.

Records themselves are never memoised, only the total.

### Changed — the "Active hash" badge stopped recounting on every page open
`GET /api/watchlist/summary` feeds the badge loaded on **every page open**, and its record count is the same `COUNT` over the same 895 157 production records — ~1.3 s of server work, every time. It shares the memoised total now: the badge and the paginated browse count exactly the same universe, so they share one number. A test asserts that equality in both orders, because whichever runs first would otherwise impose its figure on the other.

### Changed — the sidebar badges read the alert table once instead of five times
`GET /api/counters` issued **six** queries — five `COUNT`s on `alerts`, all over the same scope, plus one on `snapshots`. The sidebar polls this endpoint, so every round trip saved is saved on every refresh of every open tab. The five are now one pass of conditional aggregates.

A regrouping is only worth it if it returns exactly the same thing: the central test recomputes each counter row by row in Python, over a fixture built to cover every branch where the two implementations could diverge — null channel, filtering channel, overdue, no due date, closed alert, pending validation — with a guard test asserting no counter is zero, since an all-zero fixture would pass without comparing anything.

### Changed — the list row no longer hydrates the whole record
The browse query loaded 70-column ORM entities to serve 16, forcing PostgreSQL to detoast, row by row, the JSON blocks (aliases, designation reasons, addresses, documents) that serialization then threw away. It now asks only for the columns it renders — the lesson the fuzzy scan had already learned (25 000 full ORM records cost ~2.5 s per chunk; light tuples, under half a second).

Three of the sortable columns (`origin`, `country`, `official_reference`) are **not** part of the served row, so sorting on them means ordering by a column that is not selected. A test sweeps all eight sortable columns in both directions.

### Changed — the two list payloads left over from the previous round
The last payload pass measured two responses and deliberately left them alone: both fed a detail panel that reuses the **already-loaded row**, so lightening the list would have broken the detail. They now have a detail endpoint, so the lists can drop their heavy field. Measured on production:

| | Before | After | |
|---|---|---|---|
| `GET /api/sync/reports?limit=25` | 537 KB | 11.6 KB | **−97.8 %** |
| `GET /api/history?page_size=25` | 123 KB | 8.4 KB | **−93.2 %** |

The screens request 50 rows, so the real saving is roughly twice that on each.

Both keep serving everything **by default**. `include_details=false` is opt-in and the two screens pass it; the audit journal and the sync report archive are evidence a controller can ask for, and an integration that archives them must keep receiving whole records. Detail is read one record at a time through `GET /api/sync/reports/{id}` and `GET /api/history/{id}`.

Two things the lightening had to carry over, both locked by test:

- The sync table shows a **partial-failure badge** (EUR-Lex acts or PDFs the source could not deliver), counted from inside `delta_report`. Without the delta it would have counted zero in silence, so the row now carries the derived `partial_failures`.
- The audit journal derives `list_type` from the `decision_tree` for records written before the column existed. On production **24 of the 25 rows on the first page** depend on that fallback — dropping the tree without keeping the server-side derivation would have turned the whole "Liste" column into "Inconnue". The derivation stays server-side; only the tree stops travelling.

### Changed — one list-replacement cycle instead of three
`run_ofac_sync` (138 lines) and `run_dgt_sync` (124 lines) each re-implemented by hand the cycle already written in `_run_list_replacement_sync` — download, hash dedupe, ingestion, delta against the live list, then supersede or wait for approval. Measured: **98 of 138 lines identical between OFAC and DGT (71 %)**, 90 and 91 lines identical with the generic runner. Three copies of the most safety-critical path in the application, so three places to fix for every correction — and they had already drifted apart.

One difference was real and is why the copies survived: OFAC also refreshes the ownership graph (`ProfileRelationships`) at the same time as the list. It now goes through an optional `after_persist` hook on the generic runner, which runs in the same transaction as the records — so the graph can never be out of step with the list that was just ingested. **−201 lines**, and the ownership refresh gains the test coverage it never had.

What the two sources inherit: the snapshot's persisted progress is now closed properly (`processed_count` finalised, `phase` moving to `DELTA` then `DONE` instead of staying stuck on `PERSIST`), and the conditional-download path.

**Measured before claiming it as a gain, and it is not one today**: neither publisher honours conditional requests. OFAC advertises a `Last-Modified` but ignores `If-Modified-Since` — a conditional request with the exact advertised date returns 200 and transfers 126 MB. DGT sends neither `Last-Modified` nor `ETag` (12 MB). So the path is inert on both; it costs nothing and starts paying the day either publisher fixes their side.

Two user-visible wordings are harmonised with the other 40 sources: `"… fiches importees depuis la source OFAC."` replaces `"… depuis le fichier OFAC officiel."`, and the DGT no-change message says `"Le fichier DGT est identique …"` instead of `"Le registre DGT …"`.

### Removed — the old daily scheduler, dead and dangerous to revive
`api.py` still carried `_run_scheduled_syncs()`, replaced long ago by `_cron_sync_tick` (per-source cron scheduling). Nothing referenced it — not the code, not the templates, not the front end — but it was not harmless:

- it listed **15 sources by hand** where the `_SYNC_RUNNERS` registry now holds **42**; every source added since was silently outside it;
- it reloaded the cache with `load_watchlist_cache(db)`, the process-local form corrected everywhere else to `_refresh_production_cache` (the only cross-process invalidation channel). Revived as written, it would have put a list into production that no other process could see — the exact compliance bug fixed two rounds ago, reintroduced.

Deleted (52 lines), with a test locking its absence and the invariant it broke: the registry covers **every** configurable source, and every runner has an API alias — no hand-written list anywhere.

### Added — no pivot key falls into the void
Each source parser yields a "pivot schema" dict that `build_watchlist_entity` picks up key by key with `item.get(...)`. A misspelt key raises nothing: the field is simply missing from the stored record, and therefore from screening. It has happened — the builder still carries two read fallbacks (`adress`, `additional_info`) that are the scar tissue of past typos on the CSV import side.

The duplication detector flags these three construction blocks as near-identical, but they are **schema declarations**: every value comes from source-local variables, and folding them behind a thirty-argument constructor would remove no line while destroying the one thing that makes them readable — seeing each field next to where it comes from. The real risk is the key that leads nowhere, so that is what the new test locks, statically and across all parsers at once.

### Fixed — a test double that had drifted from the function it doubles
`test_rescreen.py` stubbed `download_to_file` with a hand-copied signature, and its own comment said the double must mirror the real one. It no longer did: the real function had gained `validators` and `headers`. The stub only survived because DGT never took the code path that passes them. The double now **verifies** the signature (`inspect.signature(...).bind(...)`) instead of copying it, so the next drift fails on the mismatch rather than on an unrelated `TypeError`.


### Fixed — client aliases were screened by the engine, but no door let them in
The question was whether screening uses aliases. Answer, measured rather than assumed:

| | Blocking | Scoring |
|---|---|---|
| Alias of the **listed** record | yes | yes |
| Alias of the **client** | **no** | yes |

So the case first asked about — a listed record whose *alias* is "Vladimir Putin" against a client named "Vladimir Putin" — already worked end to end, and still scores 90/ALERT. The missing half was the other direction: `fiskr/scoring.py` reads `client["aliases"]`, but **no entry path could carry them** — no column on `ClientEntity`, no field on the screening request, no column recognised at import. The branch was dead code.

`client_aliases` now exists and crosses every door: CSV import of the client referential (separated by `;` or `,`), batch campaign, direct API call, webhook, and re-screening — which picks it up for free since it copies every column.

**The part that mattered more than the field itself**: an alias now produces a **blocking key**. Without it the pair is never a candidate, so the scoring — which would have handled it — never sees it. An alias accepted into the database and silently ignored at screening time would be worse than refusing it. Blocking and scoring are commanded by the same capability, so cutting it cuts both and the index stays consistent with the probe; a test asserts exactly that.

Aliases on the listed side remain **high-priority only**, unchanged: weak aliases are the classic source of false positives, and that arbitration is not modified here.


### Added — token-set similarity, for the case the engine could not see
`token_sort` fixes token **order**, not **inclusion**. A sanctions list carries long names — Russian patronymics, Spanish double surnames, Arabic filiation — where the client referential keeps only part. Measured on the engine, with **no contextual data** (no date of birth, no gender, no country — the common shape of a listed record, where the decision then rests on the name alone):

```
Vladimir Putin      vs Vladimir Vladimirovich Putin      60.6
Igor Sechin         vs Sechin Igor Ivanovich             63.2
Maria Carmen Lopez  vs Maria del Carmen Lopez Hernandez  63.9
```

All under the 75 cut-off: the same person did not raise an alert. **Correction to a first reading of my own measurements**: with matching date of birth and nationality, the contextual adjustments rescue every one of those pairs (92 to 99) — the weakness is real but bounded to records without context, which is precisely where a list is weakest.

`token_set_similarity` compares the token intersection against both full sets and keeps the best of the three, so a name entirely contained in the other scores 100. Sweeping its weight over 15 pairs (8 same person, 7 different people):

| Weight | Same person found | Different people alerted |
|---:|---:|---:|
| 0.0 | 1/8 | 4/7 |
| 0.2 | 4/8 | 4/7 |
| **0.4** | **7/8** | **4/7** |

Detection goes from 1/8 to 7/8 **with no added noise on this set**. The set is mine, not production data: the real answer comes from running the backtest against the 500-client panel and the live referential, which is what it exists for.

**The default weight is zero.** Adding a metric to the score would shift every score at once — therefore the calibrated thresholds, the anti-false-positive rules and the backtests already approved. A compliance engine does not change behaviour on the occasion of an update; the operator enables it, measures the gap with a backtest, then decides. A test asserts the neutrality of the default.

### Added — every field can now be part of the blocking key
Blocking offered three components (country, entity type, name phonetics). All fields are now available: year of birth, gender, place of birth, city, tax id, LEI, BIC, IBAN, IMO, national registry — thirteen components in total, derived from one registry so that adding a field there is enough to offer it in the settings screen.

Two properties make the opening safe rather than merely richer, and both are pinned by tests:

- **Symmetry.** A component must compute on **both** sides — client profile and listed record. One that computed on a single side would produce a key that is never met: the listed party would become unreachable, with no error anywhere. Each registry entry therefore carries both extractors, and a test asserts that every field extracts on both sides and reads the same value for the same person. Identifiers ignore formatting (`FR 123 456 789` and `FR123456789` are one key), and partial dates still yield a year — lists publish `1960`, `1960-00-00`, `circa 1960`.
- **Wildcards.** A listed record that does not fill the field carries a wildcard, and the probe queries the wildcarded variants too. Without this, adding "year of birth" would lose every record without a date — that is, most of the official lists. Tests cover one missing field and several missing at once.

Because each added field **doubles** the number of probes, the layout accepts at most three of them. The cap is explicit rather than suffered.


### Performance — the list screen shipped 2.6 KB per row to display a handful of columns
Measured on production: a page of 100 listed records weighed **255 KB**, i.e. 2 615 bytes each, while the table shows about a dozen columns. The bulk of it — aliases, designation reasons, addresses, identity documents — appears only in the details modal, which is opened on **one** record at a time.

The list now serves only the displayed columns (**392 bytes per record, −85%**) and the modal loads the full record when it is opened, through a new `GET /api/watchlist/db/{id}`. This is the screen used most, and it is paginated, so the saving applies to every page turn.

Two details that make it safe rather than merely smaller:

- **The table must keep everything it reads.** A missing field would render an empty column with no error — the worst kind of failure. A test derives the list of fields the table reads *from the frontend source* and asserts each one is served; adding a column to the table without adding it to the row now fails there.
- **A record that is already complete is not refetched.** The Ctrl+K palette and the post-edit refresh hand over full records; the modal only fetches when `aliases` is absent, which is exactly what distinguishes a light row.

A routing trap was paid once and is now pinned: declared as `/api/watchlist/db/{id}`, the detail route captured `/api/watchlist/db/fuzzy` — FastAPI read "fuzzy" as an identifier and the fuzzy scan answered 422. The path is explicit (`/entity/{id}`) rather than relying on declaration order, which would have re-set the same trap for the next literal sub-path. Four existing tests caught it, and a fifth now guards it directly.

Provenance fields (`snapshot_id`, `snapshot_uploaded_at`, `snapshot_file_name`, list type, status) stay on the row: no screen reads them, but they are part of this public endpoint's contract and cost about sixty bytes. Removing them would have saved little and broken a consumer not visible from here — it is the per-entity JSON blocks that weigh, and only those moved to the detail.


### Performance — page load went from 670 KB to 7.4 KB
Startup eagerly loaded **six screens the user is not looking at** — and the application **re-downloads every one of them** when its tab is opened. The spend bought nothing.

Measured against production:

| Preloaded screen | Weight |
|---|---:|
| Listed records (`/api/watchlist/db`) | 247 KB |
| Snapshots | 267 KB |
| Audit trail | 132 KB |
| Pending approvals | 11 KB |
| Alerts + whitelist | 4 KB |
| **Total invisible** | **661 KB of 670 KB** |

Startup now loads only what the home screen shows plus the sidebar's own state: home dashboard, active-hash badge, configuration, ingestion settings, counters. **7.4 KB — 98.9% less.**

The removal is only legitimate because the other half holds: every screen still loads when its tab opens. `switchSubTab` covers the sub-tabs, `switchTab` covers entering a section, and a deep link goes through **both** (`applyHashRoute` calls them in turn), so `#watchlist-mgmt/watchlist-review` still arrives populated. The approval badge was checked separately: it comes from the lightweight counters endpoint, not from the approval screen, which is what makes dropping that preload safe.

Both halves are pinned by tests, per screen: none of the six is called at page load, and each one *is* reachable from the tab routing. A future preload — or a screen removed from the routing — fails there.


### Fixed — a manually added listed person was invisible to the worker daemon
Found while hunting for optimisations, and more serious than what was being looked for.

The engine cache lives in each process's memory. The **epoch** — one integer in the database — is the only invalidation channel between processes: the worker daemon cannot touch a web process's memory, it can only notice a number that changed.

Four endpoints modified the production referential while reloading **only their own** cache: manual addition of a listed entity, batch addition, correction of a record, and retention purge. In production, where Passenger runs several web processes and the daemon carries batch campaigns and re-screening:

> a listed person entered by hand was **not screened against by the daemon's campaigns**, until some synchronisation happened to bump the epoch.

That is a screening miss, not a display lag. All four now go through `_refresh_production_cache`, which bumps the epoch and reloads locally — exactly what the ingestion path already did.

Five settings routes had the same shape (blocking layout, engine capabilities, linguistic resources, learned equivalences, ingestion settings). Their own comments say the index must be rebuilt for the setting to take effect — but they rebuilt only the local one, so two web processes could screen with different engine parameters. They now share the same channel. The reasoning is uniform: **a route that reloads the cache has already decided the cache is stale, and in a multi-process deployment that decision has to be shared.**

Pinned by tests on each path, plus a syntax-tree guard: no writing route may call `load_watchlist_cache` without also going through the shared channel. That guard is what found the five settings routes.

### Measured, and deliberately not changed
Two investigations that produced negative results, recorded so they are not redone:

- **The write path is sound.** Ingestion looked like one SQL statement per record — until the same measurement on PostgreSQL showed **37 queries for 20 records as for 200**. SQLAlchemy batches inserts properly there; the per-record behaviour was a SQLite artefact of the test environment, not a production problem.
- **The list-browsing indexes work.** A local PostgreSQL 16 at production proportions (780 k rows, 92% out of production) first showed a `Parallel Seq Scan` on the production-scope count — but that was an unvacuumed bulk load. After `VACUUM ANALYZE` the plan is an **Index Only Scan with zero heap fetches**, 54 ms to count 270 k live rows. The partial index does its job.


### Performance — the approval screen cost three queries per pending list
Found by counting SQL statements per endpoint at two data sizes — deterministic, unlike a stopwatch, and load-independent. Across nine read endpoints, exactly one grew with the data:

```
/api/review/pending    1 pending snapshot -> 4 queries
                      11 pending snapshots -> 34 queries      <== N+1
```

Three queries per row: a `COUNT` of excluded records (on `watchlist_entities`, 11 M rows in production), a lookup of the list currently in production, and the stored sync delta. After a synchronisation wave — nineteen lists pending, observed in production — that screen issued about sixty queries to render nineteen rows.

The three are now read in batch: one `GROUP BY` for the excluded counts, one query for the production snapshot of every type involved, one for the stored deltas. **34 queries → 4, and constant** whatever the number of pending lists.

Batching is only worth anything if it returns exactly the same thing, so a test compares the endpoint's output, field by field, against the original row-by-row loop, on the cases where a careless batch would diverge: a stored delta still valid, a stale one that must *not* be served, a list with no production yet, exclusions that must land on the right snapshot. A second test asserts the query count does not grow with the number of rows — a reintroduced loop would show up there and nowhere else.

### Fixed — the index tool ignored twelve of the fifteen indexes it was told to create
Startup **defers** any missing index on a large table (an ordinary `CREATE INDEX` would lock it for minutes) and logs: *"run `python tools/create_perf_indexes.py`"*. But that tool carried its own **hard-coded** list of three indexes, unrelated to the model's declarations. Any index added to `_PERFORMANCE_INDEXES` after the tool was written was therefore **never created on a large production database** — while the log claimed the opposite.

The tool now derives its statements from `_PERFORMANCE_INDEXES` itself, compiling each declared index to PostgreSQL DDL and adding `CONCURRENTLY IF NOT EXISTS`. Three statements became fourteen. Partial indexes keep their `WHERE` clause — without it the production index would carry the 92% of records it exists to avoid, which a test now checks explicitly. Another test asserts every declared index is covered, so the two lists cannot drift apart again.


### Performance — the snapshots list carried 2.57 MB of backtest reports it never showed
Measured against production, and load-independent unlike a timing: `GET /api/snapshots` returned **2.84 MB**, of which **95.1% was `backtest_report`** — 2.57 MB for 518 snapshots, re-downloaded on **every page load**, since the endpoint returned whole ORM objects and FastAPI serialised everything on them.

No screen reads it from that list. The snapshots table uses `uploaded_at`, `file_name`, `file_hash`, `file_type`, `status`, `record_count`, `processed_count` and `phase`; the three places in the frontend that render a report fetch it from a detail endpoint. The list now serves an explicit set of columns plus `backtest_verdict` and `backtest_gap_pct`, which is all the status badge needs — under 150 KB, with **no frontend change**. Unknown columns are skipped rather than raising, so a column added later cannot break the list.

Two other payloads were measured and deliberately left alone for now: `/api/sync/reports?limit=25` (581 KB, 98.9% `delta_report`) and `/api/history?limit=25` (138 KB, 97% `config_state` + `decision_tree`). Both feed detail panels that reuse the **already-loaded row** instead of refetching, so lightening the list would break the detail — they need a detail endpoint and a screen change, which is a different kind of work.

A note on method: a first round of timings suggested `/api/counters` at 5.9 s and `/api/watchlist/summary` at 10.4 s. A second round minutes later gave 1.13 s and 1.63 s for the same endpoints. Those numbers were noise, and nothing here rests on them — response *sizes*, unlike timings, do not depend on server load.

### Fixed — a consolidated backtest now says which list caused the gap
Consolidating the backtest (previous release) had a consequence that was not anticipated: the verdict became a single figure for the whole wave. Observed on the first production run — verdict WARN, gap 13.69%, **nineteen lists covered** — with nothing to say which list was responsible. One backtest per list attributed it implicitly; consolidation lost that.

Every pair (client × listed entity) already carries the list type of the entity that triggered it, so attribution is **exact, never an estimate**. Each covered list now reports its own `new_pairs_count` and `resolved_pairs_count` alongside its delta sizes, and both review screens show the breakdown when more than one list is covered.

Movements attributable to no tested list — a candidate anti-false-positive rule can suppress pairs on lists that are not part of the backtest — are reported separately as `unattributed_pairs` rather than being silently dropped. A test asserts that the attributions plus that remainder sum back exactly to the report's global counts: an attribution that does not add up would be lying by omission.


### Added — approval history: reopen a decision months later
Approvals left no reviewable trace. The backtest report lives on the snapshot, but its *context* does not survive: the delta of a candidate list reads "against production", and approving the list **makes it production**. After the fact, recomputing would compare the snapshot to itself and return an empty delta — precisely the information worth keeping. One more synchronisation and the baseline is gone for good.

Every decision — approval **and** rejection — now creates a `ReviewRecord` frozen at the moment it is taken: the delta against the list then in production (summary and bounded details), the backtest report with its verdict, the exclusions applied, the compared-against baseline, the reviewer and their comment. These records are never rewritten, on the same principle as the screening audit trail.

The ordering is the whole point and is pinned by a test: the record is captured **before** the status flips. Captured after, it would freeze an empty delta and the feature would silently record nothing.

`GET /api/review/history` (paginated, filters on decision and list type applied **server-side** — a browser-side filter would only see the displayed page, whereas an audit search covers the whole history) and `GET /api/review/history/{id}` for the full record. A new *Historique* tab under Watchlists lists the decisions and reopens any of them in place, showing delta and backtest as they stood, alongside the snapshot's *current* status — a list approved back then may well have been superseded since.

### Fixed — the 50% rule ignored aggregate ownership
`compute_inherited_risk` tested `ownership_pct >= 50` **on a single edge**. Verified by running it rather than by reading it:

```
25 % + 25 % aggregated  ->  NO RISK DETECTED
60 % from one owner     ->  detected
```

That is precisely the case both regulators target. OFAC: *"if Blocked Person X owns 25 percent of Entity A, and Blocked Person Y owns another 25 percent of Entity A, Entity A is considered to be blocked"* — and holdings aggregate even across different sanctions programmes. The EU's updated best practices (2024): *"Ownership interests of EU-designated persons in an entity should be aggregated to determine whether such entity is owned 50% or more by EU-designated persons."* In practice, a company held 30% by one designated person and 25% by another is frozen in law, and Fiskr said nothing.

Holdings are now aggregated per target entity before the threshold is applied, at every level of the chain. Each retained owner carries *why* it was retained (`via_aggregation`, `aggregated_pct`, `aggregated_owners`) so a reviewer can redo the arithmetic.

Three guards matter as much as the feature, and all are pinned by tests:
- **Only designated holders aggregate.** Summing a listed person's stake with an ordinary shareholder's would manufacture an imaginary freeze — a listed person at 30% plus a bank at 30% is not a freeze. Only holders matching a production record count.
- **Duplicate edges for one owner do not add up.** Two rows for the same holder (two sources) would fabricate 50% out of 30% real.
- **Aggregation only settles undecided cases.** If a holder already crosses the threshold alone, the question is answered; adding its minority co-shareholders would drown the signal without changing the decision. This keeps the fix strictly *additive* — no previously detected case changes, new ones are detected. The first draft did not have this restriction, and three unrelated tests caught it by failing: the extra chains propagated into screening decision trees.

### Changed — one backtest for a whole sync wave, instead of one per source
"Synchronise enabled sources" dropped one snapshot per source, hence one backtest per source. Backtests are serialised (`SERIAL_KINDS`), so the queue stretched by tens of minutes — observed in production: six backtests waiting, the oldest for 57 minutes — for largely redundant work, each one re-screening the **same shared universe**.

A single consolidated backtest now covers every pending delta. `run_backtest` accepts several candidate snapshots and mirrors what approving all of them would produce: each tested list's production snapshots leave the candidate universe, the others stay. The delta is computed **list by list** — mixing entity ids across lists would make a record look deleted merely because it does not exist in the other list — then merged for the three passes. The number of screening passes stays at three whatever the number of lists, which is the whole point: the dominant cost is the shared pass, and it is shared.

The scope is resolved **at execution, not at submission**: a wave drops its snapshots over several minutes, and a backtest still queued must cover the ones landing after it. The queue's dedupe key does the grouping — the second sync submits nothing. Snapshots that land while the backtest is already running are picked up by one follow-up run, submitted only if such snapshots exist. The same report is stored on every covered snapshot, since the candidate universe contained them all and the verdict has no meaning sliced per list; the review screen falls back to the consolidated job so it no longer shows "no backtest" while one is running for it.

### Changed — cache preloading is now opt-in, because measurement said so
The preloading added just before was **enabled by default**. Measuring it against the live service reversed the conclusion, so the default is reversed too: it is now off unless `FISKR_PRELOAD_CACHE=1`.

On shared hosting the real lever turned out not to be preloading at all, but **process survival**. Without `passenger_min_instances`, Passenger recycles a process as soon as it goes idle — one was observed being born *at the very second* of the request. The warm-up thread then competed for CPU with the request it was meant to help, and the first screening went from 64 s to 118 s: the preload cost more than it returned, because the process died before benefiting from it.

With `passenger_min_instances 1` (already recommended in the README, and now demonstrated), the process survives — verified over 12 minutes — and the cache loaded once serves every later request:

| | Ctrl+K palette | Warm screening | Process survival |
|---|---:|---:|---:|
| Without `passenger_min_instances` | 31 s | — | recycled immediately |
| With `passenger_min_instances 1` | **1.4 s** | **5.2 s** | > 12 min |

The palette drops from 31 s to 1.4 s because it finally takes the in-memory path instead of the SQL fallback — and returns 123 results instead of 77, the in-memory index also covering aliases, exactly the documented difference between the two paths.

Preloading then only spares the very first screening after a restart, at the price of 60 s of CPU and the referential's footprint in *every* process that spawns, including the extra ones Passenger creates under load. It stays available for dedicated hosting, where startup is not constrained and memory is not shared. Pinned by a test that reads the default off the syntax tree, so it cannot drift back silently.

**Caveat on the cold numbers**, stated rather than smoothed over: 64 s and 118 s were measured an hour apart, and a later attempt at the same operation exceeded 280 s. The likely cause is account CPU throttling (CloudLinux) after a dozen full cache loads in half an hour — so those three figures are not a comparable series. The *warm* timings (5.2 s, 1.4 s) are stable and reproducible.

### Performance — the engine cache is now preloaded at web-process startup
Follow-up to the Passenger `lifespan` fix, and this time driven by measurements taken against the live service rather than by an estimate. With the correctness hole closed, the remaining cost was simply misplaced: the cache still loaded **inside the first request that needed it**.

| | Measured in production |
|---|---:|
| First screening after a restart | **64 s** |
| Same screening, cache warm | 5.6 s |
| Ctrl+K palette (SQL fallback) | 30.9 s |

`fiskr/wsgi.py` now warms the cache when the process starts, so no user pays for it. Three deliberate choices:

- **In a background thread, not inline.** Blocking the boot for a minute would risk `passenger_start_timeout` (90 s by default), and a process killed at boot takes the site down — far worse than the slowness being fixed. The application answers immediately while the cache warms; a request arriving meanwhile behaves exactly as before.
- **Guarded by a lock.** Warm-up and a screening arriving mid-warm-up would otherwise read the whole referential twice in parallel — twice the time and twice the memory for an identical result. The screening now waits for the load in flight. The lock is replaced fresh after `fork()` (the screening pool forks, and a lock held at fork time stays held forever in the child, which would freeze on its first screening).
- **Never fatal.** A database that is briefly unavailable must not stop a process from starting. On failure the boot continues and `_ensure_watchlist_cache` remains the safety net, exactly as before. `FISKR_PRELOAD_CACHE=0` disables the warm-up without redeploying — the memory footprint is multiplied by the number of Passenger processes, and on shared hosting that trade-off belongs to the operator.

Pinned by tests, including a concurrency test asserting that a simultaneous warm-up and screening produce exactly **one** load, and a syntax-tree check that the boot never calls the loader at import time — a threaded call is the point, a synchronous one would be the regression.

### Fixed — under Passenger the engine cache never loaded, and screening silently cleared everyone
Reported from production as a cosmetic detail: the sidebar's **"Hash Actif"** badge showed `N/A`. Measured against the live service, it was the visible end of a chain that reached the screening engine itself.

**Root cause.** Passenger serves the app through `a2wsgi.ASGIMiddleware`. Reading that library's source: its `__call__` only ever builds a per-request `http` scope — it does **not** implement the ASGI `lifespan` protocol. FastAPI's `lifespan` therefore never runs in a web process, so `load_watchlist_cache()` is never called there and `watchlist_index`, `watchlist_store`, `watchlist_search_index` and `watchlist_hash` keep their initial module values. Nothing fails loudly, because `get_db()` calls `init_db()` lazily — so every database endpoint works and hides the hole.

Three consequences, in increasing order of seriousness:

- **The badge.** `GET /api/watchlist/summary` returned the in-process globals verbatim: `hash: "N/A"`, `count: 0`, while the database held more than thirty READY `WATCHLIST_*` snapshots. It now reads the **database**: the hash of the most recent READY watchlist snapshot and the live record count, following the same rule as the engine cache. That is the correct source anyway — the active hash is a property of the snapshot in production ("the exact referential version, the reference to cite in a case file"), not of a process's memory. With no production referential it returns `null` and the badge shows its empty state, never a fabricated hash.
- **The Ctrl+K palette was out of service.** `GET /api/search/quick` *did* guard its cache — by building it inside the HTTP request: ~900 000 records plus the blocking index. Measured against production: **no response after more than 100 seconds**. It no longer builds anything. It uses the in-memory index when it is already there (worker, development) and otherwise falls back to a bounded SQL query on the production scope. Two differences are accepted on the fallback path, for want of `pg_trgm` on this host: it matches primary name and identifier but not aliases (stored as JSON), and it follows server collation, so `francois` will not find `François`. A partial answer from a bounded query beats an endpoint that never replies; the in-memory path keeps its full reach.
- **`POST /api/screen` cleared listed parties, silently.** This is the one that matters. `screen_client_profile` reads `watchlist_index` with no guard, and the endpoint never ensured it was loaded. On an empty index the candidate set is empty, so screening returns `NO_MATCH` — **a listed person declared not listed, with no error, no warning and nothing in the audit trail to show it happened**. The ISO 20022 filtering endpoint (`POST /api/transactions/screen`) had the same hole, and additionally logged `"N/A"` as the watchlist hash of every decision it recorded. Both now call `_ensure_watchlist_cache(db)` before screening. The first screening after a restart pays for the load; that is the price of correctness on a regulatory endpoint, and it is now the only path that still pays it.

Pinned by tests that reproduce the production condition — module globals emptied, exactly as a Passenger web process starts. Including a syntax-tree guard asserting that **every** screening route calls `_ensure_watchlist_cache`, so a fourth one cannot be added without the guarantee, and an equivalence test checking that the SQL fallback returns the same keys as the in-memory path.

**`tools/audit_empty_cache_decisions.py`** lists the decisions already recorded that way, so the damage can be scoped rather than guessed. The audit trail kept everything: when no candidate is found the engine still writes a row, with `watchlist_id = 'NONE'`, `status = 'NO_MATCH'` and the process's hash — so `watchlist_hash = 'N/A'` isolates exactly the decisions taken with no lists in memory, since a loaded process always writes the real snapshot hash. The tool reads and only reads: it never rewrites the journal (immutable by design), never touches the schema, never calls `init_db()`. It reports the volume, the period, the distinct clients, and exports the list to CSV for re-screening; it also flags any row that contradicts the signature, which would mean the criterion is wrong.

Not changed here, because it is an operational trade-off rather than a defect: preloading the cache at import in `fiskr/wsgi.py` would move the cost to boot instead of to the first screening, at the price of a slower start and the cache's memory footprint multiplied by the number of Passenger processes.


### Fixed — `refresh_prod.sh` restarted only half of production
Observed live right after a refresh: `versions.worker.outdated` was `false` while `versions.api.outdated` was still `true`. The daemon was running the newly merged code; the website was not. The script killed and relaunched `python -m fiskr.worker` but never touched the web processes, which keep serving whatever code they imported at boot — so a refresh silently left the two halves on different revisions.

The script now also touches `tmp/restart.txt`, Passenger's documented restart trigger: it reloads the application on the next request, with no kill and no cutover (in-flight requests finish normally). `passenger_wsgi.py` is deliberately left alone — cPanel regenerates it on every visit to "Setup Python App", which would make the manoeuvre unreproducible. If the file cannot be written, the script says so and points at cPanel → Setup Python App → Restart rather than reporting success. The closing message now names **both** flags to check.


### Fixed — the index tool now says when the host simply doesn't provide `pg_trgm`
Reported from production (o2switch): running `tools/create_perf_indexes.py --search` produced **three raw SQL failures** in a row — "could not open extension control file", then twice "operator class gin_trgm_ops does not exist". The cause is not a misconfiguration: the host does not ship the `pg_trgm` extension at all, and nothing the operator does can change that. But the output read like a botched command.

The tool now checks the extension **before attempting anything** (`pg_extension` / `pg_available_extensions`) and, when it is absent, explains it in one sentence: the host doesn't provide it, nothing was attempted, and — importantly — the browse indexes above *are* in place, which is what fixes the slow list screen. It also no longer re-issues `CREATE EXTENSION` when the extension is already installed. On a host that does provide it, the nominal path is unchanged (verified end to end: extension created, both trigram indexes built).

`Documentation/PERFORMANCE_BASE.md` records the finding: on this host the search trade-off does not even arise, and the way out would be the engine's in-memory index (the one already serving Ctrl+K) rather than SQL.


### Fixed — a missing column no longer wipes the entire database on startup
`init_db()` contained a legacy shortcut: if `watchlist_entities` lacked the `place_of_birth` column, it called `Base.metadata.drop_all()` — **destroying every table**: approved lists, alerts, and the immutable audit trail. One absent *nullable* column, exactly the kind of gap an additive migration closes in a second, and a plain restart erased everything. No backup was requested, no confirmation asked, and the log line said only `Database schema outdated. Dropping and recreating tables...` — at INFO level. The fault was reproduced in real conditions: **2.79 million records lost to a single startup** against an incomplete schema.

A nullable column is *added*, not paid for with the database. Startup now ends with a generic sweep (`_add_missing_nullable_columns`) that aligns every existing table with the model, deriving each column's DDL type from SQLAlchemy for the current dialect — so it works on PostgreSQL and SQLite alike, with no hand-maintained list to forget. The declared additive migrations still run first and are untouched; the sweep is the safety net behind them. A `NOT NULL` column without a default genuinely cannot be added to a populated table: it is now **reported** with an explicit warning for manual action, instead of being "solved" by destruction.

Pinned by tests, including the exact scenario that caused the loss: a populated table missing `place_of_birth` keeps its rows and gains the column; a stripped-down table recovers every nullable column of the model; a `NOT NULL` gap is reported while the data stays intact; the sweep is idempotent; and `init_db` is checked on its **syntax tree** to ensure no destructive call ever returns.


### Performance — browsing the lists went from 18 seconds to milliseconds (missing indexes)
Reported from production: reads felt very slow, on a 9 GB database. Measured against the live service rather than guessed — every read endpoint answered in about a second **except** `GET /api/watchlist/db` (the "Watchlist Active" screen), which took **18 seconds to return 50 rows**, and 54 s with a search term.

The signature said it wasn't serialisation: asking for 10 rows cost the same as 50, and filtering by list type answered in 1.2 s. **The "production" scope is defined by columns that were not indexed** — `snapshots.status`, `snapshots.file_type` and `watchlist_entities.excluded` — so PostgreSQL scanned the whole table for every page. Two facts made it brutal: production holds **11.16 M rows** of which only **898 k are live** — 287 superseded snapshots account for **9.4 M rows, 92 % dead weight** — and the default ordering is on the *joined* table (`snapshots.uploaded_at`), forcing a full materialise-and-sort.

Three indexes fix it, with **no change to any query**: `snapshots(status, file_type)`, `snapshots(uploaded_at)`, and a **partial** index `watchlist_entities(snapshot_id, id) WHERE excluded IS NOT TRUE` — partial so it covers exactly the scope that is read instead of carrying the 92 % that isn't. Reproduced on a local PostgreSQL 16 at production proportions (1.4 M rows): the page went **173 ms → 1.6 ms (×106)** and the count **431 ms → 26.7 ms (×16)**, both switching from a sequential scan to an index-only scan. Production is 8× larger, where the scan cost scales linearly.

Two safeguards, because this is a live 9 GB database:
- **Startup never builds them on a big table.** An ordinary `CREATE INDEX` holds an exclusive lock for its whole build — minutes here — which would freeze the service on restart. `init_db` now creates missing performance indexes only below a row guard (fresh installs, dev), and otherwise logs exactly what to run. The row estimate uses the planner's statistics, never a `COUNT(*)`.
- **`tools/create_perf_indexes.py`** builds them with `CREATE INDEX CONCURRENTLY`, service running, no downtime, idempotent. It deliberately **never calls `init_db()`** and cannot touch the schema — pinned by a test that checks the syntax tree, not the prose.

Full-text search (`ILIKE '%…%'`) is a separate trade-off and stays **opt-in** (`--search`): trigram GIN indexes take it from 1 270 ms to 17 ms (×73) but cost about **78 % more write time on ingestion** (measured: 50 000 rows in 1.58 s without, 2.82 s with) — the operator arbitrates, so they are never created automatically.

### Performance — the post-delta rescreen now runs in parallel
The automatic rescreen re-screens the **whole client base** after every list goes live — it is the most frequent screening in production, and it was the last fully **sequential** loop (it even had to yield the GIL by hand to keep the API responsive). It could not simply be handed to the existing fork pool, because unlike the test book it **writes**: audit records and work alerts.

Splitting it along that seam solves it. The loop is now two phases: **(1)** finding each client's best match — pure computation, the near-totality of the time, and where most clients have no candidate at all — runs on a pool of forked processes that touch **no database at all**; **(2)** only the handful of clients that reach `ALERT` come back to the parent, which does every write itself, sequentially, in client order. So the writes keep exactly the transactional semantics and ordering they had.

Measured (`tools/bench_rescreen.py`, added): **1.98× on 2 usable cores** — near-linear, and it scales with the cores the server actually has. Equivalence is pinned by a test that runs the same scenario sequentially and in parallel and asserts the resulting alerts, scores, matched entities and counters are **identical**; a second test kills the pool mid-run and checks the rescreen falls back to sequential and still completes rather than losing the run. Combined with the kernel work below, the rescreen is now roughly **4× faster** than before this release.

### Performance — the screening kernel is ~2× faster (real-time, batch, backtest, rescreen)
Profiling a realistic universe screen (`tools/bench_screening.py`, added) showed the time went almost entirely into the matching kernel. Four changes, each **result-identical** (the whole test suite stays green, and the Damerau-Levenshtein rewrite was checked bit-for-bit against the old one on 200,000 random pairs):

- **Damerau-Levenshtein** was the top cost by far — it built its DP matrix in a **dict keyed by `(i, j)` tuples**, so every cell paid tuple-hashing and a `min()` call (tens of millions per test book). Rewritten to two rolling 1-D arrays with inline comparisons: same OSA distance, ~2.8× faster on that function.
- **Date parsing** (`parse_dob`) was re-`strptime`-ing the same dates for every candidate comparison — now memoised.
- **Engine-capability checks** (`caps.is_active`, called over a million times per screen) re-resolved prerequisites each time, and `describe_context` recomputed the same delta for every match — both now memoised on the effective capability set (a hashable frozenset, so a settings change yields a new key with no explicit invalidation).
- **Name normalisation** (transliteration + accent folding) was recomputed for the same names on every candidate comparison — now cached, keyed on the effective capability context.
- Plus an exact-equality fast path in the base score (frequent when a client is derived from a listed name), computed from the weights so it stays correct even with non-normalised weights.

Measured on the synthetic bench (15k listed entities, 800 clients, ~65 candidates each): **17.9 s → 8.9 s** for the same work. The win applies to every screening path — the real-time `/api/screen`, batch campaigns, the test book, and the automatic post-delta rescreen. (A further, larger lever — parallelising the sequential rescreen loop, and a C-accelerated metric kernel — is noted in `Documentation/REFLEXION_LANGAGES.md`.)

### Changed — the filtering channel's blocking key now lives in the Filtrage tab
The blocking-key editor for the **transactional filtering** channel was showing under Criblage › Paramétrage moteur, next to the client-screening one. It moves to its own "Paramétrage moteur" sub-tab inside the **Filtrage** tab (same `blocking`/admin gating), so each channel's engine settings sit with that channel.

### Added — ad-hoc name check (dry-run screening, nothing logged)
The only fuzzy path was `POST /api/screen`, which needs a full structured profile **and writes an audit record and may open an alert** — unusable for the everyday "is this name risky?" check during onboarding or doubt-clearing, since each check would pollute the regulatory trail and could spawn alerts. New `GET /api/screen/preview` runs the **same engine** (quality gate, blocking, transliteration, phonetics, DOB/gender/geography adjustments, per-list thresholds) but is **strictly read-only**: no audit row, no alert, no counter touched. Being a GET, it is available to the read-only auditor role too.

When no country is supplied, the check broadens across every country partition of the blocking index (it looks up the `_{type}_{phonetic}` suffix) so a name-only query does not silently miss a listed record whose nationality happens to be unknown to the searcher. It also carries the FATF country-risk lens: a name unknown to the lists but tied to a high-risk jurisdiction is still flagged. Front: a compact "Vérification rapide d'un nom" card at the top of the real-time screening screen, clearly labelled as an unlogged preview (translated, RTL-aware). Pinned by tests that assert a fuzzy match is found **and that the alert/audit tables are untouched**.

### Security — two vulnerabilities fixed (stored XSS in the audit modal, path traversal on upload)
A defensive review of the code and documentation found two exploitable issues, both now fixed and pinned by tests.

**Stored XSS in the audit modal.** The engine's decision-tree labels — the hard-match reason and the DOB/gender/geography adjustment descriptions — embed fields taken from the *screened* data (passport number and its country, "other identifier" type, country labels). Those fields arrive from a CSV upload, an ISO 20022 payment message, or an inbound webhook, so they can be attacker-controlled. They were interpolated **raw** into the audit modal's `innerHTML` (`viewAuditLogDetail`), where a forged profile could inject script that runs in a compliance officer's browser. All three sinks now pass through `escapeHtml`. The server-side twin (the printable dossier) already escaped via `html.escape`, and was verified clean. A static regression test guards the sinks.

**Path traversal / arbitrary file write on ingestion.** `POST /api/ingest` built its temp path as `temp_dir / file.filename` with the **raw** upload name. A name like `../../passenger_wsgi.py` or `/etc/cron.d/x` would make the upload land outside the intended directory — an arbitrary-write primitive available to any authenticated user (potentially code execution by overwriting `passenger_wsgi.py`). Filenames are now reduced to a safe basename by a single shared helper, `safe_upload_filename` (no path separator, no `..`, no absolute path, no dot-file), prefixed with a unique token. The two already-sanitised upload sites (batch, exclusion evidence) were unified onto the same helper.

**Hardening.** The app ships with a default `SECRET_KEY` and `ADMIN_PASSWORD` in the source (a known signing key lets anyone forge an admin session cookie). Startup now logs a prominent warning when either is still the built-in default, and the remote diagnostic (`/api/diagnostic/jobs`) reports which secrets are unset — by **name only**, never the value.

### Added — geographic-risk lens: FATF high-risk jurisdictions
Fiskr matched **names** against sanctions lists but carried no **jurisdiction-risk** view — a standard AML/CFT expectation. It now flags clients and payment parties tied to a FATF high-risk jurisdiction, whether or not their name is listed: **call for action** (counter-measures — Iran, DPRK, Myanmar) and **increased monitoring** (the grey list — 22 jurisdictions as of the 19 June 2026 plenary). New module `fiskr/country_risk.py`, endpoints `GET /api/country-risk` (the dated reference) and `GET /api/country-risk/assess`, and a `country_risk` summary added to the screening response, surfaced as a red/amber banner in the real-time screening result (translated, RTL-aware).

Deliberately **outside** the scoring engine: it changes neither `final_score`, the `decision_tree`, nor the verdict — the name-matching engine and the test-book memoisation are untouched. The reference list is **hot-overridable** via a `country_risk` block in `config.yaml`, so the ~3×/year FATF plenary updates need no redeployment; the built-in default carries its `as_of` date so nobody applies it believing it current after a plenary has moved.

### Added — an open tab now says when a new version has been delivered
Following on from the cache-busting fix below: assets are correctly refreshed on **reload**, but a tab left open keeps running the code it loaded when it was opened. After a deployment it therefore runs the **old** version with nothing to signal it — which is exactly what happened in production, where the screen looked frozen ("it seems to have crashed") while everything was in fact working; a reload was all it took.

The application now compares the version it loaded against `GET /api/version` every five minutes, and, when they differ, shows a discreet floating banner offering **Recharger** / **Plus tard**. "Plus tard" holds until the next reload, so nobody gets nagged every five minutes. Translated into the six languages, RTL-aware.

### Changed — a wave of test books no longer re-screens the same universe N times
In delta mode a test book runs three passes, but only one of them is the size of a universe: the **shared universe** (all the other lists, plus the unchanged records of the list under test). Measured in production: 19 queued test books, each re-screening the same **770,000** records — around eight minutes apiece, roughly three hours of queue — to assess lists of a few hundred entries.

That pass is now **reused from one test book to the next**, so a wave costs one full pass instead of N. Reuse is keyed on a fingerprint of everything that can change the outcome: panel, the exact set of shared snapshots, the unchanged records of the list under test, hot settings (`app_settings`, which carries thresholds, blocking and engine toggles), anti-false-positive rules, whitelist, linguistic resources (files **and** learned equivalences), and the manual-edit journal — a record corrected by hand does not change its snapshot id, only its journal records it. The fingerprint is **deliberately coarse**: a setting with no bearing on screening still invalidates the memo. A needless recompute only costs time; a wrongful reuse would produce a **false verdict**. Nothing is persisted, a single entry is kept, and it dies with the process. Reports carry `shared_pass_reused` for traceability. Pinned by tests on both sides: reuse when nothing moved, recompute when a setting changes, when the list differs, and when a record is edited by hand.

### Fixed — deployments were invisible to browsers, and a wide table pushed a card off-screen
Reported from production right after a deployment: the newly added sources were nowhere to be seen, plus display glitches. Two distinct root causes, both now fixed at the root.

**Deployments were invisible.** The pages referenced their assets with a **hand-maintained version** (`app.js?v=7.0`). That number hadn't moved in months, so browsers kept serving the **cached** copy of a file the server had long since updated — a deployment could reach production and remain invisible to every user, with nothing to signal it. The version is now the **fingerprint of the assets' content** (`buildinfo.STATIC_VERSION`), injected when the page is served: it changes as soon as the content changes, and never otherwise — so the cache stays effective and no manual purge is needed. Pinned by a test.

**A wide table pushed a card off-screen.** The `.grid-layout` columns were `1.1fr 0.9fr`, and a `1fr` track **never shrinks below its content's minimum width**. As the sources table grew with the catalogue, its column inflated and pushed the neighbouring card 114 px past the viewport edge, forcing the whole page to scroll sideways. Now `minmax(0, …)`: the track can shrink and `.table-container` scrolls internally, as designed. Same fix applied to `.home-grid` and `.details-grid`, which carried the same latent bug. Measured after the fix: **0 px of horizontal overflow** on every screen checked.

### Fixed/Added — every source verified against the live internet; 16 public sources added
All 40 wired sources were queried **one by one from the internet**, not inferred from documentation (report: `Documentation/VERIFICATION_DES_SOURCES.md`).

**The find**: the Israeli NBCTF source pointed at `il_nbctf_sanctions`, a slug that **has never existed** in the OpenSanctions catalogue — a 404 `NoSuchKey` on every run, meaning the source had been reporting nothing since it was wired. Corrected to `il_mod_terrorists` (2,056 records). Two more sources were dead ends: **DFAT Australia** (HTTP/2 stream error on the CSV, 404 on the XLSX) and the **World Bank** (401, subscription key now required) — both now have a working OpenSanctions route, with the native connectors kept for manual import / for whoever obtains a key. All 27 registry slugs are now validated against the live catalogue (462 datasets).

**The probe tool no longer drifts**: `tools/diagnostic_sources.py` derived its list from a hand-copied block — six sources were missing from it (Canada, AMF, DFAT, HK-SFC, World Bank, OFAC Non-SDN) and three URLs had gone stale, so it could report "all good" about addresses the product no longer used. It now reads `get_sync_config()`: it probes exactly what the product downloads, 73 probes.

**16 public sources added**, each chosen for what no already-wired list carries: national terrorism lists under UNSCR 1373 (**UAE, Saudi Arabia, Qatar, Egypt, Türkiye MASAK, Indonesia DTTOT, South Africa FIC, Tunisia**), European neighbourhood freezes (**Monaco**, 12,929 records; **Czechia**), what the Anglo-Saxon lists don't hold (**US FTO** — OFAC carries the freeze, not the State Department's terrorist-organisation designation; **UK FCDO** and **proscribed organisations** — OFSI carries the financial freeze, not those), and **designated crypto wallets** (Israel MoD, 4,284 — Fiskr already hard-matches crypto addresses). Every one is **off by default**, with its own list type and therefore its own score threshold. No paid source was wired: all require a contract (see `SOURCES_PREMIUM.md`).

Two consistency gaps closed on the way, each now pinned by a test: **every list type has a front label** (the 11 registry sources had none — they displayed as raw `WATCHLIST_*` in tables and selectors) and **every registry source appears on the Sources screen** (without which it has no sync button and no scheduling).

### Added — bulk homologation: approve several lists in one gesture, from a single overview
A morning after the scheduled syncs leaves several lists waiting. Approving them meant opening each one, reading its delta, checking its verdict, deciding, coming back. The pending queue is now a **decision table**: each row carries the list's **delta (additions / modifications / removals)** next to its **test-book verdict and gap**, with checkboxes and a **"Homologuer la sélection"** button (`POST /api/review/snapshots/approve-bulk`).

Guarantees, because a batch must not be a back door: every list in the batch goes through **exactly the same controls** as a single approval (exclusion justifications, mandatory test book with an `OK` verdict) — the single-approval body was extracted and is replayed as-is. A refused list **does not stop the batch**: it comes back with its reason while the others go through, so one blocked list no longer forces a one-by-one restart. The whole batch is traced in the admin journal, and lists of the same type still follow the usual rule — the most recently approved supersedes the other.

The queue's delta uses the **delta stored at sync time** (instant); when it isn't applicable — manual import, production changed since — the row says so ("à l'examen", "premier import") rather than showing a misleading 0/0/0 or making the queue crawl through a recompute over hundreds of thousands of records. The detail view still recomputes exactly.

### Fixed — four broken sources repaired (Canada, US CSL, AMF) and one documented (World Bank)
The remote diagnostic showed four sources failing every night. Root causes and fixes, each verified against the live upstream:

- **Canada (404)**: Global Affairs Canada **withdrew its CSV**. The XML export is now the served route — and it carries more than the CSV ever did: the flat-record reader gained XML support (streamed via `iterparse`), the parser learned the XML's own column names (`EntityOrShip`, `DateOfBirthOrShipBuildDate`, `ShipIMONumber`, `TitleOrShip`), and the result went from 3,509 records to **5,684 — the 2,175 legal entities and designated ships were previously being dropped**, 731 of them with an IMO number now populated. A test pins CSV and XML to identical output so the switch does not read as a wholesale list replacement in the delta.
- **US CSL (TLS failure)**: `api.trade.gov` serves an **expired certificate**. Switched to `data.trade.gov` — same JSON, same structure, 25,921 records.
- **AMF (404)**: the page moved *and* stopped publishing entities in HTML (PDF per category only). Switched to the AMF's **daily open-data export** on data.gouv.fr (stable "latest" resource URL, so the timestamped filename never breaks it): 3,395 entities with their listing date and warning category. Two bugs surfaced and were fixed along the way — the CSV reader assumed comma separators (**European semicolon files parsed as a single column and yielded an empty list, silently**), and "Date d'inscription" normalises to `datedinscription` (the elided *d* survives), so it never matched the file's `date_inscription`.
- **World Bank (401)**: their API now **requires a subscription key** ("missing subscription key"). No code fix possible; `config.yaml` documents how to supply it through the existing `auth_headers` mechanism with a `.env` variable, and to leave the source disabled until then.

### Added — one-shot SQLite → PostgreSQL migration tool: `tools/migrate_sqlite_to_postgres.py`
When PostgreSQL is unreachable, Fiskr silently falls back to SQLite — all production data then lives in `fiskr.sqlite3`. The day the PostgreSQL connection is repaired, **nothing migrates by itself**: Pg starts empty (freshly seeded admin), and the API and the daemon can even end up on *different* databases. The new tool copies the entire SQLite content to PostgreSQL faithfully (every table in FK dependency order, ids preserved, JSON/date types converted, auto-increment sequences reset, per-table count verification, batches of 5000). Guard rails: the SQLite file is never modified; the run aborts if the target is not empty unless `--wipe-target`; `--pg-url`/`--sqlite` overrides for non-standard layouts. Validated end-to-end against a real PostgreSQL 16 (6,560 rows, all counts equal, sequence continuity checked). The printed epilogue walks through the cut-over: `database.fallback_to_sqlite: false`, rename the SQLite file, restart app + daemon, verify `system.db_engine == "postgresql"` in `GET /api/diagnostic/jobs`.

### Fixed — identical-content republishes no longer enter homologation (nor trigger backtests)
Observed in production via the remote diagnostic: the homologation queue filled with snapshots showing a **0/0/0 delta** — OpenSanctions (and others) republish their files daily with fresh metadata (timestamps, row order), so the byte-hash dedup sees a "new" file whose **parsed content is identical** to the production list. Each of those non-events cost a full homologation entry plus an automatic test book — the very jobs that were clogging the queue.

The three sync paths (OFAC, DGT, generic list-replacement runner) now share `_discard_content_identical`: after the delta is computed, a snapshot whose delta vs the current production list is empty is **archived as SUPERSEDED** (never shown to homologation), production stays untouched, and the report says `NO_CHANGE` ("contenu identique à la liste active — seules les métadonnées du fichier ont changé"). First imports (no comparison base) still go through homologation. Tested in both modes (direct promotion and staged homologation).

### Added — one-shot production refresh script: `tools/refresh_prod.sh`
Everything the deployment procedure requires, in the right order and with guard rails: venv activation, `git pull` (**fast-forward only** — refuses uncommitted local changes and diverged history), `pip install -r requirements.txt`, then stop-and-relaunch of the worker daemon — the step everyone forgets, and the daemon keeps running the OLD code until it happens. The kill targets only the current account's `python -m fiskr.worker` processes (SIGTERM first, SIGKILL only after 10 s), the immediate relaunch is race-free thanks to the flock singleton, and the script ends by pointing at `GET /api/diagnostic/jobs` to verify `versions.worker.outdated: false`. Default paths match the cPanel layout, overridable via `FISKR_VENV`/`FISKR_DIR`/`FISKR_BRANCH`.

### Added — remote production diagnosis: `GET /api/diagnostic/jobs` + code-version stamps
Production kept blocking on test-book jobs while every fix looked deployed. The missing piece was **eyes on the box**: one read-only endpoint now returns the whole job-queue radiography in a single call, designed to be queried **from outside** (support/ops/assistant) over HTTPS without shell access:

- **Version stamps** (`fiskr/buildinfo.py`): every process freezes a fingerprint of the `fiskr/*.py` sources it loaded at startup; the daemon publishes its own in its heartbeat. The endpoint compares API-loaded, daemon-loaded and on-disk fingerprints — `worker.outdated: true` (or a live daemon with **no** fingerprint) proves the #1 deployment trap: a daemon still running the OLD code because it was never killed after `git pull`.
- **Queue radiography**: counts by status, RUNNING jobs with heartbeat freshness, the waiting queue in daemon claim order, last errors with their cause, and the serialized group's holder.
- **Daemon forensics**: supervision view + flock lock (is the written PID alive?), machine/process memory, load, DB engine, and the tail of `worker.log` — the daemon's last words when it died.
- **Access**: admin session, or an **`auditor`-role API key** via `X-API-Key` — read-only by construction (all writes are refused to that role), revocable at any time. Admin-role keys remain forbidden (least privilege).

### Changed — test-book (backtest) overhaul: bounded RAM, continuous progress, visible failures, one-click rollback
Full review of the backtest path (`fiskr/backtest.py`, `fiskr/jobs.py`, homologation stepper) with four outcomes:

- **RAM bounded by construction**: universes were already loaded in projection and passes already sequential; the remaining vector was two heavy jobs of *different* kinds running side by side. Backtests and engine simulations now form an **exclusive serialized group** (`SERIAL_KINDS`): while any member is RUNNING with a fresh heartbeat, no other member starts — at most one screening universe in RAM at any time, whatever the mix. The daemon keeps its second slot for light jobs (promotions, syncs), which now explicitly bypass the wait instead of queueing behind a backtest.
- **Continuous, legible progress**: one single 0 → 100 % bar covers the whole test book. The total cumulates every pass (2 in full mode, 3 in delta mode), each pass is named ("passe 1/3 — univers partagé", "passe 2/3 — fiches retirées"…), and universe loads — previously silent minutes on large referentials — announce themselves (`LOAD_UNIVERSE`). The operations panel re-renders **in place** (patched rows, animated bar, no flicker) instead of rebuilding its DOM on every poll.
- **Failures visible where the user works**: the homologation stepper's step 3 now shows the last backtest job's state — an ERROR banner with the cause, attempt count and a **↻ Relancer** button; a QUEUED notice with **✕ Annuler**; a CANCELLED notice. (`GET /api/review/snapshots/{id}` returns `backtest_job`.) Combined with the continuous zombie repair, a crash can no longer be invisible.
- **Rollback guarantees stated and tested**: a queued test book cancels atomically (nothing ran, exact return to the previous state), and the archived report is written **only on success, in one commit** — a failed re-run never clobbers the last valid report.

New tests: progress monotonicity/continuity across delta passes, and cross-kind exclusivity of the serial group. Documentation updated accordingly (README, in-app guide, `Documentation/PRODUCTION_DES_LISTES.md` — the business process itself is unchanged).

### Fixed — zombie RUNNING jobs no longer freeze the whole queue
Production observation: two "Cahier de tests" entries RUNNING **simultaneously** (impossible within one daemon — backtests are serialized) with every other job stuck QUEUED. Those were **zombies**: jobs left RUNNING by a daemon incarnation killed mid-run (OOM). `requeue_stale` only ran **at daemon startup** — as long as the current daemon lived, zombies stayed RUNNING forever, occupied the daemon's slots in the display, and the serialization check counted them as "busy", blocking their whole kind. Two fixes:

- **Periodic repair**: the daemon's heartbeat loop now runs `requeue_stale` every ~60 s — a zombie is re-queued (or marked ERROR past max attempts) within a minute, no restart needed.
- **Serialization on fresh heartbeats only**: `claim_next` and `_serial_kind_busy` ignore RUNNING rows whose heartbeat is older than 90 s — a dead backtest can no longer block the next one.

Reminder for deployments: the daemon keeps running the OLD code until killed — after `git pull` + `pip install -r requirements.txt`, kill it (`kill $(head -1 fiskr-worker.lock)`); the cron/autostart relaunches it on the new code and the startup repair re-queues everything wedged.

### Added — queued actions can be rolled back before they run
A queued action has not executed nor modified anything yet — cancelling it is an exact return to the previous state. The bell panel's "Opérations en cours" now shows a **✕ Annuler** button (administrators) on every QUEUED entry, with a confirmation dialog; the job becomes CANCELLED, is logged in the admin journal, and the panel announces "Opération annulée". Two robustness fixes underneath: the cancellation is now **atomic** (`UPDATE … WHERE status='QUEUED'` — if the daemon claims the job in the same instant, the call answers 409 instead of clobbering a running job), and `run_job` gained a guard so a job cancelled while waiting for its serialization turn **never executes** (the thread path used to run it anyway). Running jobs remain non-cancellable — cooperative interruption of an executing task is a different, riskier feature.

### Fixed — workers no longer get stuck forever on test-book (backtest) jobs
Production symptom: the job queue fills with waiting operations because a backtest never finishes. Root cause: the backtest screens its panel through a `fork()` process pool, and when a pool child dies — typically **killed by the OOM killer**, the very memory risk backtests are known for — `multiprocessing.Pool` silently replaces the child but **never re-runs the lost chunk**. The parent then waited on `map_async` with no timeout and no child-death detection: the job span forever, its separate heartbeat thread kept it looking alive (so the stale-job recovery never fired), the backtest serialization kept every following test book QUEUED indefinitely, and one of the daemon's slots was consumed for good.

Fixes:
- **Watchdog on the screening pool** (`screenpool._wait_with_watchdog`): a child dead with a non-zero exit code (OOM: −9) or **no progress tick at all for `jobs.screen_stall_timeout_s`** (default 900 s) raises `PoolStalled` and terminates the pool — the infinite wait no longer exists.
- **Self-healing backtest**: on `PoolStalled`, the test book automatically restarts **sequentially** (minimal memory, same screening body, no pool) instead of failing — it completes rather than blocking the queue.
- Immediate production remedy (before deploying): restart the worker daemon — `requeue_stale` re-queues the wedged jobs on startup.

### Fixed/Changed — full application sweep: self-hosted fonts, favicon, redundant scans, audit indexes
A three-pass sweep (static consistency audit of tab targets/ids/panels, a browser crawl of every screen collecting JS errors and failed requests, and a backend review) found and fixed:

- **Fonts are now self-hosted** (Inter + Outfit, latin/latin-ext woff2 under `/static/fonts/`, SIL OFL). The UI no longer makes ANY request to Google Fonts: faster first paint, works fully offline/behind restrictive egress, and a compliance workstation no longer calls a third-party CDN on every page.
- **Favicon**: the anchor logo now exists as `/static/favicon.svg` (tab icon at last) and a `/favicon.ico` route ends the 404 previously logged on every page load.
- `/api/watchlist/db`: an exact search counted its perimeter **twice** (the same LIKE count re-executed) — now reused; one full scan saved per search.
- `/api/alerts`: total and open counts were two separate table scans per queue load — merged into one aggregate.
- New indexes on the audit trail (`status`, `list_type`) covering the `/api/history` filters, created idempotently on existing databases.
- Static audit came back clean: no duplicate ids, no orphan tab targets, every `switchSubTab` destination exists in its section; the browser crawl over all 10 tabs and 32 sub-tabs reports zero JS errors and zero failing requests.

### Changed — table filters audited at production scale; the typo-tolerant watchlist search now streams its results
Every filtered endpoint was measured against a production-sized bench (300,000 listed parties, 300,000 audit rows, 80,000 alerts). Verdict: alert queues, audit history, sync reports and the client-side table filter bars all answer in 20–300 ms — no action needed (the filter bars did gain a small 120 ms debounce). One path was catastrophic: **typing a typo in the watchlist search blocked one request for 40.4 s**, because the fuzzy fallback scored the entire referential in Python inside the HTTP request.

That fallback now **streams**: the browse endpoint answers immediately (`fuzzy_pending`, 0.4 s), and the screen drives a **chunk-by-chunk scan** (`GET /api/watchlist/db/fuzzy`, keyset cursor) that fills the table as matches arrive, with a live progress banner ("N % du périmètre parcouru, M résultats"). Each chunk is bounded (~0.4 s server-side: scoring runs on light column tuples, full records are hydrated only for the retained matches, Jaro-Winkler accelerated by the new required `rapidfuzz` dependency — bit-identical scores to the in-house implementation, pure-Python fallback kept). The scan always goes to the end of the perimeter so the best match can never be missed (display capped to the 200 best), is cancelled instantly by any new search, and no longer monopolises a worker: measured full-scan 6 s in 13 cancellable slices with first results after 0.4 s, versus one 40.4 s blocking request. Small perimeters (≤ 5,000 records) keep the historical inline fuzzy behaviour.

### Changed — the Ctrl+K global search became instantaneous
Each keystroke used to fire two heavy endpoints: `/api/watchlist/db` (SQL `LIKE` with a join, a `count`, and — whenever the exact match found nothing yet, i.e. most of the time while typing — a **fuzzy fallback that scanned the ENTIRE referential in Python** with Jaro-Winkler) and `/api/alerts` (three scans: total, open count, sorted fetch). On a production-sized referential that meant seconds per keystroke.

The palette now uses a dedicated `GET /api/search/quick`: listed parties are searched in a **normalized in-memory index built together with the screening cache** (accent/case-insensitive, aliases and entity ids included, prefix matches ranked first — zero disk access, zero join), and alerts through one single bounded SQL query. Measured at 500,000 listed parties: **74 ms vs 5.3 s (×72)** for the watchlist part alone, in ONE round-trip instead of two. Front side: navigation results render instantly before the network call, stale responses from earlier keystrokes are discarded, a discreet "Recherche…" indicator shows while pending, and the debounce dropped to 150 ms. The watchlist screen's typo-tolerant search is unchanged — it deliberately keeps the deep fuzzy pass.

### Added — a built-in guide: the whole site and every process explained, including the end-to-end CFT flow
A new **Guide** tab (bottom of the sidebar, book icon, reachable via Ctrl+K and `#guide/...` deep links) documents the application from inside the application, in seven chapters: *Démarrer* (spaces, roles, gestures), *Flux CFT* (a step-by-step diagram from official sources to TRACFIN filing — synchronization, quality gates, 4-eyes approval, production hash, both alert channels, instruction, outcomes, evidence), *Listes*, *Criblage*, *Filtrage*, *Alertes & audit* and *Administration*. Every chapter links straight into the screens it describes ("Ouvrir" buttons). The guide's body is deliberately French (national AML/CFT frame of reference); its navigation labels are translated like the rest of the UI.

### Changed — every sub-tab bar now fits on a single line
Sub-tab labels were shortened to their essence ("Criblage Temps Réel" → "Temps réel", "Screening de Masse (Batch)" → "Batch", "Sources Automatiques" → "Sources", "Paramétrage moteur" → "Moteur"…), with the full historical wording kept as a translated tooltip. The bars no longer wrap: `nowrap` with a discreet horizontal-scroll last resort, a tightened intermediate breakpoint (1025–1280 px) so even the 7-tab Criblage bar fits without scrolling on small desktops, and the existing swipe band below 1024 px.

### Fixed — leftovers of the Criblage/Filtrage split
- Batch-screening hit links opened the *Filtrage* queue and payment-party hit links opened the *Criblage* queue (inverted targets); both now land on their own channel.
- The Ctrl+K palette still navigated to the removed "Alertes" tab (blank screen) — entries now target the right spaces, and defensive aliases in `switchTab`/`switchSubTab` transparently reroute any residual `alerts` call.
- The analyst filter and saved views of both alert queues were no longer loaded after the split — they now load when a queue sub-tab opens.
- The "Mon compte" nav entry was accidentally admin-gated while "Utilisateurs" was visible to everyone; the gating is back on the right item.

### Changed — screening and payment filtering became two separate top-level spaces
Client screening (name screening against the referential) and ISO 20022 payment filtering generate alerts through different engines and methods, yet their queues, tools and screens were scattered across the "Alerts", "Screening" and other menus. The information architecture now reflects the business split:

- **Criblage** groups everything about client screening: real-time screening, batch campaigns, the SCREENING alert queue, the whitelist, engine tuning (blocking keys), linguistic resources and false-positive rules.
- **Filtrage** is a new top-level space for payments: the ISO 20022 message upload/filtering screen and the FILTERING alert queue.
- The old "Alertes" menu entry disappears; each space carries its own open-alerts badge in the sidebar (fed by the per-channel counters).
- **Old deep links keep working**: `#alerts/alerts-screening`, `#alerts/alerts-filtering` and every internal `switchTab('alerts')` call are transparently redirected to the right space — bookmarks, notification links and e-mails never break.

### Added — a full "My account" space
The account fragments scattered under Settings became a dedicated top-level tab. A person is now more than a username:

- **Profile**: photo (client-side square-cropped to 256 px and compressed, stored as a data URI ≤ 300 KB, shown in the sidebar badge), full name, job title, phone, email and free-text description — visible to colleagues through assignments, 4-eyes validations and the decision log. New columns on `users` with additive migrations, `GET /api/me/profile` and `PUT /api/me/avatar`.
- **Password change**, and the existing **two-factor (TOTP)** and **absence & delegation** cards moved in.
- **Account notifications**: a per-account master switch plus per-category checkboxes. Muting is a personal filter applied *after* the role-based routing (`notification_opt_out` on the user, honoured by the notifier's recipient resolution) — it never changes anybody else's routing.
- **Display preferences**: interface language, theme toggle and a shortcut to the home-page customizer.

### Added — bell notifications can be purged one by one, per section, or entirely
Every entry in the bell panel now carries a dismiss cross; "Recent jobs" has a *Clear* action, "To handle" a *Hide* action, and a *Clear all* sits at the top. Dismissals persist per browser; a hidden "to handle" item **reappears on its own if its counter grows** past the value it was dismissed at — purging can never hide new work. The badge counts only what is visible.

### Changed — tabs and filters became finger-friendly on tablet and phone
Sub-tab rows turn into a single-line horizontal swipe band with larger touch targets under 1024 px; filter bars wrap with stretching fields, then stack full-width under 640 px; the header compacts (engine status collapses to its dot), the bell panel docks full-width, and modals go near-fullscreen.


### Fixed — regulator alert-list extraction no longer produces kilometre-long "names" (HK SFC)
The HTML table extractor behind the regulator alert lists (HK SFC, AMF) swallowed the content of `<script>`/`<style>` tags inside cells and lost its state on nested layout tables — embedded JavaScript could become a multi-kilobyte "name" on imported records. The extractor now suppresses script/style/noscript/template content and handles nested tables with a stack; downstream, a plausibility guard discards any row whose "name" exceeds 200 characters or 24 words (extraction residue, never an identity).

### Fixed — the engine hash badge displays again
The sidebar "Active hash" badge called `GET /api/watchlist`, which serializes the ENTIRE in-memory referential — on a production of hundreds of thousands of records the response never arrived and the badge stayed on "Loading...". A new light `GET /api/watchlist/summary` (hash, version, record count) feeds the badge; the tooltip now also shows the loaded record count.

### Added — collapsible sidebar (icon mode)
A chevron button collapses the left menu to a 68 px icon rail: navigation icons stay clickable with their label as tooltip, the logo, user block and hash badge fold away, and the state is persisted per browser. Independent from the existing mobile off-canvas behaviour.

### Changed — pypdf became a required dependency
The EUR-Lex PDF fallback (extracting listings from the archived official PDF when an act's HTML is unreachable) shipped as an optional dependency; it is now installed by default so the fallback works everywhere. The code remains tolerant of its absence.


### Changed — EUR-Lex extracts listings from the acts themselves (HTML, PDF fallback)
The consolidated EU FSF list only refreshes every ~2 months, while designations appear in the Official Journal immediately — screening only the FSF between refreshes is a regulatory exposure. The EUR-Lex source therefore switched its default from *alert* (early-warning signal, no records) to **extract**:

- Records are extracted from the **annex tables of each restrictive-measures act** (name / identifying information / grounds columns), merged incrementally onto the EU list (pending approval as usual). Each record carries the act in **`official_reference`** — and the **official PDF of the act keeps being archived with its SHA-256**: that PDF is precisely the supporting evidence a regulator asks for.
- **PDF fallback**: when an act's HTML is unreachable, the listing is extracted from the archived official PDF itself (`pypdf`, optional dependency — without it the act stays visibly reported as failed, never silently skipped).
- When the FSF does refresh, it remains authoritative and supersedes these records, delistings included. The *alert* mode is still available (`sync.eurlex.mode`).

### Fixed — the automatic backtest after a sync actually starts
The post-sync automatic backtest silently abstained when no test panel existed — the most common reason it "never started". The automatism now **generates its own panel** (500 pseudo-clients derived from production) when none is available; every other abstention reason keeps being reported in the sync job result.

### Changed — backtests are serialized and run in delta mode
Two concurrent backtests each load a full watchlist universe and have exhausted a production machine's RAM:

- **Heavy job kinds are serialized** (`backtest`, `engine_simulation`): never two running at once, whatever the process — the queue chains them while lighter jobs keep flowing (enforced both at worker claim time and in the API-thread execution path).
- **Delta mode** (default when no candidate rule is evaluated): the two universes are identical except for the changed entities of the list under test, so the engine now runs **one full pass on the shared universe plus two tiny passes** — the removed/old versions (lost hits, production side) and the added/new versions (gained hits, candidate side). Alerts are counted as (client, entity) pairs over disjoint sets, so the numbers are **exactly** those of two full passes, in roughly half the screening time. Evaluating a candidate anti-FP rule (which can suppress pairs on unchanged entities) keeps the full two-pass mode. The report states `mode` and the delta sizes.

### Changed — the whole interface dropped emojis for a unified line-icon set
Every emoji in the application (about 450 occurrences across navigation, titles, buttons, tiles, toasts, the login page) was replaced by a **single monochrome SVG icon set** (stroke-based, inheriting the text colour, one inline sprite referenced by `<use>`) or removed where the wording alone is clearer. Language flags became plain codes, the anchor logo is an SVG, the theme toggle uses sun/moon icons. The i18n engine now generates **emoji-stripped aliases** of its historical dictionary keys at load time, so all six languages keep working without rewriting the ~800 emoji-bearing entries.

### Changed — the home grid packs densely and gained six more panels
- The dashboard grid now uses `grid-auto-flow: dense`: a small panel that follows a large one backfills the hole instead of leaving a gap in the row; cards stretch to equal row height.
- Six new panels in the catalogue: **Whitelist** and **Active rules** KPI tiles (asynchronous counters), **Latest screenings** and **Batch campaigns** tables, on top of the previous fifteen.

### Added — manual additions can target an existing list, one by one or in batch
Manual entry used to feed a single generic snapshot typed `WATCHLIST_EU`. Now:

- The form has a **target list** selector: with a list chosen, the entity lives in a dedicated `manual-watchlist-<type>` snapshot bearing that list's type — it counts in that list's filters, per-list cut-off thresholds and alert labels. Without a choice, the historical generic snapshot is used unchanged.
- **Batch add** (`POST /api/watchlist/entities/batch`, max 500): one line per entity in the UI (`Type;Primary name;Aliases;Country`); each line passes the quality gate individually — rejections are returned line by line and never block the others; one commit and one cache reload for the whole batch.
- Manual snapshots (generic and per-list) are **never superseded by synchronisations** — the sparing rule was generalised from the single historical id to the whole `manual-watchlist*` family (delta bases and backtest comparisons skip them too).

### Fixed — two synchronisation-screen bugs
- **Checkboxes no longer untick themselves** in the Automatic Synchronisation card: the background operations poll was rebuilding the whole table from server state every few seconds, wiping unsaved edits. The poll now patches only the per-source "State" cells; inputs survive until Save.
- **Sync reports no longer stay PENDING_REVIEW forever**: approving or rejecting the snapshot now settles the linked report (SUCCESS on approval, REJECTED on rejection) via its stored `snapshot_id`.
- Bonus root fix: the live-state cell matching compared `sync:OFACNONSDN`/`sync:ofac_nonsdn` against the server token `sync:ofacnonsdn` and silently never matched for multi-word sources; the comparison is now normalised (lowercase alphanumeric), so the State column works for every source in both tables.

### Changed — the Sources screen got a one-click global run and a compact layout
- **“Synchronize enabled sources”** launches every enabled source in one click (a source already running answers 409 and is simply counted as busy; the summary toast reports launched / busy / failed).
- The sources table is **dense**: one text-height per row, the long per-source descriptions moved to hover tooltips; the Automatic Synchronisation card and its intro texts were tightened for the same reason.

### Added — the home page became a dashboard each user composes
The overview tab was a fixed layout: six tiles, three charts, two lists, identical for everyone. It is now a **grid of panels each user arranges for themselves**.

- **A 🎛 Customize mode on the home page**: add panels from a gallery grouped by category (*Indicators* / *Charts* / *Tables*), remove them, cycle each through **three sizes** (small / half / full width), and **reorder by drag & drop**. Save, cancel, or reset to the shipped default at any time.
- **The layout is stored per user** (`user_dashboards` table, one row per account) through three endpoints: `GET /api/me/dashboard` (null = shipped default), `PUT` (validated: known sizes, no duplicates, sane panel identifiers, at most 30 panels — a corrupt layout is never stored), `DELETE` (reset = the row disappears). Layouts are strictly isolated between accounts.
- **A richer catalogue than the old fixed page**: the seven KPI tiles (screening, filtering, 4-eyes, approval queue, **SLA overdue**, false-positive rate, average decision time), the three native SVG charts, and five tables — oldest open alerts, **latest synchronizations** (now a list, not just the last one), **recent jobs**, **workload per analyst**, and **client data quality** (global completeness score, alert-threshold flag, three weakest fields). Every panel reuses an existing read endpoint; a broken panel logs and never takes the page down.
- Unknown panel identifiers from an older version are simply skipped at render; background refreshes never clobber an edit in progress.

### Changed — the recent-jobs panel of the notification centre reads in one glance
A multi-list production run used to flood the bell panel with six near-identical cards — each with a ✅ icon *and* a "Done" badge, a full timestamp, and the same @user on every line.

- **One line per job**: a coloured status dot (the status is stated once — green/red/amber), the label on a single line, and a **relative time** ("12 min ago") plus duration. The exact timestamp, duration and initiator moved to the tooltip.
- **Bursts are folded**: consecutive jobs of the same kind and outcome (e.g. *Mise en production × 6*) collapse into one expandable line; expanded children show only the distinguishing part of their label (the list name), since the common prefix is already in the header.
- **Failures stay loud**: errors are never grouped, keep their message inline and the one-click retry button. The panel itself got wider, scrolls beyond 74 vh, and the whole redesign is translated in the six languages (including "N min ago" patterns).
Follow-up to the automatic-synchronisation panel: an audit of `config.yaml` found eight more families that could only be changed by editing the file on the server and restarting. All eight are now **hot settings** (database over file — `config.yaml` only supplies first-boot defaults), each with an **admin-only card** and strict server-side validation, and every consumer reads the *effective* state:

- **🏛 Reporting institution (TRACFIN)** — name, SIREN (9-digit check), correspondent; feeds the suspicious-transaction draft. **🔐 Access security** — lockout failures/duration, minimum password length, session hours (deployment-bound `secure_cookies`/`SameSite` deliberately stay in the file). **📰 Adverse media** — enabled, language, max results, keywords (empty list falls back to the default AML/CFT set). **🤖 AI** — narrative reformulation and NL rule drafts: enabled + model each; enabling without a model is refused; the API key stays an environment variable, never entered in the UI. **📥 CFT inbox** — drop/archive directories (absolute paths enforced) and polling cadence, re-read every polling pass by both the API poller and the worker daemon. **🌐 Sync network** — shared timeouts, retries, backoff, User-Agent (per-source `config.yaml` overrides still win); read standalone by the HTTP helpers, so no signature changed. **⚙️ Scoring fine-tuning** — name-metric weights (all-zero refused: a blind engine) and contextual bonuses/maluses, injected into the same per-screening config dict as the hot thresholds, so the effect is immediate and the existing impact simulation applies. **📡 Outgoing notification webhooks** — URL list (http(s) only, max 20).
- **Deliberately left in the file** and stated as such: secrets (FSF token, `hooks.secret`, database credentials), process boot settings (`jobs.*`, `database.*`), per-source URLs (the diagnostic-tool workflow), deployment properties. Config export/import carries the new families **except** the CFT inbox paths — machine-specific paths must not travel between environments.
- One generic mechanism behind all of it: `_hot_section` (per-key merge, DB over file) plus `read_setting_standalone` for readers that get no session (auth policy, notification transport, sync network) — any failure falls back to the file, a setting can never break an execution path.

### Added — the notification journal is now managed: filter, delete, purge, resend
The delivery journal (`notification_deliveries`) was read-only: 30 rows, no way to remove an entry, no way to retry a failed send.

- **Server-side status/event filter** on `GET /api/notifications/log` (the filter covers the whole journal, not the loaded page), with a total count.
- **Single-entry deletion** (`DELETE /api/notifications/log/{id}`) and **bulk purge** (`POST …/purge` by status and/or age). **QUEUED rows are protected by default** — they carry the not-yet-sent digest; purging them requires asking for them explicitly. Both admin-only and written to the admin journal (`NOTIFICATION_DELETED`, `NOTIFICATIONS_PURGED` with the count).
- **Resend of a failure** (`POST …/{id}/resend`): recomposes the e-mail from the archived event and payload, sends now, updates the row (SENT/FAILED + timestamp). A SENT entry is refused — duplicating a step e-mail sows confusion.
- The journal screen gained the matching controls: status filter, 🗑 per row, 🔁 on failures, a purge button with confirmation.
- Locked by 18 tests (hot overlays reaching each consumer, validations, `403` for every non-admin role with nothing written, QUEUED protection, purge by age, resend updating the row) and a browser pass over every new card with zero console errors.

### Added — automatic synchronisation is driven from the application, admin-only
Turning scheduled fetches on or off — or excluding a single source from them — required editing `config.yaml` **on the server** and restarting. On shared hosting that is a deployment, not an operation. It is now a control panel inside the app, reserved to administrators.

- **A new admin card, ⏰ Automatic Synchronisation** (Lists → Automatic sources): a master switch, then per source its participation in scheduled fetches and its own cron expression, saved in one call. The whole card is **hidden for any non-admin role** and the endpoint refuses the write with `403` — the guard is on the server, not just in the UI.
- **`sync.auto_enabled` and `sync.<source>.enabled` became hot settings** (database over `config.yaml`, which now only supplies first-boot defaults). `get_sync_config(db)` returns the effective state and is what the schedulers read; called without a session it still returns the file-only view, so internal callers (network parameters, source URLs) never touch the database.
- **Genuinely hot, with no restart.** The thread-mode scheduler loop now always starts and re-reads the switch at each tick — starting it conditionally froze the boot-time value, so enabling synchronisation from the app would have done nothing until the next restart. The worker daemon already re-read it every tick.
- **Provenance is shown**: each value says whether it comes from the application (overridden) or is still inherited from `config.yaml`. Every change is written to the admin journal (`SETTINGS_UPDATED`, target `sync.automation`) with before/after.
- **Cutting the automation never amputates the operator**: "Synchronise" stays available on every source, including those excluded from the schedule — a manual run is an explicit, traced act.
- Locked by 11 tests: hot override beats the file and falls back to it, `get_sync_config` overlay only with a session, **`403` for user / reviewer / blocking / auditor with nothing written**, partial updates leaving the rest untouched, unknown source and bad cron rejected, and the scheduler actually honouring both the switch and per-source state. Verified in a browser on both paths: admin sees the card (26 source toggles + 26 cron fields, a real save flipping the state), a non-admin analyst does not see it at all and gets `403` on a forced request.

### Changed — after a sync, the review opens ready to decide
Requested flow: sync a list → delta against production → the review shows that delta **instantly**, the test book has **already run** on a test panel, and the reviewer just decides.

- **The delta is served, not recomputed.** The sync already computed and stored the delta (`SyncReport.delta_report`, with the production snapshot it was compared against); the review screen nevertheless reloaded *every entity of both snapshots* to recompute it on the fly — the slowness the user felt. `GET /api/review/snapshots/{id}` now serves the stored delta directly when its comparison base is still the current production (`delta_source: "stored"` — zero entities loaded, instant on any list size). If production changed since the sync, or for a manual import that has no SyncReport, it recomputes as before (`"computed"`) — the displayed delta never lies about what approval will actually change. The screen states which of the two it is showing.
- **The test book runs itself.** When a sync ends held for approval with a non-empty delta, the backtest job is submitted automatically (same token and dedupe key as the manual button, so the two can never run twice in parallel). The panel is the one forced by the new hot setting, else the most recent *generated* pseudo-client panel — never the real client base by default: a full A/B screening of 750k clients must not trigger itself. No usable panel, setting off, empty delta, job already running: the automatism abstains and says why in the sync report, it never breaks the sync.
- **The pending queue shows what is ready to decide**: a "Test book" column with the verdict (✔ OK / ⚠ with the gap), "⏳ running" while the auto-run is in flight, "—" otherwise. Opening the review reconnects to a running backtest as it already did for manual runs.
- **Settings** (hot, admin): `review.auto_backtest_enabled` (default on) and `review.auto_backtest_panel` (a READY panel, validated; empty = latest generated panel). The boolean is portable via config export; the panel id is not (it names a snapshot that only exists in one installation).
- Locked by 10 tests: stored delta served with `calculate_delta` provably never called, stale-base and manual-import fallbacks, panel resolution (forced beats latest), the four abstention paths, dedupe key parity with the manual flow, settings exposure and validation.

### Fixed — automatic syncs stuck at QUEUED are now visible and recoverable
In production (`jobs.mode: worker`), every heavy operation — including scheduled synchronisations — runs in a separate **worker daemon**, never in the API process. If that daemon is not alive, a submitted job sits `QUEUED` forever and **nothing on screen said why**. Reported symptom: "the automatic synchronisation goes to QUEUED but never starts." Reproduced exactly: no live daemon ⇒ `QUEUED` indefinitely; start the daemon ⇒ claimed within seconds.

- **The failure is now surfaced.** `GET /api/worker/status` reports the daemon's heartbeat freshness, the queue depth, and the last autostart attempt. A red banner appears for everyone the moment a daemon is *required* (worker mode), its heartbeat is stale (> 120 s), **and** work is waiting — "The processing daemon is stopped: N queued operation(s) will not start" — polled every 30 s. Silent `QUEUED` is gone.
- **One-click recovery.** `POST /api/worker/restart` (admin, logged to the admin journal) relaunches the daemon; the banner carries a **Restart the daemon** button for admins. The `flock` singleton makes the relaunch harmless — a surplus launch just exits.
- **Autostart is no longer invisible when it fails.** `ensure_worker` now passes the environment explicitly and records each attempt (success/failure + interpreter used) to `jobs.worker_autostart` — a `subprocess` refused by the host is diagnosable instead of a mystery.
- **Durable net for shared hosting (cPanel/Passenger), documented.** The API's autostart is best-effort — under Passenger the API only runs while there is traffic, so a daemon that dies overnight only restarts on the next visit. The README now gives the reliable fix: a **cron launcher every 5 minutes** (`python -m fiskr.worker`), made safe by the flock so there is never a second daemon. This decouples the daemon's life from web traffic.
- Locked by tests: status shape, the down condition (worker mode + stale heartbeat + queued), a fresh heartbeat never being "down", and the admin gate on restart.

### Added — eleven more public sources, from a registry instead of eleven copies
Coverage grew from 15 to 26 official lists: multilateral development bank debarments (ADB, IADB, EBRD, AfDB), Asia-Pacific freezes (Japan MOF, Singapore MAS, New Zealand), national counter-terrorism freeze lists (Netherlands, Belgium, Israel NBCTF) and Ukrainian NSDC sanctions.

- **One declarative registry, not eleven connectors.** All eleven ride the `targets.simple.csv` reader that PEP and SECO already use — so `fiskr/sources.py` declares each source (dataset slug, short name, list type, id prefix, family, legal-basis note) and everything else derives from it: the sync config, the runner (`make_opensanctions_runner`), the API alias, the scheduler entry, the manual-upload branch, and the 400 error message that used to hard-code the source list. Adding a twelfth is one registry line.
- **Each source keeps its own list type**, so a bank debarment, a national terrorism designation and a Ukrainian sanction can be thresholded apart — the same design point as the HK SFC / AMF / World Bank alert lists.
- **The flat-format limit is stated, not hidden** (as it already was for PEP and SECO): the OpenSanctions simple format carries no legal basis or official reference. Each registry entry records the issuer's native official URL for a future "official" path (the SECO pattern), and the config, README and premium-sources doc say so.
- **Slugs are verified from the deployment, not guessed here.** `tools/diagnostic_sources.py` now probes each dataset and judges on content (the `id,schema,name` header); a wrong slug is a one-line URL fix in `config.yaml`, never a code change. 17 tests run parameterized over the whole registry, so a source added tomorrow inherits every guarantee with no new test.

### Added — a path for paid data sources, documented and technically primed
`Documentation/SOURCES_PREMIUM.md` covers what Dow Jones/Factiva, LSEG World-Check, LexisNexis, Moody's GRID, ComplyAdvantage and the OpenSanctions commercial licence each add beyond the public lists, what to buy and whom to contact, and the state of the wiring. The one immediately actionable item is the OpenSanctions commercial licence (self-service) — the connector already exists. For the API-key providers, the common prerequisite ships now: `download_to_file`/`http_get_text` accept custom headers, and `sync.<source>.auth_headers` reads `${VAR}` secrets from `.env` — the day a contract lands, only the connector remains to write.

### Changed — the sources screen and generic table filters
- **The sources screen stopped being the worst screen in the app.** It stacked one card per source (16, heading for 27); it is now a single table grouped by nature (official sanctions / regulator alerts / multilateral debarments / aggregated data), filterable by search, family and enablement state. A shared `SOURCE_CATALOG` in the front end became the single source of truth for source labels and the cron scheduler; `handleSourceSync` no longer depends on per-source static buttons, and the EUR-Lex date is offered on click rather than as a permanent field. Verified live with a browser: 26 sources across 4 groups, family and search filters working.
- **A generic client-side table filter** (`attachTableFilters`) gives a full-text search plus column drop-downs (values inferred from the columns) to the tables that had none — sync reports, users, API keys, the admin journal, batch campaigns, several KPI tables, the mining and rules tables — with a "shown / total" counter and a `MutationObserver` so any re-render refreshes the options. It combines with the existing generic column sort and hides group headers whose group is emptied by the filter. Paginated tables keep server-side filters instead (a client filter would only see the loaded page): `GET /api/sync/reports` now accepts `source` and `status`, locked by test.

### Changed — the two remaining over-long screens were split, not just folded
The user's read was that "some tabs are unclear or too long." Three screens were the worst; the sources screen was the first (above). The other two are now cut along their natural seams, with no function moved between screens and role-masking preserved.

- **"🔑 Blocking Keys" became two honest tabs.** The tab bundled eleven heterogeneous blocks under one ambiguous name. It is now **"🔑 Paramétrage moteur"** (the two channel blocking keys plus the engine-capabilities panel that used to be its own "🧠 Algorithmes du Moteur" tab — same `blocking` role, so they belong together) and **"🗣 Ressources linguistiques"** (equivalence tables, the impact simulator, the term diagnostic and the nightly homonym mining). The old standalone engine tab is gone, merged into the first. Both new tabs sit behind the same role gate the blocking tab always had.
- **The KPI screen, ten blocks with no sub-navigation, became four sub-tabs**: Vue d'ensemble (the pilotage tiles and their four tables), Charge & SLA (analyst workload), Qualité données (customer-data completeness) and Rapport d'activité (the regulatory period report). Only the overview loads on arrival; the other three fetch when their sub-tab is opened, so the screen is lighter to land on. Deep-link hashes route to the new sub-tabs automatically.
- **The homologation review, already a step-by-step wizard, leads with the action.** Step 3 (the test book) stacked panel selection, panel generation, size and a candidate-rule picker before the "run" button. The primary path — pick a panel, run — is now first; generating a new panel and choosing a candidate rule fold into a "⚙️ Options avancées" accordion (the pattern already used elsewhere on the screen).

### Added — server-side filters the API already accepted, now reachable from the screens (F2)
- **Alert queues gained an "assigned analyst" filter.** `GET /api/alerts` already honoured `assigned_to`; both the screening and filtering queues now carry an analyst drop-down (populated from `/api/users/directory`) that drives it, alongside the existing list-type and priority filters.
- **The audit journal gained a date window.** `GET /api/history` and `GET /api/export/history.csv` now accept `date_from` / `date_to` (`YYYY-MM-DD`, inclusive bounds, `400` on a malformed value) and the audit screen exposes two date inputs that filter the paginated journal on the server — so the window covers the whole history, not just the loaded page. The CSV export honours the same window. Locked by tests.

### Changed — EUR-Lex became an early-warning signal instead of a source of designations
Reported symptom: "I still have trouble retrieving some sources, EUR-Lex in particular — is there a workaround for the anti-bot?" A diagnostic run **from the production server** answered the question differently than expected, and the answer changed the design.

- **The anti-bot hypothesis was wrong.** `tools/diagnostic_sources.py` (new) probes each channel from the machine that actually runs the syncs — the result depends on the outbound IP and the host's filtering, so it cannot be inferred from a developer workstation. From the production server the daily OJ page answers **HTTP 200 in 0.48 s**, no interstitial. The retrieval problem is *intermittent*, not a hard block, and part of what looked like "no data retrieved" is in fact `run_eurlex_sync` reporting `NO_CHANGE — "N acts found but no listed entity extracted"`: an **extraction** failure, not a network one. The annexes' layout varies from one regulation to the next and the heuristics miss it.
- **No workaround was built, and none is needed.** Evading a bot filter is a losing strategy for a regulated screening product — it breaks at the first change upstream, and a source that fails silently is a compliance risk. The EU already publishes what Fiskr needs through a machine-to-machine channel with no anti-bot at all: the **consolidated financial sanctions list (FSF)**, which the codebase has supported all along and which is authoritative — it carries **delistings**, which the OJ scraping never applied (a delisted person stayed screened forever).
- **`sync.eurlex.mode`, defaulting to `alert`.** EUR-Lex now reports that an act on restrictive measures was published, archives its official PDF with a SHA-256 fingerprint (the probative record for an audit), notifies reviewers immediately — and **writes no entity at all**. One HTTP request per day instead of one per act. The historical behaviour remains available as `mode: "extract"` so an installation without an FSF token is not left without a source; its docstring now states plainly that what comes out are *suppositions* — a regular expression decides whether a string is a person's name.
- **A missing consolidated source is now said out loud.** If `eu_fsf` is disabled, the alert-mode report and the notification both state that these designations will enter no list, with the remedy. A silent coverage gap is the worst of both worlds; this is the guard-rail that makes the demotion safe.
- **Locked by tests**: the alert mode never calls the scraper and creates no snapshot or entity; the official PDF is still archived with its fingerprint; the warning fires when FSF is off; a single request is made for the daily page.

### Added — the HTTP layer stopped provoking the throttling it then had to survive
Both fixes address the *intermittence* the diagnostic actually revealed, and both make Fiskr a better-behaved client of official portals — which is what reduces blocking in the first place.

- **`Retry-After` is honoured.** The retry loop replayed on its own schedule (linear backoff) and ignored the header telling it when the server would accept traffic again. It now waits what the server asks, in seconds or as an HTTP date, **capped at 300 s** so a hostile or absurd value cannot pin a background job for hours.
- **Conditional requests.** Every sync re-downloaded the whole file even when nothing had changed. Downloads now send `If-None-Match` / `If-Modified-Since` from validators stored **per source in the database** — so the worker daemon, the API processes and a manual re-run share one view of freshness — and a `304` short-circuits to `NO_CHANGE` with nothing downloaded and nothing parsed. A source that stops advertising validators has its entry cleared rather than conditioned on a stale one.

### Added — a source diagnostic tool that judges on content, not on status codes
`tools/diagnostic_sources.py` probes the EUR-Lex scraping path, the Publications Office machine channels (SPARQL Cellar, REST notice, FORMEX XML), the OJ RSS feed, the FSF list and every other wired source. Read-only: no database writes, no snapshots, no side effects.

Its second version exists because the first one was too lenient, and on an architecture decision that matters: a **`HTTP 300` is not a success** — Cellar returns the list of available representations, not the document — and a 234-byte body containing the word "rss" is not a feed. The tool now follows the `300` through to a real manifestation, sends a **real** SPARQL query (the one that would replace the daily-page scraping, not a trivial `SELECT`), verifies the shape of what came back, and prints body samples with `--bodies`. It states explicitly whether the groundwork for a Cellar-based rewrite is available or incomplete, so that decision rests on evidence rather than on a green checkmark that meant less than it looked.

### Added — a persistent job queue and a worker daemon: no heavy computation ever runs in the API process again
The previous fix (202 + WAL + GIL yields) made long operations *background*, but they still ran **inside the API process** — a pure-Python CPU loop degrades every request through the GIL (measured ×6-11 on p50), and the test book on a large universe **did not finish at all**. The root cause of "never finishes" was memory, not time: a full entity dict weighs ~8.4 KB, so a 750,000-record PEP universe costs ~6.3 GB — and `run_backtest` held **two universes at once**, ~12.6 GB on a 16 GB server. Process separation and memory discipline were the only real fixes; both shipped together.

- **A persistent job queue in PostgreSQL (`jobs` table, `fiskr/jobs.py`).** Every heavy path — test book, approval follow-ups, source synchronisations, imports, lookback, both impact simulations, mining, batch campaigns, rule benches, panel generation, the client-quality check — is now a named task with JSON parameters, claimed via `SELECT … FOR UPDATE SKIP LOCKED`, with per-operation exclusivity (`dedupe_key` → 409), heartbeats, persisted progress and persisted results (readable after a restart — simulation reports no longer die with a 15-minute in-memory TTL). Three execution modes: `worker` (production daemon), `thread` (degraded fallback, the previous behaviour), `eager` (inline, tests) — the existing test suite runs unmodified under `eager`.
- **A worker daemon (`python -m fiskr.worker`) owns all computation.** Unique by construction (flock, released by the kernel on death — no stale PID files), started **by the API itself** when its heartbeat is missing (at startup, on every job submission, and by a 60 s watchdog): no systemd needed, which is the reality of shared Passenger hosting. It also hosts **all periodic schedulers** (cron syncs, CFT inbox, digest, retention, notification batches, mining) — under Passenger, N API processes used to mean N schedulers firing N times; now exactly one.
- **Automatic resume, bounded.** A job interrupted by a kill keeps its row; the next daemon start re-queues it (restart from zero, capped by `attempts`); beyond the cap it lands in **ERROR with a one-click retry** in the new « Travaux » section of the notification centre (admin-only, logged to the admin journal). A queue without an executor does not lie: in `thread`/`eager` deployments, orphaned rows are marked ERROR at startup rather than left pretending to run.
- **Memory projection + sequential passes + a fork() pool.** The screening engine reads ~24 entity fields; loading only those (plus any column referenced by an active anti-FP rule, found by lexical scan of the rule code) cuts an entity to ~3.8 KB — 2.8 GB per 750k universe instead of 6.3. `run_backtest` now loads the current universe, runs pass 1, **frees it**, then loads the candidate: never two universes in memory again. Within a pass, the panel is split into id-range slices screened by `fork()`ed children sharing the parent's universe copy-on-write (`gc.freeze()`, children strictly read-only, one short-lived DB connection each); process count is bounded by CPU **and** a memory budget. **Parallel output is proven equal to sequential output by test** — same pairs, same scores, same order.
- **Cross-process cache invalidation by epoch.** The daemon cannot touch an API process's memory, so anything that changes production bumps `watchlist.epoch` in the database; every API process checks it (throttled) and reloads its own cache. This is also what makes **multiple API processes safe** — the old "one worker only" restriction now applies only to `thread` mode.
- **Two latent bugs fixed on the way.** The audit trail written by re-screening running in the daemon imported `watchlist_version/hash` from the API module's globals — it would have recorded "N/A" in an immutable regulatory log; the reference is now derived from the database (`production_watchlist_reference`). And SQLAlchemy was free to flush `client_entities`/`watchlist_entities` before their `snapshots` row inside one commit — masked for years by SQLite's unenforced foreign keys, exposed as `ForeignKeyViolation` by PostgreSQL, fixed by declaring the relationships the ORM needs for ordering.
- **Measured on the target scenario, PostgreSQL 16, 4 vCPU / 16 GB.** Synthetic PEP universe of **750,000 records in each of the two passes**, 2,000-client panel. The test book **finished in 751 s — including a deliberate `kill -9` of the daemon mid-screening**: the watchdog revived it, the job restarted from zero (`attempts=2`) and completed, verdict OK. During the entire run the API was hammered continuously: **12,550 requests, 0 errors, p50 8.6 ms / p95 15.0 ms / max 76 ms**, against 9.1 ms / 16.2 ms / 33.5 ms measured at rest before the run started. Under the previous architecture this workload did not complete at all; under the new one the API under full computation is **indistinguishable from an idle server**.
- 736 tests green against a real local PostgreSQL 16 cluster (the SQLite fallback no longer reflects production and is kept for dev only), including new suites for the queue (claim, requeue, dedupe, retry, purge), the projection (full-vs-projected equivalence, rule-column re-inclusion, a sentinel that re-derives read fields from the engine source), the fork pool (parallel == sequential, chunk failure propagation) and the daemon (flock, epoch, audit reference).

### Added — the engine's algorithms became settings instead of hard-wired code
Some forty matching mechanisms — transliteration, phonetics, linguistic equivalences, reversed name order, geographic adjustments, thirteen hard matches on identifiers — were **hard-wired**. A compliance officer could neither see them, switch them off, nor measure what each one contributes. The ACPR expects a screening system that is *documented and justified*; "the engine does transliteration" is not an answer.

- **A declarative catalogue, `fiskr/capabilities.py`.** Thirty-four capabilities in five families, settable **per channel** (client screening / transaction filtering) and **hot**. Same pattern as `fiskr/events.py`: a dependency-free module that is the single source of truth read by the settings, the API, the screen and the traceability. Adding one pilotable mechanism = adding **one entry**. The `loss` field — what the institution loses by switching it off — is **mandatory by construction**: you cannot add a toggle and forget to state its risk. Full inventory in **[Documentation/ALGORITHMES_DU_MOTEUR.md](Documentation/ALGORITHMES_DU_MOTEUR.md)**.
- **Defaults reproduce today's engine exactly.** Everything on, except the one capability that would *widen* the alert perimeter (see below). An existing installation does not change behaviour. The groundwork shipped with **581 tests green and not one existing test modified** — that was its acceptance criterion.
- **Ten toggles per script, which was previously impossible.** `quality.has_non_latin_chars` was **binary** — latin or not, on a code-point threshold — and **no code in the repository named a script**. Treating Cyrillic differently from Chinese could not be expressed. `quality.detect_scripts` classifies by Unicode range, with no new dependency; anything outside a declared range falls into `other`, which has its own toggle, so **no script can escape the setting through a forgotten range** (a test locks the detector and the catalogue together). The transliteration perimeter itself **did not move by one character**: the historical criterion remains the sole judge of "should this character be transliterated", the script naming comes after.
- **`strip_accents` stays unconditional**, and that is not an oversight: it builds the resources index and serves the API search. Making it settable would cost the index its own entries at the first setting change. The settable variant, `strip_accents_for_matching`, is reserved for comparison.
- **Ingestion normalisation stays unconditional too.** What is stored is filed with its list snapshot; making it depend on a hot setting would normalise two records of the same list differently depending on the hour of their import. Consequence, stated in the catalogue and in the documentation: a script capability takes effect immediately on the **client** probe, and on listed records **at the next full reload of their list**.
- **A finding recorded as a test.** Switching a script off does *not* make the engine wholly blind: the equivalence tables catch the names they know — « Владимир » is listed there with « Vladimir » — and the match survives without transliteration. The loss is real but **uneven**, limited to names not on file. This is why impact is measured on a panel rather than reasoned about, and the ten `loss` texts now say so.
- **A capability whose prerequisite is off is reported as inert** rather than left to look as if it acts — the trap already met on the resources: a table wired only into scoring changes nothing without the matching blocking key. Cutting `blocking.equivalences` makes the resource tables inert even when enabled, and a test locks it.
- **Phonetics and equivalences were separated.** Both came out of the *same branch of code* in `blocking.py`: cutting one cut the other. Two tests now lock the separation in both directions.
- **A missing country can be made neutral.** Today the absence of a country on one side is worth **malus −10** — absence of information treated as contrary information. A poorly filled client reference sees its scores drop with no data to justify it. `adjust.geography.missing_is_neutral` fixes that, and ships **inactive**: it widens the alert perimeter, so it is measured before it is applied.
- **Traceability that survives the years.** `decision_tree.capabilities_applied` records the channel, what is disabled, what is enabled beyond the defaults and what is inert — on **all three exits** of `match_entities`, because a 100/100 hard-match alert must be as explainable as a fuzzy one. The key appears **only when the engine departs from the catalogue defaults**: a standard installation produces exactly the decision tree it produced before. The setting lives in the database and is not copied into the `config_state` frozen at screening time; without this trace, an inspector reading a 2026 alert in 2029 could not know which mechanisms were running. Each write also produces an `ENGINE_UPDATED` line in the administration journal: who, when, before → after.
- **Measure before you decide.** `POST /api/settings/engine/simulate` (202 + `job_token`) screens the same panel against the same universe twice, under the setting in force and under the candidate, and returns the gap: volumes, interception rates, breakdown by list, and **the lost pairs one by one** — here the direction that matters is the opposite of the equivalences', since cutting *loses* alerts. No writes at all. Both passes run under a **thread-local** override, so a measurement running in the background changes nothing for screenings served in parallel — locked by test.
- **`GET`/`PUT /api/settings/engine`** under the `blocking` role, with the same **double invalidation** as the resources: the capability context **and** the index cache. The index freezes its blocking keys at load; without a reload, only the client probe would change, the two sides would never meet and the setting would have no visible effect. The setting is in `_PORTABLE_SETTINGS`, so it crosses staging → production with the configuration export.
- **Screen:** *Alerts → 🧠 Engine Algorithms*, next to Blocking Keys, same role, **entirely generated from the catalogue**. Each toggle displays what is lost, cutting one raises an explicit confirmation listing the losses, and an inert capability is flagged as such. Five languages.
- **Not exposed, deliberately: the metric weights.** They are not normalised (`compute_base_score` is a plain sum). Setting a weight to zero does not neutralise the metric — it **changes the score scale** and invalidates every threshold. Offering them as switches would be a trap; they stay in `config.yaml`.

### Fixed — the application stopped answering while a long operation was running
Launching a source synchronisation or an approval test book left the screen inert: no data came back at all, **including the operation's own progress**. Navigation still worked, which proves nothing — it is purely client-side. Three distinct causes, none of which would have been enough on its own.

- **The synchronisation ran inside the HTTP request.** `POST /api/sync/run` chained download, parsing, ingestion of tens of thousands of records, delta, cache reload and re-screening **with the request's own session** — several minutes. This was exactly the defect fixed for the test book a while ago, whose docstring already said in so many words that running it in-request *"froze the whole application"*; the synchronisation was the last long path never migrated. It now answers **202** with a token and works in the background through `_start_job`, with its own session. **Refusals stay synchronous** — unknown source or malformed date (**400**) — so nothing starts on an invalid request, and a **per-source lock shared with the cron scheduler** refuses a second concurrent run (**409**): two ingestions of the same list would otherwise overwrite each other. The report is published on the token and still archived in the database.
- **SQLite blocked every reader while writing.** The engine was created with no settings, so in the default `DELETE` journal mode a writer locks the *entire file*. Since every route touches the database — the authentication dependency reads it itself — the whole API became unreachable, `/api/progress` included: hence "nothing refreshes". The engine now switches to **WAL** (readers are never blocked by the writer), with `busy_timeout` and `synchronous=NORMAL`. No effect on PostgreSQL, which does not have the problem.
- **The dry-run screening held both the database and the GIL.** It queried the whitelist **once per alerting client**; those pairs are now loaded in a **single query**, the rules are detached from the session (a `rollback()` expires ORM objects — without this the loop would have reloaded every rule from the database on each client, making things worse), and the read transaction is released before the computation. The loop no longer touches the database at all. That fix has a counter-intuitive consequence, and the measurement is what exposed it: **those very queries were yielding the GIL by accident**. With them gone, a deliberate yield every 25 records replaces them — in the dry run and in the re-screening alike, decoupled from the progress tick, which is far too coarse (several seconds can pass between two ticks).
- **Measured before and after, and the numbers do not say what the diagnosis expected.** At rest the API answers in **10.9 ms** (p50). Two scenarios were run on the unmodified code and on the fixed code, same load, same database, one variable at a time.
  - *Test book*, 4 000 records × 1 200 clients, unmodified code: the API **still answered** — 65 ms p50, 101 ms p95, **zero errors**. Six to eight times slower than at rest, real degradation, but not a freeze.
  - *Ingestion + post-delta re-screening*, 5 000 records against 400 clients, a 288 s operation: **before** 112.6 ms p50 / 180.1 ms p95 / **850.9 ms max**, 0 errors out of 786 requests; **after** 124.8 ms p50 / 208.1 ms p95 / **413.2 ms max**, 0 errors out of 764. The **worst case is halved** — that is the measurable gain — but the median and the p95 are **slightly worse** (+11 %), which is the price of the GIL yields. Both figures are reported because both are true.
  - **The total freeze was never reproduced in this environment**, on either version. FastAPI serves `def` endpoints from a 40-thread pool, so one multi-minute request occupies one thread out of forty and the others keep answering. What the fix removes is structural — a five-minute HTTP request, a whole-file write lock, a computation that never yields — and it is proven by the test that shows the POST returning in under a second while the cycle takes 1.5 s, not by these percentiles. Anyone who reads "the freeze is fixed" into this entry is reading more than was measured.
- **An operational guard-rail, written down.** Raising `--workers` is *not* the remedy and would degrade the product: the in-memory list cache and the progress registry are **per process**, so one request in two would see a different cache and the progress badge would flicker depending on which worker answered.

### Fixed — payment parties written in a non-latin script were unreachable in filtering
The filtering index was built with `generate_blocking_keys`, but payment parties queried it with a **hand-rolled implementation** that diverged from it on three points, all lossy.

- **No transliteration on the query side.** Double metaphone only knows the latin alphabet: on « ВЛАДИМИР ПУТИН » it returns an empty key, the party fell into the `XX` bucket and was a candidate for **nothing** — while the index had duly transliterated the record. Same nature of defect as the "unknown country" hole fixed above, on a channel where names in their original script are common.
- **No equivalence keys**, so the linguistic tables were inert on this channel whatever their setting; and **no engine capabilities**, which would have made the per-channel setting a hollow promise.
- `party_blocking_keys` now goes through `lookup_blocking_keys`, like the index it queries. The filtering-specific properties are kept: both PP and PM natures are queried, and word order is not assumed reliable — name rotations guarantee every word of the free-text field is seen at least once as the first word, which reproduces the phonetics of all words without duplicating the key-generation code. A test verifies on a panel that nothing the old implementation reached is lost.
- **Limit measured and recorded as a test** rather than discovered in production: **han, hangul and arabic still do not cross the blocking**, and this is independent of the setting. Transliterating a syllabic script yields a *single* word (« 习近平 » → « XiJinPing ») where the list carries « Xi Jinping »; and arabic does not write short vowels, « محمد » yields « mhmd » against « Mohammed ». Cyrillic and greek, being alphabetic, cross without difficulty. The limit also applies to the screening blocking.
- The **Spark batch path** is declared out of scope, in the module itself: its UDF re-implements scoring by hand, ignores geography, hard-codes the threshold at 75.0 regardless of the per-list thresholds set hot, and does not go through `match_entities` — no hard match, no name variants, no capabilities. It has no caller in the repository, so putting it back inside the engine would be a cost with no use. It is documented, not disguised.

### Fixed — a listed record without a country was structurally unreachable
- **The hole.** The screening index is built from the **listed** records' blocking keys; the client queries it with its own. `COUNTRY_ISO` being a key component, a listed record whose source publishes no country falls into the `XX` partition — which **no client that has a country ever joins**. Verified directly: the record emits `XX_PM_KLTN`, a Hong Kong client emits `HK_PM_KLTN`, intersection empty. Such a record could only be found by a client that also had no country, i.e. by a gap in the institution's own reference data. This is not an edge case: regulator alert lists almost never publish a country, EUR-Lex scrapes records with no geography, the CSL carries some.
- **The fix sits on the query side, not the index side.** The client now also queries the "unknown country" variant of its own keys (`blocking.lookup_blocking_keys`, used by screening, rescreening, backtest and batch). Two properties make this acceptable, and both are tested: it is **strictly additive** — every key queried yesterday is still queried, so **no alert can be lost** — and **partitioning is preserved**: a listed record that *does* carry a country is still reached only by clients of that country.
- **Measured** on a 5 000-record / 2 000-client universe, by share of country-less records:

  | country-less share | candidates/client before | after | overhead | country-less records reachable |
  |---|---|---|---|---|
  | 2 % | 16.09 | 20.02 | +24 % | 0 → 100 % |
  | 5 % | 15.80 | 26.00 | +65 % | 0 → 100 % |
  | 10 % | 15.01 | 34.89 | +133 % | 0 → 100 % |
  | 20 % | 13.36 | 54.20 | +306 % | 0 → 100 % |

  Coverage of those records goes from **zero to complete** in all four cases, and the cost is proportional to the share — you only pay for what you enable, since sanctions lists publish a country and regulator alert lists almost never do. `blocking.country_wildcard` (default `true`) switches it off for an institution whose sources all carry geography.
- **A patch removed on the way.** The alert-list parsers first filled in the regulator's own jurisdiction to escape the `XX` partition. Once the engine-level fix existed, that patch became actively harmful: it **narrowed** reachability — an entity flagged by the SFC would have been visible only to Hong Kong clients, when a fraudulent broker targets victims anywhere. No geography is invented any more; the flagging authority stays in `designating_state`, which exists for that.

### Added — regulator alert lists and development-bank debarments
A new **category** of source, not just more of the same. HK-SFC was asked for; the other two follow from the same reader and the same reasoning.

- **Hong Kong SFC alert list** (`WATCHLIST_HK_SFC`, `run_hk_sfc_sync`) — unauthorised entities, fraudulent websites and impersonations of licensed intermediaries. Hong Kong has **no autonomous sanctions regime** (UN measures are transposed by ordinance), so this list is what provides coverage of its own.
- **AMF blacklists** (`WATCHLIST_AMF`, `run_amf_sync`) — operators and sites offering investment services without authorisation. Same nature, and the alert list a French regulated institution has the most direct use for.
- **World Bank debarred firms** (`WATCHLIST_WORLDBANK`, `run_worldbank_sync`) — a third nature again: neither a freeze nor a warning, but a **time-limited debarment** for established fraud or corruption. Its end date feeds `delisted_on`, the column that already exists for exactly that.
- **The design point that matters: an alert is not a freeze.** A hit on a regulator's warning list is a risk signal to investigate, not an asset-freezing obligation. Each list therefore keeps its **own list type** — hence its own threshold via `scoring.cut_off_overrides`, its own counters, its own queue — instead of being poured into the same stream as the freeze lists. No default override is shipped: that is a screening-parameter decision for the institution, and this project does not change screening parameters silently.
- The two regulator lists share one reader, because they share three properties: published as a **table** (usually HTML), no common column convention between regulators, and **no technical identifier**. Columns are looked up by normalised form under several English and French spellings; the HTML reader picks the page's **largest** table, which is the most robust way to skip layout tables without knowing the page; and keys derive from a stable hash of name + domain, so two homonyms on two domains stay two records.

### Fixed
- **The file format is now detected from the content, not from the URL's extension.** Regulators serve their list at an address with no extension at all. Trusting the name would read a CSV as a web page, and the import would come out at **zero records** — that is, silently, wearing the appearance of an empty list rather than an error, which in a screening engine is the worst possible failure mode. The first bytes decide: `PK\x03\x04` → workbook, `{`/`[` → JSON, `<` → HTML, otherwise a delimited table.
- **A record with no country was structurally unreachable.** `COUNTRY_ISO` is a blocking-key component and a record without a country falls into the `XX` partition, which **no client that has a country ever joins** — verified directly: a listed entity with no country emits `XX_PM_KLTN`, a Hong Kong client emits `HK_PM_KLTN`, intersection empty. Alert lists rarely publish a country, so every one of their records would have been dead on arrival. The **regulator's own jurisdiction** now fills in when the source is silent, which is also factually right: an entity the SFC warns about operates on the Hong Kong market by construction.
  - Worth flagging beyond this batch: the same trap applies to **any** listed record without a country, from any source. Making `XX` a wildcard that pairs with everything would close it, but that changes screening behaviour and belongs in its own measured batch (the test book quantifies exactly this kind of change), not as a rider on a connector.
- **`Personne morale` contains `person`.** The first draft of the entity-type test classified every French company as a natural person. Since the entity type is a blocking-key component, the error would have **discarded candidates rather than showing up** as a wrong label. Legal-person spellings are now tested first, and the ambiguous bare `person` fragment is gone.

### Added — Canadian and Australian national lists
The two candidates named as "next up" in the previous batch, now shipped. Both are published as **tables**, not structured schemas, and their column headings move between versions — the Canadian one exists in two languages on top of that. So both readers look a column up by its **normalised form** (no case, no accents, no separators) and accept several spellings per field; that tolerance is the most fragile property of the pair, and it is what the tests pin down.

- **Canada — SEMA autonomous sanctions** (`WATCHLIST_CANADA`, `run_canada_sync`). Canada designates **autonomously**: its perimeter overlaps neither the EU's nor OFAC's, so this is coverage nothing else in the app provides. The English and French editions of the CSV both parse — a download from the francophone page must not yield an empty list, and there is a test asserting the two produce identical output.
  - **The file carries no technical identifier.** Left alone, every publication would look like a wholesale replacement of the previous one and the delta would be unreadable. The matching key is therefore rebuilt from the regulatory reference (schedule + item), which is also the right thing to show an auditor; two designations sharing a schedule/item keep distinct keys, and a row with neither falls back to a **stable** hash of the name — tested by parsing the same file twice and comparing.
- **Australia — DFAT consolidated list** (`WATCHLIST_DFAT`, `run_dfat_sync`). Combines transposed UN sanctions with Australia's own designations; the originating authority is preserved in `designating_state` depending on whether a UN committee is named, the same reasoning applied to SECO. Rows repeat per name variant and are grouped by reference, exactly like the UK `ConList` — an address or a date carried by an *alias* row still joins its group.
  - Read from **CSV natively or XLSX**, chosen by the URL's extension. `openpyxl` is an **optional** dependency handled like the existing `pypdf`: its absence does not stop Fiskr from starting, it only makes the XLSX route unavailable, and it fails with a message saying what to install rather than a stack trace. The workbook reader skips the title banner that official files put above the table.
- Same reservation as the rest of the batch: written from the published formats, validated on fixtures, **not against the real files** — no host is reachable from this development environment.

### Fixed
- **Three end-to-end tests could fail depending on what their neighbours left behind.** Each imported a list and then screened a homonym, asserting on the global `best_match`. With several such tests now in the suite, a marker from one could outscore the record under test in the other's screening — `IVANOVD784BC` losing to `IVANOVA04FF1`, the same shared-prefix fuzzy-fallback trap already diagnosed on `test_watchlist_db.py`. Each screening is now **restricted to the list under test** (`screening_lists`), which is also the more faithful assertion — the claim being tested is "this list intercepts", not "nothing else in the database resembles it" — and the Canadian fixture uses a surname deliberately unrelated to the others.

### Added — two more public sources, chosen for what they close rather than for the count
Both are public, free, key-free and machine-readable. Both are off by default: a new list widens the alert perimeter, which is a screening-parameter change and belongs in the test book before production.

- **OFAC Non-SDN consolidated list** (`WATCHLIST_OFAC_NONSDN`, `run_ofac_nonsdn_sync`) — OFAC publishes **two** files in the same "Advanced" format, and Fiskr only fetched one. The second carries the regimes that do **not** entail a full asset freeze and are therefore absent from the SDN: **sectoral sanctions** (SSI, the Russia directives), FSE, NS-MBS, PLC, MEU and CMIC. An institution with dollar exposure has to screen them; loading only the SDN leaves a hole.
  - The connector **re-parses nothing** — it reuses `parse_ofac_advanced_xml`, already tested against the real file structure — and changes exactly two things, both stated in the code: ids are prefixed `NONSDN-` because `entity_id` is the key alerts and the whitelist hang off and a collision would merge two distinct entities (the two files do not overlap today, but that is a property of the source, not a guarantee of the design), and inter-profile relations are not harvested, since the ownership graph is refreshed per source and mixing two differently-prefixed id spaces would leave dangling edges.
  - Kept as a **separate list**, not merged into `WATCHLIST_OFAC`, so it can carry its own threshold: the operational consequence of a sectoral hit is not that of a freeze.
- **US Consolidated Screening List** (`WATCHLIST_CSL`, `run_csl_sync`) — the International Trade Administration's official aggregate, public JSON, no key. Its point is **not** to re-serve the SDN, which Fiskr already fetches at source, but to bring the **export-control** lists that no other connected source carries: BIS **Entity List**, **Denied Persons**, **Unverified** and **Military End User**, plus State Department **ITAR Debarred** and **Nonproliferation**. These govern trade finance — a counterparty can sit on them without appearing on any asset-freeze list.
  - **Duplication is handled rather than left to the user**: `sync.csl.exclude_sources` drops the already-covered lists while reading, defaulting to the SDN. The distinction between *absent* (take the default) and *explicitly empty* (load everything) is deliberate and tested.
  - The **US list that designated the counterparty** is kept in `designation_reasons`, and the BIS licence requirement/policy in the additional information. Without them an analyst sees a hit and cannot tell which obligation it triggers.
- **Reservation, same as SECO**: no host is reachable from this development environment — `sanctionslistservice.ofac.treas.gov` and `api.trade.gov` are both refused, as are the hosts of the sources already shipped. The CSL parser is therefore written from the published format and validated on a fixture, not against the real file; every key is read with a default, so a missing or oddly-typed field skips rather than breaks (there is a test for exactly that). The Non-SDN connector carries **no such format risk**, which is why it was picked first. First real runs should go through the homologation gate.
- Also assessed and **deliberately not shipped**: Canada's SEMA consolidated list and Australia's DFAT list. Both are public, but their exact column layouts could not be verified without network access, and an unvalidated parser in a sanctions engine is worse than a missing source. They are the obvious next candidates once one real file can be inspected.

### Added — Swiss SECO list connector
- **`run_seco_sync` / `WATCHLIST_SECO`**, opt-in like OFSI and PEP. Two routes, selected by `sync.seco.format`, both producing the same pivot schema and therefore the same screening:
  - **`xml`** (default) — the Confederation's official **SESAM export**. This is the route that carries authority: free, no licence, and the only one holding the **Swiss legal basis** (the applicable `RS` ordinance, which becomes the entity's `official_reference`) and the **listing dates**.
  - **`opensanctions`** — the `ch_seco_sanctions` dataset in flat `targets.simple.csv` form, as a fallback. Same licence reservation as the PEP dataset: free for non-commercial use, licence required beyond. A `url` left empty picks the default matching the chosen format, so switching route is a one-word change.
- **What the XML parser extracts**: individuals → I, entities → E, `<object object-type="vessel">` → V; aliases from non-main identities, with `quality="low"` degraded to low priority; birth dates from `<day-month-year>`, **year-only dates normalised to 1 January**; place of birth, nationalities, addresses (the first becomes the main address, the rest alternatives), passports / ID cards / other registrations with their issuer and expiry; `gender`, including the German file's `W` (weiblich) → F. The legal basis, the programme key and the originating authority come from a **first pass over the `<sanctions-program>` blocks**, resolved by `sanctions-set-id`.
- **Two decisions worth stating.** Swiss lists write the **surname first**; the engine expects given-then-family. The primary name is composed in the engine's order and the **document's own order is kept as an alias**, so both spellings are searchable rather than betting on one. And since Switzerland **transposes** UN and EU measures rather than issuing its own, the originating authority is preserved in `designating_state` (`EU`, `UN`) instead of being flattened to `CH` — that is a compliance fact, not a detail.
- **Reservation stated rather than hidden**: `sesam.search.admin.ch` is unreachable from this development environment, so the parser was written from the published schema and validated on a synthetic fixture covering all three target kinds — **not against the real file**. It reads by **local element name over the target's subtree** rather than by rigid path, precisely so that the exact depth at which the schema places a birth date, an address or a nationality can vary without breaking extraction. The first real synchronisation should be pointed through the homologation gate before production; that gate exists for this.
- Wired everywhere a source is expected: scheduler, per-source cron, `POST /api/sync/run`, manual upload (the file extension picks the parser, so both routes import by hand with no prior setting), sync card, upload option, snapshot badges, list filters, and the five non-French locales.
- The OpenSanctions reader is now shared: `parse_opensanctions_simple_csv` takes the id prefix, provenance and designation reason as parameters, and `parse_pep_targets_csv` delegates to it. A SECO record can therefore never be mistaken for a PEP record — different prefix, different provenance — without the reader being duplicated.

### Fixed
- **Automatic synchronisations dropped all 26 extended columns and the official reference.** The parsers extracted `official_reference`, `sanction_programs`, `listed_on`, `title`, `designating_state`, the vessel and aircraft attributes, crypto wallets, BIC, tax ID and the contact fields — and `sync.build_watchlist_entity` wrote **none of them**. Only the manual-upload path in `api.py` carried them. So a list fetched by OFAC, DGT, UN, EU FSF, PEP or OFSI **synchronisation** landed without its issuer reference, while the *same* file uploaded by hand landed complete. Nothing failed and nothing warned: the columns were simply empty, and the regulatory reference that an audit asks for was missing on every automatically synchronised entity. `EXTENDED_ENTITY_FIELDS` and the normalisation helper now live in `fiskr/sync.py` — the module both write paths can reach — and `api.py` imports them from there instead of holding its own copy.

### Changed — the Settings tab is no longer a single 4 300-pixel scroll
- **Nine stacked cards became five sub-tabs.** The Settings tab piled every cross-cutting setting into one column: review governance, SLA delays, notifications, digest, retention, scoring, checklist, API keys, webhook statistics, configuration portability, MFA and absence delegation. Measured height at 1 440 px wide: **4 329 px**, roughly five screens, with no way to tell where one subject ended and the next began. It is now grouped by *subject* — ⚖️ Governance, 🔔 Alerts, 🎯 Screening, 🔌 Integrations, 👤 My account — and the default view is down to **1 354 px**; the other panels sit between 900 and 1 192 px.
  - No new mechanism: this reuses the app's existing `switchSubTab` / `sub-btn-*` / `sub-sec-*` pattern, already used by Alerts and Audit. Deep links come for free — `#settings/settings-screening` routes through the generic `applyHashRoute`, and the browser's back button behaves as it does everywhere else.
  - The 100-line governance card was **split in two**: four-eyes review and retention on one side, SLA + notifications + digest on the other. Both halves carry the same *Save* button, because `saveIngestionSettings()` reads every field by id and sends **one** `PUT /api/settings/ingestion` — hidden panels stay in the DOM, so the settings a user cannot currently see are still saved correctly whichever button is clicked. Verified rather than assumed: splitting the fields across panels would have silently dropped half the form had the save been per-card.
  - Panels with more than one card lay out on `repeat(auto-fit, minmax(min(420px, 100%), 1fr))`. The `min(420px, 100%)` matters: the bare `minmax(420px, 1fr)` used elsewhere overflows horizontally below 420 px of viewport. Checked at 390 / 820 / 1 440 px — one, one and two columns, no horizontal scroll.
  - The five tab labels and the new section paragraph are translated into the five non-French locales, as `test_intl.py` requires.

### Added
- **`Documentation/INJECTION_CLIENTS.md`** — how to feed the client reference base through the API: the three routes (`POST /api/ingest` for bulk, `POST /api/hooks/client-upsert` for a single record, `POST /api/screen` for screening without persistence), the full CSV column list, the rejection rules, the idempotency behaviour and progress tracking. Two ready-to-import example files in `Documentation/exemples/` — a minimal one and one with all 43 columns filled, both verified against a running server.
  - Written from the code, and it surfaced the traps worth stating in writing: **invalid rows are skipped silently** (the import returns `200`, only `record_count` reveals the loss), the **SHA-256 of the file is the idempotency key** (re-sending an identical file recreates nothing), and `CLIENT_BASE` **bypasses the approval gate** even when `require_approval` is on — that gate only covers sanctions lists.

### Fixed
- **A flaky test that turned CI red and green on the same commit.** `test_watchlist_db.py` asserted "this record is absent from production" as `total == 0`. That reading is wrong: when the exact search returns nothing, `GET /api/watchlist/db` deliberately **falls back to fuzzy matching**, so the total becomes non-zero without the searched record being there. The test markers share a prefix and differ only by a random hex suffix — « Dbancienov 3f7c2b » against « Dbpendingov 3f7a2b » scores **87.5**, above the 80 threshold — so the outcome depended on the draw. Measured collision rate: **5.4 %** of draws between those two families. Absence is now asserted on `match_mode != "exact"`, which the endpoint publishes for exactly this purpose, plus the record's absence from the returned names. Seven assertions corrected. The product behaviour is unchanged and was never at fault: the fuzzy fallback is intended, and `match_mode` exists to tell the two apart.
- **A `client_alternative_addresses` column was silently ignored on import.** The reader only looked at the unprefixed spelling `alternative_addresses`, while every neighbouring column accepts both forms. A file written with the prefixed name imported successfully with the field left empty — no error, no warning.
- **An empty multi-value column stored `[""]` instead of `[]`.** `"".split(";")` returns `[""]`, so every record without an alternative address carried one empty entry, counted as an address by everything reading that field.
- Both came from one expression copy-pasted in five places (client base, OFAC, PDF, CSV watchlist, generic CSV). It is now a single `ingest.parse_multi_value` helper that accepts several column spellings, drops empty fragments and passes an already-built list straight through.

### Fixed — East Asian names were structurally unreachable
- **Hangul and kana were never transliterated at all.** `has_non_latin_chars` decided by **allow-list**, matching `CYRILLIC` / `ARABIC` / `CJK` / `HEBREW` / `THAI` / `GREEK` against the character's *Unicode name*. « 김 » is named `HANGUL SYLLABLE GIM` — none of those words; likewise `HIRAGANA LETTER`, `KATAKANA LETTER`, Devanagari, Armenian, Georgian, Ethiopic. Those names travelled through the entire screening chain in their original script, where no string metric and no phonetic key could do anything with them. The `except ValueError` fallback never caught it either — Hangul and kana *have* Unicode names. The test is now on the **code point** (beyond Latin Extended-B), which is the actual criterion.
- **The blocking key was computed on the raw string.** Double metaphone only knows the Latin alphabet: on « 陈 », « 김 » or « Владимир » it returns an **empty** key. A record written in its original script therefore produced *no phonetic key at all* and was a candidate for nothing — regardless of what the equivalence tables contained. Scoring already transliterated both sides, so the two stages of the engine contradicted each other. Blocking now transliterates first.
- **Uppercasing ran before transliteration.** `upper()` is a no-op on a non-Latin script: « 习 近平 ».upper() stays « 习 近平 », and transliteration then yielded « Xi JinPing » in mixed case against « XI JINPING » on the list side. String metrics are case-sensitive, so two spellings that are *identical* after transliteration scored only **64.40**. Fixed in `compute_base_score` and in the two search paths in `api.py`.
- **Reversed name order is now compared.** Official lists write East Asian names surname-first (« Kim Jong Un », « Xi Jinping », « Chen Quanguo », « Park Geun-hye »); a client base holds given and family names in separate fields and concatenates « given family ». The two compared strings were systematically inverted, and Jaro-Winkler + Damerau-Levenshtein — 80 % of the weight — collapse on that; only token sort (20 %) survived, never enough to clear a threshold. The inverted form is added as one more name variant. This is not an Asia-only case (counter entry, « SURNAME Firstname » interchange formats), so it applies to every natural person.
- **Korean and Japanese equivalence groups** (33 new): transliteration produces the official Revised Romanisation (박 → Bag, 이 → I, 최 → Choe) where sanctions lists use the established spelling (Park, Lee, Choi) — the Henri/Harry case exactly, so it is declared. Japanese kanji are read **in Chinese** by the transliterator (田中 → « Tianzhong », not « Tanaka »), so each Japanese reading is declared. Chinese needed **nothing**: pinyin lands exactly on the romanised term already present (陈 → `CHEN`), so no ideogram was added — verified rather than assumed.
  - Measured on the same panel and the same universe, before vs after: **East Asian segment 2/19 (10.5 %) → 19/19 (100 %)**, non-Latin scripts 5/13 → 10/13, total alerts 109 → 131, **0 alerts added on the 600 ordinary clients** despite the reversed-order comparison, **0 alerts lost**.

### Changed
- **Given names and surnames are now active on delivery** (`DEFAULT_RESOURCE_FIELDS`). Not a convenience call — a measured one, written up in `Documentation/MESURE_RESSOURCES.md`. On a 716-client panel screened against 124 designated records: **+6 true positives, 0 alerts added on the 600 ordinary clients, 0 alerts lost**, and 18 already-detected pairs whose score rises (Evgueni Prigojine × Yevgeny Prigozhin: 78.03 → 100.00) with none falling. Cities, countries and states stay inactive: they have not been measured on a real panel, and the principle stands that a screening parameter change is quantified before it is applied.
  - The measurement also settled what the tables are actually *for*. On a Latin-script base the engine already caught **97.4 %** of French press spellings on its own (Poutine/Putin, Loukachenko/Lukashenko are close character-for-character) — the tables add little **volume** there, but they move scores from "doubt" to "certainty". The decisive gain is on **non-Latin scripts**: 0 → 5 of 13, because double metaphone on « Владимир » or « قاسم » produces nothing usable and those records were never even *candidates*. The equivalence key is computed **after** normalisation, and creates the bridge.
  - Corollary, verified rather than assumed: **declaring Cyrillic terms is pointless** — « Владимир » normalises to `VLADIMIR`, a term already declared in Latin script. What must be declared are terms whose transliteration resembles nothing: `سليماني` → `SLYMNY`, which no string metric would ever tie to `SOLEIMANI`.

### Fixed
- **10.5 % of the shipped equivalence terms were inert — 321 of 3 071.** Every term whose *normalised* form contains a space was unreachable: the tables are indexed on the whole term (`LA HAYE`), while lookup walked token by token (`LA`, then `HAYE`). All the compound exonyms were dead on arrival — `The Hague` / `La Haye` / `Den Haag`, `Saint-Pétersbourg`, `Al Qahirah`, `New York`, `Nueva York` — along with the Arabic given names whose transliteration splits (`معمر` → `M MR`). 105 terms in `cities.yaml`, 91 in `countries.yaml`, 60 in `states.yaml`, 58 in `surnames.yaml`, 7 in `given_names.yaml`. `ResourceIndex.match_spans` now performs a **greedy longest-match** segmentation, shared by `canonicalize_tokens`, `applied_equivalences` and `scoring.apply_name_equivalences` — so the crossing rule and the traceability trail both benefit. Measured effect on the reference panel: `Bachar El Assad` × `Bashar Al-Assad` goes from an untraced 95.30 to a traced 100.00 (`EL ASSAD ≡ AL ASSAD`).
- **Surname equivalences could not bridge to a listed record.** A listed record holds its full name in a *single* string (« Muammar Gaddafi ») where a client has separate fields. As long as blocking only looked at the **first** word, a surname equivalence never produced a shared key: the client emitted `EQGADDAFI`, the listed record emitted only the keys of « Muammar ». The surname table was therefore inert on the ordinary case. Blocking now derives equivalence keys from the **first *and* last** word (phonetic keys are unchanged, so no pair that is a candidate today stops being one).
- **A test could delete every test panel in the installation.** `tests/test_backtest.py` cleaned up by `file_type == CLIENT_TEST_PANEL`, which is not a scope — it wiped panels a user had built for their own measurements. It now only deletes panels created during the test itself.

### Added
- **Measuring the impact of an equivalence change before applying it — the guard-rail the previous two batches claimed but did not have.** Those batches repeatedly stated that activating linguistic resources should be "measured in the test book, which quantifies the interception-rate gap". That was wrong, and worth stating plainly: `backtest.run_backtest` compares two **list universes** under an *identical* scoring configuration — it does not vary the configuration; and `/api/settings/scoring/simulate` replays **already-stored** `final_score` values against candidate thresholds, which is meaningless for equivalences since they change the scores themselves *and* the candidate set produced by blocking. Neither could answer "what does this parameter change do?".
  - **`POST /api/resources/simulate`** does: same panel, same list universe, **two dry-run screening passes under two different equivalence configurations**. Reports alerts before/after, absolute and percentage gap, interception rate on both sides, a per-list breakdown, the **gained pairs each with the equivalence that produced it** (volume alone doesn't let anyone judge quality), and the **lost pairs** — which should be zero given additive blocking keys and the crossing rule, but is measured rather than assumed.
  - **`baseline_fields: null`** compares against the configuration *currently in force*: the question asked is "what does my change add to what I already do?", not "to nothing".
  - **`include_pending_ids`** answers "what would happen if I approved these mining proposals?" **without approving them** — the candidate index is built separately and the rows stay `PROPOSED`.
  - **Thread-local context override** (`resources.use_context`): the measurement runs in a background thread while the API serves real screenings on other threads. A global override would have emitted production decisions under a configuration nobody asked for — and written them to the immutable audit trail. Nothing is written either: no alert, no audit row, no settings change.
  - **The report never leaks into `GET /api/progress/active`.** It carries client and listed-entity names, and that endpoint is polled continuously by every user's dashboard; the result is returned only against the operation's own token. An existing test asserting "no business payload in the active list" caught this while it was being introduced.
  - **The report states what it cannot measure**: it quantifies the *volume* of gained alerts, not their quality — no simulation owns ground truth. Written into the payload, not into a footnote.
- **A daily engine that finds and applies new homonyms — and 2 664 terms instead of 1 078.** The shipped equivalence tables were a starting point, not a finished state: every portfolio has its own spellings, every new list brings new variants.
  - **Shipped data more than doubled**: 2 664 terms across 599 classes and five files — given names, surnames, cities, countries, and a new `states.yaml` covering territories that carry a screening stake (Crimea, the occupied Ukrainian oblasts, Abkhazia, South Ossetia, Transnistria, Nagorno-Karabakh, Xinjiang) alongside US states, Canadian provinces, German Länder and Chinese provinces. Zero collisions. The collision detector caught the duplicate classes this expansion created (MARY/MARIAM, JAMES/YAKOV, IVAN/JOHN, MIKHAIL/MICHAEL…) and they were merged into the pre-existing groups rather than shipped as parallel universes.
  - **Where the discoveries come from** — not an external feed, but two datasets the installation already owns, whose evidential value beats any purchased dictionary. `ALIAS`: the alias graph of the lists in production — when OFAC states that a record "Muhammad AL-ASSAD" carries the alias "Mohammed AL-ASAD", **the authority itself** establishes that both spellings designate the same person; extracting the pair is a reading of official data, not an inference. `ANALYST`: alerts closed as true positives — a human validated that two spellings designate the same individual.
  - **The safeguard that makes mining usable.** The obvious trap: "Ali HASSAN" alias "Abu MUHAMMAD" is a *nom de guerre*, not a spelling variant; naive alignment would produce Ali = Abu and Hassan = Muhammad, and screening would start matching unrelated people. **A wrong equivalence table is worse than no table.** The rule eliminates this by construction: both names must have the same word count and differ on **exactly one** word — everything else being identical, the diverging word is necessarily another spelling of the same element. Plus: particles excluded (AL, BIN, VAN, DE…), minimum length, phonetic or string proximity required, a minimum number of **distinct** records carrying the pair (an isolated typo never becomes a screening rule), and individuals only.
  - **Governance.** Confidence combines repetition, proximity and source, all explainable to an examiner. A discovery whose two terms belong to **two different existing classes** is refused — merging classes on the strength of an automatic find would reunite universes someone deliberately separated. Auto-application above a configurable threshold (default 0.85), or `0` to route everything through human review; either way an applied equivalence only reaches screening if its field type is separately enabled, which is never the default — two independent locks. Every discovery keeps its **evidence** (the records or alerts that produced it), every decision is traced, and every approval stays revocable — which is what makes auto-application acceptable at all. A human rejection is never undone by the next nightly pass.
  - **Daily at 03:15** (after the nightly syncs, so on fresh lists), configurable, with progress, a notification on any pass that creates or applies something, an on-demand run, and a review queue in the dashboard sorted by descending confidence.
  - **Honest limit**: mining finds *spelling* variants. It does not find cross-language equivalents with no graphic or phonetic proximity (Henri ≡ Harry, Bill ≡ William) — nothing in the data allows deducing those safely. They remain manual curation.
- **Linguistic resource files — declared equivalence tables (the Fircosoft principle).** The engine only ever compared **strings**: transliteration + accent stripping, Jaro-Winkler, Damerau-Levenshtein, token sort, double metaphone. Those metrics catch a typo, but nothing in them can conclude that *Henri* and *Harry* are the same given name, or that *Londres* and *London* are the same city — there is no string proximity to find. That is **knowledge**, not computation, so it is now declared.
  - **Format**: YAML files under `resources/` (path configurable via `resources.directory`), one per field type — `given_name`, `surname`, `city`, `country`, `state`. Each declares **equivalence groups**: a canonical class and N terms. Homonyms and competing romanisations (Mohammad / Mohammed / Muhammad), cross-language equivalents (Henri / Harry / Heinrich / Enrique), exonyms (Londres / London, Allemagne / Germany / Deutschland / DE) and entrenched misspellings all go through the same mechanism. Adding a new universe (nationalities, legal forms, vessel aliases) means one value in `FIELD_TYPES` and one file — no engine change.
  - **Shipped data**: 1 078 terms across four files (given names, surnames, countries, cities), each with a provenance header, loaded with fingerprint `4b0d7f8b83e0ec78` and zero collisions.
  - **Applied at blocking *and* scoring — both are required.** Candidates are bucketed by the metaphone of the first word, so *Henri* and *Harry* never land in the same bucket and `compute_base_score` **never compares them**: a table wired only into scoring would have had no effect whatsoever. Blocking now emits an **additive** `EQ<class>` key alongside the untouched phonetic keys, and the country component emits the country class alongside the raw value (without it, a client whose nationality reads "Allemagne" never meets a record filed under "DE", and canonicalising countries at scoring stays dead code). **No pair that was a candidate before stops being one.**
  - **Crossing rule at scoring**: a token is rewritten to its class only when that class is present **on both sides**. Canonicalising each name independently would move the score of pairs the table has no business touching — "Henri Dupont" would become "HENRY DUPONT" even when compared to "Sofia Marchetti". With the crossing rule, any pair without a shared class comes out character-for-character identical, so the only possible effect of the table is to bring two declared-equivalent terms together.
  - **Auditability**: every applied equivalence is written to the `decision_tree` (`resource_equivalences`) and shown in the screening result, the alert modal and the audit modal — an analyst must be able to read *why* two dissimilar names matched. Collisions (a term claimed by two classes) are detected at load, reported with the offending file, and resolved deterministically by first declarant. The documented arbitration for the shipped data: *Wong* → WANG and *Ng* → WU, their most frequent readings, with the accepted consequence written into `resources/surnames.yaml`.
  - **Governance**: activation is **per field type**, hot-reloadable, and **everything ships disabled** — an existing installation sees no change until someone enables a type and measures the gap in the test book (`fiskr/backtest.py`), which is exactly what an equivalence table needs before production since it mechanically trades precision for recall. Enabling or reloading rebuilds the screening index so both sides of the comparison agree. New endpoints: `GET /api/resources` (files, fingerprint, per-type counters, collisions), `POST /api/resources/reload` (admin, traced `RESOURCES_RELOADED`), `GET /api/resources/lookup?term=…` (class and variants — the diagnostic tool for understanding a match). Dashboard card under **Blocking Keys**, with an explicit confirmation on activation.

### Fixed
- **Official-source syncs silently failed to reach production (data-integrity bug, reported in production).** `persist_pivot_items` called `db.expunge_all()` on every periodic commit, detaching **every** object in the session — including the `Snapshot` being built and the previous one, both held by the caller across the whole loop. Two consequences, in all four sync implementations:
  - reading `previous.snapshot_id` after the loop raised `Instance <Snapshot> is not bound to a Session` — the reported UN failure, where the list had actually been imported but the run was reported as `ERROR`;
  - worse, OFAC, DGT and EUR-Lex then wrote `snap.status` and `snap.record_count` on a **detached** object: the following commit persisted nothing, so the snapshot stayed `PROCESSING` with `record_count = 0` and **the list never entered production, with no visible error**. Only the generic cycle re-queried the snapshot, which is why UN failed loudly where OFAC and DGT failed silently.

  Trigger: more than 1 000 records **and** an existing previous snapshot — so any mature installation. Fixed at the root: the helper now releases **only the `WatchlistEntity` rows it created** (the ones that actually accumulate on 750k-record datasets), leaving the caller's snapshots attached; belt and braces, the four call sites capture `previous_snapshot_id` as a plain string before the loop and re-query the snapshot before writing to it, so no ORM object survives a long operation.
- **Self-repair of installations already damaged**: at startup, snapshots left in `PROCESSING` for over an hour are recounted from their **actual** entities and switched to `READY`/`PENDING_REVIEW` (or `ERROR` when empty), each correction traced `SNAPSHOT_REPAIRED` in the admin log. Never raises — a startup must not fall over on a repair.

### Added
- **Progress for every official source, not half of them.** Only the generic cycle (UN, EU FSF, PEP, OFSI) published its phases; **OFAC, DGT and EUR-Lex published nothing** and showed as an indeterminate bar. A shared `SyncProgress` helper now carries the same phases (`DOWNLOAD → HASH → PERSIST → DELTA → RELOAD`) across the four implementations — byte counts on downloads, record counts during persistence, and for EUR-Lex an **act-by-act** count, which is where a scraping run actually spends its time. Progress is also persisted on the snapshot, so it survives a restart.
- **Live status per source in the cron schedule table**: a new "État" column shows the running phase, percentage and author, fed by the background-operations poll already in place — **no extra request**. A cron-triggered sync is now as visible as one launched by hand.
- **Per-source network budget** (`sync.<source>.network` overriding `sync.network`): EUR-Lex, whose portal answers `HTTP 202` with an empty body (anti-robot interstitial), gets a more patient default (6 retries, 5 s backoff ≈ 105 s instead of 4 attempts over 18 s) without slowing any other source; a warm-up request fetches the portal home page first with the shared cookie-keeping client, and a persistent 202 now produces an **actionable** message naming the setting to raise. The `ERROR` status on total failure is unchanged (never a false `NO_CHANGE`). **Not validated against the live portal**: this sandbox's proxy blocks `eur-lex.europa.eu`, so these changes are reasoned and covered by simulated transports only.
- **Live progress for background work — a single indicator for everything that is running, and long jobs that no longer freeze the app.**
  - **The blocking bug first**: `POST .../backtest` and `POST .../approve` were `async def` endpoints calling `run_backtest` / `rescreen_after_snapshot_change` **synchronously**, so they held the event loop for the entire run — the whole application, `GET /api/progress` included, was frozen for minutes. Measured on a 4 000-record panel: the test-book request now returns in **9 ms** and `/api/health` keeps answering in 2-9 ms throughout, where it previously blocked until the A/B screening finished. Both endpoints now answer **202** with a job token and do the heavy work in a background thread. Their **refusals stay synchronous** — missing/empty panel, invalid candidate rule, unmet approval requirements are still immediate 400s, and nothing starts. For approval, the governance act itself (checks, READY flip, superseding, commit) also stays synchronous; only the cache reload and post-delta re-screening move to the job.
  - **`GET /api/progress/active`**: one payload listing everything in flight — manual and scheduled imports, syncs, test book, promotion to production, batch campaigns, post-import quality check — merging the in-memory registry with running `BatchCampaign` rows. Finished operations linger for two minutes so the UI can announce the end. Labels and counters only: no business payload is exposed.
  - **Scheduled work is finally visible**: cron-triggered syncs already fed the progress registry but **nothing ever displayed them** — the dashboard only wired its bar to a manual click. They now carry a name and an author (`système`, or the real username on a manual trigger) and show up like any other operation, along with the post-delta re-screening that follows them.
  - **Real percentages inside the engines**: `_dry_run_screen` and the re-screening loop take an optional progress callback (the `persist_pivot_items` pattern) ticking every 500 clients; the test book reports `SCREEN_CURRENT` then `SCREEN_CANDIDATE`, so the bar advances across the whole run instead of jumping at the end. New phases: `INDEX`, `SCREEN_CURRENT`, `SCREEN_CANDIDATE`, `RESCREEN`, `QUALITY`.
  - **Header pill + notification centre**: a `⚙ N en cours · X %` pill next to the 🔔 (hidden when idle, visible from every screen), and a per-operation section in the notification panel — label, phase in the UI language, CSS-only progress bar, percentage, author, and a deep link to the relevant screen. Adaptive polling: 2 s while something runs, 8 s at rest. A finished job raises a toast and re-runs the screen callback that launched it, so the test-book report renders itself and the approval refreshes its lists. **Progress survives navigation and page reloads**: reopening a snapshot whose test book is still running re-locks the button and reconnects to its completion.
  - **A failing job is never silent**: `_start_job` marks it `ERROR` with its message in the registry, and the UI surfaces it — an operation that dies is visible instead of merely absent.
  - 383 automated tests passing (15 new in `tests/test_progress_ops.py`: registry identity and backward-compatible `update`, `list_active` ordering / finished window / error reporting, active endpoint empty-then-populated, batch-campaign merge, no business data leaked, auth guard, 202 + token with the report persisted by the job, phases actually published, synchronous refusals starting no job, synchronous promotion followed by background re-screening, failing job reported). The 27 call sites in `test_backtest.py`, `test_review.py`, `test_watchlist_db.py` and `test_notifications.py` now await job completion through a shared `tests/conftest.py` helper.
- **Operations (lot Exploitation) — inbound-webhook supervision, TRACFIN draft delivery, client-data quality threshold.**
  - **Inbound webhook supervision** (`GET /api/hooks/stats`, admin, + "📡 Webhooks Entrants" card in Paramètres): the delivery table only recorded calls carrying an `X-Idempotency-Key`, so the actual traffic, its callers and every failure were invisible. Calls without the header now get a server-side `auto:<uuid4>` key — **traced but never replayed** (a server key is never retransmitted) — and refusals (401 signature, 422 payload, 400/409) are recorded instead of vanishing, so a broken upstream integration shows up as errors rather than silence. The endpoint returns 30-day totals, a per-endpoint and per-caller breakdown, a daily series and the last 20 deliveries; the card renders them with a CSS-only histogram (no external library, CSP intact). **Never exposed**: the idempotency key itself (it is caller-chosen and may carry a business reference) and the stored response. Two retention horizons: 90 days for client keys (they back the replay), **30 days for `auto:` rows** (pure supervision) — no migration, the semantics derive from the key prefix.
  - **TRACFIN draft sent to the correspondent** (`POST /api/alerts/{id}/str-draft/send`, reviewer/admin, "✉ Envoyer au correspondant" button in the case-file modal): the pre-filled report draft existed as JSON and printable HTML but its transmission stayed manual, although `institution.correspondent_email` was already configured. The printable HTML is now factored out and reused as the email body. Refusals are explicit rather than silent — **400** with no correspondent configured, **503** without SMTP, **502** with the actual SMTP error — and the send is traced `STR_DRAFT_SENT` in the alert's append-only history **only after** the mail actually left. Still **no transmission to TRACFIN**: the designated correspondent remains the sole decision-maker for the ERMES tele-declaration.
  - **Client-data quality threshold** (`quality_min_score_pct` setting, 0 = disabled): an incomplete repository degrades screening precision, yet nothing signalled an import that made it worse. A successful `CLIENT_BASE` import now runs the quality check **in a background thread** (never in the request — 750k records would double the perceived import time), caches the result, and emits the new `client_quality_low` catalog event when the score falls under the configured threshold — so activation, label, role routing, delivery log and settings screen all come for free from the notifications groundwork. `GET /api/quality/clients` gains a `threshold: {min_score_pct, below}` verdict (badge in the 🧪 Qualité card) and the KPI digest reads the cache — showing "not computed since the last import" rather than lying when a newer repository has been promoted — so the scheduler never rescans the repository.
  - 368 automated tests passing (14 new in `tests/test_exploitation.py`: header-less delivery traced as `auto:` with no stored response and no replay, 422 recorded, stats aggregates/daily/recent with no key or response leaked + admin guard, differentiated TTL purge, TRACFIN send 400/503/502 and success with recipient + HTML banner + `STR_DRAFT_SENT` + role guard, threshold default/bounds/`below` verdict, post-import check notifying only under an active threshold and never raising, background hook writing the cache, digest reading the cache and going stale on a new import).
- **Step-by-step email notifications (lot Notifications) — an email at every stage of list production, screening and filtering.**
  - **Event catalog** (`fiskr/events.py`, dependency-free): **31 notifiable stages** declared once and consumed everywhere — default activation (`fiskr/settings.py`), mail labels (`fiskr/notify.py`), routing (`fiskr/notifier.py`) and the settings screen are all derived from it. Adding a stage means adding one entry.
  - **Full coverage of list production**: pending approval (enriched with author and delta), **list approved and promoted**, **list rejected with its reason**, import succeeded / failed, sync finished (SUCCESS/NO_CHANGE/NO_PUBLICATION) and sync error, exclusions set or removed, test panel generated, **test-book run** (immediate when the verdict is WARN, since it blocks approval), bulk Good Guys, post-delta re-screening. Screening and filtering: alert created, assigned (incl. bulk), escalated, decision proposed / validated / returned, **direct closure without four-eyes**, **SLA breach**, anti-FP rule submitted / activated / returned, whitelist pair created / revoked / **nearing review deadline**, ISO 20022 HIT verdict, batch campaign finished / failed, rejected CFT inbox file, retention purge. Before this lot only **5** call sites existed in the whole codebase.
  - **Role-based recipients**: new additive `users.email` column — each stage reaches the people concerned (approval → reviewers/admins, assigned alert → the analyst **and their delegate when an absence is declared**, rules → the `rules` role, incidents → admins, decisions → the original proposer). Falls back to the historical global list (`NOTIFY_EMAIL_TO`/`SYNC_EMAIL_TO`) when no account address matches, so existing deployments keep their exact current behavior.
  - **Immediate vs bundled**: structural stages are sent at once; high-volume stages are queued and grouped into **one summary email per recipient** by a new scheduler loop (hot-configurable cron, hourly by default) that also detects **overdue SLAs** (each alert flagged once, traced as an `SLA_OVERDUE` alert event) and **whitelist pairs nearing their review deadline** — a deadline that until now expired silently.
  - **HTML emails with a direct link**: self-contained inline-styled template (key/value table, status badge, "Open in Fiskr" button) with a plain-text alternative; the button uses the existing hash routing plus the new `notifications.public_url` config key — no button at all when it isn't configured, never a broken link.
  - **Observability**: new `notification_deliveries` table doubling as the digest queue and the **delivery log** (answers "did the email go out?" without digging through server logs, 90-day TTL), `GET /api/notifications/log`, `POST /api/notifications/flush` to send the pending summary on demand, and a **"send a test email" button** returning the exact SMTP error (503/502/400) — the only way to diagnose a mail setup from the UI.
  - **Settings screen generated from the catalog**: the four hard-coded checkboxes are replaced by four collapsible categories with immediate/summary badges and the target audience per stage; per-event activation still travels through `PUT /api/settings/ingestion` (unknown keys still rejected), the bundling cron and per-category extra recipients through the new `PUT /api/settings/notifications`. Email field added to the user form and the accounts table.
  - **Guarantee kept**: a notification never blocks or fails a business operation — `emit()` never raises, and a transport that explodes is recorded as `FAILED` in the log while the endpoint still answers 200.
  - 354 automated tests passing (25 new in `tests/test_notifications.py`: catalog coherence and coverage of the required stages, role routing incl. absence delegation and global fallback, silence when disabled, queued-not-sent for bundled events, one grouped email for N events, TTL purge preserving queued rows, HTML rendering with and without the link, `emit` never raising, business hooks on import/approval/rejection/alert lifecycle, single-shot SLA and whitelist-deadline detection, batch settings round-trip with cron validation, log/flush endpoints, test email with and without SMTP, user email round-trip).
- **Compliance+ (lot Conformité+) — pre-filled TRACFIN report draft, client data quality board, inbound webhooks.**
  - **Pre-filled suspicious activity report draft (TRACFIN)** (`GET /api/alerts/{id}/str-draft` + self-contained printable `/print`, reviewer/admin, "🇫🇷 Projet de déclaration" button in the case-file modal): a structured draft laid out like an ERMES tele-declaration — declarant (new `institution` block in `config.yaml`: name, SIREN, TRACFIN correspondent), person concerned (KYC from the production client repository), listed party (programs, designation reasons, official reference), traced grounds (final/base scores, applied cut-off, contextual adjustments, 50%-rule holdings) and the append-only processing timeline. **No automatic transmission** — a prominent banner marks it as a draft to be reviewed by the designated correspondent, and every generation is traced `STR_DRAFT_GENERATED` in the alert history.
  - **Client data quality dashboard** (`GET /api/quality/clients`, "🧪 Qualité des Données Clients" card in Pilotage): completeness rate per KYC field on the production CLIENT_BASE (colored bars: green ≥ 95%, orange ≥ 80%, red below), per-segment breakdown (worst first), **screening-risk records** (natural persons without DOB or first name, records with no country) and a global score — incomplete files degrade screening precision and inflate false positives.
  - **Inbound webhooks (upstream IS → Fiskr)**: `POST /api/hooks/screening` (same payload/response as `/api/screen`, same screening core — audit and alerts identical) and `POST /api/hooks/client-upsert` (single-record create/update in the production client repository, admin-logged `CLIENT_UPSERT_HOOK`). Both restricted to **`fsk_` API keys** (human sessions get an explicit 403), with an **optional HMAC-SHA256 signature** (`X-Fiskr-Signature` over the raw body, enforced when `hooks.secret` is set) and **idempotency** via `X-Idempotency-Key` — retransmissions replay the original response (`X-Idempotency-Replayed: true`) without re-screening, deliveries kept 90 days in the self-cleaning `hook_deliveries` table. README section "Intégration SI amont" with signed curl examples.
  - 329 automated tests passing (7 new in `tests/test_conformite_plus.py`: STR draft structure incl. KYC merge and traced grounds + `STR_DRAFT_GENERATED` event, printable banner + role guard + 404, quality board field/segment/risky-record math on a crafted base, hooks refused to human sessions, API-key screening + idempotent replay with no new audit row + 422 payload, HMAC missing/wrong/valid, client upsert create/update + admin log + record count).
- **Reliability & rules (lot Fiabilité & Règles) — EUR-Lex connection fix, live import progress, rule-authoring workshop, rules in the test book.**
  - **EUR-Lex network reliability (bug fix)**: the sync retry loop only covered HTTP statuses — any `httpx` transport error (connection refused/reset, timeout, TLS/proxy cut) aborted the sync immediately. New `_with_retries` helper retries **transport errors AND transient statuses** (202 anti-robot empty body, 408/429/5xx) with linear backoff, while deterministic 403/404 fail fast. `download_to_file` gains a **browser User-Agent** (official portals filter the default httpx UA on PDFs), granular timeouts (per-chunk read instead of one monolithic 300 s), fresh file per attempt, and a shared keep-alive `httpx.Client` removes the 2N+1 TLS handshakes per EUR-Lex run. All tunable via the new `sync.network` block in `config.yaml` (timeouts, retries, backoff, user_agent).
  - **Partial failures are visible**: unreachable acts no longer vanish — the sync report carries `fetch_failures`/`pdf_failures`, the message is suffixed "⚠ N acte(s) inaccessibles (repris au prochain run)", the dashboard shows a ⚠ badge on the report row, and **all acts failing → status ERROR** (never a reassuring false NO_CHANGE on a network outage: an amputated list is a compliance risk).
  - **Live progress for large imports** (the 750k-record PEP file with zero feedback): `POST /api/ingest` moved off the event loop (sync `def` → threadpool, response contract unchanged), upload copy + SHA-256 fused in a single 1 MiB-chunk stream, and **periodic commits + identity-map purge** every 1000 rows (both the ingest loops and `persist_pivot_items`) cap the RAM of huge datasets. New in-memory progress registry (`fiskr/progress.py`, TTL 15 min) + persisted `processed_count`/`total_hint`/`phase` columns on snapshots (poll survives a restart), exposed by **`GET /api/progress?id=`** (UUID token, `sync:<source>` token or snapshot_id fallback). The dashboard shows a **live progress bar during the upload request** (phases in the UI language: téléversement, empreinte, enregistrement, delta, rechargement), sync buttons display phase + counter while running, `/api/sync/config` exposes `running`, and the snapshots table shows the live phase of a PROCESSING import.
  - **Rule-authoring workshop** (Python editor, zero external libs — CSP intact): clickable **ctx-key palette** (typed, with sub-keys for `party`/`message`/`entity`/`client`), ~8 insertable **snippets** (score threshold, country scope, name regex, missing DOB, agent party…), **debounced server-side syntax validation** (`POST /api/fprules/validate` returns line/column; clicking the error focuses the faulty line), **home-made autocompletion on `ctx["` and `.get("`** (mirror-div caret positioning, ↑↓/Enter/Tab/Escape), and **"🎯 + Test from an alert"** (`GET /api/fprules/context-from-alert/{id}` rebuilds the exact rule(ctx) of a real alert from its audit decision tree — no more hand-typed JSON test contexts).
  - **Natural-language rule creation, two ways**: ✨ **AI generation** (`POST /api/fprules/generate`, opt-in `fprules.llm_enabled` + `ANTHROPIC_API_KEY`, prompt embeds the full ctx contract and a hard-match guard-rail; output validated by `compile_rule` with one retry; **explicit errors** — 503 when unconfigured, 422 with the raw code for manual fixing) and 🧩 **structured form** (no AI: typed condition rows + AND/OR combinator + "never suppress a hard match" guard generate deterministic Python with safe `.get()` access and JSON-escaped values). Both only ever produce a **draft in the editor** — unit tests, submission and 4-eyes validation remain mandatory, governance unchanged.
  - **Anti-FP rules join the homologation test book** (the requested loop: noisy list → write a rule → replay the test book → measure the gap): `_dry_run_screen` now applies rules after the whitelist (local fail-open loop, hit counters untouched), **ACTIVE rules run on both sides** (the backtest finally mirrors production) and an optional **candidate rule** (draft/pending/active, screening channel) runs on the candidate side only. The report gains an additive `rules` key (suppressed counts per side, delta, sample of suppressed pairs with the rule name, `gap_pct_before_rules` isolating the list's own effect) — legacy reports stay valid, the approval gate still reads only `verdict`. Step-3 UI: candidate-rule selector, "Règles anti-FP" block in the report, link to the rule editor.
  - 322 automated tests passing (32 new: transport retries incl. immediate 404 failure and empty-200 retry, download UA + transient-status retry, `sync.network` defaults, EUR-Lex partial → SUCCESS + visible failures vs total → ERROR, progress registry incl. TTL/error/no-total, ingest with `progress_id` — unchanged contract, persisted columns, snapshot-id fallback — `persist_pivot_items` periodic commits + crash-proof callback, `/validate` error line, context-from-alert from audit tree, generate 503/200-mocked/422-with-raw-code/400s, backtest additive `rules` key, candidate draft delta + suppressed pairs, ACTIVE rule on both sides, invalid candidates → 400, legacy report accepted by the approval gate).
- **Investigation & tuning (lot Go) — multilingual API messages, investigation case file, threshold impact simulation.**
  - **Multilingual API messages** (`Accept-Language` negotiation): a new middleware translates the `detail`/`message` fields of JSON responses through a backend catalog (`fiskr/apimessages.py` — exact matches plus regex templates for variable messages like lockout minutes or password-policy feedback) in EN/DE/ES/ZH/AR; unknown messages and unsupported languages fall back to French. The dashboard's `apiFetch` sends the active UI language, so error toasts and confirmations now arrive translated end-to-end.
  - **Investigation case file** (`GET /api/alerts/{id}/casefile` + 📁 button in the alert modal): everything an analyst must examine in one place — alert summary, decision tree, append-only action history, attachments, client context (past screenings/alerts/whitelist), entity relations count and **50%-rule inherited risk** — plus a **hot-configurable investigation checklist** (admin setting, 20 items max; every tick/untick is stored per alert and traced as a `CHECKLIST` event) and a **self-contained printable case file** (`/casefile/print`, browser print → PDF): the single document to hand a regulator for one alert.
  - **Threshold impact simulation** (`POST /api/settings/scoring/simulate`, admin): before changing cut-offs, replay the immutable audit trail of the last N days (candidate-bearing decisions only; governed suppressions stay suppressed) against the candidate global threshold and per-list overrides — returns current vs candidate alert counts and the **delta per list**, zero writes. "🧪 Simuler l'impact" button in the thresholds card renders the comparison table; data-driven tuning instead of blind changes.
  - 290 automated tests passing (6 new in `tests/test_investigation.py`: Accept-Language resolution incl. quality factors, catalog + template translation with French fallback, middleware end-to-end in EN/ES, case-file aggregate + checklist flow (persisted state, `CHECKLIST` event, bounds) + printable dossier, hot checklist setting (bounds, default restore, admin guard), simulation (stricter ≤ current, permissive = all replayed, no write, bounds, admin guard)).
- **Internationalization, stage 2 — full descriptive paragraphs, composite strings, localized dates.**
  - **All 48 descriptive paragraphs** (`section-desc`) of every screen are now translated in the 5 target languages (EN/DE/ES/ZH/AR) via a dedicated paragraph dictionary keyed on the full normalized French text — translated as whole units so no mixed-language sentences can appear; coverage grows to **390+ dictionary entries**.
  - **Composite strings with variable numbers** (queue pagination "N élément(s) — page X / Y", bulk-selection counter, batch alert counter) translated through **regex rules with placeholders** — previously untranslatable by exact match.
  - **Localized dates and numbers**: all 19 hardcoded `fr-FR` formatting calls in the dashboard now follow the active language via `uiLocale()` (`en-GB`, `de-DE`, `es-ES`, `zh-CN`, and `ar-SA` with Latin numerals for readability of amounts and scores).
  - Dynamic status labels (`STATUS_LABELS`: snapshot, rule and delta states) and frequent runtime strings (MFA/absence card states) added to the dictionary.
  - 284 automated tests passing (extended `tests/test_intl.py`: every on-screen paragraph must exist in the dictionary — the test extracts them from `index.html` and fails on any missing one, 5-language sampling, regex rules, locale map, no residual `fr-FR` in app.js).
- **Internationalization & roles (lot Intl) — 6-language UI, absence delegation, hot score thresholds, read-only auditor role.**
  - **6-language interface (FR / EN / DE / ES / ZH / AR)**: new dependency-free i18n engine (`fiskr/static/i18n.js`) — French stays the source language in the markup, a 230+-entry dictionary translates text nodes and attributes (placeholder/title/aria-label) on load and **continuously via a MutationObserver** (JS-rendered content — queues, tables, badges, statuses, priorities — is translated too). Language picker in the header and on the login page, persisted in `localStorage`, applied before first paint; **Arabic switches the whole layout to RTL** (`dir="rtl"` + dedicated CSS: mirrored sidebar, navigation and tables). Untranslated strings always fall back to French (no holes); long explanatory paragraphs remain French in this iteration.
  - **Absence delegation**: `absent_until`/`delegate_to` on accounts — while absent, any alert assigned to the analyst is **redirected to their delegate** (single hop, traced in the alert event), and open alerts can be reassigned immediately on declaration. Self-service card "🌴 Absence & Délégation" (delegate picked from a new lightweight `GET /api/users/directory`), admin endpoint per user, 🌴 badge in the users table, `ABSENCE_SET`/`ABSENCE_CLEARED` admin-logged. Guards: future end date, mandatory delegate, no self/auditor delegation.
  - **Hot screening score thresholds** (`GET/PUT /api/settings/scoring`, admin): global cut-off + per-list overrides now editable at runtime (DB setting wins over `config.yaml scoring.*`), applied to **both client screening and ISO 20022 transaction filtering** with the recorded `cut_off_applied` following suit; portable via config export/import; 0-100 validation, `SETTINGS_UPDATED` logged. New "🎯 Seuils de Score" card in Paramètres.
  - **Read-only auditor role** (`auditor`, exclusive — cannot be combined): full read access, **every mutating request refused (403)** at the authentication layer for both JWT sessions and API keys, with the only exceptions being own-session management (logout, password change, own MFA). Role available in the user form and as a read-only API-key profile for external supervisors.
  - 283 automated tests passing (6 new in `tests/test_intl.py`: full absence flow incl. redirect + event trace + end of absence, absence validations, hot thresholds incl. per-list resolve + real screening `cut_off_applied` + bounds, auditor exclusivity + real-path read-only enforcement incl. allowed self-service, i18n asset completeness — 5 languages, ≥200 entries, observer, RTL, pickers).
- **Steering & portability (lot Pilotage & Portabilité) — archive-before-purge, analyst workload board, config export/import.**
  - **Archive before purge**: when enabled in the retention policy (default on), every purge first dumps the doomed rows as **JSON Lines per table** into a timestamped `retention_archive/purge_YYYYMMDD_HHMMSS/` folder (alerts + events + attachment metadata, screening audit rows, sync reports, batch campaigns/results) — the purge stays reversible offline and the archive path is recorded in the `RETENTION_PURGE` admin-log entry (`— sans archive` when disabled). Checkbox in the retention card; folder git-ignored, to be shipped off-box by operations.
  - **Analyst workload board** (`GET /api/alerts/workload?channel=`): open alerts per assignee broken down by priority, SLA overdue count, next due date and pending 4-eyes validations — plus the unassigned backlog (shown first) and global totals; sorted most-overdue first. New "👥 Charge de Travail des Analystes" card in Pilotage with a channel filter.
  - **Hot-settings export/import** (`GET /api/admin/config/export`, `POST /api/admin/config/import`, admin): portable JSON of all known hot settings (homologation, 4-eyes, blocking layouts, cron schedules, SLA, notifications, digest, retention) to align environments — **no secrets ever transit** (no accounts, no API keys); unknown keys are skipped and reported, every import is admin-logged `SETTINGS_IMPORTED` with the before → after delta. "💼 Portabilité de la Configuration" card in Paramètres (export button + file-picker import with confirmation).
  - 277 automated tests passing (5 new in `tests/test_pilotage.py`: purge archives JSONL incl. purged alert content + path in the admin log, no archive when disabled, workload breakdown per assignee/priority/overdue/next-due + unassigned + channel filter, config export/import round-trip incl. unknown-key skip + `SETTINGS_IMPORTED` delta + no-known-key 400, admin guards).
- **Data governance (lot Gouvernance) — retention/purge policy, saved queue views, periodic activity report.**
  - **Data retention & purge (GDPR / archiving)**: hot-editable policy (days per family, **0 = keep forever**, default) covering screening decisions, closed alerts (with their event history, attachments — files removed — and batch references), sync reports and finished batch campaigns. Guard rails: 30-day minimum when a purge is enabled, validated cron for the daily purge time (default 02:30), **the admin action log is never purged**, and screening-audit rows still referenced by a kept alert are never deleted (only expired orphans go). Preview endpoint (volumes that would be purged, zero writes), manual "purge now", dedicated scheduler loop, every purge traced `RETENTION_PURGE` with per-family volumes. Admin card in Paramètres.
  - **Saved queue views**: per-user named filter combinations (statuses, priority, list type) on both alert queues — `saved_views` table, CRUD endpoints (same name = update, other users' views invisible, delete owner-or-admin), "Vues…" selector + 💾 button in each queue toolbar, one-click restore incl. status-button highlighting.
  - **Periodic activity report** (`GET /api/reports/activity?date_from&date_to`, default last 30 days): regulator-ready period summary — screening decisions (by status/list), alerts created (by channel/priority) and decided (by outcome, average decision delay, escalations, still-open), whitelist created/revoked, syncs by status/source, batch volumes. **CSV export** (`;` + BOM) and **self-contained printable HTML** (browser print → PDF), plus a Pilotage card with date pickers.
  - 272 automated tests passing (6 new in `tests/test_gouvernance.py`: retention guard rails incl. 30-day floor and cron validation, end-to-end purge with per-family volumes + `RETENTION_PURGE` trace + idempotent second pass, admin-only guards, saved-view CRUD/update-by-name/user isolation, activity report content + date-bound validation, CSV/print outputs).
- **Operational tooling (lot Opérations) — optional TOTP MFA, bulk alert actions, scheduled KPI digest.**
  - **Optional two-factor authentication (TOTP, RFC 6238)** with zero dependencies (stdlib HMAC-SHA1, verified against the official RFC test vectors): per-account enrolment from the new "🛡 Double Authentification" card (secret + `otpauth://` URI shown **once**, activation only after a first valid code), two-step login (`totp_code` on `/api/auth/login`; a missing code returns the `totp_required` flag without counting a failure, a wrong code counts toward the anti-brute-force lockout), password-protected deactivation, **admin reset** (`POST /api/users/{id}/totp/reset`, lost phone) with MFA status in the users table; `MFA_ENABLED`/`MFA_DISABLED`/`MFA_RESET` traced in the admin action log. The login page shows the verification-code field only when the account requires it.
  - **Bulk alert actions** (`POST /api/alerts/bulk`, ≤ 200 at a time): multi-select checkboxes in both work queues (screening/filtering) with a select-all header and an action bar — self-assign or re-prioritize the whole selection (SLA deadline recomputed). Same rules as unit actions (assigning to someone else requires admin) and same traceability: **one `AlertEvent` per alert**, never silent; closed alerts are skipped and reported.
  - **Scheduled compliance digest**: hot-editable setting (enabled + 5-field cron expression, validated; default 8:00 on weekdays) sending a KPI summary through the existing email/webhook channels — open alerts per channel, SLA overdue, pending 4-eyes validations, snapshots awaiting review, 24 h created/closed volumes and last sync status per source. Runs on its own scheduler loop, independent from the sync scheduler.
  - 266 automated tests passing (7 new in `tests/test_operations.py`: RFC 6238 test vectors, TOTP verification window and rejects, full MFA lifecycle incl. two-step login and lockout accounting, admin reset, bulk assign/priority with per-alert events and skipped-closed handling, bulk validation and permission guards, digest content + cron setting validation).
- **Security hardening + operator comfort (lots Sécurité & Confort).**
  - **Login anti-brute-force**: account lockout after N failed attempts (`security.max_login_failures`, default 5) for `security.lockout_minutes` (default 15) — locked logins get a 423 with the remaining minutes; failure counter reset on success. Every session event is traced in the admin action log (`LOGIN`, `LOGIN_FAILED`, `ACCOUNT_LOCKED`, `LOGOUT`) with the caller IP.
  - **Password policy** (`validate_password`): 12+ characters with lower/upper/digit, enforced on user creation, admin reset and self-service change (`security.min_password_length` hot-configurable in `config.yaml`).
  - **Hardened sessions & HTTP responses**: JWT/cookie lifetime driven by `security.session_hours`; cookie `SameSite`/`Secure` flags from config; security headers on every response (`X-Content-Type-Options`, `X-Frame-Options: DENY`, `Content-Security-Policy` incl. `frame-ancestors 'none'`, `Referrer-Policy`, `Permissions-Policy`).
  - **Technical API keys** (`fsk_` service accounts): `POST /api/apikeys` returns the full key **once** (SHA-256 hash stored, 12-char prefix listed), authentication via `X-API-Key` or `Bearer fsk_...` resolved before JWT, `last_used_at` tracking, soft revocation (`/revoke`, immediate 401), **admin role forbidden** (least privilege). Management card in the Paramètres tab (admin).
  - **Unauthenticated healthcheck** `GET /api/health` (status/database/cache only — deliberately minimal) for load-balancers and monitoring.
  - **Composite performance indexes** created idempotently at startup (alerts status+channel and client+entity, audit trail timestamp and client, watchlist/client entities per snapshot) — the hot queries of the work queue, history and 360° view.
  - **Server-side pagination** of the alert work queues (screening/filtering, 100 per page) and the whitelist screen, with page controls under each table; filters reset to page 1.
  - **Deep links (hash routing)**: every tab/sub-tab now updates `location.hash` (`#alerts/subtab-filtering-queue`, …) and the dashboard restores the exact screen on load/back/forward — URLs become shareable between analysts.
  - **Notification bell** in the header: badge with the number of items needing attention, dropdown panel built from live counters (open alerts per channel, pending 4-eyes validations, **overdue alerts**, snapshots awaiting homologation) with one-click deep links.
  - **`GET /api/counters` enriched** with `pending_validation` (4-eyes) and `overdue_alerts` (open alerts past their SLA deadline).
  - **Client 360° view** (`GET /api/clients/{client_id}/overview` + 👤 button in the alert modal): KYC sheet from the latest production referential, screening history, all alerts and whitelist pairs of the client with counts — everything an analyst needs during an investigation, in one modal.
  - **Drag & drop** on the three file-upload zones (list ingestion, batch campaigns, ISO 20022 transactions) with visual hover feedback.
  - 259 automated tests passing (7 in `tests/test_security.py`: password policy rules + endpoint enforcement, lockout → 423 → admin-log trail → unlock → counter reset, security headers, minimal healthcheck, full API-key lifecycle incl. real `X-API-Key` authentication and revocation, admin-role key forbidden; 7 in `tests/test_confort.py`: enriched counters, overdue counting open-late alerts only, 360° overview aggregation + unknown client, alert-queue pagination without overlap + bounds validation, whitelist pagination shape, re-entrant `init_db` with indexes).
- **Relationship graph visualization + real per-source cron scheduler.**
  - **Network graph of ownership links** (`GET /api/relationships/graph/{entity_id}?depth=1..3`): two-way BFS around an entity (60-node guard, `truncated` flag), edges carrying type, ownership % and a `majority` flag (≥ 50 % or OFAC presumption — the 50 % rule). New "🕸 Graphe" modal in the entity details: **native SVG radial rendering with zero dependencies** (center + depth rings, red arrows for majority ownership, arrowheads, % labels), click a node to re-center the graph on it, depth selector.
  - **Real cron, per source** (`fiskr/cron.py`, no external dependency): 5-field expressions (`*`, lists, ranges, `*/n` steps, 0/7 = Sunday, classic dom-OR-dow rule) with strict French-labelled validation, minute matching and fast next-occurrence computation. The single daily scheduler is replaced by a **per-minute cron loop**: each enabled source fires on its own effective expression (hot setting > `config.yaml` `sync.<source>.schedule` > global daily fallback), in its own thread, with **no self-overlap** for a given source.
  - **Hot-editable scheduling**: `PUT /api/settings/sync` (admin, every expression validated, empty = back to default, admin-logged); `GET /api/sync/config` now exposes the effective `schedules` and `next_runs` per source. New "⏰ Planification par Source (cron)" card in the Sources Automatiques tab (per-source expression inputs + next-run display).
  - 245 automated tests passing (7 new in `tests/test_cron_graph.py`: cron parse/match/next-run incl. dom-OR-dow and 7=Sunday, invalid expressions rejected, schedule round-trip + validation + admin guard, graph BFS depths/majority flags/entity types).
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

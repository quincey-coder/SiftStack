# CLAUDE.md — SiftStack

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SiftStack** — Full-stack real estate investing operations platform built around DataSift.ai CRM. Covers the entire REI business lifecycle:

1. **Data Acquisition:** County clerk websites (Travis, Bell, Williamson), Odyssey court portals, MVBA Law Firm tax sale PDFs, Travis County Tax Office CSV, scanned PDF import, courthouse terminal photo import (probate, eviction, code violations, divorce), Dropbox auto-polling
2. **Enrichment Pipeline:** 10+ steps — Smarty address standardization, Zillow property data, TCAD/BCAD/WCAD appraisal district lookups, obituary/heir research, Ancestry.com SSDI, DirectSkip skip trace, Trestle phone scoring, entity research, ZIP code filtering
3. **Deal Analysis:** Comparable sales (Two-Bucket ARV), rehab estimation (4-tier room-by-room), deal analyzer (MAO/ROI/financing scenarios)
4. **Market Intelligence:** Zip code scoring, Market Finder reports, cash buyer list building, investor portfolio analysis
5. **CRM Automation:** DataSift upload, 26 TCA sequence templates, 12 niche sequential marketing presets, filter preset management, SiftMap sold property tagging
6. **Lead Management:** 4 Pillars of Motivation auto-qualification, STABM daily routine, pipeline reporting, deep prospecting (4-level framework)
7. **Operations:** Acquisition playbook generator (SOPs, scripts, checklists), Slack/Discord notifications, Google Drive upload, Apify Actor deployment

Currently focused on Travis, Bell, and Williamson counties, Texas.

8. **REI Skill Library:** 13 Claude Co-Work skill files (`.skill`/`.plugin` ZIPs) for distribution to DataSift community via [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Skills teach Claude specific REI workflows when uploaded to Co-Work sessions or Projects.

## Commands

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in credentials

# Run
python src/main.py daily                          # new notices since last run
python src/main.py historical                     # last 12 months of data
python src/main.py daily --split                  # separate CSV per county+type
python src/main.py daily --counties Travis        # only Travis county
python src/main.py daily --counties Bell,Williamson  # multiple counties
python src/main.py daily --types foreclosure,probate  # only specific types
python src/main.py daily -v                       # verbose/debug logging

# DataSift preset/sequence management
python src/main.py manage-presets --discover                      # list all presets and sequences
python src/main.py manage-presets --add-sold-exclusion            # add Sold exclusion to all presets
python src/main.py manage-presets --create-sold-sequence          # create Sold cleanup sequence
python src/main.py manage-presets --all                           # discovery + update + sequence

# SiftMap sold property tagging
python src/main.py manage-sold --months-back 12                   # tag sold properties (last 12 months)
python src/main.py manage-sold --counties Travis --min-sale-price 5000

# Courthouse photo import (build 1.0.28+)
python src/main.py photo-import --folder ./photos --photo-county Travis --photo-type probate
python src/main.py photo-import --folder ./photos --photo-county Bell --photo-type eviction --skip-obituary
python src/main.py dropbox-watch                                  # auto-poll Dropbox for new photos
python src/main.py dropbox-watch --poll-interval 300 --max-polls 5  # 5-min interval, 5 cycles
python src/main.py dropbox-watch --no-delete                      # keep photos in Dropbox after processing

# Market Finder extraction
python src/extract_market_finder.py --state "Texas" --county "Travis" -v
python src/extract_market_finder.py --state "Texas" --county "Bell,Williamson" --headless
```

All source files are in `src/` and imports assume `src/` is the working directory. Run from project root with `python src/main.py` or set `PYTHONPATH=src`.

### Local/cloud parity — `input.json` is the schedule mirror

`input.json` (gitignored, holds secrets) is kept in sync with the production
Apify schedule's runInput. The CLI reads it as its defaults layer, so a bare
`python src/main.py daily` resolves enrichment toggles (obituary, ancestry,
Zillow, entity research, DirectSkip/Trestle, split-by-county, Slack) to EXACTLY
what the scheduled cloud run does. Precedence: **explicit CLI flag >
input.json > built-in opt-in default**. `--skip-obituary`/`--skip-ancestry`/
`--skip-zillow`/`--fast` force things OFF over input.json; without an
input.json everything stays conservative opt-in. If you change the schedule's
runInput in the Apify Console, re-sync input.json (or ask Claude to). NOTE:
input.json intentionally carries no `max_notices` cap anymore — pass
`--max-notices N` for smoke tests. Off-switches that only exist as flags:
`--no-slack`, `--no-drive` (both are presence-armed from `.env` otherwise).
Cross-run state is still split-brain by design (cloud=Apify KVS, local=`data/`
+ state file): never upload to DataSift from a local `daily` run — the local
dedup/sold baselines don't know what the cloud already uploaded.

## Architecture

**Data flows:**
- **Foreclosures:** `main.py` → `scrapers/foreclosure_travis.py` (tccsearch.org) | `scrapers/foreclosure_bell.py` | `scrapers/foreclosure_wilco.py` → enrichment → CSV
- **Tax delinquent:** `main.py` → `scrapers/tax_delinquent_travis.py` (direct CSV download from Travis Tax Office) → enrichment → CSV
- **Tax sales:** `main.py` → `scrapers/tax_sale_mvba.py` (MVBA Law Firm PDFs, Bell + Williamson) → enrichment → CSV
- **Probate:** `main.py` → `scrapers/probate_odyssey.py` (shared Odyssey portal, all 3 counties) → enrichment → CSV
- **PDF import:** `main.py` → `pdf_importer.py` (pypdfium2 → `image_utils.py` OCR) → enrichment → CSV
- **Photo import:** `main.py` → `photo_importer.py` (OpenCV → `image_utils.py` OCR → `llm_parser.py`) → enrichment → CSV
- **Dropbox watch:** `dropbox_watcher.py` → `photo_importer.py` → enrichment → CSV (auto-polling loop)
- **Market Finder:** `extract_market_finder.py` → DataSift Market Finder (Playwright) → paginate all ZIP + neighborhood data → JSON

### Scrapers (`src/scrapers/`)
- **foreclosure_travis.py** — Travis County Clerk search (tccsearch.org). Infragistics UI grid, no login or CAPTCHA required. "Results List" button grabs ALL results as a batched PDF in a popup.
- **foreclosure_bell.py** — Bell County Clerk foreclosure page. Direct HTML scraping.
- **foreclosure_wilco.py** — Williamson County trustee sales (apps.wilco.org).
- **tax_delinquent_travis.py** — Direct CSV download from Travis County Tax Office (13K+ delinquent properties). No scraping needed.
- **tax_sale_mvba.py** — MVBA Law Firm PDF scraper. Handles tax sales for both Bell and Williamson counties from one source.
- **probate_odyssey.py** — Shared Odyssey (Tyler Tech) portal scraper for court records. Works with all 3 counties via config.
- **probate_travis.py** / **probate_bell.py** — County-specific Odyssey configuration.

### Core Files
- **main.py** — CLI entry point. Parses args (`daily`/`historical`, `--split`, `--counties`, `--types`, `-v`). Orchestrates scrape → dedup → enrichment → export, logs run summary stats.
- **config.py** — Credentials (from `.env`), `TX_COUNTIES` list, CAD URLs, Odyssey URLs, TX data source URLs, rate limiting constants, paths, image processing thresholds.
- **notice_parser.py** — Extracts structured fields from raw notice text using regex. Defines the `NoticeData` dataclass used throughout. Contains TX city lists (50+ cities across Travis, Bell, Williamson).
- **enrichment_pipeline.py** — 10+ step enrichment: ZIP code filtering → dedup → CAD lookup → Smarty address standardization → Zillow enrichment → obituary/heir research → phone validation → data validation.
- **data_formatter.py** — Deduplicates by address (keeps most recent), then converts `NoticeData` list to Sift upload CSV. Split mode produces `{county}_{type}_{timestamp}.csv` files.
- **zip_filter.py** — Filters notices to only investor-active ZIP codes (10+ transactions/month threshold).
- **target_zips.json** — 34 qualifying ZIPs: Travis (16), Bell (8), Williamson (10). Sourced from DataSift Market Finder.
- **cad_lookup.py** — County Appraisal District lookups. WCAD uses SODA REST API; Bell/Travis use bulk data.
- **image_utils.py** — Shared OCR utilities used by both `pdf_importer.py` and `photo_importer.py`. Exports `fix_rotation()` (Tesseract OSD) and `ocr_page(image, psm)` with configurable page segmentation mode.
- **photo_importer.py** — Courthouse phone photo import. OpenCV preprocessing chain (EXIF transpose → blur check → bilateral filter → perspective correction → Otsu threshold) → Tesseract OCR (PSM 4) → LLM parsing → NoticeData. Supports all 7 notice types.
- **dropbox_watcher.py** — Cursor-based Dropbox folder polling. Downloads new photos, resolves county + notice_type from folder path (`/Travis/eviction/photo.jpg`), processes through photo_importer, deletes from Dropbox after success. State persisted to `dropbox_state.json` + `photo_state.json`.
- **report_generator.py** — Generates per-record PDF deep prospecting reports using reportlab. Includes property summary, signing chain with phone tiers, valuation, deceased owner detection. Output to `output/reports/`.
- **extract_market_finder.py** — Playwright automation to extract ALL ZIP code + neighborhood data from DataSift Market Finder. Handles styled-component dropdowns, pagination (20 rows/page), Beamer popup dismissal. Outputs JSON. See "Market Finder Extraction Patterns" below.
- **market_analyzer.py** — ZIP code scoring engine. 6-factor weighted composite (Distress 30%, Value 20%, Equity 15%, Tax Delinquency 15%, Competition 10%, DOM 10%). Grades A/B/C/D, budget allocation across top ZIPs.
- **drive_uploader.py** — Google Drive upload via service account. `upload_file()` (generic, returns webViewLink) and `upload_csv()` (CSV-specific, returns file ID).

## Run-Health + Alerting — nothing breaks silently (build 1.1.54, 2026-08-18)

For 30 days every Apify run showed SUCCEEDED while Williamson lis_pendens was
dead 27 days (Tyler 405), Travis probate/lis_pendens parsed 0 of everything
(Temp-index lag, below), and Bell lien/lis_pendens pulled 2500-row junk on ~10
days (date filter silently not applied). The anti-silence layer:

- **`src/run_health.py`** — per-scraper outcome records collected in
  `scrapers.scrape_targets` (count/duration/error/evidence), a WARN/ERROR
  counting log handler, rolling 14-day per-scraper baselines (KVS key
  `scraper_health_state` in `sift-stack-state`; local
  `state_backups/scraper_health_state.json`), and a Slack health block that is
  **ALWAYS sent — outside the `_publish_ok` gate, even on 0 notices**. Flags:
  hard fails, zero-streaks vs baseline, parse-zero (`returned>0 kept==0`),
  result-cap/window spikes, Zillow failure share >30%, guardrail trips,
  validator warnings, registry gaps. `Actor.set_status_message` carries the
  verdict; `Actor.fail` only when EVERY target hard-failed.
- **`ScraperError(msg, partial=[...])`** (`scrapers/__init__.py`) — scrapers
  raise instead of returning `[]`; partial results still flow. The per-target
  handler records health and keeps going. One central retry re-runs a scraper
  once on Cloudflare/proxy-tunnel signatures.
- **Scrapers self-report their parse rate** via `self.last_meta`
  (`returned`/`kept`/`window_days`/`hit_cap`) — a search that returns rows the
  parser can't keep now alerts on day 1 instead of never.
- **Apify platform webhooks** (`scripts/setup_apify_webhooks.py`, idempotent)
  → Slack on ACTOR.RUN.FAILED / TIMED_OUT / ABORTED — covers crashes before
  our code runs (migrations, OOM, timeout).
- **Alert drill:** `FORCE_SCRAPER_FAIL=bell/lien` env (CLI) or the
  `force_scraper_fail` actor input makes one scraper raise intentionally to
  prove the alert path.
- **Per-scraper smoke harness:** `python src/scraper_smoke.py [--only
  Travis/probate] [--skip-slow] [--days 7] [--max-notices 5] [--notify]` —
  runs every registered scraper directly (bypasses the exception swallow),
  headed + sequential, classifies ok/zero/fail/timeout, and dumps raw page
  HTML/innerText evidence (`scrapers/debug_capture.py`,
  `SIFT_DEBUG_CAPTURE_DIR`) on any parse-zero/failure. Windows prereqs:
  Tesseract on PATH (Bell foreclosure OCR — NOT installed on the operator box,
  so Bell/foreclosure reads zero locally but works in Docker), playwright-stealth,
  residential IP.

### Travis clerk "Temp index" lag — why probate/lis_pendens read zero (2026-08-18)

Freshly-filed tccsearch documents sit in a **Temp index with NO grantor/grantee
names** (the Name cell is bare `[R]`/`[E]` markers) for several days until the
clerk verifies them. A 1-day daily window therefore only ever saw nameless rows
→ parser kept 0 → **Travis probate and lis pendens produced nothing for 30+
days while "Search returned N records" logged daily.** Fix:
`tccsearch_common.effective_from_date` widens every daily window by
`TCC_INDEX_LAG_DAYS` (default 7) so filings are re-scanned once verified —
cross-run `seen_notice_ids` dedup absorbs the repeats. All four tccsearch
scrapers use it. The parse-zero alarm stays quiet when ALL returned rows are
still Temp (`count_temp_rows`) and fires otherwise.

### Other hard-won source facts (2026-08-18)
- **Williamson Tyler portal fronts an AWS WAF "Human Verification" challenge**
  that re-arms when a second scraper hits it minutes after the first (lien ran
  → lis_pendens got HTTP 405 on `searchResults` for 27 straight days).
  `tyler_common.pass_aws_waf` waits out the auto-solving challenge;
  `recover_search_session` re-runs the disclaimer flow once on a 405, then the
  scraper raises loudly. Shared plumbing for both Williamson scrapers lives in
  `scrapers/tyler_common.py`.
- **GovOS publicsearch renamed `#docTypes-input` → `#docTypes`** (React rewrite,
  2026-08-18) — both Bell scrapers accept either id. After every search,
  `publicsearch_common.verify_window_applied` reads the results back
  (total-results header + page-1 row dates) and retries once, then raises —
  the silent "2500 rows for a 1-day window" class can't recur.
- **Travis tax sale = RealAuction, plain HTTP, no login** (`tax_sale_travis.py`):
  calendar (`dayid=` on TAX SALE cells) → PREVIEW page primes a server-side
  session → `FNC=LOAD&AREA=W` returns JSON with Cause #, Adjudged Value, Est.
  Min. Bid, Account Number (TCAD parcel), truncated address. Owner/situs
  resolve from the parcel in enrichment (fire_damage pattern). The item
  details view needs a bidder login — never fetch it. The old
  `TRAVIS_TAX_SALES_URL` tax-office page is a 404.

## TX Data Source Details

All Texas data sources are **public** — no logins or credentials required for any county source. (tccsearch.org sits behind Cloudflare — see below — and the Odyssey portals use reCAPTCHA, but neither needs an account.)

- **tccsearch.org** (Travis foreclosure/probate/lien) — Infragistics UI grid. Click "Results List" to get all results as a batched PDF popup. No pagination needed. **Behind Cloudflare's managed "Just a moment…" JS challenge** — see "tccsearch.org Cloudflare" below; requires a headed browser + playwright-stealth + a **US** residential IP.
- **Odyssey portals** (Tyler Tech) — Used for probate court records across all 3 counties. Each county has its own URL (see `ODYSSEY_URLS` in `config.py`).
- **Travis Tax Office** — Tax delinquent data is a direct CSV download (no scraping). Tax sale listings at a separate URL.
- **MVBA Law Firm** — Posts tax sale PDFs covering Bell + Williamson counties. One scraper handles both.
- **Bell/Williamson County Clerks** — Standard HTML pages for foreclosure/trustee sale listings.

### tccsearch.org Cloudflare (Travis foreclosure/probate/lien) — hard-won 2026-07-22

All three Travis tccsearch scrapers share `scrapers/tccsearch_common.py`. The
site is behind **Cloudflare's managed "Just a moment…" JS challenge**. What was
learned running it down on Apify (the block had killed every Travis pull since
~2026-07-16, silently — the scrapers logged only "client framework not ready"):

- **The decisive factor is a US residential exit IP.** Cloudflare challenges
  low-reputation / non-US residential IPs and never lets them clear (verified:
  headed browser + full stealth + a random-country residential IP still stuck on
  "Just a moment…" after 60s across three different IPs). `main.py` now pins
  `create_proxy_configuration(groups=["RESIDENTIAL"], country_code="US")`
  (override via `proxy_country` input / `SIFT_PROXY_COUNTRY` env). With a US IP
  the challenge clears **sub-second** — often no interstitial at all.
- **Also required (necessary, not sufficient):** a **headed** browser
  (`launch_tcc_context`, headed by default; Xvfb on Apify — the Actor start
  command already wraps `xvfb-run`), `--disable-blink-features=AutomationControlled`,
  and the **`playwright-stealth`** evasion suite applied to the context
  (`Stealth().apply_stealth_async`). Stealth matters because headed-under-Xvfb
  otherwise leaks a SwiftShader WebGL renderer and `navigator.webdriver` — both
  bot tells; stealth spoofs WebGL to Intel and sets `webdriver=false`.
- **Keep the fingerprint internally consistent.** UA + platform are a **Linux**
  profile (`X11; Linux x86_64`) because on Apify the browser genuinely *is*
  Chromium-on-Linux — a Mac/Windows UA over a Linux browser mismatches the real
  `sec-ch-ua-platform` header and is itself a tell. Override for local debugging
  only via `TCC_HEADLESS=1`.
- **`tccsearch_common` helpers, in call order:** `launch_tcc_context(p)` →
  `pass_cloudflare(page)` (polls the "Just a moment…" markers away, reloads once
  at ~40% to pick up the cf_clearance cookie; folded into `wait_ready`) →
  `goto_with_retry(page, url)` (the disclaimer "Accept" fires its own redirect,
  so a manual goto races it → `net::ERR_ABORTED`; retried) → `wait_ready(page)`
  (waits for the ASP.NET framework **and** the Search button `#cphNoMargin_SearchButtons1_btnSearch`
  to render — the form lags the framework on a cold proxy connection, which
  otherwise threw "checkbox not checkable / btnSearch is null") → `safe_check`
  (15s per doc-type checkbox).
- **If Travis goes dark again:** first suspect the proxy landed a bad/again-non-US
  IP or the whole Apify residential pool got flagged; the `pass_cloudflare`
  failure log now reports `turnstile=True/False` — `True` means Cloudflare
  escalated to an embedded Turnstile widget needing a solved cf-clearance token
  (2captcha, already a dependency), `False` means it's still the IP/pool.

### Travis Tax Office CSVs — schema flip + corrupt rows — hard-won 2026-08-06

Both Tax Office bulk CSVs (`TaxDelqOpenData.csv`, `TaxCurOpenData.csv`) are
mainframe exports, and on **2026-08-05** the export changed underneath us. It
broke Travis tax-delinquent AND the whole Travis CAD cache for two days
*while the Actor kept exiting 0*. All shims live in **`src/travis_tax_schema.py`**.

- **`TaxDelqOpenData.csv` headers became raw field codes.** Same 31 columns,
  same order, new names: `Account #`→`ACCOUNT#`, `Owner Name`→`OWNERSNAME`,
  `1st Year Delinquent`→`SINCE`, `Property Zip`→`PROPZIP`, `Cause #`→`"00016"`,
  … Full table in `DELQ_ALIASES`. Rows are renamed back to the **friendly
  labels**, which stay the canonical vocabulary everywhere else
  (`tax_delinquent_travis`, `travis_texdel_cleaner`, `travis_tax_cache._load_delq`).
  Both layouts parse. **If the county renames again, extend `DELQ_ALIASES` —
  do not rewrite consumers.**
- **The failure was silent, which is the real lesson.** Every row fell through
  the scraper's `no_apn` guard, so the run logged `0 records kept
  (filtered: no_apn=9009)` and succeeded. `assert_delq_schema()` now **raises**
  when a header resolves none of `DELQ_REQUIRED`; the per-target handler in
  `scrapers/__init__.py` turns that into a loud `scraper failed` ERROR. Prefer a
  raised exception to a zero-record return for any schema drift.
- **The same export pads values to fixed width** — `STREETNAME` arrives as
  `'MILTON<29 spaces>ST   W'` (96% of surviving rows), and money gained decimals
  with a bare leading dot (`.00`). `normalize_delq_row()` collapses interior
  whitespace on every value; `_parse_address` collapses again for direct callers.
- **`TaxCurOpenData.csv` headers did NOT change** (`PARCEL`, `NAMELF`,
  `MAILINGADDRESS`, …) — but the county began emitting rows with an
  **unbalanced quote** (a `ZIPCODE` of literal `"""78`). That opens a quoted
  field that never closes, so `csv` swallows the remaining ~170 MB into one
  field and dies on `_FIELD_LIMIT`, taking all **471K records / 427K parcel
  keys** with it. `sanitize_csv_lines()` strips quotes from just the physical
  lines whose quote count is odd (1 line in 492,803; the 9 legitimately-quoted
  rows like `"DEDEDO, GUAM 96929"` are untouched). Applied to both files.
- **The same export also stopped QUOTING fields that contain a comma** (fixed
  2026-08-06). Such a row splits into >31 fields and everything after the
  offending column slides right; 167 of 8,986 rows on the 2026-08-06 roll, and
  **zero** in any archived pre-flip file. 130 are harmless (comma inside the
  trailing `LEGAL`, which `_repair_shifted_row` rejoins); 15 slid far enough to
  scramble the situs block, leaving `PROPZIP` holding a street name and `LEGAL`
  holding the ZIP (`PROPZIP='SPRINGDALE RD', LEGAL='78721'`). Those are
  re-anchored on the first **bare 5-digit** 786xx/787xx value at or after the
  `STR#` slot — bare because `Property Zip` is 5-digit while the mailing
  `Zip Code` is 9, and on a shifted row that 9-digit value can land in the situs
  block and yield `('AUSTIN','TX')` as the address. 22 rows carry no ZIP at all
  and are left alone. Counts are logged by `log_row_repair_stats()`.
- **`1st Year Delinquent = 0000` is NOT missing data** — it pairs perfectly with
  `Sequence # = 0` (all 5,221 such rows) and means *delinquent on the current
  roll only*; those rows owe ~half a year of tax by `DELQTOT/APPVAL` vs ~4 years
  for the 7+ year bucket. Excluding them under the 2+ year rule is correct, so
  they get their own `removed["current_roll_only"]` bucket. `no_year` now means
  "has a delinquent sequence but no parseable year" — **0 in normal operation**,
  and `NO_YEAR_ALARM_SHARE` (2%) raises an ERROR above that. Before the split,
  this bucket was ~58% of the file in *healthy* runs, so a genuine SINCE parse
  break would have looked exactly like a normal day — the same silent-failure
  shape as the Aug-5 incident.
- **Blast radius when this cache dies is everything Travis, not just tax-del** —
  fire-damage parcel→owner, lien address backfill, code-violation owners,
  probate property lookup. The symptom in the log is
  `cad_lookup: ... lookup failed: field larger than field limit` plus
  `0 delinquent-situs records indexed`, and the build is retried (and re-fails)
  on *every* lookup, so runtime inflates too.

## Data Sources

Configured in `config.py`:

| County | Type | Source |
|--------|------|--------|
| Travis | Foreclosure | tccsearch.org |
| Travis | Tax Delinquent | Travis Tax Office CSV (13K+ records) |
| Travis | Tax Sale | RealAuction (travis.texas.realforeclose.com, plain HTTP JSON) |
| Travis | Probate | Odyssey portal |
| Travis | Lien | tccsearch.org OPR (doc-type checkboxes) |
| Travis | Fire Damage | CTECC Real-Time Fire feed (Socrata `wpu4-x69d`) |
| Bell | Foreclosure | bellcountytx.com county clerk |
| Bell | Tax Sale | MVBA Law Firm PDFs |
| Bell | Probate | Odyssey portal |
| Bell | Lien | bell.tx.publicsearch.us (GovOS, **headed**) |
| Williamson | Foreclosure | wilco.org trustee sales |
| Williamson | Tax Sale | MVBA Law Firm PDFs |
| Williamson | Probate | Odyssey portal |
| Williamson | Lien | williamson.tx.publicsearch.us (GovOS, **headed**) |

## Liens (County-Clerk OPR) — `notice_type = "lien"`

Recorded liens (Abstract of Judgment, Federal/State Tax Lien, Mechanic's Lien, …)
are pulled from the same County-Clerk Official Public Records as foreclosures.
Scrapers: `scrapers/lien_travis.py` (Travis) + `scrapers/lien_publicsearch.py`
(Bell + Williamson). Wired into `config.NOTICE_TYPES`, the `(county,"lien")`
registry, `datasift_formatter` (`Lien` list + `lien`/lien-type tags + Notes), and
enrichment **Step 3c-lien**.

- **Travis — `tccsearch.org` (same site as foreclosure).** Doc-type checkbox
  indices captured live (130 types): AJ=1, Assessment=14, Estate Tax=49,
  Federal Tax=51, Hospital=56, Judgement=60, Mechanics=65, State Judgement=106,
  State Tax=107 (`DEFAULT_LIEN_DOC_TYPES`). Releases/transfers excluded. Runs
  headless like the other Travis scrapers.
- **Bell + Williamson — `{county}.tx.publicsearch.us` (Kofile/GovOS County
  Fusion). MUST RUN HEADED.** The anti-bot blocks *old headless* chromium
  (results stick on "Loading…" forever, no API fires); a real headed browser
  (`headless=False`) + automation-signal spoofing renders results. In
  Docker/Apify (Linux, no display) run under Xvfb (`xvfb-run -a …`). Override
  with `LIEN_PUBLICSEARCH_HEADLESS=1` (will likely return 0). Flow = Advanced
  Search: add lien doc types to `#docTypes-input` (react-select: type + Enter)
  **before** filling `#recordedDateRange-start/-end` (date picker overlays the
  doc-type field otherwise) → click the **exact** "Search" button (NOT
  "Search Criteria", a section toggle) → parse the tab-separated results table.
- **Lead = the GRANTEE (debtor), NOT the grantor (creditor).** On a tax lien the
  grantor is the IRS/State and the grantee is the taxpayer we want; on an AJ the
  grantor is the bank/abstract co. and the grantee is the judgment debtor.
  (Opposite of foreclosure, where `[R]`/grantor is the borrower.)
- **Name-indexed, no address.** Liens carry a debtor name but no property
  address. Step 3c-lien backfills it via `cad_lookup.lookup_property_by_name`
  (debtor name → in-county property). No match → blank address → dropped
  downstream, which also discards the mostly-business State Tax Liens (the same
  safety property as the sold flow).

## Fire Damage (Travis) — `notice_type = "fire_damage"`

Structure fires are the earliest worst-condition signal (a burned house sits
1-3 years before any code case). `scrapers/fire_damage_travis.py` pulls the
CTECC **Real-Time Fire Incidents** Socrata feed (`wpu4-x69d`, 5-min refresh,
rolling ~12 mo, street addresses included). In `DEFAULT_SCRAPE_TYPES` (unlike
code_violation); Travis-only. ~2-3 structure fires/day
(`BOX -Structure Fire` + `BOXL- Structure Fire`; BOXMID/BOXHI mid/hi-rise
excluded — override via `FIRE_DAMAGE_PROBLEMS`). Do NOT use `v5hh-nyr8`
("AFD Fire Incidents") — refreshed only ~quarterly, no street address.

Hard-won (2026-07-23):
- **Owner comes from parcel, not address.** Feed has no city/ZIP/parcel, and
  `travis_tax_cache` address keys only cover owner-occupants (absentee parcels
  are keyed by the owner's MAILING address). The scraper resolves each fire's
  lat/long → TCAD parcel via the Travis County GIS layer
  (`TCAD_public/MapServer/0`, 386K parcels); its `geo_id` keys straight into
  the parcel index (covers ALL owners), so enrichment Step 5's parcel-first
  path resolves owners exactly. Raised owner hits 4/16 → 12/16 live.
- **GIS quirks:** the server SILENTLY returns zero features for `inSR=4326` —
  points must be pre-projected to EPSG:2277 (inline pure-python Lambert in the
  scraper, ±2.5 ft vs the server's GeometryServer). CTECC points are snapped
  to the STREET CENTERLINE (inside no parcel) — query buffers 200 ft and
  accepts the candidate whose `situs_num` equals the feed house number.
- The GIS situs supplies the authoritative ZIP; Nominatim reverse geocoding is
  fallback-only (its postcodes are often PO-box ZIPs, e.g. 78715 for 78745).
- `travis_tax_cache._normalize_street` now abbreviates spelled-out
  suffix/directional words (STREET→ST, EAST→E, symmetric on build+lookup), and
  `search_by_address` falls back to a street-only match when the ZIP is wrong
  or missing — accepted only for a unique Austin-area (786xx/787xx) ZIP, which
  keeps out-of-county mailing keys from matching.

## Deep Prospecting v5 — SmartSkip heir engine (`src/smartskip.py`, 2026-08-11)

**The heir engine is SmartSkip.** Until now the heir set was extracted from
obituary prose by an LLM — the job that hallucinated a family in the Norman
Willis incident and forced the grounding guard in `obituary_enricher`. SmartSkip
returns an owner's RELATIVES **with their phone numbers** in one batch row, so
the research layer only has to *confirm* relationships and supply the date of
death. Upstream measured it against the Enformion Person Search on 12 identical
owners: Enformion returned zero relatives on **6 of 12**, SmartSkip hit 12/12;
100 owners / 682 relatives cost **$15.90 vs $78.20** (4.9x).

Stack: SmartSkip ($0.15/hit) → DirectSkip ($0.10/hit, no-match free — gap-fill
the ~7% of relatives left phoneless; replaced Tracerfy 2026-08-20) →
obituary/web research (**mandatory**, free) → Trestle ($0.015/number). About
$0.25 per record.

- **Enformion's Person Search is retired; BusinessV2 is RETAINED** for entity
  owners only (`src/enformion_business.py`) — SmartSkip needs a first and last
  name, and DirectSkip is consumer-only, so LLCs/trusts/estates have no other path.
  BusinessV2 returns actual SOS corp filings, so it is *grounded*, unlike
  `entity_researcher`'s DuckDuckGo+LLM inference — prefer it, keep the LLM path
  as fallback. Registered agents are **flagged, not dropped** (`is_registered_agent`):
  on a small family LLC the agent is usually the owner, on a large one a paid
  front (ZenBusiness is Austin-based and very common on TX LLCs).
- **SmartSkip is WRONG ABOUT DEATH.** No date-of-death column at all, and its
  `Deceased` flag returned false for a man with a published funeral-home
  obituary. It therefore NEVER sets `owner_deceased`/`date_of_death` — the flag
  is recorded as an observation in `smartskip_deceased_flag` and the research
  layer decides. `_status_for` never returns `verified_living`; a positive
  Deceased flag IS honoured (the flag under-reports death, so a "true" is
  credible, and either way the conservative direction keeps them out of the
  signing chain).
- **THE SPOUSE-OBITUARY TRAP.** An obituary on a record does NOT mean the OWNER
  died. A live record was worked as an heir case off the *husband's* obituary
  while the owner was alive and owned the property — a caller would have opened
  by asking for a dead man. Always match the decedent against the owner of
  record first; a living owner with a recent household death is a different and
  often better record. Surfaced in `deep_prospector._run_level_3`.
- **Relationship labels are coarse** ("Possible Type"; ~63% generic). They map to
  `relative`, which carries NO signing authority, and the entry is marked
  `relationship_unconfirmed`. `canon_relationship` is deliberately
  **gender-neutral** — SmartSkip supplies no gender and guessing one from a first
  name puts a fabricated attribute on a real person; the neutral terms
  (child/parent/sibling/spouse) are already understood by the TX intestacy
  classifier, so nothing is lost.
- **Signing authority comes from the ONE implementation.** `build_heir_map`
  reuses `obituary_enricher.rank_decision_makers` (Texas Estates Code) rather
  than a second copy that could drift; SmartSkip-only contact details are merged
  onto the ranked entries afterwards.
- **Money safety.** `submit`/`status`/`download` are FREE; `pay` is the only
  billing call, is never invoked by any pipeline path, and refuses unless
  `confirm_rows` matches SmartSkip's calculated count AND the order was submitted
  by this client. The **wallet does NOT fund bulk skip** — it bills the saved
  card. An UNPAID order is invisible in `GET /bulk-skip`, so the `bulkSkipId` is
  persisted to `data/smartskip/orders.json` before payment is possible.
- **Entities are filtered OUT of the batch up front** (`build_trace_csv`), not
  discovered per record — they would bill as misses.
- Owner phones land in the flat dial slots; **relatives' phones stay inside
  `heir_map_json`** so a relative's number is never dialled as the owner's.

**Status: the HTTP lifecycle is VERIFIED live (2026-08-17).** A 3-record probate
batch ran end-to-end through `skip_orchestrator`: signin → submit → calculate (3
rows) → `pay` (amex ...1007, $0.45) → `wait_for_completion` → download → `parse_export`
(format 1, 3/3 with results). Parsing, the heir-map bridge, `apply_export`, and the
payment guards were already verified offline against both export layouts.

## DirectSkip skip trace (`src/directskip.py`, 2026-08-17) — API LIVE-VERIFIED

DirectSkip is a second skip-trace vendor, wired the SmartSkip way (operator-run
CLI, NOT in the pipeline), but it is **single-record and synchronous**, so the
client follows the stateless `phone_validator` shape, not
`SmartSkipClient`'s batch lifecycle. It reuses SmartSkip's hygiene + heir-map
helpers by import — no divergent copies.

- **API (primary), confirmed + live-tested:** `POST
  https://api0.directskip.com/v2/search_contact.php`, headers
  `Accept/Content-Type: application/json`, auth = `api_key` **in the JSON body**.
  Earlier probing of `api.directskip.com/v1/` was silent because that host/version
  is wrong. Request fields: `first_name,last_name,mailing_*,property_*` (+ optional
  `custom_field_1..3`, flags `auto_match_boost/dnc_scrub/owner_fix`). Response:
  `{status:{error}, input, result_code, contacts:[{names,phones,emails,confirmed_address,relatives}]}`.
- **Auth is IP-allowlisted** (registered via support@directskip.com with the
  account email + public IP). The operator machine (`99.67.238.70`) is registered
  and authenticates. **Apify has no static egress IP** — run the API only from the
  fixed-IP operator box; use the portal transport (cookie auth, not IP-bound) if a
  cloud run ever needs DirectSkip.
- **Money safety:** pay-per-hit, `DIRECTSKIP_COST_PER_HIT=0.10`; a **no-match bills
  $0** (verified live with a fake person). `batch_search` enforces
  `MAX_DIRECTSKIP_COST_USD` (default $5) — it never issues a call that could push
  spend past the cap, and a no-match doesn't count. Single `search` = one hit max.
- **`ResultCode` `CI`** = real match; **`AB1`/`AB2`** = address-only match that
  returns a **different person** — never treated as the owner, never fills a dial
  slot, never promoted to DM (`address_only_match`). Blank = no match.
- **Same trust boundaries as SmartSkip:** `Deceased` is an observation
  (recorded in `smartskip_deceased_flag`, never sets `owner_deceased`); owner
  phones → dial slots, relatives' phones stay in `heir_map_json`; signing
  authority via the ONE `obituary_enricher.rank_decision_makers`. Heir entries are
  tagged `source="directskip"` (deep_prospector sniffs this).
- **Portal (fallback):** `app.directskip.com` — plain `login.php` (no captcha/CSRF),
  orders at `files.php`, results at `download.php?code=<hash>`, credit balance on
  `index.php`. The 266-col `contactinfo` CSV parses via `parse_export` (same
  required-header gate as the `directskip-datasift-clean` skill). The submit wizard
  (`neworder.php`, steps 1-2 mapped: `submit_step1` upload → `header_*` select
  mapping) spends prepaid credits only at its step-4 confirm; that confirm path is
  intentionally not wired yet (needs one live discovery run of steps 3-4).
- **Verified offline:** `tests/test_directskip.py` (11 tests, mocked HTTP) — both
  parsers converge, AB never becomes owner, deceased stays an observation, cost cap
  stops the batch and a no-match is free, entity skip. **Verified live ($0):** the
  no-match API round-trip through the real client.

## Multi-provider skip-trace orchestrator (`src/skip_orchestrator.py`, 2026-08-17)

One DataSift records **export** in → SmartSkip + DirectSkip + Trestle → one merged
DataSift-ready **upload CSV** with per-number provenance. The `directskip-datasift-clean`
skill cleans a *single already-returned* vendor file; this orchestrates a *live
multi-provider run* from an export. Operator CLI, NOT wired into `main.py`; does
NOT upload (DataSift API is read-only — the CSV goes through the wizard).

- **Flow:** `load_datasift_export` (read-only; maps DataSift export headers →
  `NoticeData`, flags entities out) → `estimate` (free rundown) → `run` (SmartSkip
  bulk FIRST — primary vendor, owner preference 2026-08-20 — then DirectSkip
  per-record sync as the cross-check, then Trestle-score every unique number) →
  `merge_record` → tier-eviction to the phone cap → `write_upload_csv`.
- **Merge with provenance** (the point of this module): every `MergedPhone` carries
  a `providers` set, so the Notes label each number `[Dial First 92] [SmartSkip+DirectSkip]`.
  Relatives are unioned across vendors by name; **SmartSkip's relationship label wins**
  (DirectSkip relatives have no relationship). A **PROVENANCE / DISCREPANCIES** block
  lists only-SmartSkip vs only-DirectSkip numbers and each vendor's exclusive relatives.
- **Prioritize + cap:** `select_survivors` keeps Dial First/Second and evicts worst
  first (invalid → Drop → Dial Fourth → unscored → Dial Third) to fit `--phone-cap`
  (default 30). Same algorithm as `clean_directskip.py` (shared logic, not shared code —
  the skill is a self-contained distributable and can't import `src`).
- **Money:** `estimate` spends nothing. `run` is gated by a hard `--max-cost` ceiling
  across all three; SmartSkip's card charge additionally needs `--confirm` (mirrors
  `smartskip.pay`). DirectSkip no-match is free; Trestle dedupes before billing; if the
  Trestle budget is short, the cheapest-to-skip numbers stay UNSCORED (logged), never
  silently dropped. Rates: SmartSkip $0.15, DirectSkip $0.10, Trestle $0.015/number.
- **Litigator (TCPA):** Trestle's `litigator_checks` add-on is ON for every number
  (`DEFAULT_ADD_LITIGATOR`, verified in the live run). A flagged number is **withheld
  from the dial slots entirely** (never uploaded to a Phone column) but kept in Notes,
  loudly labelled `(!) LITIGATOR - DO NOT CALL`, plus a withheld-summary block — so the
  record's OTHER numbers still reach the person. By operator preference there is **no
  record-level `litigator` tag** (it would suppress the whole record).
- **Output:** the 30-phone DataSift layout (`Property Street Address…Phone 1..30,
  Email 1..6, Tags, Notes, Owner Deceased`). Record `Tags` include `skip_traced_MM/YYYY`
  + provider tags (`SmartSkip`/`DirectSkip` — whoever contributed) + `living`/`deceased`.
  Per-number tier/provider/relative labels live in **Notes** (the guaranteed channel;
  DataSift appends behind existing phones, so labels are number-keyed, never slot-keyed).
- **CLI:** `python src/skip_orchestrator.py estimate --input export.csv` /
  `... run --input export.csv --max-cost 50 --confirm --out upload.csv`. Skill:
  `datasift-skiptrace-run`. Tests: `tests/test_skip_orchestrator.py` (11, offline).
  `run` continues DirectSkip-only if SmartSkip fails, so output is always produced.
- **Loader note:** DataSift EXPORTS use Title-first-word headers (`Property address`,
  `Apn`, `First Name`); the loader matches case/punctuation-insensitively, prefers the
  5-digit `zip5` columns, and reads the deceased flag from the record's own `Tags`.
  Vendor names arrive ALL-CAPS and are title-cased (`_title`) — ALL-CAPS is a red flag.
  Real exports can be **ragged** (row field-count ≠ header); only late columns (e.g.
  APN passthrough, unused) are affected, never the early name/address fields.
- **Verified live 2026-08-17** on a 3-record Williamson probate export: DirectSkip 3/3,
  SmartSkip 3/3 (first live run — charged the card), Trestle 66 numbers, $1.74. Merged
  output showed `[Dial First 100] [SmartSkip+DirectSkip]`, SmartSkip relationships
  (child/in-law), DirectSkip-only relatives in the discrepancies block, all title-cased.

## SKU-grounded rehab materials (Austin 78704) — captured 2026-08-20

`rehab_estimator` prices the MATERIAL side of each category from a locked list
of real Home Depot SKUs local to **zip 78704**, instead of the blended national
cost tables. Labor never comes from the list; the engine keeps its labor tables
and the regional multiplier applies **to labor only**. Locked prices are already
Austin-local, so multiplying them by the 0.95 Austin factor would double-discount
materials — `sku_pricing` exists partly to enforce that.

- **Capture:** `python src/material_list.py --master --zip 78704 --lock` →
  `data/master_materials_locked_78704.{json,csv}` + `Master_Material_List.xlsx`.
  94 SerpApi `home_depot` searches (`delivery_zip` = local store pricing). The
  key is **SerpApi** (`serpapi.com`), read from `SERPAPI_KEY` *or* `SERPAPIKEY`.
  **This is NOT Serper.dev** (`SERPER_API_KEY`, `google.serper.dev`, used by
  `obituary_enricher`) — unrelated vendors, non-interchangeable keys.
- **Coverage:** `SKU_REGIONS = ("austin","travis","williamson")` — one Austin
  Home Depot metro. **Bell is deliberately excluded** (Killeen/Temple is a
  separate metro 60 mi north) and stays on the engine tables until
  `--master --zip 76541 --lock` captures its own list. Tier 4 is off-list by
  definition. A wrong-ZIP lock file is rejected, not silently used.
- **Degrades safely.** Missing file, wrong ZIP, uncovered region, tier 4, or any
  single missing SKU → that whole category falls back to the engine table,
  loudly. Output is then byte-identical to `use_locked_materials=False`.
  `estimate.materials_source` stamps which basis was used.

### Hard-won during the capture (2026-08-20)

- **`hd_sort=top_sellers` silently returns the store's GENERIC top sellers when
  a query does not map to a category.** `"outdoor wall light"` returned
  retaining-wall block and joint compound; `"interior door knob"` returned caulk
  and shiplap; `"30 in range hood"` returned ovens. All 5 affected keys scored
  **zero** relevance-regex matches. Dropping the sort recovered 22/22/16 correct
  in-band products. `pull_prices` now falls back to relevance sort **only** when
  top-sellers yields nothing, and records the basis per key (`"sort"` in the
  cache). top_sellers stays the default — "median in-band top-seller" is the
  documented pricing basis and it works for 89 of 94 keys.
- **The relevance regex is the quality gate, so it must pin the SIZE.**
  `pt_2x8`'s regex was `2 in\. x 8 in\.` with no length, which would have
  priced a **2x8x16 at $27.18 into a row labelled "PT 2x8x12"** — a ~35% unit
  error, silently. Regexes now require the length. A blank row beats a
  wrong-size substitution.
- **The cache checkpoints every 5 searches** (`CHECKPOINT_EVERY`, atomic
  temp-file swap). It previously wrote only after all 94 finished, so any
  timeout/crash burned every paid search and left `--cached` nothing to resume
  from. `--cached` refetches only keys absent from the cache, which is what made
  the 17-key and 5-key backfills cost 17 and 10 searches instead of 94 each.
- **SerpApi read-timeouts arrive in waves** (23 in the first pass; the client
  retries 3x with backoff). They are **not billed** — 109 searches delivered
  94 keys and the account charged only for delivered results. Budget by
  delivered keys, not attempts. A full capture takes ~1.5 h wall-clock because
  of this, so run it detached, never inside a tool timeout.
- **Baskets are FIXED quantities, not sqft-scaled** — Kitchen, baths and HVAC
  cost the same for a 900 sf and a 4,000 sf house. This is inherited upstream
  behaviour and the **engine tables have the identical property**, so it is not
  a regression introduced by SKU grounding; it is a shared limitation. Only
  flooring/paint/roof/windows scale. Retune `sku_pricing`'s basket quantities
  (6 base + 6 wall cabinets, 2 countertop sections, 35 sf backsplash, 45 sf bath
  floor) before trusting a large-house estimate.
- **Deltas vs the engine tables** (1,500 sf 3/2, built 1960, Austin): tier 1
  materials **+$3,759** (engine under-priced budget SKUs), tier 2 **-$9,307**,
  tier 3 **-$29,204**. Grand totals move +11.7% / -9.5% / -20.3%. The tier-2/3
  drops are partly real Austin pricing and partly the fixed-basket issue above —
  treat tier 3 with suspicion until the quantities are retuned.

## Key Domain Rules

- **Probate owner_name** should be the Personal Representative/Executor/Administrator — not the deceased.
- **Address dedup:** Same property can appear in multiple notices; `data_formatter.deduplicate()` keeps the most recent.
- **ZIP code filtering:** Only notices in investor-active ZIPs (from `target_zips.json`) pass through. Threshold: 10+ investor transactions/month per ZIP.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page. Travis CSV download needs no rate limiting.
- **Texas is a NON-DISCLOSURE state** — sale prices are NOT in public records. (This
  file previously claimed the opposite; the `real-estate-comping` skill always had it
  right: *"Texas | TX | Largest non-disclosure market... CAD is primary data source"*.)
  Consequence, verified live 2026-08-11 against OpenWeb Ninja `/search RECENTLY_SOLD`:
  Zillow returns TX sold listings with **no sale price** — 0/41 priced in 78723, 76541
  and 78664, versus **41/41** in Knoxville TN, Atlanta GA and Phoenix AZ. TX *list*
  prices come back fine (41/41), so this is disclosure, not an API or key problem.
  **Sold-price comping cannot work in our markets**; value must be triangulated from
  the CAD card + Zestimate / tax-assessed value (see the skill's
  `non-disclosure-prompt.md`). `comp_analyzer.fetch_comparable_sales` now logs this
  cause explicitly instead of returning a bare empty list.
- **Texas has no state transfer tax** — closing cost calculations should not include transfer tax.

## Owner-Name Orientation (NAMELF) — the #1 silent data corruptor

Owner names enter from sources with **two incompatible orientations**, and getting it
wrong ships reversed contacts to marketing. Rules, learned the hard way (2026-07-16):

- **`tax_owner_name` is ALWAYS raw `LAST FIRST MIDDLE` (NAMELF, no comma)** — that is
  its contract. `owner_name` is ALWAYS consumed as `FIRST [MIDDLE] LAST`. Never assign
  one to the other without conversion.
- **CAD/TCAD/BCAD `fullname` is NAMELF** (`"SPENCER KIMBERLY ANN"` = Kimberly Spencer).
  Convert with `parse_tax_owner_name()` (obituary_enricher) or
  `property_lookup._cad_owner_to_first_last()`. **`_clean_cad_owner_for_display()` only
  title-cases — it does NOT flip.** Never use it as an `owner_name` value.
- **NEVER fall back to the raw NAMELF string when the parser fails.** The positional
  split (`_split_name`: `parts[0]`=first) reverses it AND can drop the real first name
  (`"SPENCER KIMBERLY ANN"` → First=Spencer, Last=Ann — "Kimberly" vanishes). Instead
  route the value to **Business Name** — never fabricate a person. In practice every
  unparseable non-entity is an org `_is_entity_name` misses (`"MT ZION BAPTIST CHURCH"`
  → First=Mt/Last=Church; churches, housing authorities, rental cos), so the record
  keeps a marketable owner and loses nothing. A fabricated name is worse than no person:
  skip-tracing "Pleasanton Finance" burns a lookup or returns a **stranger's** phone.
- **`normalize_court_name()` blindly flips** — only call it from scrapers whose source
  is *known* LAST-FIRST. It is NOT idempotent and will wrongly flip an already-correct
  `FIRST LAST` name. **CAD is not uniformly NAMELF** (`"DONALD D BRIM & DANIELLE BRIM"`
  is FIRST-LAST), so blind flipping corrupts real records.
- **ALL-CAPS `Owner First/Last` in an output CSV is a red flag** — every correct path
  title-cases. ALL-CAPS ⇒ the value bypassed normalization and is probably reversed.
  Use it as a cheap audit signal.
- **Never "repair" names by swapping First/Last.** The pair is lossy (middle names are
  already dropped). Re-derive from CAD by `parcel_id`
  (`cad_lookup.lookup_property_by_parcel`) and re-run `parse_tax_owner_name`.
- **Only `code_violation` (and ownerless foreclosures) may take the CAD owner.** Their
  scrapers set `owner_name=""` by design. **Probate** owner must stay the PR/executor
  and **lien** owner must stay the grantee/debtor — overwriting those from CAD violates
  the domain rules above.
- **Classify business-vs-person on the PRISTINE name, never on the stripped one**
  (fixed 2026-08-06 in all three tax-delinquent scrapers). `strip_etux_etal()`
  cuts at the first ` & `, so classifying afterwards decapitated every entity
  whose own name contains one: `"S & B UNLIMITED LLC"` → `"S"`, `"M & JAD LP"` →
  `"M"`, `"BECHTOL ROY & LYNN FAMILY TRUST"` → `"Bechtol Roy"`. The `LLC`/`TRUST`
  went with the tail, so `is_business()` then read the remainder as a person —
  **59 Travis records** shipped fabricated owners like "Lamb", "Gold", "Rosa".
  Callers now pass `strip_spousal_tail=False` once a name is known to be an
  entity (entities have no spouse). Note the ALL-CAPS audit signal does NOT
  catch this — `title_case()` makes the stub look like a plausible person.
- **Bare `HOA` is not an HOA marker mid-name.** It's the Vietnamese given name
  Hoa, and `\bHOA\b` was silently dropping real leads as homeowners
  associations (`"HUYNH ANH HOA THI"`, `"NGUYEN HOA V & OANH K"`,
  `"ESTATE OF CHANG HENRY HOA"` — an estate, i.e. a probate lead). `HOA` now
  counts only as the first/last token or before a corporate suffix
  (`_HOA_ABBREV`, mirrored in all three scrapers); `ESTATE_PAT` outranks it.
  Real HOAs in the roll are uniformly `"<PLACE> HOA [INC]"`.
- Audit with a name oracle (`pip install names-dataset`), scoring current vs flipped
  orientation — but treat it as a *hint only*: ambiguous names (`Shay Dori`,
  `Marshall Hussain`) score "backwards" while being correct. CAD is the only truth.

## Tax-Delinquent Cross-Run Diff + Sold Tagging

All three tax-delinquent scrapers (Travis CSV, Bell/Williamson XLSX) diff each pull against the prior run's parcel-ID set and persist state across runs (`data/{county}_tax_state/` locally; Apify KVS keys `travis_texdel_state` / `bell_texdel_state` / `williamson_texdel_state`). State modules: `travis_texdel_state.py` (Travis) and the county-parameterized `tax_delinquent_state.py` (Bell + Williamson). Per-run diff JSON + raw-file archive are written for forensics; the diff is surfaced to Slack via `--notify-slack`.

- **Property city comes from the property ZIP, never the roll's `City` column.**
  The Travis delinquent roll has no property-city field — `City` is the OWNER'S
  MAILING city. Seeding Property City from it was right for owner-occupants and
  wrong for every absentee: `"5505 Manor Rd / Wilmington / TX 78723"`, plus
  Houston/San Angelo/Dallas scattered through a Travis-only list (~10% of rows).
  Smarty does NOT repair it downstream — it runs `MatchType.STRICT` and returns
  no candidate at all when city and ZIP disagree, so the bad value passes
  straight through. Use `travis_texdel_cleaner.city_for_zip()` (58 ZIPs, derived
  from the roll's own owner-occupant rows); `travis_tax_cache._load_delq` uses it
  too, instead of the blanket `"AUSTIN"` that mislabelled every Del Valle /
  Pflugerville / Manor / Leander situs. The derivation itself now lives in
  `build_zip_city_lookup()` and runs on every scrape (`learn_zip_cities()`), so
  a ZIP absent from the static table resolves from the current roll instead of
  silently defaulting to Austin — and any true fallback is logged once per ZIP.
  It reproduces all 58 static entries **exactly** from both a post-flip and a
  pre-flip roll, which is the table's regression test. Boundary ZIPs seen only
  once or twice (78619/78626/78628/78633/78642/78665/78681/78712/78717) can
  never reach the 3-vote floor, so they are pinned to their USPS city by hand.
- **DataSift normalises addresses on ingest** — it stored `Austin / 78723-4705`
  for the row we uploaded as `Wilmington`, and reorders directionals
  (`303 Pheasant Dr E` → `303 E Pheasant Dr`). So the CRM's copy can differ from
  both our CSV and the state snapshot. When re-uploading to match an existing
  record (Sold tagging), verify against the live record via
  `DataSiftAPIClient.search_records` rather than trusting the snapshot. Records
  with no house number (`Summer Lake Dr`) fail DataSift's validator and keep
  whatever we sent — those keep the old city.
- **The `last_run_records` snapshot is captured PRE-enrichment** —
  `snapshot_records()` runs inside `scrape()`, before Smarty/obituary/DM steps.
  Anything rehydrated from it (Sold rows) therefore carries raw scrape values,
  not what was uploaded. Fixing the city at scrape time (above) keeps the
  snapshot honest; don't assume snapshot == CRM.
- **Diff terms:** `NEW` = current − previous, `REPEAT` = current ∩ previous, `DROPPED` = previous − current (off the roll = paid off / sold).
- **Guardrails** (`check_guardrails`) suppress false "sold" claims: empty file, APN-format drift (<85–90% match), or >50% volume shrinkage trips the guardrail — the prior baseline + snapshot are preserved and no drop/sold claims are reported.
- **`last_run_records` snapshot:** each successful run stores `parcel_id → {address, city, state, zip, owner_name}` for the records it actually **uploaded** (post-filter). This lets the next run rehydrate any dropped parcel into a full record (not just an APN).
- **Sold flow (the round-trip you upload):** dropped parcels that were in our prior upload become `NoticeData(record_status="sold")` — exposed as each scraper's module-level `LAST_RUN_SOLD`, collected by `main._collect_sold_records()` after enrichment, and appended to the same tax-delinquent upload CSV. The formatter (`datasift_formatter._build_tags` / `_build_row`) tags them exactly **`Sold`** with a **blank Lists** column and blank tax/value fields. On upload, DataSift matches by property address → adds the `Sold` tag to the existing record → the **"Sold Property Cleanup"** sequence fires (status→Sold, remove from lists, clear tasks/assignee). New parcels in the same file upload as normal.
- **Safety property:** only previously-**uploaded** dropped parcels become Sold rows — a parcel that left the roll but was filtered out (never in DataSift) is intersected away, so no junk records are created. Sold rows bypass enrichment/skip-trace and are never added to the daily `seen` set.

## Output

CSV files land in `output/` (gitignored). Logs go to `logs/` with timestamped filenames. Sift columns: `date_added, address, city, state, zip, owner_name, notice_type, county, source_url`.

## Apify Deployment

The project runs as an **Apify Actor** in the cloud. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set, `main.py` uses the Actor SDK instead of CLI args.

```bash
# Install Apify CLI
npm install -g apify-cli

# Local test (reads input.json, simulates Actor environment)
apify run --purge

# Deploy to Apify platform
apify login
apify push

# On Apify Console: set up daily schedule and configure secrets in Actor input
```

### Actor Input (configured in Apify Console or `input.json`)
- `mode`: "daily" or "historical"
- `counties` / `types`: arrays to filter sources (empty = all)
- `google_drive_folder_id`, `google_service_account_key`: optional Google Drive upload

### Actor Output
- **Dataset**: structured records pushed via `Actor.push_data()`
- **Key-value store**: `output.csv` backup
- **Google Drive** (optional): CSV + summary text file uploaded via service account

### Key Files
- `.actor/actor.json` — Actor manifest (name, version, Dockerfile path)
- `.actor/input_schema.json` — Input fields + validation for Apify Console UI
- `Dockerfile` — Based on `apify/actor-python-playwright:3.12`
- `src/drive_uploader.py` — Google Drive upload via base64-encoded service account key
- `input.json` — Local test input (gitignored, contains credentials)

## Courthouse Photo Pipeline (build 1.0.28+)

Courthouse terminal photos → OCR → LLM parse → enrichment → DataSift. Runner takes phone photos at Travis/Bell/Williamson county terminals, uploads to Dropbox organized as `{county}/{notice_type}/`, system auto-processes.

### Notice Types (7 total)
- `foreclosure`, `tax_sale`, `tax_delinquent`, `probate` — existing from web scraper
- `eviction` — plaintiff = landlord (target contact), defendant = tenant
- `code_violation` — owner of record, violation type, compliance deadline
- `divorce` — petitioner + respondent, property from schedule page

### Critical OCR Patterns (hard-won from live testing)

**Moire pattern from terminal screens is the #1 OCR killer.** Standard Tesseract preprocessing (adaptive threshold, CLAHE) produces garbage on courthouse terminal photos. The fix:
- **Bilateral filter** (`cv2.bilateralFilter(gray, 15, 75, 75)`) removes moire while preserving text edges
- **Otsu threshold** (`cv2.THRESH_BINARY + cv2.THRESH_OTSU`) after bilateral — auto-determines optimal binary threshold
- **PSM 4** (single column variable text) for terminal screens — NOT PSM 6 (single uniform block) which was the research recommendation but fails in practice
- **Do NOT use `fix_rotation()` (Tesseract OSD) on phone photos** — EXIF transpose handles rotation. OSD on raw phone images often fails and the 270° fallback rotates correct images sideways

### Probate Deep Prospecting (from courthouse terminals)

Courthouse probate records have decedent name + PR/executor name but NO property address. Multi-tier lookup fills the gap:

**Property Address Lookup** (Step 3c in enrichment pipeline):
1. **Tier 1: CAD name search** — search TCAD/BCAD/WCAD by decedent name, score by token overlap (FIRST MIDDLE LAST → LAST FIRST MIDDLE), accept >= 0.4 match. Tries multiple name variations (with/without suffix, LAST FIRST format, first+last only).
2. **Tier 2: Executor family search** — search CAD by executor name, look for properties where decedent's last name appears in owner field (family property transferred to executor).
3. **Tier 3: People search** — search TruePeopleSearch/FastPeopleSearch for decedent's last known address in the county.

**Probate Preset** (obituary enricher):
- Triggers when court record has PR name + decedent name (no address required) — prevents wrong obituary from overriding court-named executor
- Sets DM = the named PR/executor directly, skips obituary search entirely
- Then runs DM address lookup (CAD → People Search → DirectSkip)

**DOD Sanity Check** (obituary enricher):
- Rejects obituary matches where DOD is > 3 years before the notice filing date (`MAX_DOD_GAP_YEARS = 3`)
- Prevents matching a 2014 obituary to a 2025 court filing (wrong person with same name)
- Applied to both full-page and snippet matches

**Heir-Verification Budget** (obituary enricher, `build_heir_map`) — cost guard:
- Each survivor verified = a Claude call (obituary search + parse), and deceased
  heirs recurse into their sub-heirs, so one obituary listing a large family fans
  out into dozens of calls. A couple of 20+-survivor records on 2026-07-10/11
  drove daily runs to 525-920 Claude calls (normal ~30) and blew the Actor
  timeout mid-verification.
- Caps: `MAX_HEIRS_VERIFIED = 6` (depth-0 survivors) + `MAX_SUBHEIRS_VERIFIED = 6`
  (depth-1 sub-heirs), and `MAX_HEIR_DEPTH_CEILING = 1` clamps recursion depth
  regardless of the `max_heir_depth` input (so the schedule input value is now
  effectively moot). All three are env-overridable
  (`OBITUARY_MAX_HEIRS_VERIFIED` / `_MAX_SUBHEIRS_VERIFIED` / `_MAX_HEIR_DEPTH`).
- Closest heirs (executor > spouse > children > …, via `_heir_verify_priority`)
  are verified first so the cap keeps the people with signing authority.
- **Nothing is lost:** survivors beyond the cap stay in `heir_map_json` with
  status `unverified` + a `verification_skipped` flag, so every name/relationship
  still renders in the DataSift **Notes** field (`_build_heir_summary`) — the
  signing chain labels them "NOT VERIFIED (reference only)" and the OTHER FAMILY
  section (no longer truncated) lists them all as "reference only — not verified".

### Dropbox Folder Structure
```
{DROPBOX_ROOT_FOLDER}/
├── Travis/
│   ├── eviction/
│   ├── code_violation/
│   ├── divorce/
│   ├── foreclosure/
│   ├── tax_sale/
│   └── probate/
├── Bell/
│   └── (same subfolders)
└── Williamson/
    └── (same subfolders)
```

### Environment Variables
- `DROPBOX_APP_KEY` — Dropbox OAuth2 app key
- `DROPBOX_APP_SECRET` — Dropbox OAuth2 app secret
- `DROPBOX_REFRESH_TOKEN` — Dropbox offline refresh token (auto-rotates access tokens)
- `DROPBOX_POLL_INTERVAL` — seconds between polls (default 900 = 15 min)
- `DROPBOX_ROOT_FOLDER` — root folder path in Dropbox (e.g., "TX County Data")

### Dependencies (added to requirements.txt)
- `opencv-python-headless>=4.13.0` — image preprocessing (headless = no GUI, saves 26MB in Docker)
- `numpy>=1.26.0` — required by OpenCV
- `dropbox>=12.0.2` — Dropbox SDK (minimum for post-Jan-2026 API compatibility)

## DataSift.ai (REISift) Integration

DataSift.ai (formerly REISift) is the CRM where scraped records land for niche sequential marketing campaigns. There is no public REST API — the reverse-engineered internal API exists (`src/datasift_api_client.py`, `apiv2.reisift.io`) but is **READ-ONLY by owner policy**; all writes go through the upload wizard (manual CSV or Playwright browser automation of the web UI).

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). API at `apiv2.reisift.io`.

### ⚠️ The internal API is READ-ONLY — owner policy (2026-08-12)

The `DataSiftAPIClient` must **never upload, edit, tag, or delete anything**.
It exists to READ: `search_records` (records by filter, for export),
`list_lists`/`list_tags`/`get_statuses`/`list_sequences`, custom-field reads,
and the free skip-trace **cost estimate**. Every write path is hard-blocked at
runtime (`_enforce_read_only` raises `DataSiftReadOnlyError` before any
network I/O — verified offline 17/17). Context: an unscoped API delete wiped
the whole account on 2026-07-14.

- Do NOT bypass the guard, hit the endpoints with raw `requests`, or set
  `DATASIFT_API_ALLOW_WRITES=1` without the owner's explicit instruction for a
  specific one-off.
- CRM mutations (uploads, tags, sequences, deletes) go through the DataSift
  upload wizard / UI — manually or via `datasift_uploader.py` (Playwright).
- The API-upload path in `main.py` (`--upload-datasift-api` /
  `upload_datasift_api`) is dormant: `false` in the production schedule, and
  the client now blocks it anyway (it falls back to the Playwright path).
- Read-driven workflow this enables: export records matching a filter (e.g.
  "smart-skipped + direct-skipped" preset) → run SmartSkip/DirectSkip/Trestle
  outside the CRM → build the upload CSV → upload via the wizard.

### Key Files
- `src/datasift_formatter.py` — Transforms `NoticeData` → DataSift CSV (64 columns)
- `src/datasift_uploader.py` — Playwright login + upload wizard + enrich + skip trace + preset management + sequence builder + SiftMap sold workflow
- `test_datasift_upload.py` — Headed browser test (upload + enrich + skip trace)
- `test_manage_presets.py` — Headed browser test (preset discovery + sold exclusion + sequence creation)
- `test_manage_sold.py` — Headed browser test (SiftMap sold property tagging)

### CSV Column Structure (64 columns)
- **Core auto-mapped (11):** Property Street/City/State/ZIP, Owner First/Last Name, Mailing Street/City/State/ZIP, Tags
- **Lists + Notes (2):** Lists (for niche sequential), Notes (contextual per notice type)
- **Uploadable fields:** MSL Status, Assessed Value (County), Assessed Value Year, Back Taxes Amount, Years Delinquent, Tax Auction Date, Foreclosure Date, Probate Open Date, Personal Representative, APN
- **Custom fields:** Notice Type, County, Date Added, Owner Deceased, Date of Death, Decedent Name, Decision Maker, DM Relationship, DM Confidence, DM 2/3 Name/Relationship, Obituary URL, Source URL

**The wizard has only 253 drop targets, and columns without one are DISCARDED
SILENTLY — the upload still succeeds** (dumped live 2026-08-11 by
`src/wizard_discover.py`, which is read-only and never clicks "Finish Upload").
Nine columns we had documented as "built-in, auto-mapped" had **no target at
all** and were thrown away on every upload: `Estimated Value`, `Last Sale
Date`, `Last Sale Price`, `Equity Percentage`, `Structure Type`, `Year Built`,
`Living SqFt`, `Bedrooms`, `Bathrooms`, `Lot (Acres)`. They are **removed, not
remapped** — DataSift fills all of them itself from SiftMap during "Enrich
Property Information", so we were duplicating work that was being deleted.
Three more were renamed to the real target names: `Parcel ID`→**`APN`**,
`Tax Deliquent Value`→**`Back Taxes Amount`**, `Tax Delinquent Year`→
**`Years Delinquent`** (it always held a *count* of years, never a calendar
year). `list_validator._HEADER_ALIASES` carries the old→new mapping so the
pre-upload gate keeps passing. **Before adding any column, dump the targets —
do not assume a plausible-sounding field exists.**

- **`Assessed Value (County)` is ours to carry.** DataSift's enrichment knows a
  Zestimate, not the CAD. The value stored is `totalpropmktvalue` (CAD TOTAL
  MARKET value — all three counties publish it, and it is the like-for-like
  comparison against a Zestimate); `src/assessed_value.py` fills it from the CAD
  roll and OVERWRITES the Zillow `taxAssessedValue` fallback in
  `property_enricher`. A wide Zestimate-over-CAD gap is real signal
  (under-assessed equity); the reverse suggests an over-appraisal the owner may
  be protesting.

### Niche Sequential Marketing
DataSift's niche sequential system uses filter presets to guide records through SMS → Call → Mail → Deep Prospecting phases. Two preset folders: "00 Niche Sequential Marketing" (12 presets, courthouse data) and "01. Bulk Sequential Marketing" (9 presets, bulk data). All 21 presets exclude Sold status (build 1.0.23). A "Sold Property Cleanup" sequence in the Transactions folder auto-fires on "Sold" tag to change status, remove from lists, clear tasks, and clear assignee.

- **"Courthouse Data" + "FTM" tags:** Every record gets both — first-to-market
  county data, pulled before any bulk vendor sees it (prioritized over bulk data
  in filter presets). Sold/Resolved cleanup rows return earlier with their own
  minimal tag sets and never pick these up — they are not new-to-market.
- **Lists column:** Maps `notice_type` → DataSift list name. Names match the account's existing built-in lists so the Sold/cleanup sequences (which act on list titles) fire: `foreclosure` → "Foreclosure", `probate` → "Probate", `tax_sale` → "Tax Sale", `tax_delinquent` → "Tax Delinquent", `eviction` → "Eviction", `code_violation` → "Code Enforcement", `divorce` → "Divorce", `lien` → "Liens". (`code_violation`/`lien` use the account's built-in list titles, not SiftStack's internal concept names; "Tax Sale" has no built-in equivalent and is a SiftStack-only list.) DataSift auto-creates any missing list from the CSV.
- **Tags:** Courthouse Data, FTM, notice_type, county, YYYY-MM date, deceased/living, DM confidence level, has_auction, tax_delinquent, photo_import (for photo-sourced records)

### Upload Wizard (5 Steps)
1. **Setup:** Click "Upload File" sidebar → "Add Data" → dropdown "Uploading a new list not in DataSift yet" → enter list name → organization questions
2. **Tags:** Skip through (tags are in CSV column)
3. **Upload File:** Set file on `input[type="file"]`
4. **Map Columns:** Core address fields auto-map; Tags, Lists, and enrichment columns may need manual mapping
5. **Review + Finish Upload:** Click "Finish Upload" — processing happens in background

### Column Mapping Notes
- Only core address fields (Property Street, City, State, ZIP) reliably auto-map
- Tags, Lists, Estimated Value, and enrichment columns often stay unmapped in step 4
- Notes and MSL Status sometimes auto-map
- Custom fields (TX County Data group) require drag-and-drop mapping

### Contact Logic
- **Deceased owners:** Contact = decision maker (first/last name + mailing address from DM)
- **Living owners:** Contact = property owner (owner mailing address, falls back to property address)

### Post-Upload: Enrich + Skip Trace

After CSV upload, the pipeline automatically runs two DataSift actions via Playwright:

1. **Enrich Property Information** (Manage → Enrich Data): Adds SiftMap property data (beds, baths, Zestimate, sqft, sale history) to uploaded records. "Enrich Owners" and "Swap Owners" are OFF — protects our PR/DM contact mapping.
2. **Skip Trace** (Send To → Skip Trace): Pulls phone numbers (up to 5 per owner) + emails via unlimited plan ($97/mo). Adds auto-tag `skip_traced_YYYY-MM`.

Both run in background — tracked in Activity tab. Both are ON by default when `--upload-datasift` is set.

### CLI Flags
```bash
python src/main.py daily --upload-datasift        # upload + enrich + skip trace
python src/main.py daily --upload-datasift --no-enrich       # upload only, skip enrichment
python src/main.py daily --upload-datasift --no-skip-trace   # upload + enrich, skip skip trace
python src/main.py daily --notify-slack            # send run summary to Slack/Discord
```

### Environment Variables
- `DATASIFT_EMAIL` — DataSift login email
- `DATASIFT_PASSWORD` — DataSift login password
- `SLACK_WEBHOOK_URL` — Slack/Discord webhook for run summaries

### Login Selectors (SPA quirks)
- Hidden checkboxes (Remember me, Terms) — click `<label>` elements, not `<input>`
- Use `wait_until="domcontentloaded"` (not `networkidle` — SPA keeps WebSocket connections open)
- Cookie validation: check for `/dashboard` or `/records` in URL (5s wait for SPA redirect)

### DataSift UI Automation Patterns

Hard-won patterns from build 1.0.22-1.0.23 (SiftMap, preset management, sequence builder). Follow these to avoid repeating past mistakes.

**Styled-Components (no native HTML controls)**
- No native `<select>` elements — all dropdowns are `[class*="Selectstyles__Select"]` containers
- `[class*="SelectValue"]` = current value display; `[class*="SelectOptionContainer"]` = dropdown options
- Multiple Select dropdowns exist per panel (Lists, Tags, Property Status) — always target the **LAST visible one**
- Use `x > 450` bounds check in all JS queries to avoid matching sidebar elements (sidebar is 0-400px)
- React state updates require native setter + event dispatch, not just `.value = ...`:
  ```js
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, 'new value');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  ```

**Panel Scrolling (Playwright scroll fails)**
- Filter panel is a scrollable `<div>`, NOT the viewport — `scroll_into_view_if_needed()` does nothing
- Use JS: `el.scrollIntoView({behavior: 'instant', block: 'center'})` instead
- Filter Presets section is at the BOTTOM of the filter panel — must scroll container down to reveal
- After scrollIntoView, element y-positions may be negative — don't filter by `y > 0` for the target element

**React DnD (Sequence Builder)**
- Cards have `draggable="false"` — Playwright's native drag won't work
- Must use slow mouse drag: `mouse.move()` → `mouse.down()` → 20 incremental steps (50ms each) → `mouse.up()`
- Add 500ms pauses between down/move/up phases
- "Add new Action +" button required for 2nd+ actions; first action uses initial drop zone
- Sidebar cards can scroll out of view when main area scrolls — scroll BOTH source and target into view before drag

**Pointer Interception (common blockers)**
- Beamer NPS survey iframe (`#npsIframeContainer`) blocks ALL pointer events globally — remove from DOM via `_dismiss_popups()`
- `RecordsFiltersstyles__RecordsFiltersSection` elements intercept clicks — use `page.evaluate()` JS click or `force=True`
- When Playwright click fails with "outside of viewport" or "intercept": switch to `page.evaluate(el => el.click())`
- SiftMap PropertyDetails panel blocks sidebar checkboxes — remove from DOM before interactions

**Preset Management Workflow**
- Flow: open filter panel → scroll to bottom → expand "Filter Presets" → expand folder → click preset → modify → Save (not Save New) → confirm overwrite
- Folder names have case variations ("00 Niche" vs "00 NICHE") — use `.toUpperCase()` comparison
- Preset names follow pattern `^\d{2}\.` (e.g., "00. Needs Skipped")
- 2 folders: "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- All 21 presets have Property Status "Do not include" → "Sold" (build 1.0.23)

**Sequence Builder Workflow**
- Flow: `/sequences` → Create → title + folder → drag trigger → condition → actions tab → drag actions → configure → save
- Duplicate name handling: detect error toast "different sequence title", retry with " V2" suffix
- Actions tab: navigate via "Set the Following Actions" button or URL (`/sequences/new/actions`)
- Autocomplete inputs: after each selection, `fill("")` + Escape to dismiss dropdown before next entry
- "Sold Property Cleanup" sequence exists in Transactions folder (build 1.0.23): Trigger (Property Tags Added) → Condition (Sold) → Actions (Status→Sold, Remove Lists, Clear Tasks, Clear Assignee)

**SiftMap Automation**
- Search by city (NOT county): Travis → "Austin, TX", Bell → "Killeen, TX", Williamson → "Round Rock, TX"
- PropertyDetails panel auto-opens on search — remove from DOM before other interactions
- "Add Records to Account" modal: toggle OFF "Do not replace owners", add tags, dismiss dropdown by clicking heading (NOT Escape — clears tags)
- Known limitation: SiftMap filters (price, date) set values visually but don't trigger React re-query. Only sidebar-visible properties (~3-5) get added per run

**Market Finder Extraction Patterns (build 1.0.29+)**

Hard-won patterns from building `extract_market_finder.py`. The Market Finder UI differs significantly from the rest of DataSift.

- **NO HTML `<table>` element** — data table is entirely div-based: `Tablestyles__TableContainer` → `TableRow` → `TableCell` (styled-components). Searching for `<table>` or `<tr>/<td>` finds nothing.
- **PAGINATION, not infinite scroll** — table shows 20 rows per page with "1-20 of N" text and `PaginationInnerContainer` with prev/next `<button>` elements. Must click through ALL pages to get complete data.
- **State/County selection uses `InputMultiSearch`** — NOT styled-component Select dropdowns. Inputs have placeholders: `"Select States"`, `"Select Counties"`, `"Select ZIP Codes"`. Click input → type name → click dropdown result item (`[class*="Item"]:has-text("...")`).
- **ZIP/Neighborhood toggle is a styled Select dropdown** — at the top bar with `Selectstyles__SelectValue` showing current view. Check the displayed text BEFORE clicking — if already on the correct view, clicking toggles AWAY from it. Only click to switch if the displayed text doesn't match the desired view.
- **Beamer push modal (`#beamerPushModal`)** — appears on fresh login, blocks ALL pointer events. Different from the NPS survey (`#npsIframeContainer`). Both must be removed from DOM before any click interactions. Always call dismiss with `force=True` as fallback.
- **Page body scrolling required** — pagination controls are at `y=1867`, below the viewport (`clientH=824`). Must scroll `AdminPage__AdminPageBody` container down before pagination buttons are accessible.
- **Summary panel on right side** — shows county-level aggregates: Median Home Value, Homes on Market, Mo. Investor Transactions, Homes Sold Last Month, Market Rent, Gross Rental Yield, Homeownership Rate. Extract via regex on page text.

## REI Skill Library (13 Skills)

Distribution-ready Claude Co-Work skill files at `Skills for REI/improved/`. Each `.skill` is a ZIP containing `SKILL.md` + `references/` folder. Plugins (`.plugin`) also include `commands/` and `.claude-plugin/plugin.json`.

### Skill Inventory

| # | File | Division | Score | What It Does |
|---|------|----------|-------|-------------|
| 1 | `sift-market-research.skill` | Market Intel | 9.6 | Market Finder reports, zip code scoring (6 weights verified against `market_analyzer.py`), 7-sheet Excel output |
| 2 | `first-market-county-data.skill` | Market Intel | 9.7 | County clerk data extraction for all 7 notice types, FOIA templates, marketing windows |
| 3 | `buyer-prospector.skill` | Market Intel | 9.6 | Cash buyer list from 84K+ records, LLC/trust/corp research, 50-state SOS URLs |
| 4 | `real-estate-comping.skill` | Deal Analysis | 9.7 | Two-Bucket ARV, disclosure/non-disclosure routing (12 states), adjustments verified against `comp_analyzer.py` |
| 5 | `rehab-estimator.skill` | Deal Analysis | 9.8 | 912-line skill, complete Repair Cheat Sheet verified against real contractor SOW, 4-tier system |
| 6 | `deal-analyzer.plugin` | Deal Analysis | 9.6 | Combined comp+rehab pipeline, MAO (75%/70% rules), multi-loan financing, exit strategy comparison |
| 7 | `deep-prospecting.skill` | Deal Analysis | 9.6 | 4-level research depth (L1-L4), heir verification loop, DOD sanity check (3yr), 3-site skip trace waterfall |
| 8 | `probate-property-finder.skill` | Deal Analysis | 9.7 | Property lookup for probate decedents, 3-tier search (CAD→Executor→People search), confidence scoring |
| 9 | `phone-validator.skill` | Operations | 9.8 | Trestle API scoring, 5-tier dial priority, 3 tier strategies, litigator risk check, 4.75x connect rate |
| 10 | `sequential-presets.skill` | Operations | 9.5 | 12 niche + 9 bulk filter presets, Pendulum Theory (SMS→Call→Mail→DP), DataSift UI implementation steps |
| 11 | `sift-sequences.skill` | CRM | 9.5 | 26 TCA sequence templates (verified against `sequence_templates.py`), UI walkthrough, HOT A01-A16 chains |
| 12 | `sift-operations.plugin` | CRM | 9.3 | CRM operations encyclopedia, STABM routine, lead pipeline (9 statuses), task presets, team roles |
| 13 | `playbook-creator.skill` | Operations | 9.5 | Playbook/SOP generator from transcripts, 7-node chart limit, 5th grade reading level, Word doc output |

### Cross-Skill Verified Consistency

These values are identical across all skills that reference them:
- **Phone tiers:** 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial Third), 21-40 (Dial Fourth), 0-20 (Drop)
- **Preset folders:** "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- **Sequence count:** 26 TCA templates across 5 folders (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- **Comp adjustments:** Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr (from `comp_analyzer.py`)
- **Financing defaults:** HML 12%, conventional 7%, 2 points, 2.5% closing (from `deal_analyzer.py`)
- **DOD sanity:** MAX_DOD_GAP_YEARS = 3 (from `obituary_enricher.py`)
- **Notice types:** 7 total (foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce)

### Key Corrections Made During Optimization (April 2026)
- **Hardcoded credentials removed** from sift-market-research (had email/password in SKILL.md)
- **Bedroom adjustment corrected** from $10K to $5K in real-estate-comping (matched to `comp_analyzer.py`)
- **HML points corrected** from 0% to 2% in deal-analyzer (matched to `deal_analyzer.py DEFAULT_HARD_MONEY_POINTS`)
- **Linux paths fixed** in sequential-presets (was `/home/ubuntu/skills/...`, now relative)
- **Preset names aligned** across 3 skills to match `niche_sequential.py` source code
- **Transfer tax labeled** as Texas-specific in deal-analyzer (TX has no state transfer tax)
- **"Substantial renovation" defined** in real-estate-comping: kitchen + 1 bath minimum (~$15K spend)

### Skill File Structure
```
skill-name.skill (ZIP containing):
├── SKILL.md              # Main skill instructions
├── references/            # Domain knowledge files
│   ├── *.md              # Reference documents
│   └── *.pdf             # SOPs, guides
└── scripts/              # Optional automation scripts
    └── *.py / *.js

plugin-name.plugin (ZIP containing):
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── commands/             # Slash commands
│   └── *.md
├── skills/
│   └── skill-name/
│       ├── SKILL.md
│       └── references/
└── README.md
```

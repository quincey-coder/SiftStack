# CLAUDE.md — SiftStack

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SiftStack** — Full-stack real estate investing operations platform built around DataSift.ai CRM. Covers the entire REI business lifecycle:

1. **Data Acquisition:** County clerk websites (Travis, Bell, Williamson), Odyssey court portals, MVBA Law Firm tax sale PDFs, Travis County Tax Office CSV, scanned PDF import, courthouse terminal photo import (probate, eviction, code violations, divorce), Dropbox auto-polling
2. **Enrichment Pipeline:** 10+ steps — Smarty address standardization, Zillow property data, TCAD/BCAD/WCAD appraisal district lookups, obituary/heir research, Ancestry.com SSDI, Tracerfy skip trace, Trestle phone scoring, entity research, ZIP code filtering
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
Zillow, entity research, Tracerfy/Trestle, split-by-county, Slack) to EXACTLY
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

## Data Sources

Configured in `config.py`:

| County | Type | Source |
|--------|------|--------|
| Travis | Foreclosure | tccsearch.org |
| Travis | Tax Delinquent | Travis Tax Office CSV (13K+ records) |
| Travis | Tax Sale | Travis Tax Office upcoming sales |
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

## Key Domain Rules

- **Probate owner_name** should be the Personal Representative/Executor/Administrator — not the deceased.
- **Address dedup:** Same property can appear in multiple notices; `data_formatter.deduplicate()` keeps the most recent.
- **ZIP code filtering:** Only notices in investor-active ZIPs (from `target_zips.json`) pass through. Threshold: 10+ investor transactions/month per ZIP.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page. Travis CSV download needs no rate limiting.
- **Texas is a disclosure state** — actual sale prices are available in public records (unlike non-disclosure states).
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
- Audit with a name oracle (`pip install names-dataset`), scoring current vs flipped
  orientation — but treat it as a *hint only*: ambiguous names (`Shay Dori`,
  `Marshall Hussain`) score "backwards" while being correct. CAD is the only truth.

## Tax-Delinquent Cross-Run Diff + Sold Tagging

All three tax-delinquent scrapers (Travis CSV, Bell/Williamson XLSX) diff each pull against the prior run's parcel-ID set and persist state across runs (`data/{county}_tax_state/` locally; Apify KVS keys `travis_texdel_state` / `bell_texdel_state` / `williamson_texdel_state`). State modules: `travis_texdel_state.py` (Travis) and the county-parameterized `tax_delinquent_state.py` (Bell + Williamson). Per-run diff JSON + raw-file archive are written for forensics; the diff is surfaced to Slack via `--notify-slack`.

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
- Then runs DM address lookup (CAD → People Search → Tracerfy)

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

DataSift.ai (formerly REISift) is the CRM where scraped records land for niche sequential marketing campaigns. There is **no REST API** — upload is via Playwright browser automation of the web UI.

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). API at `apiv2.reisift.io`.

### Key Files
- `src/datasift_formatter.py` — Transforms `NoticeData` → DataSift CSV (41 columns)
- `src/datasift_uploader.py` — Playwright login + upload wizard + enrich + skip trace + preset management + sequence builder + SiftMap sold workflow
- `test_datasift_upload.py` — Headed browser test (upload + enrich + skip trace)
- `test_manage_presets.py` — Headed browser test (preset discovery + sold exclusion + sequence creation)
- `test_manage_sold.py` — Headed browser test (SiftMap sold property tagging)

### CSV Column Structure (41 columns)
- **Core auto-mapped (11):** Property Street/City/State/ZIP, Owner First/Last Name, Mailing Street/City/State/ZIP, Tags
- **Lists + Notes (2):** Lists (for niche sequential), Notes (contextual per notice type)
- **Built-in fields (13):** Estimated Value, MSL Status, Last Sale Date/Price, Equity Percentage, Tax Deliquent Value, Tax Delinquent Year, Tax Auction Date, Foreclosure Date, Probate Open Date, Personal Representative, Parcel ID, Structure Type, Year Built, Living SqFt, Bedrooms, Bathrooms, Lot (Acres)
- **Custom fields (15):** Notice Type, County, Date Added, Owner Deceased, Date of Death, Decedent Name, Decision Maker, DM Relationship, DM Confidence, DM 2/3 Name/Relationship, Obituary URL, Source URL

### Niche Sequential Marketing
DataSift's niche sequential system uses filter presets to guide records through SMS → Call → Mail → Deep Prospecting phases. Two preset folders: "00 Niche Sequential Marketing" (12 presets, courthouse data) and "01. Bulk Sequential Marketing" (9 presets, bulk data). All 21 presets exclude Sold status (build 1.0.23). A "Sold Property Cleanup" sequence in the Transactions folder auto-fires on "Sold" tag to change status, remove from lists, clear tasks, and clear assignee.

- **"Courthouse Data" tag:** Every record gets this tag — signals first-to-market county data (prioritized over bulk data in filter presets)
- **Lists column:** Maps `notice_type` → DataSift list name. Names match the account's existing built-in lists so the Sold/cleanup sequences (which act on list titles) fire: `foreclosure` → "Foreclosure", `probate` → "Probate", `tax_sale` → "Tax Sale", `tax_delinquent` → "Tax Delinquent", `eviction` → "Eviction", `code_violation` → "Code Enforcement", `divorce` → "Divorce", `lien` → "Liens". (`code_violation`/`lien` use the account's built-in list titles, not SiftStack's internal concept names; "Tax Sale" has no built-in equivalent and is a SiftStack-only list.) DataSift auto-creates any missing list from the CSV.
- **Tags:** Courthouse Data, notice_type, county, YYYY-MM date, deceased/living, DM confidence level, has_auction, tax_delinquent, photo_import (for photo-sourced records)

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

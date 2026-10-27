# Bell CAD portal migration — audit, fix, and what the new site offers (2026-10-27)

Bell CAD replaced its Harris Govern "esearch" property portal with a **GSA Corp**
portal in 2026 and, separately, stopped re-posting the delinquent-roll XLSX on its
data portal after 2026-07-17. This is what that touched in SiftStack, what was
fixed today, and the links to go look at the new site yourself.

## Direct links (new site)

| What | URL | Notes |
|---|---|---|
| Search home (quick / advanced / tax / protests tabs) | https://belltx-search.gsacorp.io/ | Property ID, owner name, or address |
| Tax search tab | https://belltx-search.gsacorp.io/search/tax | Owner-name-starts-with + account |
| Interactive GIS map | https://belltx-search.gsacorp.io/map | |
| Parcel page (example) | https://belltx-search.gsacorp.io/parcel/0000100046 | Owner + mailing, situs, legal, **Property Deed History**, buildings, value history 2021-2027, jurisdictions. Year tabs: `/parcel/<id>/2025` etc. |
| Tax account page (example) | https://belltx-search.gsacorp.io/tax/0000100046 | Balance, currently due, current-year installments, **every past year** original / penalty / interest / paid / balance back to 2005, Tax Bill PDF buttons |
| Live search API (example) | https://belltx-search.gsacorp.io/api/livesearch/100046 | JSON `{html: ...}`; also takes a name or street |
| Payments cart | https://belltx-search.gsacorp.io/cart | Do not use |
| Taxpayer portal (protests, login) | https://bellcad-taxpayer.gsacorp.io/login | Not needed |
| CAD data portal (bulk files) | https://bellcad.org/data-portal/ | Still the batch source, see below |

Property IDs are **10-digit zero-padded** on the new site (`100046` → `0000100046`);
the short form redirects, but build links padded. **Dead:** `https://esearch.bellcad.org/`
and every `esearch.bellcad.org/Property/View/<id>` link (404), which is what all our
Bell tax-delinquent records carried as Source URL.

## Bulk files on the data portal (as of 2026-10-27)

| File | Posted | Size | Status in SiftStack |
|---|---|---|---|
| `BellCAD_Delinquent_Roll_Condensed_20260717.xlsx` | 2026-07-17 | 2.8 MB | **Our scrape source. Stale 102 days.** Cloud run re-downloaded this exact file (sha `d0cbf4a8…`) every day since, 0 NEW / 0 DROPPED, so Bell Sold tagging has been dead since July. |
| `BellCAD-Collections-Delinquent-Roll-20260727.rar` + `-Prior-20260727.rar` | 2026-07-27 | 8 + 5.7 MB | **New format.** RAR (Windows `tar.exe` extracts it). `coll_dat.txt` = 332K fixed-width rows, one per property × tax year × taxing entity, 8,627 real parcels (vs 8,307 in the XLSX) with owner, mailing, legal, per-entity levy / P&I / due date. No published layout; 10 days newer than the XLSX, then also stopped. |
| `BellCAD-Appraisal-Data-Condensed-2026-20260922.xlsx` / `-2027-20260922.xlsx` | 2026-09-22 | 38.8 / 39.0 MB | **Renamed** (was `2025_BellCAD_Appraisal_Data_Condensed_20260407.xlsx`). Our regex missed it, so the owner/situs cross-reference ran on the April 2025 roll. Headers compatible (`Owner_name`, `Situs_Addr`, `deed_dt`, `deed_num`, `Exemptions`); 2027 is the next-year working roll with the newest ownership. |
| `MortgageProcessFile.zip` | 2026-10-15 | 3.8 MB | **New to us.** `MortgageProcessFile.txt`, 27 MB fixed-width: mortgage-company code → property id → tax year → amounts. The set of parcels a servicer pays on = **has a mortgage**; the complement of the delinquent roll against it = the free-and-clear owners the curative model targets. |
| `2026-Bell-County-Certified-Export-3.zip` + `Appraisal-Export-Layout-8.0.33.xlsx` | 2026-10-01 | 161 MB | PACS certified export, 19 files. `APPRAISAL_INFO.TXT` carries `deed_dt`, `deed_book_id/page`, `mortgage_co_id / mortage_co_name / mortgage_acct_id`, every exemption flag (`hs`, `ov65`, `ov65s` surviving spouse, `dp`, `dv1-4`), `py_confidential_flag`, agent name/address; `APPRAISAL_LAWSUIT.TXT` = active lawsuit properties; `APPRAISAL_TAX_DEFERRAL_INFO.TXT` = tax deferrals (over-65 / disabled deferrals are a deceased-owner-in-waiting list). Layout is published, so this is parseable today. |

## What the code touched and what changed

| Where | Before | After |
|---|---|---|
| `config.CAD_URLS["Bell"]` | `https://esearch.bellcad.org/` | `https://belltx-search.gsacorp.io/`; new `BELL_GSA_BASE_URL`, `BELLCAD_DELINQUENT_STALE_DAYS` (45, env `SIFT_BELL_ROLL_STALE_DAYS`) |
| `config.BELLCAD_APPRAISAL_ROLL_PATTERN` | underscore name only | matches both naming styles |
| `bell_tax_cache.discover_latest_url` | newest `YYYYMMDD.xlsx` | newest date stamp, then highest roll year (2027 beats 2026 on the same day); `filename_datestamp`, `roll_age_days`, `delinquent_meta`; every download writes a `<file>.meta.json` sidecar (url, export stamp, downloaded_at) |
| `scrapers/tax_delinquent_bell.py` | `source_url=esearch…/Property/View/<id>` | `…gsacorp.io/parcel/<10-digit id>`; new `_check_roll_staleness`: roll older than the threshold → `LAST_ALARMS` (error-level in the Slack health block via `main._collect_health_events`), unchanged-sha streak + roll age in `last_meta` evidence |
| `src/bell_gsa.py` (new) | — | live reader: `fetch_parcel`, `fetch_tax`, `livesearch`, CLI `python src/bell_gsa.py tax 100046` |
| `property_lookup` / `tax_enricher` / `cad_lookup` docstrings | esearch | GSA |
| Tests | — | `tests/test_bell_gsa.py` (parsers on captured pages), `tests/test_bell_portal_fix.py` (regex, sort order, sidecar, Source URL, alarm) — 20 pass |

Dedup is unaffected: `data_formatter._notice_id_from_url` never matched the esearch
shape, so Bell tax-delinquent keys were already `parcel:<id>`; changing the URL
changes nothing in `seen_notice_ids`.

Not changed: the Bell **foreclosure** scraper (`bellcountytx.com` county clerk, still
200), Bell **lien / lis pendens** (`bell.tx.publicsearch.us`, a different vendor),
and the delinquent-roll XLSX as the batch source (still the newest condensed file).

## What is still open

1. **CRM Source URLs.** Every Bell tax-delinquent record uploaded before today links
   to the dead host. They are custom-field values, so the fix is a
   `update_by_property_address` wizard upload of (address → new Source URL) for the
   Bell rows, or leaving them until each record is next re-emitted. Nothing breaks
   in DataSift; the link just 404s when clicked.
2. **Bell Sold tagging** depends on the roll diff and is blind until the CAD posts a
   new roll. Proposed: a weekly `bell_gsa.fetch_tax` sweep over the parcels in
   `last_run_records` (the ~700 we uploaded); `delinquent_balance == 0` → the same
   Sold/paid-off row the diff would have produced, with the live balance as
   evidence. 700 requests at 0.5 s is six minutes; run it from the operator box or
   as a small Actor step.
3. **Ask Bell CAD** when the condensed delinquent roll is refreshed (the pattern was
   monthly through July, then nothing) and whether the RAR "Collections" export
   replaces it; request the Collections record layout. The staleness alarm will fire
   daily until a new file appears or `SIFT_BELL_ROLL_STALE_DAYS` is raised.
4. **Worth grabbing from the new files** (ranked for curative title):
   `MortgageProcessFile` (free-and-clear filter), certified export
   `APPRAISAL_INFO` exemption + deed + mortgage fields (ov65 / surviving spouse /
   deferral = aging owner signals, `deed_dt` age = long-held), `APPRAISAL_LAWSUIT`,
   and the GSA parcel page's deed table per record (affidavits of heirship, `ET AL`
   grantors, estate deeds) which the XLSX never had.
5. Deploy: `scripts/apify_push.sh` after commit; the alarm only reaches Slack once
   the build is live.

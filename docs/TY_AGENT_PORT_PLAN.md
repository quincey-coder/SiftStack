# Ty Agent Port — Implementation Plan (TX: Travis / Bell / Williamson)

**Date:** 2026-08-18. **Source:** `upstream/main` (DataSift-Ty-Personal/SiftStack, build 1.0.44).
**What happened:** 82 files brought into this repo verbatim (no existing file overwritten, nothing wired in yet — every ported module is inert until imported). This document is the plan for adapting them to our market and stack.

---

## 1. The API answer (asked explicitly)

Ty is NOT using a different secret API. He writes to the **same `apiv2.reisift.io` internal API** our read-only client wraps — but there are **two credential surfaces** (full contract now at `docs/api/datasift.md`, brought over):

| | **Open API key** (`REISIFT_API_KEY`) | **Minted user JWT** |
|---|---|---|
| What it is | Official key-based Open API, **93 routes**, never expires | `POST /api/token/` with DATASIFT_EMAIL/PASSWORD → `{access, refresh}`, ~48h expiry, re-mint every 30 min |
| Read properties/lists/tags | yes | yes |
| **Write/upsert properties** | **yes** | yes |
| Custom fields | **no — routes absent entirely** (writes 401) | yes (`PATCH /api/internal/property/{uuid}/custom-field/update-values/`) |
| Activity log (KPIs) | limited | yes |

Key safety facts, verified by auditing every HTTP verb in his write stack (`datasift_api_upload.py`, `datasift_schema_setup.py`, `sms_agent/crm.py`):
- **Zero DELETE calls anywhere.** Only GET / POST / PATCH.
- `POST /property/` is **upsert by address** — re-runs never duplicate; lists accumulate.
- His hard-won traps are documented in the module docstrings: tags must be an ARRAY, select values must be option UUIDs, entity owners must omit person keys (not send `""`), `notes` on the property payload 200s and is discarded, `/api/internal` throttles hard (single-thread ~2 req/s with backoff; 6 threads 429'd 529/740).

**Owner decision required (nothing changes until you say so):** our read-only policy (2026-08-12, after the 2026-07-14 wipe) currently blocks ALL API writes. Options:
- **A. Keep policy as-is** — rework Ty's uploaders to emit CSV for the upload wizard (loses per-record custom-field writes; the wizard covers most of them anyway).
- **B. Narrow exception** — allow POST-upsert + custom-field PATCH only (no delete verbs exist in the ported code; our `_enforce_read_only` guard stays on our client; Ty's uploader would run under an explicit new env gate, e.g. `DATASIFT_API_ALLOW_UPSERT=1`, with his "upload one record and read it back" habit made mandatory).
- **C. Open API key for writes** — get the official key (never expires, no custom fields). Safest surface (93 fixed routes, no internal API), but custom fields (Notice Type, County, DM fields…) would still need wizard or JWT.

The SMS agent's CRM leg needs at minimum tag/status PATCH writes to record conversation state — Option A alone cannot support it; it needs B (or a Zapier-action path, see memory `reference_datasift_webhooks_zapier`).

---

## 2. What was brought over (by division)

### Deal Analysis & Funding — market-agnostic, highest immediate value
| Module | What it does | TX adaptation |
|---|---|---|
| `src/post_walkthrough.py` | 9-sheet post-walkthrough workbook: dual-track ARV, exit strategy scoring, lender analysis, Sift-linked | Point at our `comp_analyzer.py` + TX triangulation ARV (no sold prices in TX — feed CAD+Zestimate ARV in, keep his exit/lender math) |
| `src/lender_package.py` + `src/lender_docs.py` | 8-piece private-money package: cover letter, promissory note, personal guarantee, closing instructions, insurance request | Legal template review for TX (notes/guarantees are state-sensitive); no transfer tax line |
| `src/comp_package.py` + `src/sku_pricing.py` | Script engines behind comp-package/rehab skills | comp_package: disclosure-state only → gate behind state check like the skill; sku_pricing reads the Knox 37914 material list → build a Travis/Bell Home Depot list the same way (`material_list.py` shows the locked-list pattern) |
| `src/material_list.py` | Frozen, git-tracked price source pattern | Reuse the MECHANISM; replace data with our-market SKU pull |
| `src/lot_split_underwrite.py` | Underwrites parcel splits as an exit lane | TX subdivision/platting rules per county; TCAD/WCAD lot data we already have |

### Dispo & Buyers — market-agnostic
| Module | What it does | TX adaptation |
|---|---|---|
| `src/deal_package.py` | 6-sheet dispo dial-sheet workbook | Near drop-in; wire to our buyer lists |
| `src/buyer_sweep.py` | Deed-level sweep of who actually buys in a ZIP, ranked by fit | Swap TN deed source → our county sources (tccsearch OPR / publicsearch GovOS — we already scrape these for liens) |
| `src/dispo_skiptrace.py` | 3-source skip waterfall with audit matrix showing which source missed | Near drop-in; align with our SmartSkip-first stack |

### Outreach & Marketing — phase-2 (gated) at Ty's; same gates here
| Module | What it does | TX adaptation |
|---|---|---|
| `src/sms_agent/` (25 files) | Two-way SMS: SmrtPhone webhooks inbound → classify → CRM write-back → Slack escalation; sender pool; daily cadence; heartbeat | We already use SmrtPhone. Needs: SmrtPhone API token, a hosted receiver (Ty uses Fly.io — `fly.toml` upstream; `deploy/requirements-sms-agent.txt` brought over), the API-write decision (§1), TCPA review, sender-pool numbers from `caller-reputation-monitor`'s healthy pool. Its `knowledge/playbook.md` voice/scripts must be rewritten for our market + NEPQ language |
| `src/mms_sender.py` | Texts the homeowner the actual auction notice image | Our notice PDFs/photos can feed it; MMS is browser-session only (no SmrtPhone MMS API) |
| `src/obituary_campaign.py`, `src/obituary_mail_export.py`, `src/obituary_opportunity.py` | Ranked, validated obituary direct-mail drop + lean-budget call order | We already have obituary enrichment; swap his obit list source for ours; validate addresses via Smarty like the rest of our pipeline |

### Sphere of Influence — whole new division (6 modules)
`soi_intake.py, soi_county_pull.py, soi_owner_db.py, soi_owner_match.py, soi_enformion.py, soi_enrich.py` — name/email-only contact exports → confirmed metro homeowners ranked by Realtor AI score. **TX adaptation is easy leverage:** `soi_county_pull` pulls TN owner rolls; we already hold TCAD/BCAD/WCAD bulk owner data (`cad_lookup.py`, `travis_tax_cache.py`) — point the join at those. Enformion resolver only pays for roster misses (matches our cost doctrine). NAMELF orientation rules apply to the owner-name join (see CLAUDE.md).

### CRM Operations — blocked pending §1 decision
`src/datasift_api_upload.py` (JWT-minting API uploader, custom-field writes, resumable, checkpoint-every-25), `src/datasift_schema_setup.py` (idempotent custom-field/list creation, dry-run by default). **Do not run with `--commit` until the owner picks an option in §1.** Dry-run modes are read-only and safe.

### Market Intelligence & Coaching
- `src/county_market_report.py` — merges Market Finder extraction + public-data bundle into the 7-sheet workbook. We have the same extraction (`extract_market_finder.py`); this automates what our sift-market-research skill does by hand. Near drop-in for TX counties.
- `src/call_coaching/` — engine behind the three coach skills (pull SmrtPhone recordings → tonality transcription → 3 rubrics). Our skills bundle equivalent scripts; adopt this as the single `src/` implementation to stop skill/pipeline drift.

### Data Acquisition — TN implementations; port the TECHNIQUES
| Module | The technique worth stealing | Our equivalent to upgrade |
|---|---|---|
| `src/ftm_runner.py` | Unattended cloud pull done right: credential/egress/state preflight, per-process proxy session rotation, **persist seen_ids ONLY after upload succeeds**, a 0-notice run reports EMPTY and exits non-zero (the anti-silent-failure rule we learned Aug-5 the hard way) | Our Apify Actor `main.py` — adopt the preflight + EMPTY-exit + commit-after-upload ordering |
| `src/ftm_schedule.py` | Long-lived business-local scheduler | Alternative to Apify schedules if we ever self-host (Fly.io) |
| `src/proxy_resolver.py` | Egress decided per-site ("the site gates on egress, not code") | Generalizes our US-residential pin for tccsearch |
| `src/captcha_solver.py` + `src/scrapfly_client.py` | Turnstile clearing via Scrapfly | Our tccsearch fallback if Cloudflare escalates to embedded Turnstile (`pass_cloudflare` already detects it) |
| `src/scraper.py` | tnpublicnotice.com saved-search driver | Study only — saved-search fan-out pattern; do not wire |
| `src/knox_ftm_pull.py` | One sweep of every source carrying an address + buy-box filter applied at intake | Template for a `tx_ftm_pull.py` over our 14 sources with an Operator buy box (we have no buy-box gate at intake today) |
| `src/knox_lien_resolve.py` | Lien debtor name → parcel → **drop satisfied liens** | Our lien path lacks the satisfied-lien drop; compare against `lien_travis`/`lien_publicsearch` + Step 3c-lien |
| `src/consolidate_foreclosures.py` | Master still-active list across N months of runs | Direct port over our `data/` run archives |

Also brought: `docs/AGENT-MAP.md` + `docs/agents.json` (his 83-agent org chart — the divisional structure is worth copying as our operating map), `docs/api/*` (7 API contract docs: DataSift, SmrtPhone, Trestle, skip-trace, county-data, OpenWebNinja, scraping-infra), `docs/setup/GETTING-STARTED.md` + `no-api-playbook.md`, `deploy/requirements-sms-agent.txt`, `archive/notice_screenshots/notice_screenshot.py` (retired at his shop; reference only).

---

## 3. Phased implementation for our use case

**Phase 0 — Foundations (no behavior change)**
1. Commit the port on a branch (`port/ty-agents`) so main stays clean.
2. Add missing root-config attributes as inert defaults (~a dozen non-SMS ones; the SMS agent reads its own `sms_agent/config.py`). Drop `TNPN_*` (tnpublicnotice creds — TN only).
3. Install extra deps only when a module is wired (`deploy/requirements-sms-agent.txt` for the agent; nothing else new).

**Phase 1 — Immediate wins (market-agnostic, no API writes, no outbound):**
`lender_package` + `lender_docs` (TX legal review of note/guarantee templates) → `post_walkthrough` (feed TX triangulated ARV) → `deal_package` → `dispo_skiptrace` → `consolidate_foreclosures` (TX data dirs) → `county_market_report` (Travis/Bell/Williamson). Each verified by generating output for one real record and eyeballing.

**Phase 2 — Pipeline upgrades (techniques into existing TX code):**
ftm_runner's preflight/EMPTY-exit/commit-after-upload into our Actor; buy-box gate at intake (`knox_ftm_pull` pattern → `tx_ftm_pull`); satisfied-lien drop into our lien flow; `buyer_sweep` on our deed sources; Scrapfly Turnstile fallback wired to `pass_cloudflare`'s `turnstile=True` branch.

**Phase 3 — SOI division (TX):** point `soi_county_pull`/`soi_owner_match` at our CAD caches; NAMELF-safe name join; Enformion only for misses; Realtor-score enrich; output to DataSift via wizard CSV.

**Phase 4 — Outreach (gated, owner go/no-go per Ty's own model):**
Obituary mail chain first (lowest risk, mail not SMS). Then the SMS agent: API-write decision (§1) → SmrtPhone API token + webhook receiver hosting → rewrite `knowledge/playbook.md` in our NEPQ voice → sender pool from caller-reputation-monitor's healthy numbers → TCPA/DNC review (litigator-checked numbers only) → seed sends at tiny volume with the mandatory read-back habit. MMS sender last (browser-session transport).

**Standing rules for every port:** owner-name NAMELF discipline; records-not-leads vocabulary; Texas non-disclosure ARV routing; no fabricated persons; anything billable or homeowner-facing gets an explicit human gate, exactly like Ty's phase-2 discipline.

---

## 4. Current state

- 82 files sit in the working tree **uncommitted** — nothing imports them, the Actor and CLI behave exactly as before.
- No API write has been made; `datasift_api_upload.py` has not been run.
- Open questions for the owner: §1 API option (A/B/C), and whether to start Phase 1 now.

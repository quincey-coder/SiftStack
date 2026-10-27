# Case-number tracking feasibility — Travis / Bell / Williamson (2026-10-22)

**Question asked:** For every probate, foreclosure and lien record we pull, is there a case
number we can store as a DataSift custom field and re-search weekly to see whether anything
new has been filed on that case / property?

**Short answer:** Not as a single mechanism. "Case number" only exists for court cases, and
only ONE of our courthouse feeds (Williamson probate) is a court docket today. Foreclosures
and liens are *recorded instruments* with no case, and Texas law requires nothing later to
be cross-referenced to them. But three narrower versions ARE feasible and two are cheap:

| Rank | What | Works? | Cost to build | Weekly cost | Value |
|---|---|---|---|---|---|
| 1 | **Tax-suit status from the Travis roll** (Cause #, Judgement Date, Bankruptcy # already in the daily CSV) | Yes, zero new scraping | ~1 day | $0 | High: "suit filed" / "judgment" are the two best tax-del motivation signals and we currently discard them |
| 2 | **Williamson probate docket monitor** (re-open stored CaseID, diff Events & Orders) | Yes, live-verified today | 2-3 days | $0, ~15 min runtime | High: letters issued, inventory, sale orders, PR + attorney names, DOD |
| 3 | **Travis probate docket** (new source: Odyssey Portal, reCAPTCHA v2 confirmed) | Yes with 2captcha | 1 week | ~$1-3 / 1,000 solves | High as a NEW-RECORD source; our current Travis "probate" feed is a late chain-of-title artifact |
| 4 | **Foreclosure outcome** (sold / re-posted / rescinded) | Only by inference | 2-3 days (extends existing sale-audit scripts) | $0 | Medium: cleans dead records; sale results are published nowhere |
| 5 | Lien release tracking | Technically yes (name re-search) | 1 week | Cloudflare/WAF fragile | Low: a release means *less* distress; nothing to act on |
| 6 | Lis pendens docket tracking | Yes for Travis/Williamson dockets | 1 week + OCR to get the cause # | captcha on Travis | Low-medium |
| 7 | Bell probate docket | Unverified (portal cert chain failed) | ? | ? | ? — verify before planning |

Below: what we actually hold today, what Texas law says gets filed later, what each portal
can do, the gaps, and the recommendation. Evidence came from the code audit, live portal
probes run today, and two research passes (law + portals) whose full notes are in the
session scratchpad (`law_lifecycle.md`, `portal_capabilities.md`).

---

## 1. What identifiers we hold today (code audit)

`NoticeData` has NO `case_number` field. `case_id` exists only for Austin code violations
and BSC condemned cases. What each feed actually carries:

| Feed | Source | Identifier captured | Where it lands |
|---|---|---|---|
| Williamson probate | `judicialrecords.wilco.org/PublicAccess` (Odyssey 2003, **court docket**) | Court case # (e.g. `24-0468-CP4`) in `raw_text` only; Odyssey `CaseID` in `source_url` | CRM custom field **Source URL** = `CaseDetail.aspx?CaseID=NNN` (17 of 20 sampled Probate-list records have it) |
| Travis "probate" | tccsearch OPR doc types **CERT COPY OF PROBATE** + **AFFIDAVIT OF HEIRSHIP** | Clerk instrument # in `source_url`. **No court case number** — these are recorded instruments, not the estate case | Source URL = `DocumentDetail.aspx?InstrumentNumber=N` |
| Bell "probate" | bell.tx.publicsearch.us doc type **PROBATE** | Instrument # | Source URL = `?instNum=N` |
| Travis / Bell / Williamson foreclosure | tccsearch grid / clerk PDFs / trustee PDFs | Travis: instrument # of the posted notice. Bell/Williamson: PDF link or `FILE` column. **No case number exists** (non-judicial, Prop. Code 51.002). The notice's trustee file # ("T.S. #…") and Deed-of-Trust instrument # are NOT parsed | Source URL |
| Liens (3 counties) | tccsearch / publicsearch / Tyler | Instrument # | Source URL |
| Lis pendens (3 counties) | same | Instrument #. The suit's cause number is printed in the notice (Prop. Code 12.007(b) requires it) but we read the index grid, not the image, so it is not captured | Source URL |
| Travis tax delinquent | Tax Office CSV | **`Cause #`, `Judgement Date`, `Bankruptcy Number` columns** — 482 / 116 rows of 7,533 populated on the current roll | Stored only in `tax_raw_meta` JSON; **never reaches DataSift** |
| Travis / Bell / Williamson tax sale | RealAuction JSON / MVBA PDFs | Cause # (RealAuction: parsed into `raw_text`; MVBA: in PDF text) | Notes only |

CRM universe today (read-only API, 2026-10-22): Foreclosure 269, Probate 440, Lis Pendens
159, Tax Sale 30, Tax Delinquent 4,514, Liens 2,687 (mostly non-courthouse), Code
Enforcement 1,709, Condemned 24, Fire Damage 133. Weekly re-checks of a few hundred
probate/foreclosure records are small.

### Side findings from the audit (bugs, independent of this project)

1. **The Travis deep link we store is dead.** `DocumentDetail.aspx?InstrumentNumber=N`
   returns `Fatal System Error: Session state is not available` even inside an accepted,
   Cloudflare-cleared session. The clerk's detail view only opens from a results row. Every
   Travis Source URL in the CRM is therefore a non-working link (it still identifies the
   instrument, which is what matters here).
2. **`data_formatter._notice_id_from_url` ignores three of our URL shapes** (`InstrumentNumber=`,
   `CaseID=`, `instNum=`), so Travis foreclosure/probate/lien, Bell probate and Williamson
   probate never get an `id:` dedup key and fall through to parcel / address composites.
3. **Hospital liens (tccsearch doc type 56) are in `DEFAULT_LIEN_DOC_TYPES`.** Prop. Code
   55.005(d) requires the notice to state the lien "does not attach to real property" — it
   attaches to the injury claim. These are not real-property distress and should be dropped.
4. **The Williamson probate filter admits adversary sub-cases** (`24-0468-CP4A`, "Frank P.
   Verock v. Tawanna Kay Verock") because the CP4 regex matches; `_build_notice` then returns
   None for lack of an "Estate of" title, so they are only noise, not bad records.
5. **The Williamson scraper never opens the case page**, so the Administrator/Executor name,
   their attorney and the DOD shown on the Register of Actions are not captured; the PR is
   recovered later from obituaries. The docket monitor below fixes this for free.

---

## 2. What Texas law says gets filed AFTER the record we pull

Linkage legend: **CAUSE** = carries the court cause number, **INSTR** = cites the earlier
instrument, **NAMES** = findable only via the grantor/grantee index. Texas requires only a
grantor/grantee index (Local Gov't Code 193.003); there is **no statute requiring releases or
deeds to cross-reference the original instrument**, so "related documents" on any clerk
portal reflect what the filer typed, not a guarantee.

### Probate (Estates Code)
| Event | Authority | Lands in | Linkage | Investor meaning |
|---|---|---|---|---|
| Application for probate / letters | 256.051, 301.002 (4-yr bar 256.003) | Court docket | CAUSE | Estate opened |
| Order admitting will / appointing PR; **Letters issued** | 256.201, 306.001 | Docket only — letters are **never recorded in the OPR** | CAUSE | **PR can now sign** (independent executor alone) |
| **Inventory** listing all Texas real property | 309.051 (≤90 days, extensions common) | Docket | CAUSE | The only public property list |
| Affidavit in lieu of inventory | 309.056 | Docket; property list stays **private** | CAUSE | Norm for independent administrations — hides the property |
| Dependent sale: application → order of sale → report of sale → order approving → deed | 356.251-.558 | Docket, then OPR deed | CAUSE / deed NAMES | Property gone to the reported bidder |
| **Independent executor's sale** | 401-402 | **Nothing in court**; only the deed in OPR | NAMES | Most common exit; invisible until the deed records |
| Closing report | 405.004 ("may") | Docket, often never | CAUSE | Estate distributed, heirs own it |
| Muniment of title | ch. 257 | Docket; cert copy recorded at sale | CAUSE / NAMES | Heirs are the sellers, no PR |
| Heirship judgment | 202.201, 202.206 | Docket + optional OPR | CAUSE / NAMES | Names the heirs |
| **Affidavit of heirship** | 203.001 | **OPR only — no case exists** | NAMES | Someone is about to sell/refinance |

### Non-judicial foreclosure (Property Code 51.002)
| Event | Lands in | Linkage |
|---|---|---|
| Notice of default / cure letter | Mailed only | none |
| Appointment of substitute trustee | Often OPR | INSTR (to the Deed of Trust) |
| **Postponement / withdrawal / reinstatement / bankruptcy** | **Nothing required.** No statute provides a postponement; a missed sale is simply re-posted with a fresh 21-day notice | Only visible as a NEW notice next month carrying the same Deed-of-Trust # |
| Sale → Trustee's / Substitute Trustee's Deed | OPR, days to ~4 weeks after first Tuesday | INSTR to the **Deed of Trust**, NOT to the notice |
| Rescission (51.016, ≤15 days) | OPR, must recite the trustee's deed recording data | INSTR |
| TRCP 736 expedited order (home-equity, reverse, HOA, tax-loan) | **District court — a real cause number**; 736.12 attaches the order to the deed | CAUSE |

51.002(f) lets the clerk dispose of posted notices after the sale date. Travis keeps them in
the Real Estate index with status "Perm" (verified live today: instrument 202641242, NOTICE OF
SUBSTITUTE TRUSTEE SALE, legal description includes `LOC 2213 WESTLAKE DRIVE`), so Travis is
searchable by instrument number AND by property address; Bell/Williamson only post PDFs.

### Liens (OPR)
| Lien | Later events | Expiry | Linkage |
|---|---|---|---|
| Abstract of judgment (ch. 52) | Release, bankruptcy discharge, revival | 10 yrs (52.006) | AOJ itself carries the suit's cause # (52.003); release cites it by practice |
| Federal tax lien | Certificate of release (IRC 6325) | Self-releases 10 yrs | INSTR by serial # |
| State tax lien (Tax Code 113) | Comptroller release | until released | NAMES |
| Mechanic's lien (ch. 53) | Release (53.152), bond to indemnify (53.171), suit + lis pendens | Suit deadline 1 yr (53.158); **lapse is silent** | INSTR by practice |
| Hospital lien (ch. 55) | Release at PI settlement | — | **Does not attach to real property (55.005(d))** |

### Lis pendens (12.007-12.008)
Notice must carry the cause number (12.007(b)). Expungement order recorded (12.0071(f)) or
release ends it; **dismissal of the suit is docket-only — the lis pendens stays of record forever**.

### Tax suits (Tax Code 33-34)
Suit filed (33.41) → judgment + order of sale (33.53) → notice (34.01, cause # printed, this is
what MVBA/RealAuction publish) → sale → deed (recites the cause) → **redemption 2 yrs
homestead / 180 days other, running from deed recording (34.21)** → resale of struck-off
property (34.05). Roll drops are ambiguous (payment, 33.06 deferral, 33.02 plan, bankruptcy,
strike-off); day-of cancellations exist only on the auction site.

### Access law
Clerk records are PIA-open (Local Gov't Code 191.006). Courts are not PIA bodies; the
re:SearchTX / eFileTexas usage agreement bars "bulk copying" and storing records "in a
searchable database accessible to third parties" outside the regular course of business.
Per-record lookups on our own records are defensible; a bulk docket feed is not. Keep polite
rates and do not resell.

---

## 3. What each portal can do (live probes today + research)

| Portal | Lookup by number | Later activity visible | Anti-bot | Verdict for weekly ×100s |
|---|---|---|---|---|
| **Williamson PublicAccess** (probate court) | Yes — "Case" search mode (`#CaseSearchValue`) and `CaseDetail.aspx?CaseID=` after any search arms the session | **Yes, verified**: Register of Actions with Dispositions + Events & Orders (e.g. `Order Granting Letters of Admin & Judgment Decl. Heirship`, `Inventory / Affidavit Due`, `Bond Approval`), party block with Administrator, retained attorney + phone, Decedent **DOD** | None | **Easy** |
| **Travis Odyssey Portal** (probate court) | Smart Search by record number; Case Cross-Reference filter | Hearings and Events list; documents reportedly served | **reCAPTCHA v2 confirmed on submit today** (site key present, 13 refs, 2 iframes) | OK with 2captcha (already a dependency) |
| **Bell Odyssey Portal** (`justice.bellcounty.texas.gov/PublicPortal`) | case # (press) | docket entries, no images (press) | TLS chain failed for the fetcher; captcha unverified | **Verify first** |
| tccsearch (Travis OPR) | Yes — `txtInstrumentNoFrom/To` field verified; also `txtLDStreetAddress` (property address) and grantor/grantee | Only by re-searching names / address for new doc types (Trustee's Deed, Release, Rescission); "related documents" panel unverified (detail page unreachable by deep link) | Cloudflare (US residential + headed + stealth) | Fragile, ~1 search per record |
| bell publicsearch (OPR) | Yes — "doc#" search; OCR full-text option | Name / full-text re-search | Headed only | OK |
| Williamson Tyler OPR | Instrument search | Name re-search | AWS WAF | Fragile |
| Foreclosure sale results | — | **Published nowhere** (clerks, trustee firms, PropertyRadar excludes TX) | — | Infer from recorded Trustee's Deeds; TCAD Prodigy deeds API (Travis, no auth, 2-4 wk lag) and tax.wilcotx.gov deed history (Williamson) already documented in memory |
| re:SearchTX | case # | register + docs ($0.10/page), 2018+ | login, bulk-copy ban | Fallback only; coverage of these courts unverified |

---

## 4. Verdict, type by type

**Probate — the one place "case tracking" is the right idea.**
*Williamson:* works today. We already store the CaseID; the portal has no captcha; the
Register of Actions exposes exactly the events an investor cares about (letters granted →
PR can sign; inventory filed; application/order to sell; closing) plus the administrator,
their attorney and the DOD. Gap to accept: independent executors sell with no court filing,
and affidavit-in-lieu hides the property list, so "no new event" is not "nothing happened".
*Travis and Bell:* our current "probate" feeds are the wrong object for tracking. CERT COPY OF
PROBATE is a chain-of-title recording made at the filer's option, usually when a sale is
already underway; affidavits of heirship have no case at all. To track Travis estates we must
first pull them from the Odyssey Portal (captcha). That change is worth more as a *new-record*
source (estates appear months earlier than the recorded cert copy) than as a monitor.

**Foreclosure — case tracking cannot work; outcome inference can.** No case number exists
for 51.002 sales; postponement, withdrawal and reinstatement generate no filing; the
Trustee's Deed cites the Deed of Trust, not our notice. The honest signals are (a) a new notice
next month for the same borrower/Deed-of-Trust = still in default, sale slipped; (b) a
recorded Trustee's / Substitute Trustee's Deed = gone (new owner is a flipper/REO lead); (c) a
recorded Rescission = back in play. (a) is already how the pipeline behaves (it re-emits the
"new" notice); (b)/(c) need a monthly ownership check, which the existing
`foreclosure_sale_audit_*` scripts plus the TCAD Prodigy deed endpoint can do without touching
tccsearch. Start parsing the Deed-of-Trust instrument number and trustee file number out of the
notice text so re-postings can be matched to the original record instead of landing as new rows.

**Liens — not worth tracking.** The only later event is a release (less distress) or, for an
AOJ, a suit that is already over. Mechanic's liens lapse silently. Drop hospital liens entirely.

**Lis pendens — marginal.** The cause number is in the notice image, not the index, so
capturing it means OCR of each image (Travis) or reading the publicsearch detail (Bell).
The payoff (detecting expungement/dismissal) mostly removes records rather than creating
opportunity. Defer.

**Tax delinquent — the best "case tracking" is already in the file.** The Travis roll
carries `Cause #`, `Judgement Date` and `Bankruptcy Number` for every parcel. A parcel
*gaining* a cause number = suit filed (owner can still sell and pay at closing — the buy
window); gaining a judgment date = a few months to the sheriff's sale; a bankruptcy number
= stop marketing. All three are discarded today. The daily diff engine
(`travis_texdel_state.py`) already compares roll-to-roll; adding these three columns to the
diff and to the upload CSV is the cheapest high-value change in this whole analysis.

---

## 5. DataSift side — feasible, with two cautions

- **Custom fields:** the "Deceased & Heir Intelligence" group already exists and the wizard
  auto-maps any CSV header that exactly matches a field label. Proposed fields: `Case Number`
  (text), `Case Status` (select: Open / Letters Issued / Inventory Filed / Sale Ordered /
  Closed), `Last Case Event`, `Last Case Event Date` (date), `Case Attorney`, `Tax Suit Cause #`,
  `Tax Judgment Date`. Fields are created in the UI (or via the existing
  `build_custom_fields.py` builder, which writes — owner approval required; the API is
  read-only by policy).
- **Weekly update upload:** the Sold flow proves the wizard matches on property address and
  merges tags/fields onto existing records. Updates would ship as a small tag-update CSV
  (`case_update_2026-10`, `probate_letters_issued`, `tax_suit_filed`, …) through the same
  headed wizard path the Actor already uses.
- **Caution 1:** tag-added sequence triggers do NOT fire on upload (verified earlier, see
  memory); if the owner wants a task or status change on "letters issued", the sequence must key
  on a status change or a human step, not the uploaded tag.
- **Caution 2:** Williamson probate records in the CRM were matched by address found *after*
  enrichment; ~15% of sampled records have no Source URL and cannot be monitored.

---

## 6. Recommendation

Build, in this order:

1. **Tax-suit status diff (Travis roll)** — surface `Cause #` / `Judgement Date` /
   `Bankruptcy Number` as custom fields + tags, and alert on transitions in the daily diff.
   No new source, no captcha, no ToS exposure. ~1 day.
2. **Williamson probate docket monitor** — weekly job: for each CRM Probate record with a
   `CaseID` Source URL, open the Register of Actions, diff against the stored event list
   (KVS `probate_case_state`), emit a tag-update CSV + Slack line for letters granted /
   inventory / sale orders / closing, and backfill Administrator + attorney + DOD onto the
   record. ~2-3 days. Also fix the Williamson scraper to read the party block on first pull.
3. **Travis probate via Odyssey Portal (pilot)** — new scraper with 2captcha on Smart Search,
   date-filed search for new estates, same monitor as (2). Keep the tccsearch CERT COPY feed as
   the "sale imminent" signal it actually is. ~1 week; measure captcha cost and solve rate for
   a month before committing. Verify the Bell portal (cert chain + captcha) in the same pilot.
4. **Foreclosure outcome check** — parse Deed-of-Trust instrument # + trustee file # from
   notice text; monthly, after the first Tuesday, check TCAD deeds (Travis) / Williamson tax
   detail for a Trustee's Deed and tag `foreclosure_sold` / `foreclosure_reposted`; extend the
   existing `foreclosure_sale_audit_*` scripts rather than building a tccsearch monitor.
5. **Do not build** lien-release tracking, lis pendens docket tracking, or a tccsearch
   "related documents" monitor. Remove hospital liens from the lien pull. Fix the dead Travis
   Source URL and the dedup-ID regex while in that code.

What would change this verdict: if the owner's real goal is *buying at or after the sale*
(trustee deeds, tax deeds, redemptions) rather than pre-sale acquisition, then item 4 and the
tax-deed redemption window become the priority and a recorded-deed monitor by property address
(tccsearch `txtLDStreetAddress`, publicsearch doc search) is worth its fragility.

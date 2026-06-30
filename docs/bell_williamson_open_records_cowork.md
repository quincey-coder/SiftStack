# Bell & Williamson Code Enforcement — Open-Records Runbook (Co-Work)

**Give this to a Claude Co-Work session that has Gmail connected.** It walks the agent
through requesting code-enforcement records from Bell & Williamson county cities,
watching for the replies, and turning each reply into clean DataSift leads.

---

## What this is (read first)

Code-enforcement leads for **Bell** and **Williamson** counties do **not** come from a
live feed. (Travis is different — that's the live Austin Code Socrata API, already
automated.) Every Bell/Williamson city answers a **Texas Public Information Act (PIA)**
request one at a time: you ask in writing, a clerk emails back a file days later.

So this is a **request → wait → ingest** loop, run on a **monthly cadence**. There is no
"standing request" under Texas law — each month is a fresh request for that month's new
cases.

You (the agent) do four things:
1. Draft + send the requests to the cities that accept email.
2. Hand the user a short checklist for the cities that are portal-only.
3. Watch the inbox for replies.
4. Filter each reply down to real leads and load them into DataSift.

---

## Before you start

- **Gmail connected** in this session — this is the account that sends the requests and
  receives the replies.
- **Requester identity** (goes on every legal request — fill these in):
  - Name: `Quincey Jackson`
  - Email: `quincey@kessair.com`
  - Phone: `(254) 541-9611`
- **Date window** for this run: from the day after your last request through today.
  *First run:* use the **last 90 days** to seed a backlog, then monthly after that.
- **(Optional) the SiftStack repo** in this session. If it's here, prefer its scripts —
  they're the canonical tools and they auto-apply the lead filter. If it's not, this doc
  has everything embedded so you can do it by hand.

---

## Hard rules — do not skip

1. **Never send a request without showing the user the drafts and getting an explicit
   "go."** These are legal requests sent under their name.
2. **Fee cap is $25.** Every request states that if the cost will exceed $25 the city
   must send an estimate first. If a city replies with a fee or estimate, **STOP and ask
   the user** before agreeing to anything.
3. **Never email an unverified address.** For "confirm first" cities, confirm the address
   by phone before sending — do not guess.
4. **Monthly cadence, fresh window each time.** Don't ask for records you already pulled.
5. **Stay factual.** A plain records request — no promises, pressure, or commentary.

---

## The workflow

### Step 1 — Build the requests
**Repo present:**
```bash
python scripts/build_open_records_requests.py --all-tiers
```
This renders one request per jurisdiction into `output/open_records/` and prints the
routing (which go by email, which by portal). Review it.

**No repo:** use the **template** and **city table** at the bottom of this doc. Fill in the
date window and requester identity for each city.

### Step 2 — Send the EMAIL-channel requests (after approval)
For each **Email** city in the routing table:
1. Compose the Gmail message — subject + body from the template, addressed to the listed
   inbox.
2. Show the user **all drafts together** in one batch for review.
3. On their **"go,"** send them.
4. **Log each send** (city, date window, date sent, message ID) so you can match the reply
   later and never double-send.

### Step 3 — Hand off the PORTAL + confirm-first cities
These can't be emailed. Give the user a short checklist — one line per city with the
portal URL and the exact request text to paste — and ask them to submit (or walk them
through it live). Mark these "submitted, awaiting reply" once done.

### Step 4 — Watch for replies
Over the following days, check the Gmail inbox for replies from the cities you emailed.
Texas law gives them ~**10 business days**. Replies come back as **CSV, Excel, PDF, or a
portal download link** — formats vary by city.

### Step 5 — Filter each reply down to real leads
When records come back, keep only what's actionable:

- **DROP closed cases.** Any status reading *closed / complied / "in compliance" /
  resolved / void / completed / inactive*. A closed case means the problem was already
  fixed — not a lead.
- **KEEP only neglect / distress violation types:**
  > tall grass, uncut lawn, weeds, overgrowth, debris, trash, rubbish, junk, junked or
  > inoperable vehicles, dilapidated / substandard / dangerous / unsafe / condemned /
  > fire-damaged structures, vacant or abandoned buildings, property abatement, nuisance,
  > accumulation, unsanitary conditions.
- **DROP administrative types** (not neglect):
  > work or building without a permit, sign / banner violations, zoning / land use /
  > setback, noise, parking, business license, alarm permits, tree / right-of-way,
  > temporary / solicitation.
- If a violation type is ambiguous, judge it by the standard: *does this signal a
  physically neglected property with a likely absentee or overwhelmed owner?* Keep if yes.

**Repo present** — the ingest applies this filter for you and handles CSV/Excel/PDF
(including scanned PDFs via OCR):
```bash
python -c "from open_records_ingest import parse_response_file; \
n = parse_response_file('PATH_TO_FILE', 'CITY', 'COUNTY'); print(len(n), 'leads kept')"
```
Then upload through the normal DataSift flow.

**No repo** — read the file yourself, apply the rules above, and output a clean CSV with
the columns in the **Output format** section. For scanned-PDF replies, read each page and
transcribe the cases.

### Step 6 — Load into DataSift
The kept records go to the **"Code Violation"** list. Owner names are often missing in the
city's data — that's expected; the enrichment step fills the owner from the county
appraisal district (CAD) and then skip-traces for phones. Tag every record
`Courthouse Data`, `code_violation`, the county, and the `YYYY-MM`.

### Step 7 — Close the loop
Update your send log with each city's outcome (records received / fee quoted / no
response). Anything still open rolls into next month's follow-up.

---

## City routing reference

### Tier 1 — priority (covers the investor target ZIPs)

| City | County | Channel | Send to |
|------|--------|---------|---------|
| Belton | Bell | **Email** ✅ | openrecords@beltontexas.gov |
| Round Rock | Williamson | **Email** ✅ | openrecords@roundrocktexas.gov |
| Georgetown | Williamson | **Email** ✅ | records@georgetowntexas.gov |
| Leander | Williamson | **Email** ✅ | orr@leandertx.gov |
| Liberty Hill | Williamson | **Email** ✅ | openrecords@libertyhilltx.gov |
| Killeen | Bell | **Portal** | https://killeentx.justfoia.com/publicportal/home/newrequest (JustFOIA) |
| Temple | Bell | **Portal** (or email) | https://cityoftempletx.nextrequest.com/ — or customercare@templetx.gov |
| Hutto | Williamson | **Portal** | https://huttotx.mycusthelp.com/WEBAPP/_rs/supporthome.aspx (GovQA) |
| Salado | Bell | **Portal** | https://www.saladotx.gov/administration/webform/open-records-request-form |
| Cedar Park | Williamson | **Confirm first** ⚠ | Address obfuscated — confirm (likely lquinn@cedarparktexas.gov) by calling 512-401-5002 |

### Tier 2 / 3 — wider coverage (lower yield; include if doing "all cities")

| City | County | Channel | Send to / note |
|------|--------|---------|----------------|
| Nolanville | Bell | **Email** ✅ | publicinformationrequests@nolanvilletx.gov |
| Taylor | Williamson | **Email** ✅ | lucy.aldrich@taylortx.gov |
| Pflugerville | Williamson | **Email** ✅ | citysecretary@pflugervilletx.gov |
| Jarrell | Williamson | **Email** ✅ | municlerk@cityofjarrell.com — *no code dept, low yield* |
| Florence | Williamson | **Email** ✅ | openrecords@florencetex.com — *low yield* |
| Morgan's Point Resort | Bell | **Email** ✅ | kelli.merolillo@mprtx.gov |
| Bartlett | Bell/Wm | **Email** ✅ | joseph.resendez@bartlett-tx.us — *one city hall serves both counties* |
| Thrall | Williamson | **Email/form** | cityclerk@cityofthrall.com — *no code dept* |
| Copperas Cove | Bell | **Portal** | https://copperascovetx.mycusthelp.com/ (GovQA) |
| Harker Heights | Bell | **Portal** | https://harkerheights.civicweb.net/Portal/ (CivicWeb) |
| Granger / Weir / Coupland / Little River-Academy | both | **Confirm first** ⚠ | Tiny towns — call City Hall for the address; most have no code enforcement |
| Unincorporated Bell | Bell | **Email** (low) | Shelley.Coston@Bellcounty.texas.gov — *county does nuisance only, via Fire Marshal* |
| Unincorporated Williamson | Williamson | **Email** (low) | piarequest@wilco.org — *no county code enforcement at all* |

> **Reality check:** the counties themselves and the tiny towns have little or no
> code-enforcement records (no zoning/building authority). The yield is in the real cities
> — Killeen, Temple, Round Rock, Georgetown, Cedar Park, Leander, Hutto, Belton, Liberty
> Hill. Don't be surprised when a county or village responds "no responsive records."

---

## The request template

Fill `{{...}}` placeholders. Keep it plain and narrow to hold costs down.

> **Subject:** Texas Public Information Act Request — Code Enforcement Records ({{CITY}})
>
> To the Public Information Officer / Records Custodian, City of {{CITY}}, Texas:
>
> Pursuant to the Texas Public Information Act (Texas Government Code, Chapter 552), I
> respectfully request copies of the following public records:
>
> All code enforcement / code compliance cases **opened or updated between {{START_DATE}}
> and {{END_DATE}}**. For each case, I request the fields your system can export,
> including: property address, parcel or account number, date opened, case/violation type
> or description, current case status, and the name of the property owner or responsible
> party where contained in the record.
>
> **Format:** To minimize cost and effort, I prefer to receive these records
> **electronically as a CSV, Excel, or delimited-text file** sent to this email address.
> If the data already exists as an exportable report or dataset, that export is acceptable
> as-is.
>
> **Cost:** If you anticipate that responding will cost more than **$25**, please send an
> itemized written estimate before doing the work so I can narrow the request if needed.
>
> I am happy to clarify or narrow this request. Thank you for your time.
>
> Quincey Jackson
> quincey@kessair.com
> (254) 541-9611

---

## Output format (no-repo path)

When producing leads by hand, output a CSV with one row per kept case:

| Column | Value |
|--------|-------|
| Property Street | property street address |
| Property City | city |
| Property State | TX |
| Property ZIP | 5-digit zip |
| Owner First Name / Owner Last Name | from the city's data if present, else leave blank (CAD fills it) |
| Lists | `Code Violation` |
| Tags | `Courthouse Data, code_violation, {{county}}, {{YYYY-MM}}` |
| Notes | violation type + status, e.g. `Violation: Tall Grass/Weeds \| Status: Open` |
| Notice Type | code_violation |
| County | Bell or Williamson |
| Date Added | the case open date (YYYY-MM-DD) |
| Parcel ID | parcel/account number if present |

Hand this CSV to the user for upload, or load it through the repo's DataSift uploader.

---

## Troubleshooting

- **City quotes a fee / estimate** → stop, show the user, get approval before agreeing.
- **No response after 10 business days** → send a short, polite follow-up citing the
  original request date.
- **City says "use our portal"** → move that city to the portal list and submit there.
- **Reply is a scanned PDF** → the repo ingest OCRs it automatically; by hand, read each
  page and transcribe the cases.
- **No owner in the returned data** → expected. Don't discard the record — the enrichment
  step resolves the owner from the appraisal district.
- **"Land Use Violation" / "Work Without Permit" cases** → drop them (administrative, not
  neglect).
- **A whole county/village replies "no records"** → normal; note it and move on.

---

*Source of truth for contacts: `src/open_records_registry.json` in the SiftStack repo.
Verify a city's current PIA email/portal there (or on the city's official site) before a
first send — government contacts drift.*

---
name: directskip-datasift-clean
description: Clean a DirectSkip (or any "contactinfo") skip-trace return into a DataSift/REISift-ready upload CSV, with every phone callable and a Notes block saying which person each number belongs to. Use this skill whenever the user uploads or points at a skip-trace result file — typically named `{jobid}-contactinfo-*.csv` with columns like `Input First Name`, `ResultCode`, `Matched First Name`, `Phone1`..`Phone7`, `Relative1 Name`, `Person2 First Name` — or says "clean this skip trace", "I got my skip trace back", "prep this skip file for DataSift", "second skip", "format these numbers for upload", "the skip came back", "get these numbers into DataSift", or mentions DirectSkip, IDI, batch skip tracing, or a re-skip of existing records. Also trigger when the user asks how to get relative/heir phone numbers into DataSift, why skip-traced numbers aren't showing as callable, or how to fit too many numbers under the 30-phone limit. Handles deceased owners (relatives become heir candidates), low-confidence address-only matches, and DataSift's 30-phone-slot ceiling — with Trestle IQ tier-priority dropping (keep Dial First/Second, cut Drop then Dial Fourth then Dial Third) when Trestle results are supplied.
---

# DirectSkip → DataSift Cleaning

Turn a raw skip-trace return into a single upload-ready CSV. Every number the
vendor found becomes a **callable** phone on the record, and the Notes field
tells the caller **whose phone each number is** — owner, co-owner, or which
relative.

Zero tolerance for: numbers silently dropped, a relative's phone presented as
the owner's, or a caller dialing someone whose relationship they can't see.

> **Getting the file:** result files can now be pulled programmatically instead
> of downloaded by hand — `python src/directskip.py list` then
> `python src/directskip.py download --id <order> --out file.csv` (portal), or
> traced fresh via the single-record API (`src/directskip.py search`). This
> cleaning skill is unchanged: it still takes a `contactinfo` CSV and produces
> the DataSift upload. Use it whenever you have the vendor CSV, however it arrived.

## CRITICAL: Before Starting

1. Read `scripts/clean_directskip.py` — the cleaning engine.
2. Ask for the vendor file only. **You do NOT need the DataSift export** that
   was sent to be skipped. The clean is standalone. If the user hands you both,
   confirm before joining them — merging is a different, rarer job (see
   "If a merge IS requested" below).
3. Confirm the vendor layout matches (below). If headers differ, **say so and
   stop** — do not silently misalign columns.
4. Run the pipeline, then run **every check in the Verification section**. Report
   the numbers. Do not claim the file is ready without them.

## What DirectSkip Returns

A ~266-column CSV, one row per input record. The shape repeats three times:

| Block | Columns |
|---|---|
| Echo of what you sent | `Input First/Last Name`, `Input Mailing *`, `Input Property *`, `Input Custom Field 1-3` |
| Match result | `ResultCode`, `Matched First/Last Name`, `Age`, `Deceased` |
| Primary person | `Phone1`..`Phone7` + `Phone{n} Type`, `Email1`, `Email2`, `Confirmed Mailing *` |
| Relatives | `Relative1`..`Relative5`, each with `Name`, `Age`, `Phone1`..`Phone5` (+ Type) |
| Additional persons | the **entire** block above repeated with `Person2 ` and `Person3 ` prefixes |

Key facts learned the hard way:

- **`Input Custom Field 1/2/3` come back EMPTY.** There is no ID passthrough.
  If you ever need to rejoin to a source list, the key is normalized
  **property address + 5-digit ZIP**.
- **`ResultCode`**: `CI` = real contact match. `AB1`/`AB2` = matched on
  **ADDRESS, not name** — the vendor returns a *different person* (real
  example: input `Barbara Smith` → returned `Donald Smith`). Blank = no match.
- **`Deceased = Y`** flips the whole record: the relatives are now **heirs**,
  and the decision maker is one of them, not the owner.
- **Line types** are `Mobile` / `Residential` / `OtherPhone`. DataSift wants
  `MOBILE` / `LANDLINE` / `OTHER`.
- **Names arrive ALL-CAPS.** Title-case them. (ALL-CAPS in an output file is a
  standing corruption red flag.) They arrive already split into first/last, so
  there is **no NAMELF flip risk here** — never reorder the parts.
- **The same number legitimately appears under several people** (a co-owner is
  usually also listed as a relative; households share a landline). Dedupe to one
  phone slot, but report it under **every** person it belongs to.

## Standing Decisions

These are settled. Do not re-litigate them unless the user says otherwise.

1. **EVERY number is uploaded callable** — owner, co-owner, and relatives all
   get real phone slots. (An earlier "relatives in Notes only" policy was
   reversed: relatives in a text field are not dialable, so they may as well not
   exist.)
2. **Notes carries a `WHO EACH NUMBER BELONGS TO` block**, grouped by person.
   This is the deliverable, not a nicety — it is the only thing standing between
   a caller and pitching a house to someone's cousin.
3. **Never overwrite the mailing address.** Use the `Input Mailing *` values.
   The vendor's `Confirmed Mailing *` goes into Notes only. Overwriting silently
   redirects in-flight mail sequences.
4. **Dial order = owner → co-owners → relatives**, Mobile before Landline within
   each person, so the best contact is always first. When Trestle scores are
   loaded, tier outranks line type within each person (a live landline beats a
   dead mobile): Dial First → Dial Second → Dial Third → unscored → Dial
   Fourth → Drop, then Mobile before Landline as tiebreak.
5. **The 30-phone cap is enforced by Trestle tier, not collection order**
   (owner decision, 2026-08-12). When the record is over the cap AND Trestle
   results are supplied (`--trestle-results`), cut the LEAST desirable numbers
   first: invalid → Drop → Dial Fourth → unscored → Dial Third → Dial Second
   → Dial First. **Dial First and Dial Second are the primary dial targets and
   are always the last to go.** Ties within a tier cut the lowest-priority
   person's number first (a Person-3 relative loses before the owner). Without
   Trestle data the legacy first-come-first-served cut applies. Records under
   the cap keep everything — even Drop-tier numbers (the phone TAGS uploaded
   from the Trestle run tell callers not to dial those).

## Pipeline

### Step 1: Property + Owner
- Property street/city/ZIP from `Input Property *`. ZIP truncated to 5 digits
  (the vendor sends ZIP+4). State uppercased. Title-case street and city.
- Owner name = `Matched First/Last Name`, title-cased. If the vendor returned
  no name, fall back to `Input First/Last Name`.
- **Never build an owner name by re-splitting a full-name string.** Both halves
  arrive separately; use them.

### Step 2: Mailing
Straight from `Input Mailing *`, ZIP truncated to 5. Do not substitute the
confirmed address (Step 6 records it instead).

### Step 3: Collect every person, in dial priority order
1. `OWNER` (label it `OWNER (DECEASED)` when `Deceased = Y`)
2. `PERSON 2` / `PERSON 3` — additional owners/residents at the property
3. Relatives of the owner — `RELATIVE n (of owner)`, or
   **`HEIR CANDIDATE n`** when the owner is deceased
4. Relatives of Person 2 and Person 3 — `RELATIVE n (of Person 2)`

Skip any relative slot whose `Name` is blank; a nameless number can't be
attributed and must not be dialed.

### Step 4: Assign phone slots
- Within each person, sort by Trestle tier first when scores are loaded
  (best tier = dialed first), then Mobile before Landline before Other.
- Walk people in the order above; assign each unseen number the next slot.
- A number already slotted keeps its one slot and is **still reported under the
  later person too**.
- Normalize to bare 10 digits (strip a leading `1`; discard anything not 10
  digits long).
- **Cap at 30 — that is DataSift's ceiling.** When over the cap with Trestle
  results loaded, cut worst-tier first per Standing Decision 5; otherwise
  numbers past slot 30 overflow first-come-first-served. Either way the cut
  numbers go to Notes (Step 7) — never silently dropped.

### Step 5: Emails
Owner, then Person2/Person3, deduped case-insensitively, into `Email 1`..`Email 6`.

### Step 6: Notes — the dial reference
Build in this order:

```
=== DIRECTSKIP SKIP TRACE - MM/YYYY ===
Matched: <name> | age <n> | result <code>

** OWNER REPORTED DECEASED - the decision maker is an heir below, not the owner. **
** LOW-CONFIDENCE MATCH (AB1) - vendor matched on address, not name.
   Input name: <sent> / Returned: <got>
   Verify identity before dialing. **

CONFIRMED MAILING ADDRESS (skip trace - NOT applied to record):
  <confirmed>
  on file: <what we kept>

--- WHO EACH NUMBER BELONGS TO (dial reference) ---
Look up the number you are calling. Order below = dial priority.

OWNER: James Parks, age 61
  325-232-4481 (M)
  432-234-0826 (M)

RELATIVE 2 (of owner): Hang Li, age 48
  916-355-0210 (L)
```

**NEVER print phone slot numbers** (`Phone 12 : ...`). See "DataSift Upload
Behavior" — the import appends behind existing phones, so any slot label is
offset by however many phones that record already had and will point a caller
at the wrong row. Key the reference on the **number itself**.

`M` = mobile, `L` = landline, `O` = other. Include the legend at the bottom.

### Step 7: Overflow / tier cut (records with >30 numbers)
Label them in **two** places so they can't be missed:
- Inline, in the person's own block. With Trestle scores:
  `512-555-0001 (M) [Drop 8]  <- CUT at phone cap (lowest Trestle tier), not uploaded`
  Without: `817-983-9264 (M)  <- OVERFLOW, not uploaded, dial manually`
- A consolidated section listing each with its owner. With Trestle scores:
```
=== CUT AT THE PHONE CAP - 6 NOT UPLOADED ===
This record found 36 numbers but DataSift only holds 30.
Lowest Trestle tiers were cut first (invalid -> Drop ->
Dial Fourth -> unscored -> Dial Third); Dial First / Dial Second
are the primary targets and are always kept first.
The numbers below have NO phone slot - dial manually only if
the uploaded numbers dead-end.

  512-555-0001 (M) [Drop 8]  -  OWNER: James Parks
  512-555-0026 (M) [Dial Fourth 30]  -  RELATIVE 2 (of owner): Rel2 Parks
```
Without Trestle scores, the legacy `=== OVERFLOW NUMBERS ===` section.
Tag the record `skip2 phone overflow` (plus `skip2 trestle trimmed` when the
tier logic did the cutting). **Never drop a number silently.**

Every uploaded number also carries its tier inline in the dial reference
(`512-555-0002 (M) [Dial First 92]`) so a caller can see dial priority
without leaving Notes.

### Step 8: Tags
Always: `skip2`, `DirectSkip`, `Second Skip MM/YYYY`.
Conditional: `living` or `skip2 deceased`, `skip2 low confidence match`,
`skip2 confirmed addr differs`, `skip2 no phone`, `skip2 phone overflow`,
`skip2 trestle trimmed` (tier logic cut numbers to fit the cap).

### Step 9: Output columns
Use DataSift's verified auto-mapping labels, phone slots extended to 30:

```
Property Street Address, Property City, Property State, Property ZIP Code,
Owner First Name, Owner Last Name,
Mailing Street Address, Mailing City, Mailing State, Mailing ZIP Code,
Phone 1..Phone 30, Email 1..Email 6,
Tags, Notes, Owner Deceased
```

Only the core address block auto-maps on import. **`Notes` and `Owner Deceased`
must be mapped by hand in step 4 of the upload wizard** — warn the user, because
a missed mapping silently discards the entire dial reference.

## DataSift Upload Behavior — VERIFIED

**The importer APPENDS phones and DEDUPES them.** Canary-verified on live
records: a number already on the record did not duplicate, and a pre-existing
number absent from the upload survived.

Consequences:
- Upload **only the new numbers**. Never merge the existing phone set back in.
- Existing phones keep their slot, their order, and — critically — their
  **Phone Status**, which is where DNC and wrong-number live.
- Our numbers land **behind** whatever was already there. Hence: no slot numbers
  in Notes, ever.

### Re-verify with a canary before any bulk upload
Do not skip this on a new account or after a DataSift release.

Build a 2-record test file with the **same header** as the real one:
- **Record A — overlapping.** Some uploaded numbers already on the record. This
  splits all three behaviors into three different phone counts:
  `replace = new count`, `append+dedup = existing + new - overlap`,
  `append+no-dedup = existing + new`.
- **Record B — zero overlap.** A clean yes/no confirmation.

Then have the user report **one phone count**, and check whether a number that
exists *only* on the record (never in the upload) survived. Survived = append.
Gone = replace.

**Reject any candidate record where two numbers differ by only 1-2 digits.** A
look-alike like `505-642-0041` vs `575-642-0041` makes the "did the old number
survive?" check unreadable and will hand you the wrong answer.

If it ever comes back **replace**, stop — the upload must carry existing + new
merged, or it destroys phone history on every record.

## Verification — run ALL of these, report the counts

Never call the file ready without these. Each one caught a real defect.

| Check | Must be |
|---|---|
| Output rows == input rows | equal |
| Phone slot gaps (a filled slot after an empty one) | 0 |
| Duplicate phone within a single record | 0 |
| Malformed phone (not exactly 10 digits) | 0 |
| Duplicate email within a single record | 0 |
| Rows missing property street or ZIP | 0 |
| ALL-CAPS owner first/last names | 0 |
| Property/mailing ZIPs not 5 digits | 0 |
| Every uploaded number appears in its own record's Notes | 100% |
| Stale `Phone <n> :` slot references in Notes | 0 |
| Unique overflow/cut numbers labeled in Notes | == overflow count |
| Tier violations (a worse-tier number kept while a better one was cut) | 0 |
| Mailing address changed from input | 0 |

Two traps when writing these checks:

- **Don't substring-match a stripped digit blob.** Concatenating a whole Notes
  field and searching for `5551234567` produces false hits across boundaries.
  Parse the formatted `NNN-NNN-NNNN` lines instead.
- **Overflow LINES > overflow NUMBERS is correct**, because a shared number is
  listed under each person it belongs to. Compare *unique* numbers.

## Running It

Two engines ship with this skill. They implement the same algorithm.

```bash
# Python — use this anywhere Python is available (Co-Work, Linux, macOS)
python scripts/clean_directskip.py \
  --input  "248923-contactinfo-my_list.csv" \
  --output "skip_trace_CLEANED_08-2026.csv" \
  --stamp  "08/2026" --verify

# With Trestle IQ tier-priority capping: run the phone-validator skill on the
# numbers first, then feed its output back in (repeatable for batched runs).
# Accepts validation_results.csv (preferred: has scores + validity) or
# phone_tags_for_datasift.csv (tags only).
python scripts/clean_directskip.py \
  --input  "248923-contactinfo-my_list.csv" \
  --output "skip_trace_CLEANED_08-2026.csv" \
  --stamp  "08/2026" --verify \
  --trestle-results "output/phone_validation/validation_results.csv"
```

```powershell
# PowerShell — for Windows boxes without Python. This is the ORIGINAL
# implementation and the one whose output was verified against a live
# 196-record run; edit its paths at the top of the file.
powershell -File scripts/clean_directskip.ps1
```

Options (Python): `--max-phones N` (default 30), `--max-emails N` (default 6),
`--trestle-results CSV` (repeatable; enables tier-priority capping),
`--verify` (run the whole checklist and print the table).

**Provenance note:** the PowerShell engine produced the verified 196-record
output. The Python engine is a direct port of that logic; run it with
`--verify` on first use and confirm all checks pass before trusting its output.
If the two ever disagree, the checklist in Verification decides — not the
script. **Trestle tier-priority capping exists only in the Python engine** —
the PowerShell engine still cuts first-come-first-served; use Python whenever
Trestle results are in play.

## Run Summary Format

```
DirectSkip Clean — 08/2026
Source: 248923-contactinfo-priority_1.csv  (196 rows, 266 cols)
Output: skip_trace_CLEANED_08-2026.csv     (196 rows, 49 cols)

Phones uploaded         : 2,979
Emails uploaded         :   351
Records over 30 slots   :     4  (56 numbers in Notes, tagged)
Trestle scores loaded   : 3,035 numbers from 1 file(s)
Tier-trimmed records    :     4  (cut: Drop 31, Dial Fourth 19, Dial Third 6)
Deceased owners         :     9  (relatives → heir candidates)
Low-confidence matches  :     6  (AB1/AB2 — verify before dialing)
Confirmed addr differs  :    71  (in Notes, mailing untouched)
Records with no phone   :     2

Verification: 12/12 checks passed.
NEXT: map Notes + Owner Deceased by hand in step 4 of the wizard.
```

## Things To Flag To The User, Not Fix Silently

- **Low-confidence rows.** Name them. `Training Tcad → Michael Caton` is a
  training/junk record that should be deleted from the source list, not dialed.
- **No-match rows.** Usually a bad owner name upstream — recommend re-deriving
  it from the CAD by parcel ID and re-skipping.
- **Deceased owners.** These need the heir/decision-maker path, not a normal
  dial. The relatives are the leads.
- **Overflow records.** Say which properties and how many numbers didn't fit.

## If A Merge IS Requested

Only when the user explicitly asks to combine the return with an existing
DataSift export (231 cols, last column literally `exported from REISift.io`):

- Join on normalized **property address + ZIP5** (no vendor ID exists).
- Round-trip the **export's own header**, not this skill's — it carries 30 phone
  slots each with `Phone Type/Status/Tags/Is Connected N` plus the bulk list
  columns. Drop `exported from REISift.io`; add `Notes` and `Owner Deceased`.
- **Never clobber `Phone Status`.** Existing phones keep slot, type, status,
  tags and connected flag; new numbers append behind them.
- Such a file sits outside SiftStack's `list_validator` header gate — it is not
  a SiftStack-built list.

Given the import appends and dedupes, a merge is almost never necessary. Prefer
the standalone clean.

# Kessair — Drive Organization Standard

**The rulebook for how files and folders are named and organized *inside* every Kessair Shared Drive.**

*Version 1.0 — 2026-07-01 · Pairs with: Kessair_Shared_Drive_Blueprint.md*

---

## How this pairs with the Blueprint

- **The Blueprint** = the *map*: which 18 Shared Drives exist, who owns them, and who can see them.
- **This Standard** = the *rulebook*: how you name and organize the folders and files *inside* those drives.

The Blueprint tells a VA which room to walk into. This Standard tells them where to put things once they're in the room — so every room looks the same and nothing gets lost.

> **The core idea:** don't organize by hand-building folders forever — organize by **defining a system once and cloning it.** Every deal, property, project, and campaign is spun up from a *template*, named by a *convention*, and aged out by a *retention rule*. Consistency comes from the system, not from willpower.

---

## 1. The Five Principles

1. **Shallow and wide, not deep and narrow.** Never bury a file more than **~4 clicks** from a drive root. If you're nesting a 5th level, either split the drive or move the detail into the *file name* instead.
2. **One item, one home.** A file lives in exactly one place. If it needs to appear somewhere else, use a Drive **shortcut**, never a copy. (Copies drift out of sync — the #1 way a drive rots.)
3. **The name carries the metadata.** Don't make a folder for every attribute. Encode the date, type, and version in the *file name* so search and sorting do the work.
4. **Active first, then archive.** Live work sits at the top; finished work moves down into an `Archive` folder. You should always see current work first.
5. **Templates over memory.** Every repeatable unit (a deal, a property, a campaign) is cloned from a `_TEMPLATES` folder — so structure is automatic and identical every time.

---

## 2. Folder Numbering System

Number folders so they sort in *workflow order*, not alphabetical order, and leave gaps to insert later.

```
00–09   Start / intake      (00_START-HERE, 01_Inbox)
10–89   Working sections     (numbered in the order work actually flows)
_       Pinned to top        (_TEMPLATES, _INBOX — leading underscore sorts first)
90–99   End-of-life          (90_Archive, 99_Reference)
ZZ_     Pinned to bottom     (ZZ_Old, ZZ_Do-Not-Use)
```

- **Gap by 10s** at the top level (`00, 10, 20…`) so you can insert `15` later without renumbering everything.
- **Gap by 1s** at the leaf level (`01, 02, 03…`) where the set is stable.
- A **leading underscore** pins special folders to the very top; a **`ZZ_`** prefix sinks junk to the bottom.

---

## 3. File Naming Convention

**The single most important rule in this document.** One pattern, used everywhere:

```
YYYY-MM-DD_[Scope]_[DocType]_[Short-Description]_vNN.ext
```

| Field | Rule | Example |
|---|---|---|
| **Date** | ISO `YYYY-MM-DD` (or `YYYY-MM` for monthly). Always first — makes files sort chronologically. | `2026-07-01` |
| **Scope** | The deal/property/entity it belongs to, hyphenated. | `123-Main-St` |
| **DocType** | From the controlled list below. | `Contract` |
| **Description** | Short, hyphenated, human-readable. | `Purchase-Agreement` |
| **Version** | `vNN` for drafts; drop it once final. | `v02` |

**Format rules:**
- **Hyphens *within* a field, underscores *between* fields** — so the name is machine-parseable (`123-Main-St` is one field).
- **No spaces** — keeps it consistent with the SiftStack automation (which already writes `Bell_probate_20260701.csv`) and clean in URLs/scripts.
- **Lowercase or Title-Case, pick one and hold it.** (Recommend Title-Case for human files, lowercase for automated.)

**Real examples:**
```
2026-07-01_123-Main-St_Contract_Purchase-Agreement_v02.pdf
2026-07-01_123-Main-St_Comp_Two-Bucket-ARV.xlsx
2026-07-01_Riverside-MF_PPM_Investor-Deck_v01.pdf
2026-06_ACQ_Campaign_Probate-SMS-Results.csv
2026-07-01_Kessair-Homes_Entity_Operating-Agreement.pdf
```

**Controlled DocType vocabulary** (extend as needed, but keep it short):

`Entity · Contract · LOI · Comp · Rehab-SOW · Analysis · HUD · Deed · Title · Lease · Loan · Insurance · Invoice · Report · Deck · PPM · K1 · Photo · Scope · Permit · Draw · Script · SOP`

> **Why this matters:** with this convention, a VA can type `123-Main-St Contract` in Drive search and instantly find the purchase agreement — regardless of which folder it's in. The name *is* the index.

---

## 4. Versioning & Dates

- **Draft files** get `_vNN` (`v01`, `v02`). The highest number is current.
- **When a doc is final, remove the version** — one clean canonical file. (`..._Purchase-Agreement.pdf`)
- **Never** `Final`, `Final-v2`, `Final-REAL`, `Final-USE-THIS`. If you need to keep old drafts, move them into the unit's `90_Archive/` folder — don't leave them beside the current one.
- **For anything dated** (contracts, reports), the date in the name is the *document's* date, not today's date.

---

## 5. Special Folders (every drive uses these)

| Folder | Where | Purpose |
|---|---|---|
| `00_START-HERE` | Root of every drive | A one-page Google Doc: what this drive is, what goes where, the naming rule, who to ask |
| `_TEMPLATES` | Any drive that spawns repeatable units | The canonical clone-from set (deal folder, SPV folder, etc.) |
| `_INBOX` | Data/ops drives | Unsorted drop zone — *must be emptied weekly*, never a permanent home |
| `90_Archive` | Inside each unit or drive | Superseded drafts and finished work |
| `99_Reference` | Where useful | Static lookups (target ZIPs, glossaries, checklists) |

> Every drive gets a `00_START-HERE` doc. It's 10 minutes of work that saves every future VA an hour of confusion.

---

## 6. Depth Limit & Shortcuts

**Max depth: 4 levels below the drive root.** Example of the deepest a file should sit:

```
K·ACQ·40 (drive)  →  01_Pipeline  →  2026-07-01_123-Main-St  →  04_Offer-&-Contract  →  the file
        1                 2                     3                        4
```

If you find yourself wanting a 5th level, **stop** — encode that detail in the *file name* instead, or split the drive.

**Shortcuts, not copies:** when the same file logically belongs in two places (e.g., a closing statement needed by both the deal team and the bookkeeper), keep the real file in one home and drop a **Drive shortcut** in the other. One source of truth, two doors.

---

## 7. The Canonical Templates

These live in each drive's `_TEMPLATES` folder. You **clone** one to spin up a new unit — never build from scratch.

### 7a. Deal folder (in `K·ACQ·40 / 01_Pipeline`)
```
2026-07-01_123-Main-St-Austin/         ← name = acquisition date + address
├── 00_Lead-&-Contact/        seller info, call notes, skip-trace/phone tiers
├── 01_Comps-&-ARV/           Two-Bucket ARV report, comp photos
├── 02_Rehab-Estimate/        4-tier SOW, contractor bids
├── 03_Deal-Analysis/         MAO/ROI analyzer, financing scenarios
├── 04_Offer-&-Contract/      LOI, executed purchase agreement, amendments
├── 05_Property-Media/        photos, video, inspection report
├── 06_Dispositions/          buyer blast, assignment agreement, buyer POF
├── 07_Title-&-Closing/       title commitment, HUD, recorded deed
└── 90_Archive/               superseded drafts
```

### 7b. Property SPV folder (in `K·PROP·60`)
```
SPV — 123-Main-St LLC/                 ← name matches the LLC
├── 00_Entity/                formation, EIN letter, operating agreement, registered agent
├── 01_Acquisition/           purchase contract, HUD, deed, title policy, inspection
├── 02_Financing/             loan docs, refi, amortization, insurance
├── 04_Financials/            rent roll, T-12, property tax, returns
├── 05_CapEx-&-Improvements/  renovation records, receipts
└── 06_Disposition/           sale or 1031 exchange docs
```
> Leases & maintenance for this property live in `K·PROP·65`, **not** here — see the mirroring rule (§8).

### 7c. Home build folder (in `K·HOM·60`)
```
SPV — 45-Vista-Ridge-Spec LLC/
├── 00_Entity/           ├── 01_Lot-&-Acquisition/    ├── 02_Construction-Loan/
├── 03_Permits-&-Plans/  ├── 04_Budget-&-Draws/       ├── 05_Subs-&-Schedule/
├── 06_Inspections-&-Warranty/   └── 07_Sale-Staging-MLS/
```

### 7d. Development project folder (in `K·DEV·60`)
```
SPV — Riverside-MF LLC/
├── 00_Entity-&-JV/      ├── 01_Entitlement-&-Zoning/  ├── 02_Design-&-Civil/
├── 03_GC-&-Construction/├── 04_Draws-&-Budget/        ├── 05_Leasing-Sale/
└── 06_Project-Reporting/
```
> Investor cap tables / distributions for this project live in `K·DEV·65` (the walled drive), **not** here.

### 7e. Portfolio company folder (in `K·EQ·65`)
```
{PortCo} LLC/
├── 00_Ownership-&-Board/   ├── 01_Monitoring-&-KPIs/
├── 02_Value-Creation/      └── 03_Exit-Planning/
```

### 7f. Campaign folder (in `K·ACQ·30 / 02_Campaigns`)
```
2026-07_Probate-SMS/                   ← name = month + channel + niche
├── 00_List-Used/        ├── 01_Creative-&-Scripts/
├── 02_Results-&-Metrics/└── 03_Opt-Outs/  ⚠ (compliance — mirror to ACQ·07)
```

### 7g. SOP document (a Google Doc template)
```
Title:  SOP — [Task]  ·  [Company]  ·  v[NN]
Sections: Purpose · When to use · Steps (numbered) · Tools/links · Owner · Last-reviewed date
```

---

## 8. The "Same Asset, Multiple Drives" Mirroring Rule

One property generates records in **three** drives. Keep them lined up by using the **exact same name** in each, so they're visually parallel:

```
K·PROP·60 (Portfolio)          →  SPV — 123-Main-St LLC/     (ownership, entity, finance history)
K·PROP·65 (Property Mgmt)      →  123-Main-St/               (leases, tenants, maintenance)
K·PROP·70 (Asset Finance)      →  123-Main-St/               (rent roll, T-12, distributions)
```

- **Same core name (`123-Main-St`) everywhere** so anyone can find all three at a glance.
- Optionally drop a **shortcut** in the `PROP·60` SPV folder pointing to its `·65` and `·70` counterparts, so the ownership folder is the "front door" to the whole asset.
- This is *not* duplication — each drive holds *different* records for the same asset, separated because they have different audiences (owner/finance vs. tenant-facing ops).

---

## 9. "How to Add X" Playbooks

### Add a new deal (wholesaling)
1. Clone `K·ACQ·40 / _TEMPLATES / Deal` into `01_Pipeline`.
2. Rename to `YYYY-MM-DD_Address`.
3. Work it through folders `00 → 07`. On close, move it to `02_Closed-Deals / {year}`.

### Add a new property (Properties / Homes / Development)
1. **Form the LLC** first (attorney).
2. File formation docs in `K·CORP·00 / 01_Entity-&-Cap-Table / [Company] / [Asset]`.
3. Clone the SPV template into the company's Portfolio/Builds/Projects drive → rename to match the LLC.
4. Create the matching `[Address]` folder in the ops + finance drives (mirroring rule §8).

### Add a new campaign
1. Clone `K·ACQ·30 / _TEMPLATES / Campaign` into `02_Campaigns`.
2. Rename `YYYY-MM_Channel-Niche`. Copy opt-outs to `K·ACQ·30 / 07_Compliance`.

### Add a new company (new vertical)
→ Follow Blueprint §11, Phases 1–7 (Groups → Drives → Permissions → Folders → People).

---

## 10. Retention & Archive Rules

| Content | Rule |
|---|---|
| **Active deals / leads** | Top of the drive; move to `Closed`/`Archive` when done |
| **Monthly lead folders** (`ACQ·30`) | Auto-archive after **12 months** to `08_Archive` |
| **Entity, tax, legal docs** | **Keep forever.** Never delete. |
| **Compliance / opt-outs / investor records** | **Keep forever, read-only.** Your legal defense. |
| **Superseded drafts** | Move to the unit's `90_Archive/` — don't delete, don't leave beside the final |
| **`_INBOX` drop zones** | Emptied weekly — never a permanent home |

---

## 11. Quick-Reference Cheat Sheet

**Naming:** `YYYY-MM-DD_Scope_DocType_Description_vNN.ext` · hyphens-within, underscores_between, no spaces

**Folders:** number in workflow order (`00, 10, 20…`), gap by 10s · `_` pins to top · `ZZ_` sinks to bottom

**Depth:** max 4 levels below a drive · need more? → put it in the file name

**Copies:** never — use a **shortcut**

**New unit:** always clone from `_TEMPLATES`

**Every drive has:** a `00_START-HERE` doc and (if it spawns units) a `_TEMPLATES` folder

**Same asset in 3 drives:** identical name in each; PROP·60 is the front door

---

*End of Organization Standard v1.0. Pairs with the Shared Drive Blueprint.*

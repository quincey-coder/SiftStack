# Kessair — Company Shared Drive Blueprint

**A holding-company Google Drive architecture: structure, reasoning, permissions, and build steps — for all six companies.**

*Version 2.0 — 2026-07-01 · Owner: Kessair Holdings · Status: all entities LIVE*

---

## What this document is

This is the master plan for how Kessair organizes **everything** in Google Drive as a multi-company holding group (like Berkshire Hathaway or Brookfield — one brand, many operating companies).

Version 2.0 designs out **all six companies in full**: the corporate center plus the five operating businesses, each with its own Shared Drives, folders, roles, and exact permissions.

> **The one idea that runs through everything:** *Your Drive mirrors your legal company structure.* Holding company at the top → shared corporate services → operating companies → individual properties/deals. The security walls in the Drive fall in exactly the same place as the legal walls between your companies.

### The six companies

| Code | Company | What it does | # Drives |
|---|---|---|---|
| `CORP` | **Kessair Holdings** | Holding company + shared back-office | 3 |
| `ACQ` | **Kessair Acquisitions** | Wholesaling (assign contracts) | 3 |
| `PROP` | **Kessair Properties** | Buy & hold rentals | 3 |
| `HOM` | **Kessair Homes** | Luxury spec homes (build to *sell*) | 3 |
| `DEV` | **Kessair Development** | Commercial + multifamily development | 3 |
| `EQ` | **Kessair Equity** | Private equity / buying businesses | 3 |

**Total: 18 Shared Drives.** Everything else in this document is a *folder* inside one of them.

---

## Table of Contents

1. [The Model — why a holding company](#1-the-model)
2. [Core Concepts — Drives, Folders, SPVs, and Permissions](#2-core-concepts)
3. [The Entity Family & Naming System](#3-the-entity-family)
4. [Global Roles — the seats that span every company](#4-global-roles)
5. [Kessair Holdings — Corporate Drives](#5-corporate)
6. [Kessair Acquisitions — Wholesaling](#6-acquisitions)
7. [Kessair Properties — Buy & Hold](#7-properties)
8. [Kessair Homes — Luxury Spec Homes](#8-homes)
9. [Kessair Development — Commercial + Multifamily](#9-development)
10. [Kessair Equity — Private Equity](#10-equity)
11. [How to Build It — Step by Step](#11-how-to-build-it)
12. [Governance & Conventions](#12-governance)
13. [Appendix — Quick Reference](#13-appendix)

---

## 1. The Model

### What Kessair is

Kessair is a **holding company (HoldCo)**. It doesn't "do" one thing — it *owns* several separate operating companies, each running its own business under the Kessair brand. This mirrors:

- **Berkshire Hathaway** — a tiny corporate center (capital, tax, legal) on top of businesses that each run themselves.
- **Brookfield / Blackstone** — one "platform" per vertical, all sharing central finance/legal/compliance functions.

### Why build it this way

| Reason | What it protects |
|---|---|
| **Liability isolation** | A lawsuit against one company is trapped there; the others and the HoldCo are shielded. |
| **Clean investor money** | Outside capital sits in its own entity with its own books — never mixed with unrelated risk. |
| **Tax efficiency** | Different activities are taxed differently; separate entities keep each one clean. |
| **Clean sale / spin-off** | Sell or refinance one company without touching the others. |
| **Right people see the right things** | Each company is its own security boundary. |

> ⚖️ **Note:** The structural logic here is standard, but exact entity types and tax elections (LLC vs. S-corp, Texas Series LLC, etc.) should be confirmed with a Texas real-estate CPA and attorney.

---

## 2. Core Concepts

### 2a. Shared Drive vs. Folder — how to tell which is which

| | **Shared Drive** | **Folder** |
|---|---|---|
| **What it is** | A top-level container owned by the *company* | A subdivision *inside* a Shared Drive |
| **Permissions** | Its own membership list — the **security boundary** | Inherits access from its drive |
| **Analogy** | A locked room | A shelf inside that room |

**The decision rule:**

> **If "who is allowed to see this?" is *different* from its parent → it's a Shared DRIVE.**
> **If "who can see this?" is the *same* as its parent → it's a FOLDER.**

> ⚠️ We use many Shared Drives instead of one big drive with folders because folder-by-folder permission overrides leak. Shared Drive *membership* is a hard wall.

### 2b. The SPV — one property, one company

An **SPV (Special Purpose Vehicle)** is a separate LLC holding **one single asset** (one house, one building, one portfolio company). It isolates that asset's liability from all the others.

**In the Drive: one SPV = one folder.** This applies to Properties, Homes, Development, and Equity's portfolio companies. (Wholesaling never owns property, so it has no SPVs.)

### 2c. Google's exact permission levels

| Level | Short | What they can do |
|---|---|---|
| **Manager** | `M` | Everything, incl. add/remove members and delete the drive |
| **Content Manager** | `CM` | Add, edit, move, delete, organize files — the "power user" |
| **Contributor** | `C` | Add and edit files — but **cannot move or delete** |
| **Commenter** | `Cmt` | View and comment only |
| **Viewer** | `V` | Read-only |
| *(none)* | `—` | Not a member — the drive is **invisible** |

### 2d. Manage people with Google Groups

**One Google Group per role.** Add the *Group* to a drive once at the right level; onboard/offboard by adding/removing people from the Group. Never touch drive sharing again.

---

## 3. The Entity Family

**Naming convention:** `K·[CODE]·[NN] — Name`
- `K` = Kessair · `CODE` = the company · `NN` = orders drives within a company

```
                        🏛️  KESSAIR HOLDINGS   (HoldCo — owns everything)
                                     │
   ┌───────────┬────────────┬────────┴─────┬─────────────┬────────────┐
  CORP        ACQ          PROP           HOM           DEV           EQ 🧱
 Back-Office Acquisitions Properties      Homes      Development     Equity
  (shared)   (wholesale)  (buy & hold)  (spec homes) (comm + MF)    (PE)
   LIVE        LIVE          LIVE          LIVE         LIVE          LIVE
```

**Legend used throughout:** `🟦 DRIVE` = a Shared Drive · `📂` = a folder · `⚠` = sensitive · `🔒` = most restricted · `🧱` = hard information wall

---

## 4. Global Roles

Three seats span **every** company. They appear in every matrix below.

| Role | Group | What they do | Cross-company access |
|---|---|---|---|
| **Owner / Chairman** (you) | `owners@` | Own and direct the whole group | `Manager` everywhere |
| **Group COO** | `ops-coo@` | Run operations across all companies | `Manager` on operating drives; limited Corporate |
| **CFO / Bookkeeper** | `finance@` | Books, payroll, tax across all of Kessair | `CM` in Corporate Finance; `Viewer` elsewhere |

> **Why the CFO/Bookkeeper crosses every wall:** consolidating the group's books *requires* seeing every company. But they **edit only in Corporate Finance** and are **read-only everywhere else** — enough to pull statements without touching operations.

Each operating company below then has its **own** functional roles.

---

## 5. Corporate

**Kessair Holdings** — the thin corporate center. Three drives, each a different audience.

```
🟦 DRIVE  —  🔒 K·CORP·00 · Governance & Capital
             Audience: Owners + CFO/Bookkeeper ONLY
   ├─ 📂 01 Entity & Cap Table/        Holdings LLC, every company's formation docs, ownership %
   ├─ 📂 02 Consolidated Finance/       group P&L, inter-company, tax, audit (by year)
   ├─ 📂 03 Banking & Capital/          bank accounts, lenders, investor relations, proof of funds
   ├─ 📂 04 M&A / New Verticals/    ⚠   targets & LOIs for the next company
   └─ 📂 05 Strategy & Board/           vision, KPIs, quarterly reviews

🟦 DRIVE  —  🔒 K·CORP·10 · People & Back Office
             Audience: Owners + COO + CFO/Bookkeeper
   ├─ 📂 01 People & HR/                contracts, payroll, 1099s, reviews (ALL companies)
   ├─ 📂 02 IT & Systems/          ⚠   domains, API-key INVENTORY (pointers only), vendor accounts
   └─ 📂 03 Automation Runbooks/        SiftStack · DataSift · Apify guides

🟦 DRIVE  —  K·CORP·20 · Company Hub (Knowledge + Brand)
             Audience: EVERYONE (the on-ramp for every hire in every company)
   ├─ 📂 01 Start Here (Onboarding)/
   ├─ 📂 02 SOP Master Library/         procedures, foldered by company + shared
   ├─ 📂 03 Training Library/
   ├─ 📂 04 Org Chart & Directory/
   ├─ 📂 05 Brand & Kessair Identity/   logos, guidelines, letterhead
   └─ 📂 06 Blank Templates/            approved blank contract/LOI/assignment templates
```

**Corporate permission matrix**

| Role | CORP·00 🔒 | CORP·10 🔒 | CORP·20 |
|---|:--:|:--:|:--:|
| Owner | `M` | `M` | `M` |
| Group COO | `V` ¹ | `CM` | `CM` |
| CFO / Bookkeeper | `CM` | `V` ² | `V` |
| *Everyone else* | `—` | `—` | `V` |

¹ Drive-level `—`; folder-share `05 Strategy & Board/` to the COO. · ² Folder-level `CM` on `01 People & HR/Payroll/` for the bookkeeper.

> ⚠️ **Never store real passwords or API keys in Drive** — `CORP·10 / 02` holds *pointers* to a password manager, not the secrets.

---

## 6. Acquisitions

**Kessair Acquisitions — Wholesaling.** Assign contracts; never owns property (no SPVs). Three drives split by *who touches the work*: data people, deal people, the playbook.

### Drives & folders

```
🟦 DRIVE  —  K·ACQ·30 · Marketing & Lead Data
   ├─ 📂 01 First-to-Market Records/            ◄◄ SiftStack RAW RECORDS (records, NOT leads)
   │     ├─ 📂 00 Inbox/                        raw records land here — scraped data, nobody has responded yet
   │     │      ├─ 📂 daily/  ← GOOGLE_DRIVE_FOLDER_ID points here → 2026/07-July/{County}/{type}/ (auto-built)
   │     │      │        types: foreclosure · tax_delinquent · tax_sale · probate · lien · eviction · code_violation · divorce
   │     │      └─ 📂 photo-import/             Dropbox photo pipeline raw drops
   │     ├─ 📂 02 Uploaded to DataSift/
   │     ├─ 📂 03 Sold / Cleanup/
   │     ├─ 📂 04 Forensics & Audit/            diffs · raw pulls · run logs
   │     ├─ 📂 05 Deep Prospecting Reports/
   │     ├─ 📂 08 Archive/
   │     └─ 📂 09 Reference/                    target ZIPs, source list, glossary
   ├─ 📂 02 Campaigns/                          SMS · Direct Mail · Cold Call · RVM
   ├─ 📂 03 Skip Trace & Enrichment/
   ├─ 📂 04 Market Intelligence/
   ├─ 📂 05 Buyer Lists/
   ├─ 📂 06 Creative Assets/
   └─ 📂 07 Compliance/                    ⚠    DNC · litigator-scrub · opt-outs (keep forever)

🟦 DRIVE  —  K·ACQ·40 · Deal Flow (Acquisitions · Dispositions · Closing)
   ├─ 📂 01 Pipeline (Active)/
   │     └─ 📂 {123 Main St, Austin}/           one folder per deal (cloned from Templates)
   │           └─ 00 Lead → 01 Comps/ARV → 02 Rehab → 03 Deal Analysis →
   │              04 Offer/Contract → 05 Photos → 06 Dispositions → 07 Title/Closing
   ├─ 📂 02 Closed Deals/                        by year
   ├─ 📂 03 Dead Deals/
   ├─ 📂 04 Templates/                           blank deal-folder set to clone
   └─ 📂 05 Dispo Buyer CRM/

🟦 DRIVE  —  K·ACQ·50 · Ops & SOPs
   ├─ 📂 01 SOP Library/      ├─ 📂 02 Playbooks & Scripts/   ├─ 📂 03 Org & Roles/
   └─ 📂 04 Daily Routines/   └─ 📂 05 Meetings & Comms/
```

### Roles

| Role | Group | Mission | Home |
|---|---|---|---|
| **VA / Data Manager** | `acq-data@` | Ingest, clean, upload lead data; list hygiene | `ACQ·30` |
| **Cold Caller / Prospector** | `acq-callers@` | Dial lists, log outcomes, set appointments | `ACQ·30` |
| **Lead Manager** | `acq-leads@` | Qualify leads, route hot ones, keep pipeline clean | `ACQ·30/40` |
| **Acquisitions / Closer** | `acq-closers@` | Run offers, negotiate, get contracts signed | `ACQ·40` |
| **Dispositions** | `acq-dispo@` | Build buyer list, market & assign deals | `ACQ·40` |
| **TC / Transaction Coordinator** | `acq-tc@` | Contract → close: title, docs, deadlines | `ACQ·40` |
| **Sales Manager** | `acq-sales@` | Lead & coach the team; own scripts + KPIs | `ACQ·30/40/50` |

### Permission matrix

| Role | CORP·00 | CORP·20 | ACQ·30 Data | ACQ·40 Deals | ACQ·50 Ops |
|---|:--:|:--:|:--:|:--:|:--:|
| Owner | `M` | `M` | `M` | `M` | `M` |
| Group COO | `V` | `CM` | `M` | `M` | `M` |
| CFO / Bookkeeper | `CM` | `V` | `V` | `V` | `V` |
| Sales Manager | `—` | `V` | `CM` | `CM` | `CM` |
| Lead Manager | `—` | `V` | `CM` | `C` | `V` |
| Acquisitions / Closer | `—` | `V` | `V` | `CM` | `V` |
| Dispositions | `—` | `V` | `V` | `CM` | `V` |
| TC / Transaction Coordinator | `—` | `V` | `—` | `CM` | `V` |
| Cold Caller / Prospector | `—` | `V` | `C` | `—` | `V` |
| VA / Data Manager | `—` | `V` | `CM` | `—` | `V` |
| SiftStack bot (service acct) | `—` | `—` | `CM` | `—` | `—` |

> **Cold Caller = Contributor** on lead data: adds call notes, *cannot delete or reorganize lists*. **Data and deals stay separate** — data VAs never see deal financials, closers only view the lead context.

---

## 7. Properties

**Kessair Properties — Buy & Hold.** Acquire and *hold* rentals for cash flow. Long-term = capital gains / depreciation / 1031, so it stays walled off from dealer activity (Homes, wholesaling). **Every property is its own SPV.**

### Drives & folders

```
🟦 DRIVE  —  K·PROP·60 · Portfolio (Assets)
   ├─ 📂 SPV — 123 Main St LLC/                 one property = one LLC = one folder
   │     └─ 00 Entity/ → 01 Acquisition/ → 02 Financing & Refi/ →
   │        03 Insurance & Tax/ → 04 Disposition/1031/
   ├─ 📂 SPV — 456 Oak Ave LLC/  (same shape)
   └─ 📂 _Acquisition Pipeline/                 rentals under evaluation (pre-SPV)

🟦 DRIVE  —  K·PROP·65 · Property Management
   ├─ 📂 01 Leases & Tenants/              ⚠    applications, leases, tenant PII (by property)
   ├─ 📂 02 Maintenance & Work Orders/          vendors, turns, inspections
   ├─ 📂 03 Vendor Roster & COIs/               contractors + insurance certificates
   └─ 📂 04 Leasing & Marketing/                vacancy listings, screening

🟦 DRIVE  —  K·PROP·70 · Asset Finance & Investor Reporting
   ├─ 📂 01 Rent Roll & T-12/             ├─ 📂 02 Refinance & Debt/
   ├─ 📂 03 Distributions/           ⚠    └─ 📂 04 Investor Reporting/  ⚠  (if outside capital)
```

### Roles

| Role | Group | Mission | Home |
|---|---|---|---|
| **Asset Manager** | `prop-asset@` | Portfolio performance, refi/disposition decisions | `PROP·60/70` |
| **Property Manager** | `prop-pm@` | Day-to-day operations, tenants, maintenance | `PROP·65` |
| **Leasing Coordinator** | `prop-leasing@` | Fill vacancies, screen tenants | `PROP·65` |
| **Maintenance Coordinator** | `prop-maint@` | Work orders, vendors, unit turns | `PROP·65` |
| **Acquisitions Analyst** | `prop-acq@` | Source & underwrite new rentals | `PROP·60` |

### Permission matrix

| Role | CORP·00 | CORP·20 | PROP·60 Portfolio | PROP·65 Prop Mgmt ⚠ | PROP·70 Finance ⚠ |
|---|:--:|:--:|:--:|:--:|:--:|
| Owner | `M` | `M` | `M` | `M` | `M` |
| Group COO | `V` | `CM` | `M` | `M` | `M` |
| CFO / Bookkeeper | `CM` | `V` | `V` | `V` | `CM` |
| Asset Manager | `—` | `V` | `CM` | `V` | `CM` |
| Property Manager | `—` | `V` | `V` | `CM` | `V` |
| Leasing Coordinator | `—` | `V` | `—` | `C` | `—` |
| Maintenance Coordinator | `—` | `V` | `—` | `C` | `—` |
| Acquisitions Analyst | `—` | `V` | `C` | `—` | `V` |

> **Tenant PII lives in `PROP·65`.** Leasing and maintenance are `Contributors` (add applications/work orders, can't reorganize or delete). Only the Property Manager organizes it, and Finance is read-only — separating personal data from the money.

---

## 8. Homes

**Kessair Homes — Luxury Spec Homes.** Buy lots, build, and *sell* to retail buyers (dealer income). Consumer-facing with construction-defect/warranty liability, so **every home is its own SPV.**

### Drives & folders

```
🟦 DRIVE  —  K·HOM·60 · Builds (Projects)
   ├─ 📂 SPV — 45 Vista Ridge Spec LLC/         one home = one LLC = one folder
   │     └─ 00 Entity/ → 01 Lot & Acquisition/ → 02 Construction Loan/ →
   │        03 Permits & Plans/ → 04 Budget & Draws/ → 05 Subs & Schedule/ →
   │        06 Inspections & Warranty/ → 07 Sale · Staging · MLS/
   └─ 📂 _Lot Pipeline/                          lots under evaluation

🟦 DRIVE  —  K·HOM·65 · Design & Trade Library      (reusable across all builds)
   ├─ 📂 01 Plan Library/           ├─ 📂 02 Finish & Selections Catalog/
   ├─ 📂 03 Spec Standards/         └─ 📂 04 Subcontractor Roster & COIs/  ⚠

🟦 DRIVE  —  K·HOM·70 · Sales & Marketing
   ├─ 📂 01 Active Listings/        ├─ 📂 02 Staging & Photography/
   ├─ 📂 03 Buyer Pipeline/         └─ 📂 04 Realtor & Referral Network/
```

### Roles

| Role | Group | Mission | Home |
|---|---|---|---|
| **Construction / Project Manager** | `hom-pm@` | Run builds end-to-end: subs, schedule, budget | `HOM·60/65` |
| **Site Superintendent** | `hom-super@` | On-site daily execution, quality, inspections | `HOM·60` |
| **Estimator / Purchasing** | `hom-estimating@` | Budgets, buyouts, selection pricing | `HOM·60/65` |
| **Design & Selections** | `hom-design@` | Finishes, plans, buyer selections | `HOM·65` |
| **Sales / Listing Manager** | `hom-sales@` | Market & sell finished homes | `HOM·70` |
| **Land Acquisition** | `hom-land@` | Source & tie up lots | `HOM·60` |

### Permission matrix

| Role | CORP·00 | CORP·20 | HOM·60 Builds | HOM·65 Design/Trade | HOM·70 Sales |
|---|:--:|:--:|:--:|:--:|:--:|
| Owner | `M` | `M` | `M` | `M` | `M` |
| Group COO | `V` | `CM` | `M` | `M` | `M` |
| CFO / Bookkeeper | `CM` | `V` | `V` | `—` | `V` |
| Construction / Project Manager | `—` | `V` | `CM` | `CM` | `V` |
| Site Superintendent | `—` | `V` | `C` | `V` | `—` |
| Estimator / Purchasing | `—` | `V` | `CM` | `CM` | `—` |
| Design & Selections | `—` | `V` | `V` | `CM` | `V` |
| Sales / Listing Manager | `—` | `V` | `V` | `V` | `CM` |
| Land Acquisition | `—` | `V` | `C` | `—` | `—` |

> **The Design & Trade Library is shared reusable knowledge** (plans, finish catalogs, vetted subs) — separate from the per-home financials in `Builds`, so a designer or estimator works the library without seeing every home's loan and margin.

---

## 9. Development

**Kessair Development — Commercial + Multifamily.** Ground-up development funded with **outside LP/JV capital** over multi-year horizons. Each project is an SPV. Capital/investor information is sensitive, so it gets its **own restricted drive.** Build-to-hold projects hand off to Kessair Properties.

### Drives & folders

```
🟦 DRIVE  —  K·DEV·60 · Projects (Development)
   ├─ 📂 SPV — Riverside MF LLC/                 one project = one LLC = one folder
   │     └─ 00 Entity & JV Agreement/ → 01 Entitlement & Zoning/ →
   │        02 Design & Civil Eng/ → 03 GC & Construction/ → 04 Draws & Budget/ →
   │        05 Leasing/Sale/ → 06 Project Reporting/
   └─ 📂 SPV — Main St Retail LLC/  (same shape)

🟦 DRIVE  —  🔒 K·DEV·65 · Capital & Investors            ⚠ RESTRICTED
   ├─ 📂 01 Capital Raises (PPMs · Subscriptions)/   ├─ 📂 02 Investor Cap Tables/
   ├─ 📂 03 Capital Calls & Distributions/           └─ 📂 04 Investor Reporting & K-1s/

🟦 DRIVE  —  K·DEV·70 · Predevelopment Pipeline
   ├─ 📂 01 Land & Site Search/     ├─ 📂 02 Feasibility & Underwriting/
   ├─ 📂 03 Zoning & Municipal/     └─ 📂 04 Broker & Seller Relationships/
```

### Roles

| Role | Group | Mission | Home |
|---|---|---|---|
| **Development Director** | `dev-director@` | Own projects end-to-end (project executive) | `DEV·60/70` |
| **Predevelopment / Entitlement Mgr** | `dev-predev@` | Land, zoning, permitting, feasibility | `DEV·70` |
| **Capital Markets / Investor Relations** | `dev-capital@` | Raise & manage LP/JV capital | `DEV·65` ⚠ |
| **Construction Manager (Owner's Rep)** | `dev-construction@` | Oversee the GC, budget, schedule | `DEV·60` |
| **Investment Analyst** | `dev-analyst@` | Underwriting, financial models | `DEV·60/65/70` |
| **Asset Manager (Lease-up)** | `dev-asset@` | Stabilization, leasing, hand-off | `DEV·60` |

### Permission matrix

| Role | CORP·00 | CORP·20 | DEV·60 Projects | DEV·65 Capital 🔒⚠ | DEV·70 Predev |
|---|:--:|:--:|:--:|:--:|:--:|
| Owner | `M` | `M` | `M` | `M` | `M` |
| Group COO | `V` | `CM` | `M` | `V` | `M` |
| CFO / Bookkeeper | `CM` | `V` | `V` | `CM` | `V` |
| Development Director | `—` | `V` | `CM` | `V` | `CM` |
| Predev / Entitlement Mgr | `—` | `V` | `V` | `—` | `CM` |
| Capital Markets / IR | `—` | `V` | `V` | `CM` | `V` |
| Construction Manager | `—` | `V` | `CM` | `—` | `V` |
| Investment Analyst | `—` | `V` | `C` | `C` | `C` |
| Asset Manager (Lease-up) | `—` | `V` | `CM` | `—` | `—` |

> **`DEV·65 Capital & Investors` is the walled drive.** Only the Owner, CFO, and Capital/IR lead manage it; the Director sees it read-only; analysts contribute models. Construction and predev staff have **no access** — investor cap tables and distributions are none of their concern.

---

## 10. Equity

**Kessair Equity — Private Equity.** Acquire operating businesses as investments (the Berkshire model). This arm handles **confidential, market-sensitive deal information ("MNPI")**, which by law must be siloed behind **information barriers ("ethical walls")**.

> 🧱 **Kessair Equity lives in its own Google Workspace Organizational Unit (or a separate tenant).** This is a wall stronger than drive-sharing. Even the Group COO and CFO get *limited* access, and **within** the deal drive, each live deal is restricted to its assigned deal team (folder-level) so conflicted staff can't see it.

### Drives & folders

```
🟦 DRIVE  —  🔒 K·EQ·60 · Deal Pipeline                  ⚠⚠ MNPI — deal-team silos
   ├─ 📂 Deal — {Target Co}/  (access limited to that deal's team)
   │     └─ 00 Sourcing & NDA/ → 01 CIM & Financials/ → 02 Due Diligence/ →
   │        03 LOI & Valuation/ → 04 Purchase Agreement/
   └─ 📂 _Sourcing Funnel/                         top-of-funnel targets

🟦 DRIVE  —  K·EQ·65 · Portfolio Companies
   ├─ 📂 {PortCo} LLC/                             one company = one folder
   │     └─ 00 Ownership & Board/ → 01 Monitoring & KPIs/ →
   │        02 Value Creation/ → 03 Exit Planning/

🟦 DRIVE  —  🔒 K·EQ·70 · Fund & LP Administration       ⚠ investor-facing
   ├─ 📂 01 Fund Formation/         ├─ 📂 02 LP Agreements & Subscriptions/
   ├─ 📂 03 Capital Calls & Distributions/  └─ 📂 04 LP Reporting & K-1s/
```

### Roles

| Role | Group | Mission | Home |
|---|---|---|---|
| **Managing Partner** (you) | `eq-partners@` | Direct the fund; final investment decisions | all `EQ` |
| **Investment Partner / Principal** | `eq-partners@` | Lead deals & portfolio companies | `EQ·60/65` |
| **Associate / Analyst** | `eq-associates@` | Sourcing, diligence, modeling | `EQ·60/65` |
| **Operating Partner** | `eq-ops@` | Drive value creation post-close | `EQ·65` |
| **Fund Controller** | `eq-controller@` | Specialized PE fund accounting | `EQ·70` |
| **Investor Relations (LP)** | `eq-ir@` | Manage LP relationships & reporting | `EQ·70` |
| **Compliance Officer** | `eq-compliance@` | Enforce the information barrier | oversight |

### Permission matrix

| Role | CORP·00 | EQ·60 Pipeline 🔒⚠ | EQ·65 Portfolio | EQ·70 Fund/LP 🔒⚠ |
|---|:--:|:--:|:--:|:--:|
| Owner / Managing Partner | `M` | `M` | `M` | `M` |
| Investment Partner / Principal | `—` | `CM` ¹ | `CM` | `V` |
| Associate / Analyst | `—` | `C` ¹ | `C` | `—` |
| Operating Partner | `—` | `—` | `CM` | `—` |
| Fund Controller | `CM` ² | `V` | `V` | `CM` |
| Investor Relations (LP) | `—` | `—` | `V` | `CM` |
| Compliance Officer | `—` | `V` | `V` | `V` |
| Group COO | `—` | `—` | `V` | `—` |

¹ Access to `EQ·60` is further restricted **per deal** at the folder level — you only see the deals you're assigned to. · ² The Fund Controller may be a dedicated seat (PE fund accounting is specialized) rather than the group bookkeeper.

> 🧱 **The information barrier is the point.** Deal staff are walled from other companies, the broader team is walled from live deals, and conflicted individuals are walled from specific deals — the strictest segregation in all of Kessair.

---

## 11. How to Build It

**Order: Groups → Drives → Permissions → Folders → People.** Do it once per company.

### Phase 0 — Prerequisites (one-time)
1. Register `kessair.com`.
2. **Google Workspace Business Standard or higher** (Shared Drives are *not* in Business Starter). *Confirm current tiers when signing up.*
3. For **Kessair Equity**, create a **separate Organizational Unit** in the Admin console for the information wall.

### Phase 1 — Create the Google Groups (all companies)
```
GLOBAL   owners@   ops-coo@   finance@
ACQ      acq-data@ acq-callers@ acq-leads@ acq-closers@ acq-dispo@ acq-tc@ acq-sales@
PROP     prop-asset@ prop-pm@ prop-leasing@ prop-maint@ prop-acq@
HOM      hom-pm@ hom-super@ hom-estimating@ hom-design@ hom-sales@ hom-land@
DEV      dev-director@ dev-predev@ dev-capital@ dev-construction@ dev-analyst@ dev-asset@
EQ       eq-partners@ eq-associates@ eq-ops@ eq-ir@ eq-controller@ eq-compliance@
```

### Phase 2 — Create the 18 Shared Drives
```
CORP: K·CORP·00 · Governance & Capital   K·CORP·10 · People & Back Office   K·CORP·20 · Company Hub
ACQ:  K·ACQ·30 · Marketing & Lead Data    K·ACQ·40 · Deal Flow               K·ACQ·50 · Ops & SOPs
PROP: K·PROP·60 · Portfolio               K·PROP·65 · Property Management     K·PROP·70 · Asset Finance
HOM:  K·HOM·60 · Builds                    K·HOM·65 · Design & Trade Library   K·HOM·70 · Sales & Marketing
DEV:  K·DEV·60 · Projects                  K·DEV·65 · Capital & Investors 🔒   K·DEV·70 · Predevelopment
EQ:   K·EQ·60 · Deal Pipeline 🔒 (own OU)  K·EQ·65 · Portfolio Companies       K·EQ·70 · Fund & LP Admin 🔒
```

### Phase 3 — Add Groups to each drive per the matrices in Sections 5–10.

### Phase 4 — Build the folder skeletons (Sections 5–10). Dated lead folders auto-create via SiftStack.

### Phase 5 — Wire automation. Add the **SiftStack service account** as `Content Manager` on `K·ACQ·30` **only**; point the Drive uploader at `K·ACQ·30 / 01 First-to-Market Records`.

### Phase 6 — Add people to Groups. Onboarding = add to a Group; offboarding = remove.

### Phase 7 — Per-asset folders. As you acquire/build, add one SPV folder per property (PROP/HOM/DEV) or portfolio company (EQ).

---

## 12. Governance

| Convention | Rule |
|---|---|
| **Naming** | `K·[CODE]·[NN] — Name` on every drive |
| **Security boundary** | New *audience* = new Shared **Drive**; same audience = a **folder** |
| **People management** | Always via Google **Groups** |
| **One SPV = one folder** | Every property/project/portfolio company gets its own LLC + folder |
| **Secrets** | Drive stores *pointers* to a password manager — never raw keys |
| **Compliance/investor records** | Keep-forever, read-only (`ACQ·07`, `DEV·65`, `EQ·70`) |
| **Least privilege** | Default new people to the lowest access that lets them work |
| **Information barrier** | `Kessair Equity` runs in its own OU with per-deal folder silos |
| **Dealer vs. hold** | Homes (build-to-sell) and Properties (hold) stay in separate entities for tax integrity |

---

## 13. Appendix

### All 18 Shared Drives at a glance

| Drive | Audience | Purpose |
|---|---|---|
| 🔒 `K·CORP·00` Governance & Capital | Owners + CFO | Entity, finance, banking, M&A, strategy |
| 🔒 `K·CORP·10` People & Back Office | Owners + COO + CFO | HR, IT/credentials, vendors |
| `K·CORP·20` Company Hub | Everyone | Onboarding, SOPs, training, brand, templates |
| `K·ACQ·30` Marketing & Lead Data | Data + calling team | SiftStack raw records (Inbox/daily), campaigns, buyers |
| `K·ACQ·40` Deal Flow | Deal team | Pipeline, contracts, closings |
| `K·ACQ·50` Ops & SOPs | Whole ACQ team | Playbook, scripts, routines |
| `K·PROP·60` Portfolio | Asset mgmt + acq | Property SPV folders, acquisition, disposition |
| ⚠ `K·PROP·65` Property Management | Property mgmt team | Leases, tenants (PII), maintenance, vendors |
| ⚠ `K·PROP·70` Asset Finance | Asset mgr + CFO | Rent rolls, refi, distributions, investor reporting |
| `K·HOM·60` Builds | Construction team | Per-home SPV folders, permits, loans, draws |
| `K·HOM·65` Design & Trade Library | Design + estimating | Plans, finishes, subcontractor roster |
| `K·HOM·70` Sales & Marketing | Sales team | Listings, staging, buyer pipeline |
| `K·DEV·60` Projects | Development team | Per-project SPV folders |
| 🔒⚠ `K·DEV·65` Capital & Investors | Owner + CFO + IR | PPMs, cap tables, distributions, K-1s |
| `K·DEV·70` Predevelopment | Dev + predev team | Land, feasibility, zoning |
| 🔒⚠ `K·EQ·60` Deal Pipeline | Deal team (per-deal silos) | Sourcing, CIMs, diligence, LOIs |
| `K·EQ·65` Portfolio Companies | EQ team | Board, monitoring, value creation |
| 🔒⚠ `K·EQ·70` Fund & LP Admin | Owner + controller + IR | Fund docs, capital calls, LP reporting |

### The 5 Google permission levels

`Manager` (full + members) › `Content Manager` (add/edit/move/delete) › `Contributor` (add/edit only) › `Commenter` (view+comment) › `Viewer` (read-only)

### Global seats

`Owner` (Manager everywhere) · `Group COO` (Manager on operating drives, limited Corporate/EQ) · `CFO/Bookkeeper` (edits Corporate Finance, read-only elsewhere)

---

*End of blueprint v2.0. Questions or changes → Kessair Holdings.*

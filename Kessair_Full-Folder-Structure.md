# Kessair — Full Folder Structure (Master)

**The complete tree: every LLC → every Shared Drive → every folder → what goes inside.**

*Version 1.0 — 2026-07-02 · Consolidates: Blueprint + Organization Standard + Naming Cards*

---

## Legend

```
📦 LLC              a legal entity (owns the drives beneath it)
🟦 DRIVE            a Google Shared Drive (security boundary)
📂 / name/          a folder
→                   what goes in that folder
🔒 restricted   ⚠ sensitive / keep-forever   🧱 information wall
00_START-HERE       the welcome doc pinned to the top of every drive
_TEMPLATES/         clone-from set for repeatable units (deals, SPVs…)
_NAMING             the naming card kept in each file-heavy folder
90_Archive/         superseded drafts / finished work
```

**Universal file name:** `YYYY-MM-DD_Scope_DocType_Description_vNN.ext`

---

## LLC Ownership Tree

```
🏛️ Kessair Holdings LLC              (parent — owns all below)
├── 📦 Kessair Acquisitions LLC       wholesaling (no assets held)
├── 📦 Kessair Properties LLC         buy-hold + holdco for rental SPVs
├── 📦 Kessair Homes LLC              spec builder + holdco for home SPVs
├── 📦 Kessair Development LLC        developer + holdco for project SPVs
└── 📦 Kessair Equity LLC (+ Fund I LP, GP LLC)   PE management co
```

---
---

# 📦 KESSAIR HOLDINGS LLC  →  Corporate drives

## 🟦 K·CORP·00 · Governance & Capital  🔒 *Owners + CFO only*
```
📂 00_START-HERE                         → what this drive is, rules, who to ask
📂 01_Entity-&-Cap-Table/            ⚠   → one subfolder per LLC (KEEP FOREVER)
   ├── 📂 Kessair-Holdings-LLC/          → formation, EIN, operating agreement, cap table
   ├── 📂 Kessair-Acquisitions-LLC/      → formation, EIN, OA
   ├── 📂 Kessair-Properties-LLC/        → formation, EIN, OA
   ├── 📂 Kessair-Homes-LLC/             → formation, EIN, OA
   ├── 📂 Kessair-Development-LLC/       → formation, EIN, OA
   └── 📂 Kessair-Equity-LLC/            → formation, EIN, OA (+ Fund I LP, GP LLC docs)
       └── _NAMING → YYYY-MM-DD_<LLC>_<Formation|EIN|Operating-Agreement|Cap-Table>
📂 02_Consolidated-Finance/              → group P&L, balance sheet, inter-company, tax, audit
   └── (by year)  _NAMING → <YYYY|YYYY-Qn>_<Entity>_<PL|Balance-Sheet|Tax-Return|Audit>
📂 03_Banking-&-Capital/                 → bank accounts, lenders, investor relations, proof of funds
📂 04_M&A-New-Verticals/             ⚠   → acquisition targets, LOIs for the next company
📂 05_Strategy-&-Board/                  → vision, KPIs, quarterly reviews  (COO gets this folder)
📂 _TEMPLATES/                           → blank "new LLC" doc set
```

## 🟦 K·CORP·10 · People & Back Office  🔒 *Owners + COO + CFO*
```
📂 00_START-HERE
📂 01_People-&-HR/                   ⚠   → one subfolder per person (KEEP; restricted)
   └── 📂 <Last-First>/                  → contract, W-9, 1099, offer, reviews, onboarding
       └── _NAMING → YYYY-MM-DD_<Last-First>_<Contract|W9|1099|Offer|Review>
📂 02_IT-&-Systems/                  ⚠   → domains, API-KEY INVENTORY (pointers only), vendor accounts
📂 03_Automation-Runbooks/               → SiftStack · DataSift · Apify operating guides
```
> ⚠️ Never store real passwords/API keys here — only *pointers* to a password manager.

## 🟦 K·CORP·20 · Company Hub  *Everyone (the on-ramp)*
```
📂 00_START-HERE                         → read this before anything else
📂 01_Start-Here-Onboarding/             → how we work, tool setup, first-week checklist
📂 02_SOP-Master-Library/                → COMPANY-WIDE SOPs only
   ├── 📂 SHARED/                        → onboarding, IT, expenses, security, "how to write an SOP"
   └── 📂 _Index/                        → links out to each company's own SOP library
       └── _NAMING → SOP_<Company>_<Task>_vNN
📂 03_Training-Library/                   → recorded walkthroughs, how-tos
📂 04_Org-Chart-Directory/               → who does what, contact list
📂 05_Brand-&-Identity/                  → logos, brand guidelines, letterhead
📂 06_Blank-Templates/                   → approved blank contracts / LOI / assignment forms
```
> 🚫 No lead data, deal docs, financials, or personal info here — **everyone** can read this drive.

---
---

# 📦 KESSAIR ACQUISITIONS LLC  →  Wholesaling (no SPVs)

## 🟦 K·ACQ·30 · Marketing & Lead Data
```
📂 00_START-HERE
📂 01_First-to-Market-Records/           ◄◄ SiftStack RAW RECORDS (AUTOMATED — records, NOT leads; do not rename)
   ├── 📂 00_Inbox/                      → raw scraped records land here (nobody has responded yet)
   │      ├── 📂 daily/                   ← GOOGLE_DRIVE_FOLDER_ID points HERE (SiftStack scraper feed)
   │      │      └── 2026/ → 07-July/ → {County}/ → {type}/   (Year / MM-Month / County / Type — auto-built)
   │      │            e.g.  daily/2026/07-July/Travis/probate/datasift_travis_probate_2026-07-02_153448.csv
   │      │            types: foreclosure · tax_delinquent · tax_sale · probate · lien · eviction · code_violation · divorce
   │      └── 📂 photo-import/            → Dropbox photo pipeline raw drops (eviction · code_violation · divorce)
   ├── 📂 02_Uploaded-to-DataSift/       → exact files pushed to the CRM + upload manifest
   ├── 📂 03_Sold-Cleanup/               → "sold" round-trip records
   ├── 📂 04_Forensics-&-Audit/          → diffs · raw-archive · run-logs
   ├── 📂 05_Deep-Prospecting-Reports/   → per-record PDF reports (Year / MM-Month / County / type)
   ├── 📂 08_Archive/                    → records older than 12 months
   └── 📂 09_Reference/                  → target ZIPs · source registry · notice-type glossary
📂 02_Campaigns/                         → one folder per campaign
   └── 📂 2026-07_Probate-SMS/           → 00_List · 01_Creative-&-Scripts · 02_Results · 03_Opt-Outs
       └── _NAMING → YYYY-MM_<Channel-Niche>_<List|Script|Results>
📂 03_Skip-Trace-&-Enrichment/           → Tracerfy / Trestle outputs, phone-tier reports
📂 04_Market-Intelligence/               → Market Finder exports, ZIP scoring, market reports
📂 05_Buyer-Lists/                       → cash-buyer research (Bell master, etc.)
📂 06_Creative-Assets/                   → campaign creative
📂 07_Compliance/                    ⚠   → DNC · litigator-scrub · opt-outs (KEEP FOREVER, read-only)
📂 _TEMPLATES/                           → blank Campaign folder
```

## 🟦 K·ACQ·40 · Deal Flow  *(Acquisitions · Dispositions · Closing)*
```
📂 00_START-HERE
📂 01_Pipeline/                          → ACTIVE deals — one folder each
   └── 📂 2026-07-02_123-Main-St-Austin/     (cloned from _TEMPLATES)
          ├── 📂 00_Lead-&-Contact/       → seller info, call notes, skip-trace
          ├── 📂 01_Comps-&-ARV/          → Two-Bucket ARV report, comp photos
          ├── 📂 02_Rehab-Estimate/       → 4-tier SOW, contractor bids
          ├── 📂 03_Deal-Analysis/        → MAO/ROI analyzer, financing scenarios
          ├── 📂 04_Offer-&-Contract/     → LOI, executed purchase agreement, amendments
          ├── 📂 05_Property-Media/       → photos, video, inspection
          ├── 📂 06_Dispositions/         → buyer blast, assignment agreement, buyer POF
          ├── 📂 07_Title-&-Closing/      → title commitment, HUD, recorded deed
          └── 📂 90_Archive/              → superseded drafts
          _NAMING → YYYY-MM-DD_<Address>_<DocType>_<Desc>_vNN
📂 02_Closed-Deals/                      → finished deals, by year
📂 03_Dead-Deals/                        → dead for now, kept for follow-up
📂 04_Templates/                         → the blank Deal folder (clone this)
📂 05_Dispo-Buyer-CRM/                   → buyer relationships, proof of funds on file
```

## 🟦 K·ACQ·50 · Ops & SOPs
```
📂 00_START-HERE
📂 01_SOP-Library/                       → Acquisitions-specific SOPs  (_NAMING → SOP_ACQ_<Task>_vNN)
📂 02_Playbooks-&-Scripts/               → cold-call scripts, objection handling
📂 03_Org-&-Roles/                       → role scorecards, RACI
📂 04_Daily-Routines/                    → daily checklists, KPI dashboards (STABM)
📂 05_Meetings-&-Comms/                  → meeting notes, announcements
```

---
---

# 📦 KESSAIR PROPERTIES LLC  →  Buy & Hold  *(one SPV per property)*

## 🟦 K·PROP·60 · Portfolio (Assets)
```
📂 00_START-HERE
📂 SPV — 123-Main-St LLC/                → one property = one LLC = one folder
   ├── 📂 00_Entity/                 ⚠   → formation, EIN, operating agreement, registered agent
   ├── 📂 01_Acquisition/                → purchase contract, HUD, deed, title policy, inspection
   ├── 📂 02_Financing/                  → loan docs, refi, amortization, insurance
   ├── 📂 04_Financials/                 → property tax, returns (asset-level)
   ├── 📂 05_CapEx-&-Improvements/       → renovation records, receipts
   └── 📂 06_Disposition/                → sale or 1031 exchange docs
       _NAMING → YYYY-MM-DD_<Property>_<Entity|Deed|Title|Loan|Insurance>
📂 SPV — 456-Oak-Ave LLC/                (same shape)
📂 _Acquisition-Pipeline/                → rentals under evaluation (pre-LLC)
📂 _TEMPLATES/                           → blank Property SPV folder
```

## 🟦 K·PROP·65 · Property Management  ⚠ *tenant PII*
```
📂 00_START-HERE
📂 123-Main-St/                          → SAME name as its SPV (mirroring rule)
   ├── 📂 01_Leases-&-Tenants/       ⚠   → applications, leases, screening (by unit)
   ├── 📂 02_Maintenance-&-Work-Orders/  → work orders, turns, inspections
   ├── 📂 03_Vendor-Roster-&-COIs/       → contractors + insurance certificates
   └── 📂 04_Leasing-&-Marketing/        → vacancy listings, screening pipeline
       _NAMING → YYYY-MM-DD_<Property>_<Lease|Application|Work-Order>_<Unit-or-Tenant>
📂 456-Oak-Ave/                          (same shape)
📂 _TEMPLATES/                           → blank Property-Mgmt folder
```

## 🟦 K·PROP·70 · Asset Finance & Investor Reporting  ⚠
```
📂 00_START-HERE
📂 01_Rent-Roll-&-T12/                   → by property + period
📂 02_Refinance-&-Debt/                  → refi packages, debt schedule
📂 03_Distributions/                 ⚠   → owner/investor distributions
📂 04_Investor-Reporting/            ⚠   → if outside capital involved
   _NAMING → <YYYY-MM>_<Property-or-Portfolio>_<Rent-Roll|T12|Refi|Distribution>
```

---
---

# 📦 KESSAIR HOMES LLC  →  Luxury Spec Homes  *(one SPV per home)*

## 🟦 K·HOM·60 · Builds (Projects)
```
📂 00_START-HERE
📂 SPV — 45-Vista-Ridge-Spec LLC/        → one home = one LLC = one folder
   ├── 📂 00_Entity/                 ⚠   → formation, EIN, operating agreement
   ├── 📂 01_Lot-&-Acquisition/          → lot contract, closing, survey
   ├── 📂 02_Construction-Loan/          → loan docs, draw schedule
   ├── 📂 03_Permits-&-Plans/            → permits, approved plans
   ├── 📂 04_Budget-&-Draws/             → budget, Draw-01, Draw-02…
   ├── 📂 05_Subs-&-Schedule/            → subcontractor contracts, build schedule
   ├── 📂 06_Inspections-&-Warranty/     → inspection reports, warranty docs
   └── 📂 07_Sale-Staging-MLS/           → staging, listing, sale docs
       _NAMING → YYYY-MM-DD_<Build-Address>_<Permit|Plan|Draw|Selection|Inspection>
📂 _Lot-Pipeline/                        → lots under evaluation
📂 _TEMPLATES/                           → blank Home-Build folder
```

## 🟦 K·HOM·65 · Design & Trade Library  *(reusable across all builds)*
```
📂 00_START-HERE
📂 01_Plan-Library/                      → reusable house plans
📂 02_Finish-&-Selections-Catalog/       → finish options, vendor catalogs
📂 03_Spec-Standards/                    → Kessair build spec standards
📂 04_Subcontractor-Roster-&-COIs/   ⚠   → vetted subs + insurance certificates
```

## 🟦 K·HOM·70 · Sales & Marketing
```
📂 00_START-HERE
📂 01_Active-Listings/                   → current homes for sale
📂 02_Staging-&-Photography/             → media
📂 03_Buyer-Pipeline/                    → prospective buyers
📂 04_Realtor-&-Referral-Network/        → agent relationships
```

---
---

# 📦 KESSAIR DEVELOPMENT LLC  →  Commercial + Multifamily  *(one SPV per project)*

## 🟦 K·DEV·60 · Projects (Development)
```
📂 00_START-HERE
📂 SPV — Riverside-MF LLC/               → one project = one LLC = one folder
   ├── 📂 00_Entity-&-JV/            ⚠   → formation, JV/operating agreement
   ├── 📂 01_Entitlement-&-Zoning/       → entitlements, zoning approvals
   ├── 📂 02_Design-&-Civil-Eng/         → architectural + civil drawings
   ├── 📂 03_GC-&-Construction/          → GC contract, construction docs
   ├── 📂 04_Draws-&-Budget/             → budget, draws
   ├── 📂 05_Leasing-Sale/               → lease-up or sale
   └── 📂 06_Project-Reporting/          → status reports
       _NAMING → YYYY-MM-DD_<Project>_<JV|Entitlement|Civil|GC-Contract|Draw>_vNN
📂 SPV — Main-St-Retail LLC/             (same shape)
📂 _TEMPLATES/                           → blank Dev-Project folder
```
> 🚫 Investor cap tables / distributions do **NOT** go here — they live in K·DEV·65.

## 🟦 K·DEV·65 · Capital & Investors  🔒⚠ *Owner + CFO + IR · keep-forever*
```
📂 00_START-HERE
📂 01_Capital-Raises-PPMs-Subscriptions/ → PPMs, subscription docs
📂 02_Investor-Cap-Tables/               → per-project cap tables
📂 03_Capital-Calls-&-Distributions/     → calls, distribution notices
📂 04_Investor-Reporting-&-K1s/          → investor reports, K-1s
   _NAMING → <YYYY|YYYY-Qn>_<Project-or-Fund>_<PPM|Cap-Table|Capital-Call|Distribution|K1>
```

## 🟦 K·DEV·70 · Predevelopment Pipeline
```
📂 00_START-HERE
📂 01_Land-&-Site-Search/                → sites under evaluation
📂 02_Feasibility-&-Underwriting/        → feasibility models
📂 03_Zoning-&-Municipal/                → zoning research, municipal relationships
📂 04_Broker-&-Seller-Relationships/     → broker/seller contacts
```

---
---

# 📦 KESSAIR EQUITY LLC (+ Fund I LP · GP LLC)  →  Private Equity  🧱 *own Organizational Unit*

## 🟦 K·EQ·60 · Deal Pipeline  🔒⚠ *MNPI — access limited per deal*
```
📂 00_START-HERE
📂 Deal — {Target-Co}/                   → one folder per target (assigned deal team ONLY)
   ├── 📂 00_Sourcing-&-NDA/             → sourcing notes, signed NDA
   ├── 📂 01_CIM-&-Financials/           → confidential info memo, financials
   ├── 📂 02_Due-Diligence/              → DD workstreams
   ├── 📂 03_LOI-&-Valuation/            → IOI, LOI, valuation model
   └── 📂 04_Purchase-Agreement/         → definitive docs
       _NAMING → YYYY-MM-DD_<Target>_<NDA|CIM|DD|LOI|Purchase-Agreement>_vNN
📂 _Sourcing-Funnel/                     → top-of-funnel targets
📂 _TEMPLATES/                           → blank PE-Deal folder
```

## 🟦 K·EQ·65 · Portfolio Companies
```
📂 00_START-HERE
📂 {PortCo} LLC/                         → one acquired company = one folder
   ├── 📂 00_Ownership-&-Board/          → ownership docs, board materials
   ├── 📂 01_Monitoring-&-KPIs/          → KPI reports, monitoring
   ├── 📂 02_Value-Creation/             → value-creation plan, initiatives
   └── 📂 03_Exit-Planning/              → exit strategy, exit memo
       _NAMING → <YYYY-Qn>_<PortCo>_<Board-Deck|KPI-Report|Value-Creation-Plan|Exit-Memo>
📂 _TEMPLATES/                           → blank Portfolio-Company folder
```

## 🟦 K·EQ·70 · Fund & LP Administration  🔒⚠ *Owner + Fund Controller + IR · keep-forever*
```
📂 00_START-HERE
📂 01_Fund-Formation/                    → fund formation docs (Fund I LP, GP LLC)
📂 02_LP-Agreements-&-Subscriptions/     → LPAs, subscription docs
📂 03_Capital-Calls-&-Distributions/     → calls, distributions
📂 04_LP-Reporting-&-K1s/                → LP reports, K-1s
   _NAMING → <YYYY-Qn>_<Fund>_<Fund-Formation|LPA|Capital-Call|Distribution|K1>_<LP-or-Desc>
```

---
---

## Recap — 6 LLCs, 18 drives

| LLC | Drives | Per-asset SPV folders |
|---|---|---|
| Kessair Holdings LLC | CORP·00 / 10 / 20 | — |
| Kessair Acquisitions LLC | ACQ·30 / 40 / 50 | none (holds no property) |
| Kessair Properties LLC | PROP·60 / 65 / 70 | one per rental |
| Kessair Homes LLC | HOM·60 / 65 / 70 | one per home |
| Kessair Development LLC | DEV·60 / 65 / 70 | one per project |
| Kessair Equity LLC (+Fund/GP) | EQ·60 / 65 / 70 | one per portfolio company |

**Every drive** starts with a `00_START-HERE` doc. **Every unit-spawning drive** has a `_TEMPLATES/` folder. **Every file-heavy folder** keeps a `_NAMING` card. **Nothing is nested more than 4 levels deep.**

---

*End of Full Folder Structure v1.0.*

# Kessair — Naming-Convention Cards

**Drop-in cards to keep at the top of each file-generating folder.**

*Version 1.0 — 2026-07-01 · Pairs with: Kessair_Drive_Organization_Standard.md*

---

## How to use these

1. Copy the card for a folder below.
2. Paste it into a file at the **top of that folder**, named `_NAMING` (a Google Doc or `_NAMING.md`). The leading `_` pins it to the top so it's the first thing anyone sees.
3. Only folders that **generate lots of files** need a card — not every small folder. The index below lists the ones that do.

**The universal pattern (all cards inherit this):**
```
YYYY-MM-DD_<Scope>_<DocType>_<Short-Description>_vNN.ext
```
*hyphens-within-a-field · underscores_between-fields · no spaces · drop `vNN` when final*

---

## Deployment index — where each card goes

| Card | Drive → Folder |
|---|---|
| 1. Deal | `K·ACQ·40 / 01_Pipeline / {Deal}` |
| 2. Campaign | `K·ACQ·30 / 02_Campaigns / {Campaign}` |
| 3. Records Vault (auto) | `K·ACQ·30 / 01_First-to-Market-Records / 00_Inbox / daily` |
| 4. Compliance | `K·ACQ·07_Compliance` |
| 5. Buyer Lists | `K·ACQ·30 / 05_Buyer-Lists` |
| 6. Entity & Cap Table | `K·CORP·00 / 01_Entity-&-Cap-Table` |
| 7. Consolidated Finance | `K·CORP·00 / 02_Consolidated-Finance` |
| 8. People & HR | `K·CORP·10 / 01_People-&-HR` |
| 9. SOP | `K·CORP·20 / 02_SOP-Master-Library` + each OpCo SOP folder |
| 10. Property SPV | `K·PROP·60 / SPV — {property}` |
| 11. Leases & Property Mgmt | `K·PROP·65 / {property}` |
| 12. Asset Finance | `K·PROP·70` |
| 13. Home Build | `K·HOM·60 / SPV — {build}` |
| 14. Dev Project | `K·DEV·60 / SPV — {project}` |
| 15. Capital & Investors | `K·DEV·65` |
| 16. PE Deal Pipeline | `K·EQ·60 / {Target}` |
| 17. Portfolio Company | `K·EQ·65 / {PortCo}` |
| 18. Fund & LP Admin | `K·EQ·70` |

---

## ⬛ MASTER (blank) — copy this for any new folder type

```
📛 NAMING CONVENTION — <Folder Name>
Keep this card at the top of this folder.

PATTERN:   YYYY-MM-DD_<Scope>_<DocType>_<Short-Description>_vNN.ext
SCOPE:     <what every file in this folder is about, e.g. the address / entity>
DOCTYPES:  <the short list of allowed types for this folder>

EXAMPLES:
   <example 1>
   <example 2>

RULES:
   • hyphens-within, underscores_between, no spaces
   • one file, one home — need it elsewhere? use a shortcut, not a copy
   • superseded drafts → 90_Archive/  (never "Final-v2-REAL")
```

---

## 1. 📛 Deal Folder — `K·ACQ·40 / 01_Pipeline / {Deal}`
```
PATTERN:  YYYY-MM-DD_<Address>_<DocType>_<Desc>_vNN.ext
SCOPE:    the deal address (e.g. 123-Main-St)
DOCTYPES: Seller-Info · Call-Notes · Skip-Trace · Comp · ARV · Rehab-SOW · Bid ·
          Analysis · Financing · LOI · Contract · Amendment · Photo · Inspection ·
          Buyer-Blast · Assignment · POF · Title · HUD · Deed

EXAMPLES:
   2026-07-01_123-Main-St_Contract_Purchase-Agreement_v02.pdf
   2026-07-01_123-Main-St_Comp_Two-Bucket-ARV.xlsx
   2026-07-01_123-Main-St_HUD_Settlement-Statement.pdf
RULES: keep the address in the name even inside the deal folder — so files stay
       self-identifying if shared or moved.
```

## 2. 📛 Campaign Folder — `K·ACQ·30 / 02_Campaigns / {Campaign}`
```
PATTERN:  YYYY-MM_<Channel-Niche>_<DocType>_<Desc>.ext
SCOPE:    month + channel + niche (e.g. Probate-SMS)
DOCTYPES: List · Script · Creative · Results · Opt-Outs

EXAMPLES:
   2026-07_Probate-SMS_List_Uploaded.csv
   2026-07_Probate-SMS_Results_Response-Metrics.csv
RULES: copy any Opt-Outs to K·ACQ·07_Compliance (keep-forever).
```

## 3. 📛 Records Vault (AUTOMATED) — `K·ACQ·30 / 01_First-to-Market-Records / 00_Inbox / daily`
```
PATTERN:  datasift_{county}_{type}_{YYYY-MM-DD}_{HHMMSS}.csv   ← written by SiftStack, DO NOT rename
SCOPE:    county + notice type — RAW RECORDS (scraped data, not leads)
EXAMPLES:
   datasift_bell_probate_2026-07-02_153448.csv
   datasift_travis_foreclosure_2026-07-02_153448.csv
RULES: machine-managed. Do not hand-rename or reorganize — the pipeline and
       DataSift upload depend on these exact names. Auto-foldered into
       daily/Year/MM-Month/County/type.
```

## 4. 📛 Compliance — `K·ACQ·07_Compliance`  ⚠ keep-forever
```
PATTERN:  YYYY-MM-DD_<Channel>_<DocType>_<Desc>.ext
SCOPE:    channel (SMS / Call / Mail)
DOCTYPES: Opt-Out · DNC-Scrub · Litigator-Check

EXAMPLES:
   2026-07-01_SMS_Opt-Out-Log.csv
   2026-07-01_Call_Litigator-Check_Scrub-Results.csv
RULES: NEVER delete or edit. Read-only legal record.
```

## 5. 📛 Buyer Lists — `K·ACQ·30 / 05_Buyer-Lists`
```
PATTERN:  YYYY-MM_<Market>_<DocType>_<Desc>.ext
SCOPE:    market / source (e.g. Bell-County)
DOCTYPES: Buyer-List · Buyer-Research

EXAMPLES:
   2026-07_Bell-County_Buyer-List_Cash-Buyers.csv
   2026-07_Travis_Buyer-Research_LLC-Ownership.xlsx
```

## 6. 📛 Entity & Cap Table — `K·CORP·00 / 01_Entity-&-Cap-Table`  ⚠ keep-forever
```
PATTERN:  YYYY-MM-DD_<LLC-Name>_<DocType>_<Desc>_vNN.ext
SCOPE:    the exact LLC (e.g. Kessair-Homes-LLC)
DOCTYPES: Formation · EIN · Operating-Agreement · Cap-Table · Registered-Agent · Amendment

EXAMPLES:
   2026-07-01_Kessair-Homes-LLC_Operating-Agreement.pdf
   2026-07-01_Kessair-Holdings-LLC_Cap-Table_v03.xlsx
RULES: one sub-folder per LLC. Never delete — permanent record.
```

## 7. 📛 Consolidated Finance — `K·CORP·00 / 02_Consolidated-Finance`
```
PATTERN:  <YYYY | YYYY-Qn>_<Entity>_<DocType>_<Desc>.ext
SCOPE:    entity + period (group or a specific company)
DOCTYPES: PL · Balance-Sheet · Tax-Return · Audit · Inter-Company

EXAMPLES:
   2026-Q2_Kessair-Group_PL_Consolidated.xlsx
   2025_Kessair-Holdings_Tax-Return_Form-1065.pdf
RULES: period comes first (year or quarter) so books sort chronologically.
```

## 8. 📛 People & HR — `K·CORP·10 / 01_People-&-HR`  ⚠ sensitive
```
PATTERN:  YYYY-MM-DD_<Last-First>_<DocType>_<Desc>.ext
SCOPE:    the person (Last-First)
DOCTYPES: Contract · W9 · 1099 · Offer · Review · Onboarding

EXAMPLES:
   2026-07-01_Doe-Jane_Contract_VA-Agreement.pdf
   2026-07-01_Doe-Jane_Review_Q2.pdf
RULES: one sub-folder per person. Restricted to Owners + COO + Bookkeeper.
```

## 9. 📛 SOP — `K·CORP·20 / 02_SOP-Master-Library` + each OpCo SOP folder
```
PATTERN:  SOP_<Company>_<Task>_vNN            (living docs — version, no date needed)
SCOPE:    company + task
COMPANY:  SHARED (cross-company) · ACQ · PROP · HOM · DEV · EQ

EXAMPLES:
   SOP_ACQ_Pull-Travis-Foreclosure-List_v02
   SOP_SHARED_Employee-Onboarding_v01
RULES: cross-company SOP → CORP·20. Company-specific SOP → that OpCo's Ops drive.
       One SOP, one home — never both (see Blueprint §12).
```

## 10. 📛 Property SPV — `K·PROP·60 / SPV — {property}`  ⚠ entity docs keep-forever
```
PATTERN:  YYYY-MM-DD_<Property>_<DocType>_<Desc>.ext
SCOPE:    property short name (e.g. 456-Oak-Ave)
DOCTYPES: Entity · Contract · Deed · Title · HUD · Loan · Insurance · Property-Tax

EXAMPLES:
   2026-07-01_456-Oak-Ave_Deed_Warranty.pdf
   2026-07-01_456-Oak-Ave_Loan_DSCR-Note.pdf
RULES: use the SAME property name here, in PROP·65, and PROP·70 (mirroring rule).
```

## 11. 📛 Leases & Property Mgmt — `K·PROP·65 / {property}`  ⚠ tenant PII
```
PATTERN:  YYYY-MM-DD_<Property>_<DocType>_<Unit-or-Tenant>.ext
SCOPE:    property (+ unit # for multifamily)
DOCTYPES: Lease · Application · Screening · Work-Order · Notice · COI

EXAMPLES:
   2026-07-01_456-Oak-Ave_Lease_Unit-2-Smith.pdf
   2026-07-01_456-Oak-Ave_Work-Order_HVAC-Repair.pdf
RULES: tenant PII — leasing/maintenance are Contributors only. Do not export.
```

## 12. 📛 Asset Finance — `K·PROP·70`
```
PATTERN:  <YYYY-MM>_<Property-or-Portfolio>_<DocType>_<Desc>.ext
SCOPE:    property or "Portfolio" + period
DOCTYPES: Rent-Roll · T12 · Refi · Distribution · Reserve

EXAMPLES:
   2026-06_456-Oak-Ave_T12_Trailing-Twelve.xlsx
   2026-06_Portfolio_Rent-Roll_All-Units.xlsx
```

## 13. 📛 Home Build — `K·HOM·60 / SPV — {build}`
```
PATTERN:  YYYY-MM-DD_<Build-Address>_<DocType>_<Desc>.ext
SCOPE:    build address (e.g. 45-Vista-Ridge)
DOCTYPES: Entity · Lot-Contract · Loan · Permit · Plan · Budget · Draw ·
          Selection · Inspection · Warranty · MLS

EXAMPLES:
   2026-07-01_45-Vista-Ridge_Draw_Draw-03.pdf
   2026-07-01_45-Vista-Ridge_Selection_Kitchen-Finishes.pdf
RULES: number draws and phases (Draw-01, Draw-02…) so they sort in order.
```

## 14. 📛 Dev Project — `K·DEV·60 / SPV — {project}`
```
PATTERN:  YYYY-MM-DD_<Project>_<DocType>_<Desc>_vNN.ext
SCOPE:    project (e.g. Riverside-MF)
DOCTYPES: Entity · JV · Entitlement · Zoning · Civil · GC-Contract · Draw · Lease · Report

EXAMPLES:
   2026-07-01_Riverside-MF_JV_Operating-Agreement_v04.pdf
   2026-07-01_Riverside-MF_Entitlement_Site-Plan-Approval.pdf
RULES: investor docs do NOT go here — they belong in K·DEV·65 (walled).
```

## 15. 📛 Capital & Investors — `K·DEV·65`  🔒⚠ restricted · keep-forever
```
PATTERN:  <YYYY | YYYY-Qn>_<Project-or-Fund>_<DocType>_<Desc>.ext
SCOPE:    project or fund + period
DOCTYPES: PPM · Subscription · Cap-Table · Capital-Call · Distribution · K1 · Investor-Report

EXAMPLES:
   2026-Q2_Riverside-MF_Distribution_Q2-Notice.pdf
   2025_Riverside-MF_K1_Investor-K1.pdf
RULES: investor-facing legal record — restricted to Owner + CFO + IR. Never delete.
```

## 16. 📛 PE Deal Pipeline — `K·EQ·60 / {Target}`  🔒⚠ MNPI · per-deal access
```
PATTERN:  YYYY-MM-DD_<Target>_<DocType>_<Desc>_vNN.ext
SCOPE:    target company (code-name if confidential)
DOCTYPES: NDA · CIM · Financials · DD · IOI · LOI · Valuation · Purchase-Agreement

EXAMPLES:
   2026-07-01_TargetCo_CIM_Confidential-Info-Memo.pdf
   2026-07-01_TargetCo_LOI_Non-Binding_v02.pdf
RULES: confidential deal information. Access limited to the assigned deal team only.
```

## 17. 📛 Portfolio Company — `K·EQ·65 / {PortCo}`
```
PATTERN:  <YYYY-Qn>_<PortCo>_<DocType>_<Desc>.ext
SCOPE:    portfolio company + period
DOCTYPES: Board-Deck · KPI-Report · Value-Creation-Plan · Financials · Exit-Memo

EXAMPLES:
   2026-Q2_AcmeCo_KPI-Report_Quarterly.xlsx
   2026-Q2_AcmeCo_Board-Deck_Q2-Meeting.pdf
```

## 18. 📛 Fund & LP Admin — `K·EQ·70`  🔒⚠ investor-facing · keep-forever
```
PATTERN:  <YYYY-Qn>_<Fund>_<DocType>_<LP-or-Desc>.ext
SCOPE:    fund + LP/period
DOCTYPES: Fund-Formation · LPA · Subscription · Capital-Call · Distribution · K1 · LP-Report

EXAMPLES:
   2026-Q2_Fund-I_Capital-Call_Call-02.pdf
   2025_Fund-I_K1_LP-Smith.pdf
RULES: restricted to Owner + Fund Controller + IR. Permanent record.
```

---

*End of Naming-Convention Cards v1.0.*

# Tax payment history → curative-title signal (Travis / Bell / Williamson)

**Date:** 2026-10-27. **Question:** can we look up WHO has been paying the taxes on our
tax-delinquent records, flag payments made by someone other than the owner of record, and
turn that into a curative-title prospecting skill?

**Short answer:** yes for Travis and Williamson, no for Bell (online). Both working
counties expose a per-payment receipt that names the payer, reachable with plain HTTP
(no login, no captcha, no browser). Bell's new GSA portal shows amounts paid per year but
never the payer. Everything below was verified live on 2026-10-27 with `curl` from the
operator box; the probe files are in the session scratchpad `probe/` folder.

---

## 1. What each county exposes

| County | Portal | Payment history | Payer name | Deed chain on same site | Access |
|---|---|---|---|---|---|
| **Travis** | `travis.go2gov.net` (Tax Office cart) | Yes: receipt list per account (receipt #, tax year, date, amount) | **On the receipt PDF**, `PAYMENT TENDER → Paid By`. Populated for card (`CRC`) and counter (`MPW`) payments; **blank** for lockbox mail (`RPD`) and transfers (`TRN`) | No (use TCAD Prodigy API) | Plain GET, cookie jar, no login |
| **Williamson** | `tax.wilcotx.gov/Property-Detail` | Yes: every receipt since ~2000 (tax year, txn date, effective date, amount, receipt #) | **On the receipt PDF**, `Payor:` line. Also a hidden `payorPartyID` per row in the HTML, so non-owner rows can be pre-filtered BEFORE downloading a PDF | Yes (DEED HISTORY table: date, seller, buyer, instr #) | Plain GET + one POST per receipt |
| **Bell** | `belltx-search.gsacorp.io` (GSA, replaced esearch.bellcad.org which is now 404) | Partial: per tax year `Original / Penalty / Interest / Paid / Balance` only | **No.** No receipts, no payer anywhere on `/tax/{id}`; the "Tax Bill" PDF is the statement | Yes (`/parcel/{id}` Property Deed History incl. AFFIDAVIT OF HEIRSHIP rows) | Plain GET, CSRF token on POSTs |

### 1a. Travis recipe (verified)

```
POST https://travis.go2gov.net/cart/responsive/quickSearch.do   criteria.heuristicSearch=<14-digit account>
GET  https://travis.go2gov.net/showPropertyInfo.do?account=<acct>          owner, 2025 taxes due, links
GET  https://travis.go2gov.net/showPaymentReceipts.do?account=<acct>       table: Receipt | Tax Year | Payment Date | Amount
GET  https://travis.go2gov.net/exportReceipt?receipt=<n>&account=<acct>    PDF, ~25 KB
GET  https://travis.go2gov.net/export?type=cur|org&account=<acct>[&year=]  current / original bill PDF
```

The 2026-09-24 note that the detail link "404s without a browser session" was wrong for
these paths: a `curl` cookie jar is enough. The account search on
`tax-office.traviscountytx.gov/properties/taxes/account-search` is just an iframe over
go2gov.

Receipt PDF fields (pypdfium2 text extraction works): `Owner Name`, `Mailing Address`,
`TCAD PID`, `Receipt Number`, `Process Type` / `Received By`, `Effective Date`, per-entity
amounts, then `PAYMENT TENDER / Tender Type / Tender Reference / Paid By`.

Sample of 17 receipts across 7 delinquent accounts:

| Process type | Count | Paid By |
|---|---|---|
| `RPD` (lockbox / mailed check) | 8 | blank (only reference numbers) |
| `CRC` (online card) | 6 | **name** (`CYNTHIA CRUMLEY`, once `OWNER`) |
| `MPW` (counter) | 1 | `CASH` + **name** |
| `TRN` (transfer) | 3 | numeric reference only |

So Travis names the payer on roughly half of payments, biased toward card/counter payers,
which is exactly the population that pays someone else's taxes (heirs, occupants, buyers
under unrecorded contracts pay online or in person; owners with escrow pay via lockbox).

**Live example found in the sample:** account `01028702260000`, 10419 Crumley Ranch Rd
78738. Owner of record `CRUMLEY ESTIN`; roll mailing address is `% CYNTHIA CRUMLEY YEO`
(care-of); every 2017-2019 payment was made by card with Paid By `CYNTHIA CRUMLEY`;
payments stop after 10/2019 and the account has been delinquent since 2022 ($4,503 due).
That is the textbook shape: an owner who is likely deceased or incapacitated, a family
member who carried the taxes for a while and then stopped, no probate. One query found it.

### 1b. Williamson recipe (verified)

```
GET  https://tax.wilcotx.gov/Property-Detail?PropertyQuickRefID=R######      ~450 KB HTML
       → data-ownerName, DEED HISTORY table, PAYMENT HISTORY rows, each row carries
         OpenReceiptPDF('R…', transactionID, date, amount, paidForPartyID, payorPartyID, partyID)
POST https://tax.wilcotx.gov/Proxy-WilliamsonTax/tax/Receipt
       QuickRefID, TransactionID, PaidForPartyID, PayorPartyID, PartyID   → "Receipt-R…-<txn>.pdf"
GET  https://tax.wilcotx.gov/Proxy-WilliamsonTax/documents/pdf/<filename>/   PDF, ~11 KB
```

Party-ID semantics (from the three parcels probed):

- `partyID` = the CURRENT owner party on the account.
- `paidForPartyID` = the owner party the payment was applied for at the time (blank on some
  batch rows).
- `payorPartyID` = who paid. When it differs from `partyID` the PDF `Payor:` line names
  them. Examples: `@UNIVERSITY FEDERAL CREDIT UNION` paying for CASIANO (the `@` prefix
  marks a mortgage-company / escrow payor); `LOGAN, HAROLD H` (the father, prior owner per
  the deed table) paying 2007-2017 on a parcel now owned by `LOGAN HAROLD H JR & LINDA`.
- The same human can have several party IDs (474055 and 884830 both print as
  `LOGAN, HAROLD H`), so the ID is a pre-filter, the PDF name is the truth.

Because the party IDs are in the HTML, Williamson needs ONE request per parcel to decide
whether any payment came from a non-owner, and PDFs only for those rows. The deed table on
the same page lets the classifier tell "prior owner paid before the deed" from "stranger
paid after the deed" without a second source.

### 1c. Bell (no online payer)

`/tax/0000100046` lists every tax year back to 2005 with Paid / Balance but no receipts.
Options, in order:

1. **Open-records request to the Tax Appraisal District of Bell County (collections)** for
   a receipt export (account, receipt date, amount, payor name) on a list of account
   numbers; the Travis and Williamson receipts prove the field exists in every TX
   collections system (Tyler / True Automation / GSA all store a payor party). Reuse the
   PIA template from the code-enforcement project.
2. **Counter request** for duplicate receipts on a short list (Belton / Killeen / Temple
   offices print them on request; customer service already told a caller "we do not
   provide anything unless it's requested").
3. Meanwhile, use the GSA parcel page's **Property Deed History** as the Bell-only
   signal: AFFIDAVIT OF HEIRSHIP, deeds from an `ESTATE OF`, `ET AL` grantors and gaps are
   visible there today (the probe parcel shows a 2011 affidavit of heirship recorded in
   2025, then a 2025 warranty deed between the heirs).

---

## 2. What a non-owner payment means (classification)

Texas law lets the collector take payment from anyone and the payment by itself gives the
payer no interest (Tax Code ch. 31), so the signal is behavioral, not legal. The classes
we can separate with the data above:

| Payer class | Tell | Curative value |
|---|---|---|
| **Mortgage / escrow** | Williamson `@` prefix; lender / servicer names; Travis `RPD` batches with lender reference | None, drop |
| **Prior owner before the deed** | Payer matches a SELLER in the deed table and the payment date precedes that deed | None, normal |
| **Owner, different party record** | Same person, new party ID (name match) | None |
| **Same-surname relative** (child, spouse, sibling) paying on an individual owner, especially with a `%` care-of mailing or an `ESTATE OF` / `ET AL` owner | Heir or caretaker in possession; parent likely deceased or incapacitated; **no probate** | **High**: the affidavit-of-heirship + warranty-deed buyout model, and the payer is the person to call |
| **Different-surname individual** paying repeatedly over years | Buyer under an unrecorded contract-for-deed, adverse-possession claimant (5-year AP under CPRC §16.025 requires paying taxes), partner, tenant | **High**: broken chain of title, the payer thinks they own it |
| **Investor / LLC** paying someone else's taxes | Tax-lien transfer (Tax Code §32.06 property tax loans) or a wholesaler protecting a contract | Medium: competition marker or a transfer-of-lien to buy out |
| **Payments stopped** after years of a non-owner paying, now delinquent | The payer died, moved, or gave up | **Highest**: nobody is minding the property |

The DataSift field that already exists for this is `Notes` + tags; a custom field
`Tax Payer (last)` / `Tax Payer Class` would need the schema setup script before upload.

---

## 3. Skill plan: `curative-tax-payer-audit`

**Input:** a DataSift export (Tax Delinquent list, filtered to 2+ years) or the raw roll
(`TaxDelqOpenData.csv`, Williamson delinquent XLSX). Keyed by Travis 14-digit account /
Williamson `R` number, both of which we already carry as `APN`.

**Scope today:** Travis roll has 2,440 accounts delinquent 2+ years (1,465 with a mailing
ZIP different from the situs ZIP). Williamson 2+ year real count is ~500 (the October
current-year inflation bug makes the XLSX say 1,255). Bell is deferred to the records
request.

**Steps**

1. **Fetch** (module `src/tax_payer_history.py`, CLI, operator-run, not in the Actor):
   Travis: receipts page per account, then every receipt PDF not already cached.
   Williamson: Property-Detail page per parcel, then PDFs only for rows with
   `payorPartyID != partyID`. Cache by receipt number under `data/tax_payer_cache/`; a
   re-run only pulls new receipts. ~1 request/second, ~5 h for the full Travis set the
   first time, minutes after that. Both sites are plain HTTP; keep a UA string and a
   cookie jar, no proxy needed.
2. **Parse**: pypdfium2 text → `{account, receipt, date, amount, process_type, paid_by,
   owner_name_on_receipt, mailing}`. Williamson: `Payor:` line, strip the `@`, record it
   as `payor_is_lender`.
3. **Classify** each payer against the owner of record and the deed chain (TCAD Prodigy
   deeds for Travis, the on-page deed table for Williamson) using the existing NAMELF
   parser and `_is_entity_name`; never flip orientation blindly. Emit one row per account:
   last payer, payer class, years paid by non-owner, date of last payment, current
   delinquency, care-of mailing flag, owner-entity flag.
4. **Score**: `stopped_non_owner_payer` > `same_surname_relative` > `different_surname_
   repeat` > `investor` > `lender` / `prior_owner` (drop). Combine with the roll fields we
   already have (years delinquent, Cause # / judgment, mail-returned, PO box) so the output
   matches the Bobbitt targeting parameters.
5. **Output**: the 64-column DataSift upload CSV for the existing Tax Delinquent list,
   tagged `curative_payer_<class>` + `upload_YYYY-MM-DD`, Notes block listing every
   non-owner payment (date, amount, payer, how paid) and the deed chain. Plus a reviewer
   CSV sorted by score. No skip trace on upload (owner rule). The payer's name goes to
   Notes and to the deep-prospecting queue, not into the owner columns.
6. **Skill wrapper** (`.claude/skills/curative-tax-payer-audit/SKILL.md`): estimate →
   run → review the top of the scored CSV → build the upload file. No vendor spend, so no
   approval gate is needed; the only cost is runtime.

**Open items before building**

- Confirm the Travis `Process Type` code meanings with one more sample (`MPW`, `TRN`,
  `CRC`, `RPD`) so the "blank Paid By" rows are labelled by channel, not treated as unknown.
- Decide whether a non-owner payer who is paying CURRENTLY (not delinquent) is in scope.
  They are not on the delinquent roll, so reaching them means sweeping the full
  Williamson roll or Travis `TaxCurOpenData` accounts with a care-of mailing. Suggested:
  phase 2.
- Bell records request wording (payor field by account list).
- DataSift schema: add `Tax Payer Class` select + `Last Tax Payer` text in the TX County
  Data group via `datasift_schema_setup.py --commit`, or keep everything in Notes/tags.

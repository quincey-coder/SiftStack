# Owner reconcile — the upload-batch audit, automated (2026-11-11)

Every manual upload batch from 2026-09-18 to 2026-11-11 needed the same
hand-audit before the Apify run CSVs could go into DataSift. On the 11-11 batch
(runs 11-06..11-11) that audit patched 15 owners and held 23 of 114 new
records. This change moves that audit into the pipeline so the Actor's CSVs come
out the way the patches left them.

Replay of the 11-11 batch through the new step: **91 kept / 23 held — the same
records the manual audit kept and held**, and all 15 owner patches came out
identical (`output/pending_upload_2026-11-11/scripts/replay_reconcile.py`).

## What runs where

| Defect (seen in batches) | Fix | File |
|---|---|---|
| Substitute trustee / notice party as owner (Kipp Marshall, Brandon Wolf, Christopher Baxter, ...) | Foreclosures + tax sales take the **owner of record** when the filed name doesn't match it; the filed name stays in Notes ("Name on the filing: ...") | `src/owner_reconcile.py` (Step 5b) |
| Ownerless rows / no parcel id | Travis address → parcel via the TCAD parcel fabric (`travis_gis.geo_id_by_situs`) → owner + mailing. Unresolvable addresses are marked `owner_check="unresolved"` and **sealed**, so they stop re-emitting daily | `travis_gis.py`, `owner_reconcile.py`, `main._is_complete_for_seen` |
| Lien / lis pendens on someone else's house (spouse, parent, near-miss name) | Debtor must BE an owner of record (surname + first name; middle initials agree). Otherwise → REVIEW CSV | `owner_reconcile.person_matches` |
| Hospital as lien debtor | → REVIEW CSV | `owner_reconcile` |
| Reversed / particle names ("Schaaf Diana", "Delden Van") | Re-oriented from the owner of record when the debtor matched | `owner_reconcile` |
| City glued onto street ("4611 Outpost Trace Lago Vista") | Stripped | `owner_reconcile.strip_glued_city` |
| No house number ("Norwood Ln") | → REVIEW CSV | `owner_reconcile` |
| Bell mailing glued ("3463 Sweet Gum Drbelton, Tx 76513") and mailing city = property city | `Ownr_Addr` split into street/city/state/zip; parcel lookups never borrow the situs city | `bell_tax_cache.split_owner_address`, `cad_lookup._parcel_lookup_one` |
| Travis mailing street = a co-owner NAME ("Natalie N Van Delden") | First street-like line of Address 1/2/3 | `travis_tax_cache._pick_mail_line` |
| Fake people on LLC/trust rows (First="3423", "Gugler Clark", "Jarolik") | Entity name-parse rejects digits and re-orients NAMELF trusts; formatter only promotes a 2-part name to First/Last | `entity_researcher`, `datasift_formatter._is_real_person_name` |

## The REVIEW CSV

Held records never reach the per-type upload CSVs. They are written to
`datasift_REVIEW_<ts>.csv` (DataSift layout + a leading **Review Reason**
column), pushed to Drive under `<year>/<month>/review/`, to the KVS key
`datasift_review.csv`, and listed in the Slack "CSVs created" block. A record
the operator clears can be uploaded as-is after deleting the first column.
They are sealed into `seen_notice_ids` so the same record does not reappear in
the file every day. The run-health report carries a warn-level
`owner_reconcile` line with the count.

## Untouched by design

Probate (owner = PR), eviction, divorce, condemned (BSC party) and
tax-delinquent (owner = the roll) keep their owner. Only the no-house-number
rule and the glued-city fix apply to them.

## Escape hatch

`SIFT_SKIP_OWNER_RECONCILE=1` bypasses the step. `--skip-tax` / actor
`enrich_cad_tax=false` also skips it (no county lookups at all).

Tests: `tests/test_owner_reconcile.py` (offline, real cases from the batches).

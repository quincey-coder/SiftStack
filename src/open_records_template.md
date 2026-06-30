# Texas Public Information Act — Code Enforcement Records Request Template

Rendered per-jurisdiction by the open-records pipeline. `{placeholders}` are filled
from `open_records_registry.json` + run parameters. Keep the language plain and the
scope narrow to minimize fees (Texas PIA lets agencies charge for staff time/copies
above ~50 pages or significant labor).

---

**Subject:** Texas Public Information Act Request — Code Enforcement Records ({city})

To the Public Information Officer / Records Custodian, City of {city}, Texas:

Pursuant to the Texas Public Information Act (Texas Government Code, Chapter 552), I
respectfully request copies of the following public records:

> All code enforcement / code compliance cases **opened or updated between
> {start_date} and {end_date}**. For each case, I request the fields your system
> can export, including: property address, parcel or account number, date opened,
> case/violation type or description, current case status, and the name of the
> property owner or responsible party where contained in the record.

**Format:** To minimize cost and effort, I prefer to receive these records
**electronically as a CSV, Excel, or delimited-text file** sent to this email
address. If the data already exists as an exportable report or dataset, that export
is acceptable as-is.

**Cost:** If you anticipate that responding will cost more than **${fee_cap}**,
please send an itemized written estimate before doing the work so I can narrow the
request if needed.

I am happy to clarify or narrow this request. Thank you for your time.

{requester_name}
{requester_email}
{requester_phone}

---

## Notes for operators
- **Cadence:** re-send on a fixed interval (default **monthly**); each request only
  covers records that exist when filed (TX PIA has no "standing future request").
- **Portal cities** (Killeen, Hutto, Copperas Cove, Harker Heights): paste the same
  body into the portal's request field instead of emailing.
- **Unverified-email cities** (Cedar Park, Salado, Granger, Weir, etc.): confirm the
  address by phone before the first send — never send a legally-operative PIA to a
  guessed inbox.
- **Low/no-yield** (tier 3, `has_code_enforcement: false`, both counties): skip or
  send once to confirm there's nothing to get.

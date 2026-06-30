"""Dry-run generator for Bell/Williamson code-enforcement open-records requests.

Loads open_records_registry.json, renders the Texas PIA request text per
jurisdiction, and routes each to its channel (email / portal / needs-confirm).
Writes one file per jurisdiction to output/open_records/ and prints a routing
summary.

THIS SENDS NOTHING. It is a review step — eyeball the rendered requests and the
routing before any live send pipeline is wired up.

Usage:
    python scripts/build_open_records_requests.py                    # tier-1 code-enforcement cities, both counties
    python scripts/build_open_records_requests.py --county Bell      # one county
    python scripts/build_open_records_requests.py --all-tiers        # include tier 2/3
    python scripts/build_open_records_requests.py --start 2026-05-01 --end 2026-05-31 --fee-cap 25

Requester identity is read from env (or left as a visible placeholder):
    OPEN_RECORDS_REQUESTER_NAME, OPEN_RECORDS_REQUESTER_EMAIL, OPEN_RECORDS_REQUESTER_PHONE
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_PATH = ROOT / "src" / "open_records_registry.json"
OUT_DIR = ROOT / "output" / "open_records"

TEMPLATE = """\
Subject: Texas Public Information Act Request — Code Enforcement Records ({city})

To the Public Information Officer / Records Custodian, City of {city}, Texas:

Pursuant to the Texas Public Information Act (Texas Government Code, Chapter 552),
I respectfully request copies of the following public records:

All code enforcement / code compliance cases opened or updated between {start_date}
and {end_date}. For each case, I request the fields your system can export,
including: property address, parcel or account number, date opened, case/violation
type or description, current case status, and the name of the property owner or
responsible party where contained in the record.

Format: To minimize cost and effort, I prefer to receive these records
electronically as a CSV, Excel, or delimited-text file sent to this email address.
If the data already exists as an exportable report or dataset, that export is
acceptable as-is.

Cost: If you anticipate that responding will cost more than ${fee_cap}, please send
an itemized written estimate before doing the work so I can narrow the request if
needed.

I am happy to clarify or narrow this request. Thank you for your time.

{requester_name}
{requester_email}
{requester_phone}
"""


def _channel(j: dict) -> str:
    """Decide how this jurisdiction's request would be delivered."""
    methods = j.get("submission_method", [])
    primary = methods[0] if methods else "unknown"
    if primary in ("portal", "webform"):
        return "PORTAL"
    if primary == "email":
        return "EMAIL" if j.get("email_verified") else "EMAIL (CONFIRM ADDRESS FIRST)"
    if primary in ("form", "fax", "mail", "phone"):
        return f"{primary.upper()} (manual)"
    return "UNKNOWN (manual)"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--county", choices=["Bell", "Williamson"], help="Limit to one county")
    ap.add_argument("--all-tiers", action="store_true", help="Include tier 2 and 3 (default: tier 1 only)")
    ap.add_argument("--include-no-code", action="store_true",
                    help="Include jurisdictions with has_code_enforcement=false")
    ap.add_argument("--start", help="Records-opened start date YYYY-MM-DD (default: 30 days ago)")
    ap.add_argument("--end", help="Records-opened end date YYYY-MM-DD (default: today)")
    ap.add_argument("--fee-cap", default="25", help="Dollar threshold for an estimate (default: 25)")
    args = ap.parse_args()

    end = args.end or datetime.now().strftime("%Y-%m-%d")
    start = args.start or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")

    name = os.getenv("OPEN_RECORDS_REQUESTER_NAME", "[YOUR NAME]")
    email = os.getenv("OPEN_RECORDS_REQUESTER_EMAIL", "[your-email@example.com]")
    phone = os.getenv("OPEN_RECORDS_REQUESTER_PHONE", "[your phone]")

    registry = json.loads(REGISTRY_PATH.read_text())
    jurisdictions = registry["jurisdictions"]

    selected = []
    for j in jurisdictions:
        if args.county and j["county"] != args.county:
            continue
        if not args.all_tiers and j["tier"] != 1:
            continue
        if not args.include_no_code and not j.get("has_code_enforcement", True):
            continue
        selected.append(j)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    by_channel: dict[str, list[str]] = {}

    for j in selected:
        body = TEMPLATE.format(
            city=j["city"], start_date=start, end_date=end, fee_cap=args.fee_cap,
            requester_name=name, requester_email=email, requester_phone=phone,
        )
        channel = _channel(j)
        if "PORTAL" in channel:
            target = j.get("portal_url") or j.get("pia_email") or j.get("code_dept_phone")
        elif "EMAIL" in channel:
            target = j.get("pia_email") or j.get("code_dept_phone")
        else:
            target = j.get("pia_email") or j.get("portal_url") or j.get("code_dept_phone")
        target = target or "(no contact — call to obtain)"

        fname = j["city"].lower().replace(" ", "_").replace("'", "") + ".txt"
        (OUT_DIR / fname).write_text(
            f"# {j['city']}, {j['county']} County — channel: {channel}\n"
            f"# deliver to: {target}\n"
            f"# {j.get('notes', '')}\n\n{body}"
        )
        by_channel.setdefault(channel, []).append(f"{j['city']:<24} → {target}")

    print(f"\nRendered {len(selected)} request(s) for window {start} → {end} "
          f"(fee cap ${args.fee_cap}), written to {OUT_DIR.relative_to(ROOT)}/\n")
    for channel in sorted(by_channel):
        print(f"[{channel}]  ({len(by_channel[channel])})")
        for line in by_channel[channel]:
            print(f"    {line}")
        print()

    if name == "[YOUR NAME]":
        print("⚠ Requester identity not set — set OPEN_RECORDS_REQUESTER_NAME/EMAIL/PHONE "
              "env vars before any real send.\n")


if __name__ == "__main__":
    sys.exit(main())

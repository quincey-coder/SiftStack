"""Gate B — sold-record rehydration + "Sold" tagging round-trip.

Proves the two gaps closed in the tax-delinquent pipeline:

  1. Full detail: a parcel that drops off the roll is rehydrated from the
     prior run's snapshot (last_run_records) into a record with the owner +
     property address — not a bare APN.
  2. Sold flow: that rehydrated record is tagged exactly "Sold" with a BLANK
     Lists column, so an upload applies the tag to the matching DataSift record
     by address and the "Sold Property Cleanup" sequence fires.

Critical safety property: a parcel that drops off the roll but was NEVER in our
upload (filtered out by the amount/years/zip thresholds) must NOT become a Sold
row — otherwise we'd create a junk record in DataSift for a property that was
never there. Stored snapshots hold only the uploaded (post-filter) set, so the
intersection with dropped naturally excludes those.

Two simulated Apify processes (in-memory cache cleared between them) per county.

Run: PYTHONPATH=src python tests/test_texdel_sold_tagging.py
"""

import os
import sys
from pathlib import Path

# Force the Apify code path so we exercise the in-memory cache (not disk).
os.environ["APIFY_IS_AT_HOME"] = "1"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrapers import tax_delinquent_state as st  # noqa: E402
import datasift_formatter  # noqa: E402
from notice_parser import NoticeData  # noqa: E402

TODAY = "2026-06-20"

_failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}")
    if not cond:
        _failures.append(label)


def _uploaded(apn: str, owner: str, street: str, city: str, zip5: str) -> NoticeData:
    """A normal (post-filter) tax-delinquent record we'd upload to DataSift."""
    return NoticeData(
        notice_type="tax_delinquent",
        county="Bell",
        state="TX",
        date_added=TODAY,
        address=street,
        city=city,
        zip=zip5,
        owner_name=owner,
        parcel_id=apn,
    )


def simulate_run(
    county: str,
    key: str,
    fake_kvs: dict,
    source_apns: set,
    uploaded: list,
):
    """One full simulated Apify run. Returns (diff, sold_notices)."""
    st._APIFY_STATE_CACHE.clear()

    stored = fake_kvs.get(key) or st._empty_state()
    st.inject_apify_state(county, stored)

    state = st.load_state(county)
    prev_apns = set(state.get("last_run_apns") or [])
    prev_records = state.get("last_run_records") or {}
    current_records = st.snapshot_records(uploaded)
    diff = st.compute_diff(source_apns, prev_apns, prev_records)

    st.commit_run(
        county, state,
        current_apns=source_apns,
        raw_path=Path(f"apify-kvs://{county.lower()}_test.xlsx"),
        raw_sha="deadbeef",
        diff=diff,
        current_records=current_records,
    )

    sold_notices = [st.build_sold_notice(r, county, TODAY) for r in diff.dropped_records]

    fake_kvs[key] = st.snapshot_apify_state(county)
    return diff, sold_notices


def main() -> int:
    print("Gate B: tax-delinquent Sold-record rehydration + tagging round-trip")
    county = "Bell"
    key = "bell_texdel_state"
    fake_kvs: dict = {}

    # ── Run 1 ──
    # Uploaded (post-filter) parcels — these land in DataSift + the snapshot.
    up1 = [
        _uploaded("490001", "Jane Doe", "1 A St", "Belton", "76513"),
        _uploaded("490002", "Bob Smith", "2 B St", "Killeen", "76541"),
        _uploaded("490003", "Acme Holdings LLC", "3 C St", "Temple", "76501"),
    ]
    # Source set includes one extra parcel (490099) that was on the roll but
    # filtered OUT (e.g. under the $ threshold) — so it's NOT in `up1`.
    src1 = {"490001", "490002", "490003", "490099"}
    diff1, sold1 = simulate_run(county, key, fake_kvs, src1, up1)

    print("\n=== Run 1 (seed) ===")
    check("run1 is_first_run", diff1.is_first_run is True)
    check("run1 no sold rows yet", sold1 == [])
    check(
        "run1 snapshot stored only the 3 uploaded parcels",
        set(fake_kvs[key].get("last_run_records", {}).keys()) == {"490001", "490002", "490003"},
    )
    check(
        "run1 snapshot carries the address detail",
        fake_kvs[key]["last_run_records"]["490001"]["address"] == "1 A St",
    )

    # ── Run 2 ──
    # 490001 paid off (sold), 490099 also left the roll. 490002/490003 remain.
    up2 = [
        _uploaded("490002", "Bob Smith", "2 B St", "Killeen", "76541"),
        _uploaded("490003", "Acme Holdings LLC", "3 C St", "Temple", "76501"),
    ]
    src2 = {"490002", "490003"}
    diff2, sold2 = simulate_run(county, key, fake_kvs, src2, up2)

    print("\n=== Run 2 (diff + sold) ===")
    check("run2 not first run", diff2.is_first_run is False)
    check("run2 not guardrail tripped", diff2.guardrail_tripped is False)
    check("run2 DROPPED off roll == {490001, 490099}", set(diff2.dropped) == {"490001", "490099"})
    # GAP 1: dropped_records carries full detail, only for the uploaded subset.
    check("run2 exactly ONE sold row (490099 was never uploaded)", len(sold2) == 1)
    sold = sold2[0] if sold2 else None
    check("run2 sold row is parcel 490001", bool(sold) and sold.parcel_id == "490001")
    check("run2 sold row has the property address", bool(sold) and sold.address == "1 A St")
    check("run2 sold row has the owner name", bool(sold) and sold.owner_name == "Jane Doe")
    check("run2 sold row marked record_status='sold'", bool(sold) and sold.record_status == "sold")

    # GAP 2: formatter emits Tags=Sold with a blank Lists column.
    if sold:
        row = datasift_formatter._build_row(sold)
        tags = [t for t in row["Tags"].split(",")]
        check("run2 sold row Tags contains exactly-cased 'Sold'", "Sold" in tags)
        check("run2 sold row Lists is BLANK (cleanup seq removes lists)", row["Lists"] == "")
        check("run2 sold row keeps property address for DataSift match",
              row["Property Street Address"] == "1 A St" and row["Property ZIP Code"] == "76513")
        check("run2 sold row Notes explains the drop",
              "tax-delinquent roll" in row["Notes"])

    # ── Run 3: guardrail trips (empty file) — snapshot must be preserved ──
    diff3, sold3 = simulate_run(county, key, fake_kvs, set(), [])
    print("\n=== Run 3 (guardrail) ===")
    check("run3 guardrail tripped on empty file", diff3.guardrail_tripped is True)
    check("run3 produced no sold rows (no false 'sold' claims)", sold3 == [])
    check(
        "run3 preserved prior snapshot (490002/490003)",
        set(fake_kvs[key].get("last_run_records", {}).keys()) == {"490002", "490003"},
    )

    print()
    if _failures:
        print(f"RESULT: {len(_failures)} FAILED — {_failures}")
        return 1
    print("RESULT: ALL PASSED — Sold rehydration + tagging holds, no junk rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

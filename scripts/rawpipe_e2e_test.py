"""RAWPIPE end-to-end test — pure-Python upload, no browser.

Logs in to DataSift via the discovered /api/token/ endpoint, uploads a
tiny test CSV via the presigned-S3 + commit flow, and verifies the list
appears in the user's account.

This is the moment of truth for RAWPIPE: if this runs in <10 seconds and
the list shows up in DataSift, we can retire the Playwright wizard for
uploads.

Usage:
    PYTHONPATH=src python scripts/rawpipe_e2e_test.py
"""

import csv
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datasift_api_client import DataSiftAPIClient, DataSiftAPIError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)

# A clearly-labeled test list so it's easy to find + delete after.
TEST_LIST = f"RAWPIPE_e2e_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
TEST_TAG = "RAWPIPE_e2e"
TEST_CSV = ROOT / "output" / "datasift_capture" / f"e2e_{datetime.now().strftime('%Y%m%dT%H%M%S')}.csv"


def write_test_csv(path: Path) -> None:
    """Write a 5-row CSV using DataSift's exact built-in column names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "Property Street Address", "Property City", "Property State", "Property ZIP Code",
        "Owner First Name", "Owner Last Name",
    ]
    rows = [
        ["111 RAWPIPE Test Way", "Austin", "TX", "78701", "Alpha", "TestRecord"],
        ["222 RAWPIPE Test Way", "Austin", "TX", "78702", "Bravo", "TestRecord"],
        ["333 RAWPIPE Test Way", "Austin", "TX", "78703", "Charlie", "TestRecord"],
        ["444 RAWPIPE Test Way", "Austin", "TX", "78704", "Delta", "TestRecord"],
        ["555 RAWPIPE Test Way", "Austin", "TX", "78705", "Echo", "TestRecord"],
    ]
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(headers)
        w.writerows(rows)


def main() -> int:
    write_test_csv(TEST_CSV)
    print(f"[STEP 0] Wrote test CSV → {TEST_CSV}")

    started = time.monotonic()

    print(f"[STEP 1] Authenticating via /api/token/ (or cached refresh)…")
    try:
        client = DataSiftAPIClient.from_env()
    except (DataSiftAPIError, RuntimeError) as e:
        print(f"[FAIL] Login failed: {e}", file=sys.stderr)
        return 1

    print(f"[STEP 2] GET /api/internal/user/ to confirm session…")
    user = client.get_user()
    print(f"         email   = {user.get('email')}")
    print(f"         account = {user.get('account')}")
    print(f"         plan    = {user.get('plan_name')}")

    print(f"[STEP 3] Uploading {TEST_CSV.name} as list {TEST_LIST!r}…")
    try:
        result = client.upload_csv(
            TEST_CSV,
            list_name=TEST_LIST,
            tags=[TEST_TAG],
            upload_type="new_properties",
        )
    except DataSiftAPIError as e:
        print(f"[FAIL] Upload failed: {e}", file=sys.stderr)
        return 2

    elapsed = time.monotonic() - started
    print()
    print(f"[RESULT]  success           = {result['success']}")
    print(f"[RESULT]  storage_key       = {result['storage_key']}")
    print(f"[RESULT]  line_count        = {result['line_count']}")
    print(f"[RESULT]  verified_in_lists = {result['verified_in_lists']}")
    print(f"[RESULT]  wall time         = {elapsed:.2f}s")
    print()

    # Show the suggested mapping for verification
    mapping = result.get("suggested_mapping", {})
    addr = mapping.get("property", {}).get("address", {})
    owner = mapping.get("owner", {})
    print(f"[CHECK]   auto-mapping picked these columns:")
    print(f"          property.address.street       = column {addr.get('street')}    (expected: 0)")
    print(f"          property.address.city         = column {addr.get('city')}      (expected: 1)")
    print(f"          property.address.state        = column {addr.get('state')}     (expected: 2)")
    print(f"          property.address.postal_code  = column {addr.get('postal_code')} (expected: 3)")
    print(f"          owner.first_name              = column {owner.get('first_name')}    (expected: 4)")
    print(f"          owner.last_name               = column {owner.get('last_name')}     (expected: 5)")

    expected = {"street": 0, "city": 1, "state": 2, "postal_code": 3}
    actual = {k: addr.get(k) for k in expected}
    if actual != expected:
        print(f"\n[WARN] Auto-mapping does not match expected — DataSift may have shifted columns.")
        print(f"       expected {expected}, got {actual}")
    else:
        print(f"\n[PASS] Auto-mapping is correct (street→0, city→1, state→2, postal_code→3).")

    if not result["verified_in_lists"]:
        print(f"\n[WARN] List {TEST_LIST!r} not visible in account yet — may still be processing.")
        print(f"       Open https://app.reisift.io/records/properties and filter by this list to confirm.")
        return 3

    print(f"\n[SUCCESS] RAWPIPE end-to-end works.")
    print(f"          Test list in your DataSift account: {TEST_LIST!r}")
    print(f"          (Delete it from the UI when convenient.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Purge orphaned owner/contact records from DataSift via the internal API.

Context: deleting property records in DataSift does NOT cascade to owners —
owners are a separate collection (/api/internal/owner/). After a "start fresh"
property wipe you're left with orphaned owner contacts. This clears them.

DRY-RUN BY DEFAULT. It only reports the count + a sample unless you pass
--confirm. There is NO undo on the delete.

    python delete_owners.py                 # dry run: count + sample, deletes nothing
    python delete_owners.py --confirm        # actually delete ALL owners
    python delete_owners.py --filter dirty   # restrict to owner_type=dirty (default: clean = all)

Auth comes from the cached refresh token (datasift_api_tokens.json) or
DATASIFT_EMAIL / DATASIFT_PASSWORD in .env — same as the rest of the pipeline.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

# Local CLI use — never let an Apify token divert this to the Actor path.
os.environ.pop("APIFY_TOKEN", None)

from datasift_api_client import DataSiftAPIClient  # noqa: E402

# The delete endpoint rejects filter-based select-all (owner_type isn't a valid
# choice there) AND the POST /owner/ search caps at 10 rows and ignores offset.
# So we enumerate every owner via GET /owner/ (real offset+limit paging) and
# delete by explicit UUID in batches — the schema-valid `owners:[…]` selection.
PAGE = 500          # GET /owner/ honors this
DELETE_BATCH = 500  # UUIDs per delete call
COUNT_FILTER = {"owner_type": "clean"}  # search-side catch-all (all owners)


def enumerate_owners(client: DataSiftAPIClient) -> list[dict]:
    """Return every owner record via GET /owner/ pagination."""
    out: list[dict] = []
    offset = 0
    while True:
        resp = client._request("GET", f"/api/internal/owner/?offset={offset}&limit={PAGE}")
        resp.raise_for_status()
        rows = resp.json().get("results") or []
        if not rows:
            break
        out.extend(rows)
        offset += PAGE
        print(f"  …enumerated {len(out)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge DataSift owner records.")
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete. Without this flag the script is a dry run.",
    )
    args = ap.parse_args()

    client = DataSiftAPIClient.from_env()
    before = client.count_records(COUNT_FILTER, mode="owner")
    print(f"Owner records: {before}")
    if before == 0:
        print("Nothing to delete.")
        return 0

    print("Enumerating all owner UUIDs…")
    owners = enumerate_owners(client)
    uuids = [o["uuid"] for o in owners if o.get("uuid")]
    print(f"Enumerated {len(uuids)} owners with UUIDs.")

    # Always write a backup before an irreversible delete — cheap insurance.
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = f"owners_backup_{stamp}.csv"
    with open(backup, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["uuid", "first_name", "last_name", "company", "properties_count"])
        for o in owners:
            w.writerow([o.get("uuid"), o.get("first_name"), o.get("last_name"),
                        o.get("company"), o.get("properties_count")])
    print(f"Backup written: {backup}")

    if not args.confirm:
        print(f"\nDRY RUN — nothing deleted. Re-run with --confirm to delete "
              f"{len(uuids)} owner records. This cannot be undone.")
        return 0

    print(f"\nDeleting {len(uuids)} owners in batches of {DELETE_BATCH}…")
    deleted = 0
    for i in range(0, len(uuids), DELETE_BATCH):
        batch = uuids[i:i + DELETE_BATCH]
        client.delete_owners(owner_uuids=batch)
        deleted += len(batch)
        print(f"  deleted {deleted}/{len(uuids)}")

    after = client.count_records(COUNT_FILTER, mode="owner")
    print(f"\nDone. Owners remaining: {after}.")
    if after:
        print("Some remain — deletes may lag server-side; re-run to drain to 0.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

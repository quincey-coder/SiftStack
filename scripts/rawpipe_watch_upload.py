"""RAWPIPE watch-mode upload — every step printed so the user can verify
field mapping in real time before the commit lands in DataSift.

Walks through the four-call RAWPIPE pipeline manually instead of calling
the wrapped DataSiftAPIClient.upload_csv(), so each request/response is
visible: presigned URL → S3 PUT → suggested-mapping → commit. The
mapping table is printed with the actual CSV header names alongside
each schema slot, with `-1`s (unmapped) called out.

Usage:
    PYTHONPATH=src python scripts/rawpipe_watch_upload.py <csv_path> <list_name>

Example:
    PYTHONPATH=src python scripts/rawpipe_watch_upload.py \\
        output/probate_may13_3records.csv "Probate"
"""

import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import requests
from datasift_api_client import DataSiftAPIClient, DataSiftAPIError


def banner(title: str) -> None:
    print()
    print("━" * 78)
    print(f"  {title}")
    print("━" * 78)


def show_response(label: str, body) -> None:
    """Pretty-print a JSON-ish response with size cap."""
    if isinstance(body, (dict, list)):
        text = json.dumps(body, indent=2, default=str)
    else:
        text = str(body)
    if len(text) > 2000:
        text = text[:2000] + f"\n... [truncated {len(text) - 2000} more chars]"
    print(f"\n{label}:\n{text}")


def walk_mapping(mapping: dict, headers: list[str]) -> None:
    """Print the suggested mapping with friendly column-name annotations."""
    print(f"{'SCHEMA PATH':<50} {'COL #':>6}   CSV HEADER")
    print("-" * 78)

    def visit(node: dict, prefix: str = "") -> None:
        for key, val in node.items():
            path = f"{prefix}{key}" if not prefix else f"{prefix}.{key}"
            if isinstance(val, dict):
                visit(val, path + ".")
            elif isinstance(val, int):
                if val == -1:
                    print(f"  {path:<48} {'-':>6}   (unmapped)")
                else:
                    header = headers[val] if 0 <= val < len(headers) else "?"
                    print(f"  {path:<48} {val:>6}   {header!r}")
            else:
                # Non-int leaves (e.g. custom_fields: {}). Skip.
                pass

    visit(mapping)

    unmapped_headers = set(range(len(headers)))
    def collect_used(node):
        for v in node.values():
            if isinstance(v, dict):
                collect_used(v)
            elif isinstance(v, int) and v >= 0:
                unmapped_headers.discard(v)
    collect_used(mapping)

    if unmapped_headers:
        print("\nCSV columns NOT auto-mapped to a DataSift built-in field:")
        for idx in sorted(unmapped_headers):
            print(f"  col {idx:>3}: {headers[idx]!r}")
        print(
            "\nNote: unmapped columns won't land unless they match an existing\n"
            "custom field by label name (DataSift auto-matches custom fields too).\n"
            "Common SiftStack columns (Notice Type, County, DM Confidence, etc.)\n"
            "land in your account's custom-field group if the build-custom-fields\n"
            "skill has been run."
        )


def main():
    if len(sys.argv) < 3:
        print("Usage: rawpipe_watch_upload.py <csv_path> <list_name>")
        return 1
    csv_path = Path(sys.argv[1])
    list_name = sys.argv[2]

    if not csv_path.exists():
        print(f"[FAIL] CSV not found: {csv_path}")
        return 1

    # ── Read CSV headers + row count for sanity ──
    banner("STEP 0 — Inspect the CSV locally")
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = list(reader)
    print(f"File:    {csv_path}")
    print(f"Records: {len(rows)}")
    print(f"Columns: {len(headers)}")
    print(f"First 6 columns:")
    for i, h in enumerate(headers[:6]):
        print(f"  col {i}: {h}")
    print("  …")

    # ── Connect ──
    banner("STEP 1 — Authenticate against apiv2.reisift.io")
    client = DataSiftAPIClient.from_env()
    user = client.get_user()
    print(f"Logged in as: {user.get('email')}")
    print(f"Account UUID: {user.get('account')}")
    print(f"Plan:         {user.get('plan_name')}")

    # ── Snapshot existing list state ──
    banner(f"STEP 2 — Check existing {list_name!r} list (before upload)")
    lists_before = client.list_lists(limit=200)
    target_before = next((l for l in lists_before if l.get("title") == list_name), None)
    if target_before:
        print(f"List EXISTS: title={target_before.get('title')!r}")
        print(f"             uuid={target_before.get('uuid')}")
        print(f"             properties_count={target_before.get('properties_count')}")
        print("New records will MERGE into this list.")
    else:
        print(f"List does NOT exist yet — DataSift will create {list_name!r} on commit.")

    # ── Get S3 presigned URL ──
    banner("STEP 3 — GET /api/internal/upload/presigned-upload-url/")
    print("Asking DataSift for a one-time S3 PUT URL to drop the CSV bytes at.")
    presigned = client._request_json("GET", "/api/internal/upload/presigned-upload-url/")
    storage_key = presigned["key"]
    print(f"\nstorage_key: {storage_key}")
    print(f"S3 URL:      {presigned['presigned_post']['url']}")
    print(f"signed fields: {len(presigned['presigned_post']['fields'])} fields "
          f"(policy + x-amz-signature + x-amz-credential + ...)")

    # ── PUT to S3 ──
    banner("STEP 4 — POST CSV bytes to S3 (multipart presigned)")
    fields = presigned["presigned_post"]["fields"]
    s3_url = presigned["presigned_post"]["url"]
    print(f"Uploading {csv_path.stat().st_size:,} bytes…")
    started = time.monotonic()
    with csv_path.open("rb") as f:
        s3_resp = requests.post(
            s3_url, data=fields,
            files={"file": (csv_path.name, f, "text/csv")},
            timeout=120,
        )
    elapsed = time.monotonic() - started
    print(f"S3 returned HTTP {s3_resp.status_code} in {elapsed:.2f}s")
    if s3_resp.status_code not in (200, 201, 204):
        print(f"S3 body: {s3_resp.text[:300]}")
        return 2

    # ── Check upload usage (line_count sanity) ──
    banner("STEP 5 — POST /upload/usage/ (line_count sanity check)")
    usage = client.get_upload_usage(storage_key)
    show_response("Usage", usage)
    line_count = usage.get("line_count")
    print(f"\nDataSift parsed {line_count} records from the CSV "
          f"(local row count was {len(rows)})")
    if line_count != len(rows):
        print(f"⚠ MISMATCH: local rows={len(rows)} vs DataSift line_count={line_count}")

    # ── Get suggested mapping — the headline check ──
    banner("STEP 6 — POST /upload/suggested-mapping/ (THE KEY MAPPING STEP)")
    print("DataSift reads the CSV at the storage_key and auto-maps each column")
    print("by header name to its schema slots. This is what you want to verify.\n")
    mapping_resp = client._request_json(
        "POST", "/api/internal/upload/suggested-mapping/",
        json={"storage_key": storage_key, "upload_type": "new_properties"},
    )
    mapping = mapping_resp["suggested_mapping"]
    walk_mapping(mapping, headers)

    # ── Commit ──
    banner(f"STEP 7 — POST /api/internal/upload/  (commit upload into {list_name!r})")
    commit_body = {
        "storage_key": storage_key,
        "mapping": mapping,
        "upload_type": "new_properties",
        "tags": ["Courthouse Data"],
        "lists": [list_name],
        "filename": csv_path.name,
        # IMPORTANT: enrich_property OFF so DataSift doesn't overwrite the
        # carefully-built Notes / Probate Open Date / Personal Representative
        # / Decedent fields with its own SiftMap data.
    }
    print("Request body (mapping nested object collapsed for brevity):")
    show_body = {**commit_body, "mapping": "<see STEP 6 above>"}
    print(json.dumps(show_body, indent=2))

    resp = client._request("POST", "/api/internal/upload/", json=commit_body)
    print(f"\nResponse: HTTP {resp.status_code}")
    if resp.text:
        print(f"Body: {resp.text[:200]}")
    if resp.status_code not in (200, 201, 202, 204):
        print(f"[FAIL] Commit rejected.")
        return 3

    # ── Verify ──
    banner(f"STEP 8 — GET /api/internal/list/?limit=200  (verify {list_name!r} grew)")
    time.sleep(2)  # DataSift processes asynchronously
    lists_after = client.list_lists(limit=200)
    target_after = next((l for l in lists_after if l.get("title") == list_name), None)
    if not target_after:
        print(f"[FAIL] List {list_name!r} not visible post-commit (may still be processing).")
        return 4
    before_count = (target_before or {}).get("properties_count") if target_before else None
    after_count = target_after.get("properties_count")
    print(f"List uuid:              {target_after.get('uuid')}")
    print(f"properties_count BEFORE: {before_count}")
    print(f"properties_count AFTER:  {after_count}")
    list_url = f"https://app.reisift.io/records/properties?list={target_after['uuid']}"
    print(f"\nView in DataSift: {list_url}")

    # ── Pull one record back to confirm field landing ──
    banner("STEP 9 — Sample one uploaded record back via /api/internal/property/")
    print("Fetching the most-recently-created property to verify fields landed correctly.")
    props = client._request_json("GET", "/api/internal/property/?limit=5&ordering=-created")
    items = props.get("data") or []
    # Find one whose address matches our CSV
    csv_addresses = {row[0] for row in rows}
    match = next(
        (p for p in items if (p.get("address", {}).get("street") or "") in csv_addresses),
        None,
    )
    if not match:
        # Fall back: just show the newest
        match = items[0] if items else None
    if match:
        # Highlight the fields the user cares about
        print(f"\nRECORD SAMPLE (UUID: {match.get('uuid', '?')}):")
        addr = match.get("address", {})
        owner = match.get("owner", {}) or {}
        owner_addr = owner.get("address", {}) or {}
        print(f"  Property address: {addr.get('street')!r}, "
              f"{addr.get('city')!r}, {addr.get('state')!r} {addr.get('postal_code')!r}")
        print(f"  Owner name:       {owner.get('first_name')!r} {owner.get('last_name')!r}")
        print(f"  Owner mailing:    {owner_addr.get('street')!r}, "
              f"{owner_addr.get('city')!r}, {owner_addr.get('state')!r} {owner_addr.get('postal_code')!r}")
        notes = match.get("notes")
        if notes:
            preview = (notes[:160] + "…") if len(notes) > 160 else notes
            print(f"  Notes (preview):  {preview}")
        else:
            print(f"  Notes:            <empty>")
        # Custom fields if present
        custom = match.get("custom_fields") or {}
        if custom:
            print(f"  Custom fields:    {len(custom)} field(s) populated")
            for k, v in list(custom.items())[:8]:
                print(f"    • {k}: {v}")
    else:
        print("[WARN] Could not retrieve a sample record. Check the DataSift UI directly.")

    banner("DONE")
    print(f"3 records added to {list_name!r}.")
    print(f"Direct UI link: {list_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

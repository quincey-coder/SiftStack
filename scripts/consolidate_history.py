"""Consolidate all historical DataSift CSVs into per-distress-type files.

Reads every datasift_upload_DMs_*.csv (+ today's OCTOLIST per-type CSVs),
deduplicates by Property Street Address (most recent wins), groups by the
Lists column (distress type), and writes one datasift_{type}_ALL_{ts}.csv
per group. Uploads each to Google Drive.
"""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import config
from datasift_formatter import DATASIFT_COLUMNS

OUTPUT_DIR = Path("/Users/quincey/Desktop/SiftStack/output")
TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H%M%S")


def load_all_rows() -> list[dict]:
    """Read every DataSift-format CSV in output/, sorted by mtime (oldest first)
    so most-recent data overwrites older during dedup."""
    files = list(OUTPUT_DIR.glob("datasift_upload_DMs_*.csv"))
    # Include today's OCTOLIST per-type CSVs so the split-type runs also get merged.
    for pattern in ("datasift_foreclosure_*.csv", "datasift_probate_*.csv",
                    "datasift_tax_*.csv", "datasift_eviction_*.csv",
                    "datasift_code_violation_*.csv", "datasift_divorce_*.csv"):
        files.extend(OUTPUT_DIR.glob(pattern))
    # Don't re-ingest any ALL-consolidation outputs.
    files = [f for f in files if "_ALL_" not in f.name]
    files.sort(key=lambda p: p.stat().st_mtime)

    rows: list[dict] = []
    for f in files:
        with open(f, newline="", encoding="utf-8") as fh:
            r = csv.DictReader(fh)
            for row in r:
                # Normalize to DATASIFT_COLUMNS — fill missing, drop unknown.
                clean = {col: row.get(col, "") for col in DATASIFT_COLUMNS}
                clean["_src_file"] = f.name  # for diagnostics only
                rows.append(clean)
    print(f"Loaded {len(rows):,} rows from {len(files)} files")
    return rows


def dedupe_and_group(rows: list[dict]) -> dict[str, list[dict]]:
    """Dedupe by (Property Street Address, ZIP) keeping most-recent row.
    Groups survivors by Lists column value."""
    # Since rows are ordered oldest → newest, later assignments win.
    latest_by_key: dict[tuple, dict] = {}
    for row in rows:
        addr = (row.get("Property Street Address") or "").strip().lower()
        zipc = (row.get("Property ZIP Code") or "").strip()
        if not addr:
            continue
        key = (addr, zipc)
        latest_by_key[key] = row

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in latest_by_key.values():
        lst = (row.get("Lists") or "").strip() or "Unknown"
        groups[lst].append(row)
    return groups


def write_group(list_name: str, rows: list[dict]) -> Path:
    slug = list_name.lower().replace(" ", "_").replace("/", "_")
    path = OUTPUT_DIR / f"datasift_{slug}_ALL_{TIMESTAMP}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DATASIFT_COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow({col: row.get(col, "") for col in DATASIFT_COLUMNS})
    print(f"  {list_name}: {len(rows):,} records → {path.name}")
    return path


def main() -> None:
    rows = load_all_rows()
    groups = dedupe_and_group(rows)
    print(f"\nDeduplicated to {sum(len(v) for v in groups.values()):,} unique records across {len(groups)} lists\n")

    # Write per-type CSVs in canonical order.
    from datasift_formatter import NOTICE_TYPE_TO_LIST
    canonical = list(NOTICE_TYPE_TO_LIST.values())
    ordered = [lst for lst in canonical if lst in groups]
    ordered += [lst for lst in groups if lst not in canonical]

    written: list[tuple[str, Path]] = []
    for lst in ordered:
        path = write_group(lst, groups[lst])
        written.append((lst, path))

    # Upload to Drive.
    if not (config.GOOGLE_DRIVE_FOLDER_ID and config.GOOGLE_SERVICE_ACCOUNT_KEY):
        print("\n⚠️  Drive creds not set — skipping upload")
        return

    from drive_uploader import upload_file
    print("\nUploading to Google Drive...")
    for label, path in written:
        link = upload_file(
            path,
            config.GOOGLE_DRIVE_FOLDER_ID,
            config.GOOGLE_SERVICE_ACCOUNT_KEY,
        )
        if link:
            print(f"  {label} → {link}")
        else:
            print(f"  {label}: upload FAILED")


if __name__ == "__main__":
    main()

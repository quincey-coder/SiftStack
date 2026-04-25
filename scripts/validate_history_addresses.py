"""Run Smarty address validation on the consolidated history CSVs.

Reads the datasift_{type}_ALL_*.csv files (foreclosure / probate /
tax_delinquent), runs every property address through Smarty US Street API
for standardization, writes _VALIDATED_ CSVs, and uploads to Drive.

No obituary enrichment. No DataSift upload. Address cleanup only.
"""
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
import config
from notice_parser import NoticeData
from datasift_formatter import DATASIFT_COLUMNS
from address_standardizer import standardize_addresses

OUTPUT_DIR = Path("/Users/quincey/Desktop/SiftStack/output")
TIMESTAMP = datetime.now().strftime("%Y-%m-%d_%H%M%S")

# Find the latest ALL-consolidation files (one per distress type).
INPUT_FILES = sorted(OUTPUT_DIR.glob("datasift_*_ALL_*.csv"))


def row_to_notice(row: dict) -> NoticeData:
    """Build a minimal NoticeData from a DataSift CSV row. Only fields
    needed for Smarty validation + later row rebuild are set."""
    n = NoticeData()
    n.address = (row.get("Property Street Address") or "").strip()
    n.city = (row.get("Property City") or "").strip()
    n.state = (row.get("Property State") or "").strip() or "TX"
    n.zip = (row.get("Property ZIP Code") or "").strip()
    return n


def apply_cleaned_to_row(row: dict, n: NoticeData) -> None:
    """Overwrite the row's property address columns with cleaned values."""
    if n.address:
        row["Property Street Address"] = n.address
    if n.city:
        row["Property City"] = n.city
    if n.state:
        row["Property State"] = n.state
    if n.zip:
        row["Property ZIP Code"] = n.zip


def process_file(path: Path) -> Path:
    print(f"\n── {path.name} ──")
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    print(f"  Loaded {len(rows):,} rows")

    # Build NoticeData objects in parallel with rows (same index).
    notices = [row_to_notice(r) for r in rows]

    # Run Smarty (mutates notices in place).
    standardize_addresses(
        notices, config.SMARTY_AUTH_ID, config.SMARTY_AUTH_TOKEN
    )

    confirmed = sum(1 for n in notices if n.dpv_match_code == "Y")
    print(f"  USPS-confirmed: {confirmed}/{len(notices)}")

    # Apply cleaned address values back to each row.
    for row, notice in zip(rows, notices):
        apply_cleaned_to_row(row, notice)

    # Write _VALIDATED_ CSV preserving the original DataSift schema.
    out_name = path.name.replace("_ALL_", "_VALIDATED_").replace(
        path.stem.split("_ALL_")[1], TIMESTAMP,
    )
    out_path = OUTPUT_DIR / out_name
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=DATASIFT_COLUMNS)
        w.writeheader()
        for row in rows:
            w.writerow({col: row.get(col, "") for col in DATASIFT_COLUMNS})
    print(f"  Wrote {out_path.name}")
    return out_path


def main() -> None:
    if not (config.SMARTY_AUTH_ID and config.SMARTY_AUTH_TOKEN):
        print("ERROR: Smarty credentials missing from .env — aborting")
        sys.exit(1)
    if not INPUT_FILES:
        print(f"ERROR: No datasift_*_ALL_*.csv files found in {OUTPUT_DIR}")
        sys.exit(1)

    print(f"Input files ({len(INPUT_FILES)}):")
    for f in INPUT_FILES:
        print(f"  {f.name}")

    written: list[tuple[str, Path]] = []
    for path in INPUT_FILES:
        # Derive distress-type label from filename: datasift_{slug}_ALL_*.csv
        slug = path.name.split("datasift_", 1)[1].split("_ALL_", 1)[0]
        label = slug.replace("_", " ").title()
        out_path = process_file(path)
        written.append((label, out_path))

    # Upload to Drive.
    if not (config.GOOGLE_DRIVE_FOLDER_ID and config.GOOGLE_SERVICE_ACCOUNT_KEY):
        print("\nDrive creds not set — skipping upload")
        return

    from drive_uploader import upload_file
    print("\nUploading validated CSVs to Google Drive...")
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

"""Run the full SiftStack enrichment pipeline on consolidated history.

Reads the datasift_{type}_ALL_*.csv files, reconstructs NoticeData,
re-runs the standard daily enrichment pipeline (Smarty → Zillow → CAD →
Tax → filters → validation) with obituary enrichment DISABLED, then splits
by distress type via write_datasift_by_notice_type() and uploads to Drive.

No obituary. No ancestry. No DataSift upload.
"""
import csv
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Configure logging before importing pipeline modules so their loggers inherit it.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
# Line-buffer stdout so progress streams in real time even when piped.
sys.stdout.reconfigure(line_buffering=True)

import config  # noqa: E402
from notice_parser import NoticeData  # noqa: E402
from datasift_formatter import write_datasift_by_notice_type  # noqa: E402
from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline  # noqa: E402
from scrapers.foreclosure_travis import _is_substitute_trustee  # noqa: E402

OUTPUT_DIR = Path("/Users/quincey/Desktop/SiftStack/output")

# ── DataSift row → NoticeData ────────────────────────────────────────


def _split_name(first: str, last: str) -> str:
    first = (first or "").strip()
    last = (last or "").strip()
    if first and last:
        return f"{first} {last}"
    return first or last


def _normalize_date(s: str) -> str:
    """Convert DataSift's M/D/YYYY date back to YYYY-MM-DD for the pipeline
    validator. Accepts already-ISO strings untouched. Returns empty on blank
    or unparseable input."""
    s = (s or "").strip()
    if not s:
        return ""
    # Already ISO
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    # M/D/YYYY or MM/DD/YYYY
    parts = s.split("/")
    if len(parts) == 3:
        m, d, y = parts
        if len(y) == 2:
            y = "20" + y
        try:
            return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        except ValueError:
            return ""
    return ""


def row_to_notice(row: dict) -> NoticeData:
    """Reconstruct a NoticeData from a DataSift CSV row. Preserves the
    enriched fields the pipeline can use to detect already-done work and
    the fields that end up back in the output via _build_row()."""
    n = NoticeData()

    # Core property address
    n.address = (row.get("Property Street Address") or "").strip()
    n.city = (row.get("Property City") or "").strip()
    n.state = (row.get("Property State") or "").strip() or "TX"
    n.zip = (row.get("Property ZIP Code") or "").strip()

    # Owner. If the historical CSV carries a known Travis County substitute
    # trustee as the owner (Angela Zavala, Israel Saucedo, etc.), blank it —
    # that was the filing attorney, not the property owner. The scraper
    # upstream now handles this at source, but existing CSVs pre-date the fix.
    owner_candidate = _split_name(
        row.get("Owner First Name"), row.get("Owner Last Name")
    )
    if _is_substitute_trustee(owner_candidate):
        n.owner_name = ""
    else:
        n.owner_name = owner_candidate

    # Mailing / PR address. DataSift stored this as the contact address
    # (PR for deceased, owner for living). Since we don't know which, stash
    # into owner_street fields (pipeline respects existing data).
    n.owner_street = (row.get("Mailing Street Address") or "").strip()
    n.owner_city = (row.get("Mailing City") or "").strip()
    n.owner_state = (row.get("Mailing State") or "").strip()
    n.owner_zip = (row.get("Mailing ZIP Code") or "").strip()
    n.mailing_address = n.owner_street

    # Phones / emails (preserve Tracerfy results so re-enrichment doesn't lose them)
    n.primary_phone = row.get("Phone 1", "")
    n.mobile_1 = row.get("Phone 2", "")
    n.mobile_2 = row.get("Phone 3", "")
    n.mobile_3 = row.get("Phone 4", "")
    n.mobile_4 = row.get("Phone 5", "")
    n.mobile_5 = row.get("Phone 6", "")
    n.landline_1 = row.get("Phone 7", "")
    n.landline_2 = row.get("Phone 8", "")
    n.landline_3 = row.get("Phone 9", "")
    n.email_1 = row.get("Email 1", "")
    n.email_2 = row.get("Email 2", "")
    n.email_3 = row.get("Email 3", "")
    n.email_4 = row.get("Email 4", "")
    n.email_5 = row.get("Email 5", "")

    # Property details (carry forward so re-running Zillow has a baseline)
    n.estimated_value = row.get("Estimated Value", "")
    n.mls_status = row.get("MSL Status", "")
    n.mls_last_sold_price = row.get("Last Sale Price", "")
    n.equity_percent = row.get("Equity Percentage", "")
    n.tax_delinquent_amount = row.get("Tax Deliquent Value", "")
    n.tax_delinquent_years = row.get("Tax Delinquent Year", "")
    n.parcel_id = row.get("Parcel ID", "")
    n.property_type = row.get("Structure Type", "")
    n.year_built = row.get("Year Built", "")
    n.sqft = row.get("Living SqFt", "")
    n.bedrooms = row.get("Bedrooms", "")
    n.bathrooms = row.get("Bathrooms", "")
    n.lot_size = row.get("Lot (Acres)", "")

    # Auction / filing date (DataSift split this across 3 columns by type).
    # Dates come back as M/D/YYYY from _format_date; pipeline validator expects YYYY-MM-DD.
    n.auction_date = _normalize_date(
        row.get("Tax Auction Date") or row.get("Foreclosure Date") or ""
    )
    n.date_added = _normalize_date(row.get("Date Added", ""))
    n.mls_last_sold_date = _normalize_date(row.get("Last Sale Date", ""))

    # Notice metadata
    n.notice_type = (row.get("Notice Type") or "").strip().lower()
    n.county = (row.get("County") or "").strip()
    n.source_url = row.get("Source URL", "")

    # Deceased / DM data (preserve — we're not re-running obituary)
    n.owner_deceased = row.get("Owner Deceased", "")
    n.date_of_death = _normalize_date(row.get("Date of Death", ""))
    n.decedent_name = row.get("Decedent Name", "")
    # Probate PR lives in DataSift's built-in "Personal Representative" column.
    # Fall back to the custom "Decision Maker" field if the first is blank.
    n.decision_maker_name = (
        (row.get("Personal Representative") or "").strip()
        or (row.get("Decision Maker") or "").strip()
    )
    n.decision_maker_relationship = row.get("DM Relationship", "")
    n.dm_confidence = row.get("DM Confidence", "")
    n.decision_maker_2_name = row.get("DM 2 Name", "")
    n.decision_maker_2_relationship = row.get("DM 2 Relationship", "")
    n.decision_maker_3_name = row.get("DM 3 Name", "")
    n.decision_maker_3_relationship = row.get("DM 3 Relationship", "")
    n.obituary_url = row.get("Obituary URL", "")
    n.decision_maker_status = row.get("DM 1 Status", "")
    n.decision_maker_source = row.get("DM 1 Source", "")
    n.decision_maker_2_status = row.get("DM 2 Status", "")
    n.decision_maker_3_status = row.get("DM 3 Status", "")
    n.heirs_verified_living = row.get("Heirs Living", "")
    n.signing_chain_count = row.get("Signing Chain Count", "")
    n.signing_chain_names = row.get("Signing Chain Names", "")
    n.dm_confidence_reason = row.get("DM Confidence Reason", "")
    n.missing_data_flags = row.get("Data Flags", "")
    n.entity_type = row.get("Entity Type", "")
    n.entity_person_name = row.get("Entity Contact", "")
    n.entity_person_role = row.get("Entity Contact Role", "")

    # Tag this so dedup inside the pipeline uses a stable key
    n._dedup_key = f"{n.address.lower()}|{n.zip}"

    return n


def load_notices() -> list[NoticeData]:
    files = sorted(OUTPUT_DIR.glob("datasift_*_ALL_*.csv"))
    files = [f for f in files if "_VALIDATED_" not in f.name]
    notices: list[NoticeData] = []
    for f in files:
        with open(f, newline="", encoding="utf-8") as fh:
            r = csv.DictReader(fh)
            for row in r:
                n = row_to_notice(row)
                if n.address:
                    notices.append(n)
    print(f"Loaded {len(notices):,} notices from {len(files)} consolidated files")
    return notices


# ── Main ─────────────────────────────────────────────────────────────


def main() -> None:
    notices = load_notices()
    if not notices:
        print("No notices to process — exiting")
        return

    # Summary pre-enrichment
    from collections import Counter
    pre_types = Counter(n.notice_type or "unknown" for n in notices)
    print(f"Pre-enrichment distribution: {dict(pre_types)}")

    # Full daily-run options, obituary disabled (default).
    opts = PipelineOptions(
        skip_parcel_lookup=False,    # run CAD lookup
        skip_smarty=False,           # run Smarty (re-validate addresses)
        skip_zillow=True,            # skip Zillow — existing records already carry it; re-querying 1.4K properties hangs
        skip_tax=False,              # run tax enricher
        skip_geocode=False,          # geocode if Smarty didn't
        skip_obituary=True,          # ← user request
        skip_ancestry=True,          # obituary-internal
        skip_heir_verification=True, # obituary-internal
        skip_dm_address=True,        # obituary-internal
        skip_entity_research=True,   # opt-in only
        skip_zip_filter=False,       # enforce target ZIPs
        skip_condo_filter=False,     # remove condos/apts
        skip_mls_filter=False,       # remove Active/Pending/Sold after Zillow
        skip_commercial_filter=True, # keep commercial
        skip_vacant_filter=True,     # keep vacant
        skip_entity_filter=True,     # keep entity owners
        source_label="Re-enrich history (no obituary)",
    )

    print("\nRunning full enrichment pipeline (obituary disabled)...")
    notices = run_enrichment_pipeline(notices, opts)
    print(f"\nPost-enrichment: {len(notices):,} notices remain")
    post_types = Counter(n.notice_type or "unknown" for n in notices)
    print(f"Post-enrichment distribution: {dict(post_types)}")

    # Split by distress type via the new OCTOLIST function.
    csv_infos = write_datasift_by_notice_type(notices, keep_government=False)
    print("\nOCTOLIST split complete:")
    for info in csv_infos:
        print(f"  {info['label']}: {info['count']} records → {info['path'].name}")

    # Upload to Drive only (no DataSift upload).
    if not (config.GOOGLE_DRIVE_FOLDER_ID and config.GOOGLE_SERVICE_ACCOUNT_KEY):
        print("\nDrive creds not set — skipping upload")
        return

    from drive_uploader import upload_file
    print("\nUploading to Google Drive...")
    for info in csv_infos:
        link = upload_file(
            info["path"],
            config.GOOGLE_DRIVE_FOLDER_ID,
            config.GOOGLE_SERVICE_ACCOUNT_KEY,
        )
        if link:
            print(f"  {info['label']} → {link}")
        else:
            print(f"  {info['label']}: upload FAILED")


if __name__ == "__main__":
    main()

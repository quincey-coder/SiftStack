"""Travis County tax delinquent scraper — direct CSV download + skill-ported cleaner.

Downloads the Travis County Tax Office's open data CSV of all delinquent
properties, archives the raw CSV, applies the full cleaning pipeline
ported from the `travis-county-texdel-clean` Claude skill, and computes
a cross-run diff (NEW / REPEAT / DROPPED APNs) against the last run.

Source: https://tax-office.traviscountytx.gov/voterdata/TaxDelqOpenData.csv
Updated: Monthly refresh by the tax office
Records: ~13,000 delinquent properties

Cleaning steps applied to every row:
  1. Owner-name overflow merge (Address 1 → Owner Name).
  2. Business vs person classification.
  3. Two-stage mailing-address cleaning (strip %/C-O/ATTN/FBO, validate).
  4. Blank Property Zip resolution via subdivision + street lookup.
  5. Filters: 2+ years delinquent, $2,500 min, no munis/HOAs/condos (A4), target ZIPs.

Cross-run diff: writes JSON state at data/travis_tax_state/ and archives
the raw CSV at data/travis_tax_raw/. See travis_texdel_state.py for details.
"""

import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path

import requests

from notice_parser import NoticeData, normalize_court_name
from scrapers import register
from scrapers import travis_texdel_cleaner as cleaner
from scrapers import travis_texdel_report as report_mod
from scrapers import travis_texdel_state as state_mod

logger = logging.getLogger(__name__)

CSV_URL = "https://tax-office.traviscountytx.gov/voterdata/TaxDelqOpenData.csv"
REQUEST_TIMEOUT = 60  # Large file, give it time

# Module-level attach point so main.py can pull the last run's diff/report
# without plumbing a tuple return through scrape_targets. Reset on every
# scrape() call.
LAST_RUN_DIFF: dict | None = None
LAST_RUN_STATS: dict | None = None
LAST_RUN_REMOVED: dict | None = None
LAST_RUN_REPORT_PATH: Path | None = None
LAST_RUN_RAW_CSV_PATH: Path | None = None


def _parse_address(street_num: str, street_name: str) -> str:
    """Combine street number and name into a single address string."""
    num = street_num.strip()
    name = street_name.strip()
    if not num and not name:
        return ""
    if not num:
        return name.title()
    if not name:
        return num
    return f"{num} {name}".title()


def _property_type_label(code: str) -> str:
    """Convert property type code to human-readable label."""
    types = {
        "A1": "Single Family",
        "A2": "Multi-Family",
        "A4": "Condominium",
        "B1": "Multi-Family (5+)",
        "B2": "Multi-Family (5+)",
        "C1": "Vacant Land",
        "D1": "Agricultural",
        "F1": "Commercial",
        "F2": "Industrial",
        "J1": "Utilities",
        "L1": "Commercial Personal",
        "M1": "Mobile Home",
        "O1": "Other",
    }
    return types.get(code.upper().strip(), code.strip())


def _build_tax_raw_meta(row: dict) -> str:
    """Pack the 13 skill 'green header' extra-tax fields into a JSON blob.

    Keeps NoticeData from bloating with 13 rarely-surfaced columns while
    preserving every byte of the source for future use.
    """
    keys = [
        "Owner ID", "Last Tax Roll Year", "Land Value", "Improvement Value",
        "Ag Market", "Ag Taxable Value", "Assessed Value", "Exemptions",
        "Current Year Total", "Sequence #", "Cause #", "Judgement Date",
        "Country",
    ]
    meta = {k: (row.get(k) or "").strip() for k in keys}
    meta = {k: v for k, v in meta.items() if v}
    return json.dumps(meta, separators=(",", ":")) if meta else ""


# Default filter thresholds — configurable via CLI.
DEFAULT_MIN_DELINQUENT_YEARS = 2
DEFAULT_MIN_DELINQUENT_AMOUNT = 3000.0


@register("Travis", "tax_delinquent")
class TravisTaxDelinquentScraper:
    """Download, clean, and diff Travis County tax delinquent CSV."""

    def __init__(
        self,
        min_years: int = DEFAULT_MIN_DELINQUENT_YEARS,
        min_amount: float = DEFAULT_MIN_DELINQUENT_AMOUNT,
        fixture_csv: str | Path | None = None,
        skip_cleaner: bool = False,
        skip_target_zip: bool = False,
    ):
        self.min_years = min_years
        self.min_amount = min_amount
        self.fixture_csv = Path(fixture_csv) if fixture_csv else None
        self.skip_cleaner = skip_cleaner
        self.skip_target_zip = skip_target_zip

    def _fetch_csv_text(self) -> str | None:
        """Download the Travis CSV or read a fixture file for tests."""
        if self.fixture_csv:
            if not self.fixture_csv.exists():
                logger.error("Fixture CSV not found: %s", self.fixture_csv)
                return None
            logger.info("Reading fixture CSV: %s", self.fixture_csv)
            return self.fixture_csv.read_text()
        try:
            resp = requests.get(CSV_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to download tax delinquent CSV: %s", e)
            return None
        # Travis Tax Office serves the CSV without a charset header, so
        # requests guesses ISO-8859-1 and the leading UTF-8 BOM bytes come
        # through as three literal chars ("ï»¿") instead of a single
        # \ufeff. Decode content explicitly as utf-8-sig so BOM is stripped.
        return resp.content.decode("utf-8-sig")

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        global LAST_RUN_DIFF, LAST_RUN_STATS, LAST_RUN_REMOVED
        global LAST_RUN_REPORT_PATH, LAST_RUN_RAW_CSV_PATH
        LAST_RUN_DIFF = None
        LAST_RUN_STATS = None
        LAST_RUN_REMOVED = None
        LAST_RUN_REPORT_PATH = None
        LAST_RUN_RAW_CSV_PATH = None

        logger.info(
            "Travis tax delinquent: filters %d+ years, $%.0f+ owed, "
            "skill cleaner=%s, target-zip filter=%s",
            self.min_years, self.min_amount,
            "OFF" if self.skip_cleaner else "ON",
            "OFF" if self.skip_target_zip else "ON",
        )

        raw_text = self._fetch_csv_text()
        if raw_text is None:
            return []

        # Strip UTF-8 BOM for downstream readers
        raw_text = raw_text.lstrip("\ufeff")

        # Archive raw CSV BEFORE processing — if the run blows up later,
        # we still have the source for post-mortem / state rebuild.
        try:
            raw_csv_path, raw_csv_sha = state_mod.archive_raw_csv(raw_text)
            LAST_RUN_RAW_CSV_PATH = raw_csv_path
            logger.info("Archived raw CSV: %s", raw_csv_path)
        except Exception as e:
            logger.warning("Raw CSV archival failed: %s", e)
            raw_csv_path, raw_csv_sha = None, ""

        # First pass: parse every row into a dict so we can build the
        # blank-zip lookup before the second pass runs transforms.
        reader = csv.DictReader(io.StringIO(raw_text))
        rows = list(reader)

        subdiv_zips, street_zips = (
            cleaner.build_zip_lookup(rows) if not self.skip_cleaner else ({}, {})
        )

        notices: list[NoticeData] = []
        today = datetime.now().strftime("%Y-%m-%d")

        # Gather APN set for cross-run diff BEFORE filtering. Otherwise a
        # property that previously matched the filter but drops out today
        # would falsely register as "paid off / sold" when really it's
        # just below threshold now. Use the full source set for diffing.
        source_apns: set[str] = set()
        removed = {
            "no_year": 0, "under_years": 0, "muni": 0, "hoa": 0,
            "prop_type": 0, "under_amt": 0, "zip": 0, "zero_delq": 0,
            "no_owner": 0, "no_apn": 0, "validation_blank": 0,
        }

        for row in rows:
            if max_notices and len(notices) >= max_notices:
                break

            account = (row.get("Account #") or "").strip()
            if not account:
                removed["no_apn"] += 1
                continue
            source_apns.add(account)

            owner_raw = (row.get("Owner Name") or "").strip()
            if not owner_raw:
                removed["no_owner"] += 1
                continue

            addr1 = (row.get("Address 1") or "").strip()
            addr2 = (row.get("Address 2") or "").strip()
            addr3 = (row.get("Address 3") or "").strip()

            # ── Step 1 — Owner name overflow merge ────────────────
            if not self.skip_cleaner and cleaner.is_name_overflow(owner_raw, addr1):
                full_owner = f"{owner_raw} {addr1}".strip()
                addr1_for_mail = ""
            else:
                full_owner = owner_raw
                addr1_for_mail = addr1

            # ── Step 2 — Business vs person ──────────────────────
            business_name = ""
            person_name = ""
            if not self.skip_cleaner and cleaner.is_business(full_owner):
                business_name = cleaner.title_case(full_owner)
            else:
                # Hybrid: SiftStack's normalize_court_name handles TX
                # LAST FIRST flipping + JR/SR/II/III/IV/ETAL suffixes.
                # Skill's title_case is just a safety pass if the
                # normalizer returned an uppercase string.
                flipped = normalize_court_name(full_owner)
                person_name = (
                    cleaner.title_case(flipped)
                    if not self.skip_cleaner
                    else (flipped.title() if flipped.isupper() else flipped)
                )

            # ── Step 3 — Filter: years delinquent ────────────────
            first_year_raw = (row.get("1st Year Delinquent") or "").strip()
            if first_year_raw in ("0", "00", "000", "0000"):
                first_year_raw = ""
            years_delinquent: int | None = None
            if first_year_raw.isdigit():
                try:
                    years_delinquent = datetime.now().year - int(first_year_raw)
                    if years_delinquent < 0:
                        years_delinquent = None
                except ValueError:
                    years_delinquent = None

            if years_delinquent is None:
                removed["no_year"] += 1
                continue
            if self.min_years > 0 and years_delinquent < self.min_years:
                removed["under_years"] += 1
                continue

            # ── Step 3b — HOA filter (skill-only) ────────────────
            if not self.skip_cleaner and business_name and cleaner.HOA_PAT.search(business_name):
                removed["hoa"] += 1
                continue

            # ── Step 3c — Condo property-type filter ─────────────
            prop_type_code = (row.get("Property Type Code") or "").strip().upper()
            if prop_type_code in cleaner.EXCLUDED_PROP_TYPES:
                removed["prop_type"] += 1
                continue

            # ── Step 3d — Min amount owed filter ─────────────────
            delinquent_total = (row.get("Delinquent Total") or "0").strip()
            total_due = (row.get("Total Due") or "0").strip()
            try:
                owed = float(delinquent_total) if delinquent_total else 0.0
                due = float(total_due) if total_due else 0.0
            except ValueError:
                owed = due = 0.0
            if owed <= 0 and due <= 0:
                removed["zero_delq"] += 1
                continue
            if self.min_amount > 0 and owed < self.min_amount:
                removed["under_amt"] += 1
                continue

            # ── Step 4 — Blank property zip resolution ───────────
            prop_zip_raw = (row.get("Property Zip") or "").strip()
            pzip5 = prop_zip_raw[:5]
            if not pzip5 or pzip5 == "00000":
                if not self.skip_cleaner:
                    pzip5 = cleaner.resolve_blank_zip(
                        row.get("Legal Description", ""),
                        row.get("Street Name", ""),
                        subdiv_zips,
                        street_zips,
                    )

            # ── Step 4b — Target ZIP filter ──────────────────────
            if (
                not self.skip_cleaner
                and not self.skip_target_zip
                and cleaner.TARGET_ZIPS
                and pzip5 not in cleaner.TARGET_ZIPS
            ):
                removed["zip"] += 1
                continue

            # ── Step 5 — Mailing-address cleaning + validation ───
            if not self.skip_cleaner:
                mail_addr = cleaner.clean_mailing_address(
                    addr1_for_mail, addr2, addr3,
                )
                if not cleaner.validate_address(mail_addr):
                    logger.debug(
                        "Address validation failed for APN %s: %r — blanking",
                        account, mail_addr,
                    )
                    mail_addr = ""
                    removed["validation_blank"] += 1
            else:
                mail_addr = " ".join(p for p in [addr1_for_mail, addr2, addr3] if p).strip()

            mail_city = (row.get("City") or "").strip()
            mail_city = cleaner.title_case(mail_city) if mail_city else ""
            mail_state = (row.get("State") or "").strip().upper()
            mail_zip_raw = (row.get("Zip Code") or "").strip()
            mail_zip = cleaner.fmt_zip(mail_zip_raw)

            # ── Step 6 — Property address + build NoticeData ────
            prop_addr = _parse_address(
                row.get("Street Number", ""),
                row.get("Street Name", ""),
            )

            owner_name_out = business_name if business_name else person_name

            notice = NoticeData(
                notice_type="tax_delinquent",
                county="Travis",
                state="TX",
                date_added=today,
                address=prop_addr,
                city=(row.get("City") or "").strip().title() or "Austin",
                zip=pzip5,
                owner_name=owner_name_out,
                parcel_id=account,
                source_url=f"https://tax-office.traviscountytx.gov/properties/{account}",
            )

            # New skill-derived fields
            notice.business_name = business_name
            notice.mailing_address = mail_addr
            notice.tax_raw_meta = _build_tax_raw_meta(row)
            # Preserve the pristine "Owner Name" column (pre-flip, pre-cleanup)
            # so deep prospecting can see ETAL / JR / SR / trust markers.
            # If the row had the name-overflow pattern (name spilled into
            # Address 1), stash the merged raw string so nothing is lost.
            notice.tax_owner_name = full_owner

            # Mailing address components (used by DataSift formatter when
            # mailing differs from property)
            if mail_addr:
                notice.owner_street = mail_addr
                notice.owner_city = mail_city
                notice.owner_state = mail_state or "TX"
                notice.owner_zip = mail_zip

            # Tax delinquency fields
            notice.tax_delinquent_amount = delinquent_total
            notice.tax_delinquent_years = str(years_delinquent)

            # Estimated value from appraisal (Appraisal Value column)
            appraisal_raw = (row.get("Appraisal Value") or "").strip()
            if appraisal_raw:
                try:
                    notice.estimated_value = str(int(float(appraisal_raw)))
                except (ValueError, TypeError):
                    pass

            # Property type
            if prop_type_code:
                notice.property_type = _property_type_label(prop_type_code)

            # Raw dump (preserved for downstream enrichment / debugging)
            notice.raw_text = (
                f"Account: {account}\n"
                f"Owner: {row.get('Owner Name', '')}\n"
                f"Address: {prop_addr}, {notice.city}, TX {notice.zip}\n"
                f"Mailing: {mail_addr}\n"
                f"Delinquent: ${delinquent_total} (since {first_year_raw})\n"
                f"Total Due: ${total_due}\n"
                f"Value: ${appraisal_raw}\n"
                f"Legal: {row.get('Legal Description', '')}"
            )

            notices.append(notice)

        # ── Cross-run diff + state commit ─────────────────────────
        try:
            state = state_mod.load_state()
            prev_apns = set(state.get("last_run_apns") or [])
            diff = state_mod.compute_diff(source_apns, prev_apns)
            stats = report_mod.build_stats(notices)

            if raw_csv_path is not None:
                state_mod.commit_run(
                    state,
                    current_apns=source_apns,
                    raw_csv_path=raw_csv_path,
                    raw_csv_sha=raw_csv_sha,
                    diff=diff,
                )

            report_path = report_mod.write_report_json(
                diff, stats, removed, raw_csv_path=raw_csv_path or "",
            )

            LAST_RUN_DIFF = diff.to_dict()
            LAST_RUN_STATS = stats
            LAST_RUN_REMOVED = removed
            LAST_RUN_REPORT_PATH = report_path

            if diff.guardrail_tripped:
                logger.warning(
                    "Travis tax-delinquent guardrail tripped: %s — "
                    "dropped-count suppressed, prior state preserved",
                    diff.guardrail_reason,
                )
            elif diff.is_first_run:
                logger.info(
                    "Travis tax-delinquent first run: seeding state with %d APNs",
                    len(source_apns),
                )
            else:
                logger.info(
                    "Travis tax-delinquent cross-run diff: NEW=%d REPEAT=%d DROPPED=%d "
                    "(dropped = likely sold/paid off — see %s for APN list)",
                    diff.new_count, diff.repeat_count, diff.dropped_count,
                    report_path,
                )
        except Exception:
            logger.exception("Cross-run diff/state update failed — continuing with notices")

        logger.info(
            "Travis tax delinquent: %d records kept (filtered: %s)",
            len(notices),
            ", ".join(f"{k}={v}" for k, v in removed.items() if v) or "none",
        )
        return notices

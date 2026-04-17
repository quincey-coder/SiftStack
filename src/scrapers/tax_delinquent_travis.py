"""Travis County tax delinquent scraper — direct CSV download.

Downloads the Travis County Tax Office's open data CSV of all delinquent
properties. No scraping needed — direct HTTP download.

Source: https://tax-office.traviscountytx.gov/voterdata/TaxDelqOpenData.csv
Updated: Monthly refresh by the tax office
Records: ~13,000 delinquent properties

CSV columns used:
  Account #, Owner Name, Street Number, Street Name, Property Zip,
  Delinquent Total, 1st Year Delinquent, Total Due,
  Appraisal Value, Property Type Code, Legal Description
"""

import csv
import io
import logging
import re
from datetime import datetime, timedelta

import requests

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

CSV_URL = "https://tax-office.traviscountytx.gov/voterdata/TaxDelqOpenData.csv"
REQUEST_TIMEOUT = 60  # Large file, give it time


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


@register("Travis", "tax_delinquent")
class TravisTaxDelinquentScraper:
    """Download and parse Travis County tax delinquent CSV."""

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        logger.info("Downloading Travis County tax delinquent CSV...")

        try:
            resp = requests.get(CSV_URL, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("Failed to download tax delinquent CSV: %s", e)
            return []

        # Parse CSV (UTF-8 with BOM)
        text = resp.text.lstrip("\ufeff")
        reader = csv.DictReader(io.StringIO(text))

        notices: list[NoticeData] = []
        skipped = 0
        today = datetime.now().strftime("%Y-%m-%d")

        for row in reader:
            if max_notices and len(notices) >= max_notices:
                break

            # Build address
            address = _parse_address(
                row.get("Street Number", ""),
                row.get("Street Name", ""),
            )

            owner_name = row.get("Owner Name", "").strip()
            if not owner_name:
                skipped += 1
                continue

            # Title-case if all caps
            if owner_name.isupper():
                owner_name = owner_name.title()

            # Parse delinquency info
            delinquent_total = row.get("Delinquent Total", "0").strip()
            total_due = row.get("Total Due", "0").strip()
            first_year = row.get("1st Year Delinquent", "").strip()
            appraisal_value = row.get("Appraisal Value", "").strip()
            account = row.get("Account #", "").strip()

            # Skip zero-delinquency records
            try:
                if float(delinquent_total) <= 0 and float(total_due) <= 0:
                    skipped += 1
                    continue
            except ValueError:
                pass

            notice = NoticeData(
                notice_type="tax_delinquent",
                county="Travis",
                state="TX",
                date_added=today,
                address=address,
                city=row.get("City", "").strip().title() or "Austin",
                zip=row.get("Property Zip", "").strip()[:5],
                owner_name=owner_name,
                parcel_id=account,
                source_url=f"https://tax-office.traviscountytx.gov/properties/{account}",
            )

            # Set tax delinquency fields
            notice.tax_delinquent_amount = delinquent_total
            if first_year and first_year != "0" and first_year != "0000":
                try:
                    years = datetime.now().year - int(first_year)
                    notice.tax_delinquent_years = str(max(years, 1))
                except ValueError:
                    pass

            # Set estimated value
            if appraisal_value:
                try:
                    notice.estimated_value = str(int(float(appraisal_value)))
                except (ValueError, TypeError):
                    pass

            # Set property type
            prop_type = row.get("Property Type Code", "").strip()
            if prop_type:
                notice.property_type = _property_type_label(prop_type)

            # Store raw data for enrichment
            notice.raw_text = (
                f"Account: {account}\n"
                f"Owner: {row.get('Owner Name', '')}\n"
                f"Address: {address}, {notice.city}, TX {notice.zip}\n"
                f"Delinquent: ${delinquent_total} (since {first_year})\n"
                f"Total Due: ${total_due}\n"
                f"Value: ${appraisal_value}\n"
                f"Legal: {row.get('Legal Description', '')}"
            )

            notices.append(notice)

        logger.info(
            "Travis tax delinquent: %d records parsed, %d skipped (of %d total rows)",
            len(notices), skipped, len(notices) + skipped,
        )
        return notices

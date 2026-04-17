"""Williamson County foreclosure scraper — trustee sales index PDF.

Scrapes the monthly trustee sales index from the Williamson County Clerk's
dedicated portal. Each month has an index PDF listing all notices with
owner names, addresses, and legal descriptions.

Source: https://apps.wilco.org/countyclerk/trustee_sales/
Index URL: {base}/{Month}/MM-DD-YYYY_File_IDX.pdf

The index PDF has columns:
  OWNER'S LAST NAME | FILE NO | CREATION DATE | ADDRESS | CITY | LEGAL DESCRIPTION
"""

import io
import logging
import re
from datetime import datetime, timedelta

import requests
from pdfminer.high_level import extract_text

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

BASE_URL = "https://apps.wilco.org/countyclerk/trustee_sales"

# Month names for URL construction
MONTHS = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def _get_month_file_list(month_name: str) -> list[str]:
    """Fetch the file listing page for a month and return PDF filenames."""
    url = f"{BASE_URL}/{month_name}/files.aspx"
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        # Extract PDF filenames from HREF attributes
        return re.findall(r'HREF="([^"]+\.pdf)"', resp.text, re.IGNORECASE)
    except requests.RequestException as e:
        logger.debug("Failed to fetch %s file listing: %s", month_name, e)
        return []


def _find_index_pdf(filenames: list[str]) -> str | None:
    """Find the index PDF filename (contains _IDX or _Index)."""
    for fn in filenames:
        if "IDX" in fn.upper() or "INDEX" in fn.upper():
            return fn
    return None


def _download_pdf_text(month_name: str, filename: str) -> str:
    """Download a PDF and extract text."""
    url = f"{BASE_URL}/{month_name}/{filename}"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        return extract_text(io.BytesIO(resp.content))
    except Exception as e:
        logger.warning("Failed to download/parse %s: %s", url, e)
        return ""


def _parse_index_pdf(text: str, sale_month: str, sale_year: int) -> list[NoticeData]:
    """Parse the foreclosure index PDF text into NoticeData records.

    The PDF has a multicolumn layout. After extraction, columns appear
    sequentially: all owner names first, then file numbers, then
    date+address+city+legal blocks. We extract each column as a list
    and zip by position.
    """
    lines = [l.strip() for l in text.split("\n")]
    sale_date = _first_tuesday(sale_month, sale_year)

    # Skip header — find "NOTICE OF FORECLOSURES" or first owner name
    data_start = 0
    for i, line in enumerate(lines):
        if "NOTICE OF FORECLOSURES" in line.upper():
            data_start = i + 1
            break
    if data_start == 0:
        data_start = 15  # Fallback

    # Phase 1: Extract owner names (ALL CAPS lines before file numbers start)
    owners = []
    file_nums_start = None
    _HEADER_WORDS = {"FILE NO", "CREATION", "DATE", "ADDRESS", "CITY", "LEGAL DESCRIPTION",
                     "OWNER'S LAST NAME", "NOTICE OF FORECLOSURES"}

    for i in range(data_start, len(lines)):
        line = lines[i]
        if not line:
            continue
        if line.upper() in _HEADER_WORDS:
            continue
        # File numbers are standalone small digits — marks end of owner column
        if re.match(r"^\d{1,3}$", line) and int(line) < 500:
            file_nums_start = i
            break
        # Owner names: ALL CAPS with letters, spaces, slashes, commas
        if re.match(r"^[A-Z][A-Z\s/,.'()\-&]+$", line) and len(line) > 1:
            owners.append(line)

    # Phase 2: Extract date+address+city blocks
    # After file numbers, we find lines like "3/26/2026 NO ADDRESS" or "3/26/2026\n6541 LEONARDO CV\nROUND ROCK\nLOT..."
    entries = []
    _KNOWN_CITIES = {"ROUND ROCK", "CEDAR PARK", "GEORGETOWN", "LEANDER", "AUSTIN",
                     "TAYLOR", "HUTTO", "LIBERTY HILL", "JARRELL", "FLORENCE",
                     "PFLUGERVILLE", "MANOR", "GRANGER", "BARTLETT", "THRALL",
                     "WEIR", "COUPLAND"}

    # Find where date+address blocks start (first line with a date pattern after file nums)
    date_start = file_nums_start or data_start
    for i in range(date_start, len(lines)):
        if re.match(r"\d{1,2}/\d{1,2}/\d{4}", lines[i]):
            date_start = i
            break

    i = date_start
    while i < len(lines):
        line = lines[i]
        date_match = re.match(r"(\d{1,2}/\d{1,2}/\d{4})\s*(.*)", line)
        if date_match:
            filed_date = date_match.group(1)
            rest = date_match.group(2).strip()
            address = ""
            city = ""

            if rest and "NO ADDRESS" not in rest.upper():
                address = rest

            # Look ahead for address and city on subsequent lines
            j = i + 1
            while j < len(lines) and j < i + 4:
                next_line = lines[j].strip()
                if not next_line:
                    j += 1
                    continue
                # Next date = new entry
                if re.match(r"\d{1,2}/\d{1,2}/\d{4}", next_line):
                    break
                # Known city
                if next_line.upper() in _KNOWN_CITIES:
                    city = next_line
                    j += 1
                    continue
                # Address (starts with number, not a date)
                if re.match(r"\d{1,5}\s+[A-Z]", next_line) and not address:
                    address = next_line
                    j += 1
                    continue
                # Legal description or other — skip
                j += 1

            entries.append({"date": filed_date, "address": address, "city": city})
            i = j
        else:
            i += 1

    logger.debug("Parsed %d owners, %d entries from index", len(owners), len(entries))

    # Zip owners with entries by position
    notices = []
    count = min(len(owners), len(entries))
    for idx in range(count):
        owner = owners[idx].title()
        entry = entries[idx]
        address = entry["address"].title() if entry["address"] else ""
        city = entry["city"].title() if entry["city"] else ""

        # Parse filed date
        filed_date = ""
        try:
            dt = datetime.strptime(entry["date"], "%m/%d/%Y")
            filed_date = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

        notice = NoticeData(
            notice_type="foreclosure",
            county="Williamson",
            state="TX",
            date_added=filed_date or datetime.now().strftime("%Y-%m-%d"),
            auction_date=sale_date,
            address=address,
            city=city,
            owner_name=owner,
            source_url=f"{BASE_URL}/{sale_month}/files.aspx",
        )
        notice.raw_text = f"{owners[idx]} | {entry['address']} | {entry['city']}"
        notices.append(notice)

    return notices


def _first_tuesday(month_name: str, year: int) -> str:
    """Calculate the first Tuesday of a given month."""
    month_num = MONTHS.index(month_name) + 1
    # Find the first Tuesday (weekday 1)
    for day in range(1, 8):
        dt = datetime(year, month_num, day)
        if dt.weekday() == 1:  # Tuesday
            return dt.strftime("%Y-%m-%d")
    return ""


@register("Williamson", "foreclosure")
class WilliamsonForeclosureScraper:
    """Scrape foreclosure notices from Williamson County trustee sales index."""

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        now = datetime.now()

        # Determine which months to scrape
        if mode == "historical":
            # Last 12 months
            months_to_check = []
            for i in range(12):
                dt = now - timedelta(days=30 * i)
                months_to_check.append((MONTHS[dt.month - 1], dt.year))
        else:
            # Current month + next month (notices posted 21 days before sale)
            months_to_check = [(MONTHS[now.month - 1], now.year)]
            next_month = now.month % 12 + 1
            next_year = now.year + (1 if next_month == 1 else 0)
            months_to_check.append((MONTHS[next_month - 1], next_year))

        logger.info(
            "Williamson foreclosure scrape: mode=%s, months=%s",
            mode, [f"{m} {y}" for m, y in months_to_check],
        )

        all_notices: list[NoticeData] = []

        for month_name, year in months_to_check:
            files = _get_month_file_list(month_name)
            if not files:
                logger.debug("No files for %s %d", month_name, year)
                continue

            idx_file = _find_index_pdf(files)
            if not idx_file:
                logger.debug("No index PDF for %s %d", month_name, year)
                continue

            logger.info("Processing %s %d index: %s", month_name, year, idx_file)
            text = _download_pdf_text(month_name, idx_file)
            if not text:
                continue

            notices = _parse_index_pdf(text, month_name, year)
            all_notices.extend(notices)
            logger.info("  %s %d: %d foreclosure notices", month_name, year, len(notices))

            if max_notices and len(all_notices) >= max_notices:
                all_notices = all_notices[:max_notices]
                break

        logger.info(
            "Williamson foreclosure scrape complete: %d notices",
            len(all_notices),
        )
        return all_notices

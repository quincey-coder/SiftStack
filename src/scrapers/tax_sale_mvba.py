"""MVBA Law Firm tax sale bid sheet scraper — Bell + Williamson counties.

Downloads monthly bid sheet PDFs from mvbalaw.com and extracts property
data for upcoming tax foreclosure sales.

Source: https://mvbalaw.com/tax-sales/month-sales/
PDF URL pattern: https://mvbalaw.com/wp-content/TaxUploads/MMYY_County.pdf

Bid sheet data (per tract):
  - Owner name (from "County v OWNER NAME" style)
  - Property address (embedded in legal description)
  - Account/parcel number
  - Minimum bid amount (not always in text extraction)
  - Sale date

Bell and Williamson counties both use MVBA for tax sales.
Sales happen 4-8 times per year on first Tuesday.
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

MVBA_MONTH_SALES_URL = "https://mvbalaw.com/tax-sales/month-sales/"

# Pattern to find PDF links for specific counties
_PDF_LINK_RE = re.compile(
    r'href="(https?://mvbalaw\.com/wp-content/TaxUploads/\d{4}_(\w+)\.pdf)"',
    re.IGNORECASE,
)

# Extract owner from "The County of X, Texas v\nOWNER NAME"
_STYLE_RE = re.compile(
    r"(?:County of \w+|Tax Appraisal District)[^v]+v\s+(.+?)(?=\n\n|\d+\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# Extract address from description — "123 STREET NAME, CITY, Texas ZIPCODE"
_ADDR_RE = re.compile(
    r"(\d{1,5}\s+[\w\s.'-]+?)"
    r",\s*([\w\s]+?)"
    r",?\s*Texas\s+"
    r"(\d{5})",
    re.IGNORECASE,
)

# Extract account number
_ACCT_RE = re.compile(r"Account\s*#\s*(\w+)", re.IGNORECASE)

# Extract sale date from header
_SALE_DATE_RE = re.compile(
    r"(?:SOLD ON|SALE[^:]*:?)\s+(\w+ \d{1,2},?\s*\d{4})",
    re.IGNORECASE,
)


def _find_county_pdf_urls(county: str) -> list[str]:
    """Fetch the MVBA month-sales page and find PDF links for a county."""
    try:
        resp = requests.get(
            MVBA_MONTH_SALES_URL,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        logger.warning("Failed to fetch MVBA month-sales page: %s", e)
        return []

    urls = []
    for match in _PDF_LINK_RE.finditer(resp.text):
        pdf_url = match.group(1)
        pdf_county = match.group(2).lower()
        if pdf_county == county.lower():
            urls.append(pdf_url)

    return urls


def _download_and_parse_pdf(url: str) -> str:
    """Download a PDF and extract text."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        resp.raise_for_status()
        return extract_text(io.BytesIO(resp.content))
    except Exception as e:
        logger.warning("Failed to download/parse PDF %s: %s", url, e)
        return ""


def _parse_bid_sheet(text: str, county: str) -> list[NoticeData]:
    """Parse a bid sheet PDF text into NoticeData records.

    The PDF has a multicolumn layout. After text extraction:
    - Left column (tracts/owners) comes first
    - Right column (property descriptions/addresses/accounts) comes second
    We extract all three lists separately and zip them together by position.
    """
    notices = []

    # Find sale date from header
    sale_date = ""
    sale_match = _SALE_DATE_RE.search(text)
    if sale_match:
        raw = sale_match.group(1).strip()
        for fmt in ("%B %d, %Y", "%B %d %Y"):
            try:
                dt = datetime.strptime(raw, fmt)
                sale_date = dt.strftime("%Y-%m-%d")
                break
            except ValueError:
                continue

    # Extract all owners from "County v OWNER" patterns. Keep the pristine
    # pre-title-case string alongside the display version so the DataSift
    # notes section can surface the original legal name for deep prospecting.
    owners = []      # display (title-cased) names
    owners_raw = []  # pristine pre-title-case copies aligned by index
    for m in re.finditer(
        r"(?:County of \w+|Tax Appraisal District)[^v]+v\s+(.+?)(?=\n\s*\n|\d{1,3}\s*\n\s*\n|\Z)",
        text, re.IGNORECASE | re.DOTALL,
    ):
        owner = re.sub(r"\s+", " ", m.group(1)).strip()
        # Truncate at property description keywords
        owner = re.split(
            r"(?:PROPERTY DESCRIPTION|\d+\.\d+ Acre|Lot \d|East part|West part|A Manufactured|MINIMUM)",
            owner,
        )[0].strip()
        raw_owner = owner
        if owner.isupper():
            owner = owner.title()
        if owner and len(owner) > 2:
            owners.append(owner)
            owners_raw.append(raw_owner)

    # Extract all addresses
    addresses = []
    for m in _ADDR_RE.finditer(text):
        addr = re.sub(r"\s+", " ", m.group(1)).strip().title()
        city = m.group(2).strip().title()
        zip_code = m.group(3)
        addresses.append((addr, city, zip_code))

    # Extract all account numbers
    accounts = [m.group(1) for m in _ACCT_RE.finditer(text)]

    logger.debug(
        "Bid sheet parse: %d owners, %d addresses, %d accounts",
        len(owners), len(addresses), len(accounts),
    )

    # Zip together — they should align positionally
    count = max(len(owners), len(addresses), len(accounts))
    for i in range(count):
        owner = owners[i] if i < len(owners) else ""
        owner_raw = owners_raw[i] if i < len(owners_raw) else ""
        addr, city, zip_code = addresses[i] if i < len(addresses) else ("", "", "")
        acct = accounts[i] if i < len(accounts) else ""

        if not owner and not addr:
            continue

        notice = NoticeData(
            notice_type="tax_sale",
            county=county,
            state="TX",
            date_added=datetime.now().strftime("%Y-%m-%d"),
            auction_date=sale_date,
            address=addr,
            city=city,
            zip=zip_code,
            owner_name=owner,
            parcel_id=acct,
            source_url="https://mvbalaw.com/tax-sales/",
        )
        # Preserve the pristine "County of X v NAME" string from the PDF.
        notice.tax_owner_name = owner_raw

        notice.raw_text = f"Tract {i+1}: {owner} | {addr}, {city} TX {zip_code} | Acct: {acct}"
        notices.append(notice)

    return notices


async def _scrape_mvba(county: str, max_notices: int | None = None) -> list[NoticeData]:
    """Scrape MVBA bid sheets for a county."""
    logger.info("Searching MVBA for %s County bid sheets...", county)

    pdf_urls = _find_county_pdf_urls(county)
    if not pdf_urls:
        logger.info("No MVBA bid sheets found for %s County", county)
        return []

    logger.info("Found %d bid sheet PDFs for %s County", len(pdf_urls), county)

    all_notices: list[NoticeData] = []
    for url in pdf_urls:
        logger.info("Processing: %s", url)
        text = _download_and_parse_pdf(url)
        if not text:
            continue

        notices = _parse_bid_sheet(text, county)
        all_notices.extend(notices)
        logger.info("  Extracted %d properties from bid sheet", len(notices))

        if max_notices and len(all_notices) >= max_notices:
            all_notices = all_notices[:max_notices]
            break

    logger.info(
        "%s County tax sale: %d properties from %d bid sheets",
        county, len(all_notices), len(pdf_urls),
    )
    return all_notices


@register("Williamson", "tax_sale")
class WilliamsonTaxSaleScraper:
    """Scrape Williamson County tax sale bid sheets from MVBA."""

    async def scrape(self, mode="daily", since_date=None, max_notices=None) -> list[NoticeData]:
        return await _scrape_mvba("Williamson", max_notices)


@register("Bell", "tax_sale")
class BellTaxSaleScraper:
    """Scrape Bell County tax sale bid sheets from MVBA."""

    async def scrape(self, mode="daily", since_date=None, max_notices=None) -> list[NoticeData]:
        return await _scrape_mvba("Bell", max_notices)

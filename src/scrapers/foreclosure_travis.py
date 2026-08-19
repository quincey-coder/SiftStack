"""Travis County foreclosure scraper — tccsearch.org (Travis County Clerk).

Scrapes "Notice of Substitute Trustee Sale" filings from the Travis County
Clerk's real estate records index. ASP.NET WebForms site with Infragistics
UI controls. No CAPTCHA, no login required.

Site: https://www.tccsearch.org/RealEstate/SearchResults.aspx
Doc type checkbox index: 72 = "NOTICE OF SUBSTITUTE TRUSTEE SALE"

Key insight: The results grid already contains enough data to build NoticeData
records WITHOUT clicking into each detail page:
  - Name [R] = grantor (borrower/owner)
  - Associated Name [E] often contains the sale date
  - Legal Description contains "LOC {address}" with full street + city + zip

Flow:
  1. Accept disclaimer gate
  2. Navigate to Real Estate search
  3. Check foreclosure doc type, set date range
  4. Submit search → results page (20 per page, paginated)
  5. Parse each result row from the grid
  6. Paginate through all pages
  7. Return list[NoticeData]
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright, Page

from notice_parser import NoticeData
from scrapers import register
from scrapers.tccsearch_common import (
    click_search,
    effective_from_date,
    goto_with_retry,
    launch_tcc_context,
    pass_cloudflare,
    safe_check,
    wait_ready,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tccsearch.org"
SEARCH_URL = f"{BASE_URL}/RealEstate/SearchEntry.aspx"

# Document type checkbox index for foreclosure filings
DOC_TYPE_FORECLOSURE = 72  # "NOTICE OF SUBSTITUTE TRUSTEE SALE"

# Selectors
SEL_DISCLAIMER_ACCEPT = "#cph1_lnkAccept"
SEL_DATE_FROM_ID = "cphNoMargin_f_ddcDateFiledFrom"  # Infragistics ID (for $find())
SEL_DATE_TO_ID = "cphNoMargin_f_ddcDateFiledTo"

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0

# Regex to extract address from legal description "LOC" field
# e.g. "LOC 16800 AVOLA CV PFLUGERVILLE TX 78660 PDV 5"
# or   "LOC 6006 PALM CIR AUSTIN TX 78741"
_LOC_ADDR_RE = re.compile(
    r"LOC\s+"
    r"(\d{1,5}\s+[\w\s.'-]+?"  # house number + street
    r"(?:ST|AVE|RD|DR|LN|BLVD|WAY|CIR|CT|PL|CV|LOOP|RUN|PATH|TRL|PKWY|TER|HWY|PIKE|XING|PASS|PT|BND|VW|HOLW|GLN|CRES|SPUR|CMN"
    r"|STREET|AVENUE|ROAD|DRIVE|LANE|BOULEVARD|CIRCLE|COURT|PLACE|COVE|TRAIL|TERRACE|PARKWAY|HIGHWAY|CROSSING|BEND|POINT|VIEW|RIDGE|ROW|WALK|KNOLL|OVERLOOK))"
    r"\s+"
    r"([\w\s]+?)"  # city name
    r"\s+TX\s+"
    r"(\d{5})",  # zip
    re.IGNORECASE,
)

# Fallback: simpler LOC pattern for addresses not matching suffix list
_LOC_SIMPLE_RE = re.compile(
    r"LOC\s+"
    r"(\d{1,5}\s+.+?)"  # everything after LOC until city/state
    r"\s+(AUSTIN|PFLUGERVILLE|MANOR|DEL VALLE|LAKEWAY|BEE CAVE|WEST LAKE HILLS|ROUND ROCK|CEDAR PARK|LEANDER|GEORGETOWN|HUTTO|TAYLOR|KILLEEN|TEMPLE|BELTON)"
    r"\s+TX\s+"
    r"(\d{5})",
    re.IGNORECASE,
)

# Extract sale date from associated name field "[E] 06/02/2026 (+)"
_SALE_DATE_RE = re.compile(r"\[E\]\s*(\d{2}/\d{2}/\d{4})")

# Extract grantor name from "[R] LAST FIRST (+)" or "[R] LAST FIRST MIDDLE"
_GRANTOR_RE = re.compile(r"\[R\]\s*(.+?)(?:\s*\(\+\))?$")

# Substitute-trustee blocklist now lives in scrapers/trustee_blocklist.py so
# Bell + Williamson scrapers can share it. Keep a local alias so external
# callers (scripts/reenrich_history.py) that import _is_substitute_trustee
# from this module continue to work.
from scrapers.trustee_blocklist import is_substitute_trustee as _is_substitute_trustee  # noqa: E402,F401


def _doc_type_selector(index: int) -> str:
    return f"#cphNoMargin_f_dclDocType_{index}"


def _date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _clean_name(raw: str) -> str:
    """Clean up an ALL-CAPS grantor name to Title Case."""
    name = raw.strip().rstrip(",;.")
    # Remove common trust/entity indicators for display
    if name.isupper() or name.islower():
        name = name.title()
    return name


def _parse_address_from_legal(legal_desc: str) -> tuple[str, str, str]:
    """Extract (address, city, zip) from legal description LOC field.

    Returns ("", "", "") if no address found.
    """
    if not legal_desc:
        return "", "", ""

    # Try detailed regex first
    m = _LOC_ADDR_RE.search(legal_desc)
    if m:
        addr = m.group(1).strip().title()
        city = m.group(2).strip().title()
        zip_code = m.group(3)
        return addr, city, zip_code

    # Try simpler regex with known city names
    m = _LOC_SIMPLE_RE.search(legal_desc)
    if m:
        addr = m.group(1).strip().title()
        city = m.group(2).strip().title()
        zip_code = m.group(3)
        return addr, city, zip_code

    return "", "", ""


async def _accept_disclaimer(page: Page) -> None:
    """Click the disclaimer acceptance link on the landing page."""
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    # First navigation to the domain — clear the Cloudflare interstitial here so
    # the cf_clearance cookie is set before the disclaimer/search steps.
    await pass_cloudflare(page)
    accept = page.locator(SEL_DISCLAIMER_ACCEPT)
    if await accept.count() > 0:
        await accept.click()
        await page.wait_for_load_state("domcontentloaded")
        logger.info("Disclaimer accepted")


async def _set_date_range(page: Page, from_date: datetime, to_date: datetime) -> None:
    """Set the date range using the Infragistics WebDatePicker JS API."""
    from_str = _date_str(from_date)
    to_str = _date_str(to_date)
    await page.evaluate(f"""
        var dp1 = $find('{SEL_DATE_FROM_ID}');
        if (dp1) dp1.set_value(new Date('{from_str}'));
        var dp2 = $find('{SEL_DATE_TO_ID}');
        if (dp2) dp2.set_value(new Date('{to_str}'));
    """)
    logger.info("Date range: %s to %s", from_str, to_str)


async def _submit_search(page: Page) -> int:
    """Submit the search form and return total results count."""
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
        await click_search(page)  # null-guarded + retried (the old bare
        # getElementById().click() crashed on 10 of 30 days when the form
        # render raced the framework)
    await page.wait_for_timeout(2000)

    # Extract total count from "Showing Records 1 through 20 ( 150 records found ... )"
    body_text = await page.inner_text("body")
    count_match = re.search(r"(\d+)\s+records?\s+found", body_text)
    total = int(count_match.group(1)) if count_match else 0
    logger.info("Search returned %d records", total)
    return total


async def _parse_results_page(page: Page) -> list[NoticeData]:
    """Parse all result rows on the current results page.

    Each row in the grid has:
      - Instrument # (used as source identifier)
      - Date Filed
      - Name [R] = grantor (owner/borrower)
      - Associated Name [E] = may contain sale date
      - Legal Description with LOC = property address
    """
    notices = []

    # Get all text blocks from result rows
    # The grid uses alternating row colors, each row has multiple lines
    body_text = await page.inner_text("body")

    # Split into logical records by looking for numbered rows
    # Pattern: number \n View \n instrument# \n date \t DOC_TYPE \t [R] NAME
    lines = body_text.split("\n")
    lines = [l.strip() for l in lines if l.strip()]

    # Find the start of results: look for the pattern of numbered rows
    i = 0
    while i < len(lines):
        # Look for a row number followed by "View" and instrument number
        if not lines[i].isdigit():
            i += 1
            continue

        row_num = lines[i]

        # Collect the next several lines that form this record
        record_lines = []
        j = i + 1
        while j < len(lines) and j < i + 8:
            if lines[j].isdigit() and j > i + 2:
                # Hit the next row number
                break
            record_lines.append(lines[j])
            j += 1

        record_text = " | ".join(record_lines)

        # Parse the record
        notice = _parse_record(row_num, record_text, record_lines)
        if notice:
            notices.append(notice)

        i = j

    return notices


def _parse_record(row_num: str, record_text: str, lines: list[str]) -> NoticeData | None:
    """Parse a single result record into NoticeData."""
    # Skip "View" line
    data_lines = [l for l in lines if l != "View"]
    if len(data_lines) < 3:
        return None

    notice = NoticeData(
        notice_type="foreclosure",
        county="Travis",
        state="TX",
    )

    # First data line should contain instrument number
    instrument = ""
    for line in data_lines:
        if re.match(r"^\d{9,}$", line):
            instrument = line
            break

    if instrument:
        notice.source_url = f"{BASE_URL}/RealEstate/DocumentDetail.aspx?InstrumentNumber={instrument}"

    # Look for date filed (MM/DD/YYYY followed by doc type)
    for line in data_lines:
        date_match = re.match(r"(\d{2}/\d{2}/\d{4})\s+", line)
        if date_match:
            try:
                dt = datetime.strptime(date_match.group(1), "%m/%d/%Y")
                notice.date_added = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
            break

    # Look for grantor name [R]. When the grid lists a known substitute
    # trustee (the attorney who filed the notice), skip it — the real
    # borrower is hidden behind the "(+)" indicator and will be filled
    # in downstream via CAD lookup by property address.
    for line in data_lines:
        m = _GRANTOR_RE.search(line)
        if m:
            raw_name = _clean_name(m.group(1))
            if _is_substitute_trustee(raw_name):
                logger.info(
                    "Skipped substitute trustee %r (real borrower hidden in grid); "
                    "relying on CAD address lookup downstream", raw_name,
                )
                break
            from notice_parser import normalize_court_name
            # Preserve the pristine county-record name for deep prospecting —
            # surfaced in Notes by datasift_formatter._build_notes().
            notice.tax_owner_name = raw_name
            notice.owner_name = normalize_court_name(raw_name)
            break

    # Look for sale date in [E] field
    for line in data_lines:
        m = _SALE_DATE_RE.search(line)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%m/%d/%Y")
                notice.auction_date = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
            break

    # Extract address from legal description (LOC field)
    for line in data_lines:
        if "LOC " in line.upper():
            addr, city, zip_code = _parse_address_from_legal(line)
            if addr:
                notice.address = addr
                notice.city = city
                notice.zip = zip_code
            break

    # Store raw text for LLM fallback parsing
    notice.raw_text = "\n".join(data_lines)

    # Only return if we got at least a name or address
    if notice.owner_name or notice.address:
        return notice
    return None


async def _go_to_page(page: Page, page_num: int) -> bool:
    """Navigate to a specific results page via the pagination dropdown.

    The site uses a <select> with onchange=itemChange(this).
    Select ID: cphNoMargin_cphNoMargin_OptionsBar1_ItemList (top)
    Option values are 1-based page numbers, labels are "Page N".
    """
    sel = page.locator("#cphNoMargin_cphNoMargin_OptionsBar1_ItemList")
    if await sel.count() == 0:
        return False

    # Check if the target page option exists
    option = sel.locator(f"option[value='{page_num}']")
    if await option.count() == 0:
        return False

    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=15000):
            await sel.select_option(str(page_num))
        await page.wait_for_timeout(1500)
        return True
    except Exception as e:
        logger.warning("Failed to navigate to page %d: %s", page_num, e)
        return False


@register("Travis", "foreclosure")
class TravisForeclosureScraper:
    """Scrape foreclosure filings from tccsearch.org results grid."""

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        # Calculate date range
        to_date = datetime.now()
        if since_date:
            try:
                from_date = datetime.strptime(since_date, "%Y-%m-%d")
            except ValueError:
                logger.error("Invalid since_date: %s", since_date)
                from_date = to_date - timedelta(days=30)
        elif mode == "historical":
            from_date = to_date - timedelta(days=365)
        else:
            from_date = to_date - timedelta(days=30)

        # Cover the clerk's Temp-index lag so paper-filed notices that were
        # nameless on day 1 get re-scanned once verified (dedup absorbs repeats).
        from_date = effective_from_date(from_date, to_date, mode)

        logger.info(
            "Travis foreclosure scrape: mode=%s, range=%s to %s",
            mode, from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d"),
        )

        all_notices: list[NoticeData] = []

        async with async_playwright() as p:
            # Cloudflare-resistant headed launch (see tccsearch_common).
            browser, context, page = await launch_tcc_context(p)

            try:
                # Step 1: Accept disclaimer
                await _accept_disclaimer(page)

                # Step 2: Navigate to search
                await goto_with_retry(page, SEARCH_URL)
                await page.wait_for_timeout(1500)
                # Fail fast (~12s) with a clear message if the client framework
                # was withheld (datacenter-IP block) instead of a 30s check timeout.
                await wait_ready(page)

                # Step 3: Check foreclosure document type — a failed check
                # means the search would run UNFILTERED, so it's fatal.
                if not await safe_check(page, _doc_type_selector(DOC_TYPE_FORECLOSURE)):
                    from scrapers import ScraperError
                    raise ScraperError(
                        "Travis foreclosure: doc-type checkbox not checkable — "
                        "search would be misfiltered"
                    )

                # Step 4: Set date range
                await _set_date_range(page, from_date, to_date)

                # Step 5: Submit search
                total = await _submit_search(page)
                self.last_meta = {
                    "returned": total,
                    "window_days": (to_date - from_date).days + 1,
                }
                if total == 0:
                    await browser.close()
                    return []

                # Step 6: Parse results, paginating through all pages
                current_page = 1
                while True:
                    logger.info("Parsing page %d...", current_page)
                    page_notices = await _parse_results_page(page)
                    all_notices.extend(page_notices)
                    logger.info(
                        "  Page %d: %d notices (total so far: %d)",
                        current_page, len(page_notices), len(all_notices),
                    )

                    if max_notices and len(all_notices) >= max_notices:
                        all_notices = all_notices[:max_notices]
                        logger.info("Reached max_notices limit (%d)", max_notices)
                        break

                    # Try to go to the next page
                    current_page += 1
                    if not await _go_to_page(page, current_page):
                        logger.info("No more pages (stopped at page %d)", current_page - 1)
                        break

                    await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

            except Exception as e:
                logger.error("Travis foreclosure scraper failed: %s", e)
                raise
            finally:
                await browser.close()

        self.last_meta["kept"] = len(all_notices)
        logger.info(
            "Travis foreclosure scrape complete: %d notices from %d total records",
            len(all_notices), total,
        )
        return all_notices

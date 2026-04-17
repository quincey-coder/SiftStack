"""Odyssey PublicAccess probate scraper — Williamson County.

Scrapes probate cases from the legacy Odyssey PublicAccess system
(Tyler Technologies, 2003 version). No CAPTCHA, no login required.

Site: https://judicialrecords.wilco.org/PublicAccess/
Search: Search.aspx?ID=200 (Civil, Family & Probate Case Records)
Probate cases: County Court at Law #4 (case numbers contain "CP4")

Flow:
  1. Establish session at default.aspx
  2. Click "Civil, Family & Probate Case Records" menu link
  3. Select "Date Filed" search, set date range
  4. Submit → results grid (all civil/family/probate combined)
  5. Filter to probate cases (CP4 suffix or "Estate Of" in title)
  6. Click each case detail for PR/executor name
  7. Return list[NoticeData]

Travis County and Bell County use the newer Odyssey Portal (2017+) which
has reCAPTCHA — those will be added when a CAPTCHA solution is integrated.
"""

import logging
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright, Page

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

# ── Williamson County config ──────────────────────────────────────────
WILCO_BASE = "https://judicialrecords.wilco.org/PublicAccess"
WILCO_DEFAULT = f"{WILCO_BASE}/default.aspx"
WILCO_SEARCH_ID = 200  # Civil, Family & Probate

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0

# Probate case patterns
# Case numbers containing CP4 = County Court at Law #4 (Probate Division)
_PROBATE_CASE_RE = re.compile(r"CP4", re.IGNORECASE)

# "Estate Of NAME, Deceased" pattern
_ESTATE_OF_RE = re.compile(
    r"Estate\s+Of\s+(.+?),\s*Deceased",
    re.IGNORECASE,
)

# Guardianship pattern (also handled by probate court)
_GUARDIANSHIP_RE = re.compile(
    r"Guardianship\s+Of\s+(.+?)(?:,\s*An\s+Incapacitated|$)",
    re.IGNORECASE,
)

# Probate case types we care about
PROBATE_CASE_TYPES = {
    "letters testamentary",
    "independent administration",
    "independent administration w/heirship",
    "dependent administration",
    "temporary administration",
    "muniment of title",
    "small estate affidavit",
    "determination of heirship",
    "probate of will",
}


def _is_probate_case(case_number: str, case_title: str, case_type: str) -> bool:
    """Check if a case is a probate case we want to scrape."""
    # CP4 suffix = County Court at Law #4 (Probate)
    if _PROBATE_CASE_RE.search(case_number):
        return True
    # "Estate Of ... Deceased" in title
    if _ESTATE_OF_RE.search(case_title):
        return True
    # Probate case type
    if case_type.lower().strip() in PROBATE_CASE_TYPES:
        return True
    return False


def _extract_decedent_name(case_title: str) -> str:
    """Extract decedent name from 'Estate Of NAME, Deceased'."""
    m = _ESTATE_OF_RE.search(case_title)
    if m:
        name = m.group(1).strip()
        if name.isupper():
            name = name.title()
        return name
    return ""


async def _establish_session(page: Page, base_url: str) -> None:
    """Navigate to the default page to establish an ASP.NET session."""
    await page.goto(f"{base_url}/default.aspx", wait_until="networkidle")
    await page.wait_for_timeout(1500)
    logger.info("Session established at %s", base_url)


async def _navigate_to_search(page: Page, base_url: str, search_id: int) -> None:
    """Navigate to the case search page via menu link."""
    # Click the appropriate menu link
    if search_id == 200:
        link = page.locator('a:has-text("Civil, Family & Probate")')
    else:
        link = page.locator(f'a[href*="ID={search_id}"]')

    if await link.count() > 0:
        await link.first.click()
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)
    else:
        # Fallback: direct navigation (may lose session)
        await page.evaluate(f'window.location.href = "Search.aspx?ID={search_id}"')
        await page.wait_for_load_state("domcontentloaded")
        await page.wait_for_timeout(1500)


async def _submit_date_search(
    page: Page, from_date: datetime, to_date: datetime
) -> int:
    """Select Date Filed search, fill dates, submit. Returns record count."""
    # Select "Date Filed" radio
    await page.check("#DateFiled")
    await page.wait_for_timeout(300)

    # Fill date range (MM/DD/YYYY format)
    from_str = from_date.strftime("%m/%d/%Y")
    to_str = to_date.strftime("%m/%d/%Y")
    await page.fill("#DateFiledOnAfter", from_str)
    await page.fill("#DateFiledOnBefore", to_str)

    logger.info("Date range: %s to %s", from_str, to_str)

    # Submit
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
        await page.click("#SearchSubmit")
    await page.wait_for_timeout(2000)

    # Extract record count from "Record Count: NNN"
    body = await page.inner_text("body")
    count_match = re.search(r"Record\s+Count:\s*(\d+)", body)
    total = int(count_match.group(1)) if count_match else 0
    logger.info("Search returned %d total civil/family/probate records", total)
    return total


async def _parse_results_page(page: Page) -> list[dict]:
    """Parse case rows from the current results page using JS cell extraction.

    PublicAccess grid has 4 cells per row:
      cell[0]: Case number (e.g., "26-0407-CP4")
      cell[1]: Title (e.g., "Estate Of\\nDanita Joyce Futch,\\nDeceased")
      cell[2]: Date + Court merged (e.g., "04/01/2026County Court at Law #4")
      cell[3]: Case type + Status merged (e.g., "Letters TestamentaryFiled")

    Returns list of dicts filtered to probate cases.
    """
    # Extract all rows with CaseDetail links via JS (much faster than Playwright locators)
    raw_rows = await page.evaluate("""
        (() => {
            var rows = document.querySelectorAll('tr');
            var results = [];
            rows.forEach(row => {
                var link = row.querySelector('a[href*="CaseDetail"]');
                if (!link) return;
                var cells = row.querySelectorAll('td');
                var cellTexts = [];
                cells.forEach(c => cellTexts.push(c.textContent.trim()));
                results.push({
                    caseNum: link.textContent.trim(),
                    href: link.getAttribute('href') || '',
                    cells: cellTexts
                });
            });
            return results;
        })()
    """)

    records = []
    for raw in raw_rows:
        cells = raw.get("cells", [])
        if len(cells) < 4:
            continue

        case_number = raw["caseNum"]
        case_title = cells[1] if len(cells) > 1 else ""

        # Parse date from cell[2] — format "MM/DD/YYYYCourt Name"
        date_court = cells[2] if len(cells) > 2 else ""
        filed_date = ""
        court = ""
        date_match = re.match(r"(\d{2}/\d{2}/\d{4})(.*)", date_court)
        if date_match:
            filed_date = date_match.group(1)
            court = date_match.group(2).strip()

        # Parse case type from cell[3] — format "Case TypeStatus"
        type_status = cells[3] if len(cells) > 3 else ""
        case_type = re.sub(r"(?:Filed|Closed|Pending|Disposed|Active)\s*$", "", type_status).strip()

        is_probate = _is_probate_case(case_number, case_title, case_type)

        records.append({
            "case_number": case_number,
            "case_title": case_title,
            "filed_date": filed_date,
            "court": court,
            "case_type": case_type,
            "detail_url": raw["href"],
            "is_probate": is_probate,
        })

    return records


def _build_notice(record: dict, base_url: str, county: str) -> NoticeData | None:
    """Build a NoticeData from a probate case grid record.

    Extracts decedent name from the case title. PR/executor name will be
    populated later by the enrichment pipeline (obituary search).
    """
    notice = NoticeData(
        notice_type="probate",
        county=county,
        state="TX",
        source_url=f"{base_url}/{record.get('detail_url', '')}",
    )

    # Date filed
    filed_date = record.get("filed_date", "")
    if filed_date:
        try:
            dt = datetime.strptime(filed_date, "%m/%d/%Y")
            notice.date_added = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Decedent name from title ("Estate Of NAME, Deceased")
    case_title = record.get("case_title", "")
    decedent = _extract_decedent_name(case_title)
    if decedent:
        notice.decedent_name = decedent
    elif _GUARDIANSHIP_RE.search(case_title):
        # Skip guardianship cases — we only want estate/probate
        return None

    # Store case info as raw text for enrichment pipeline
    notice.raw_text = (
        f"Case: {record.get('case_number', '')}\n"
        f"Title: {case_title}\n"
        f"Type: {record.get('case_type', '')}\n"
        f"Court: {record.get('court', '')}\n"
        f"Filed: {filed_date}"
    )

    # Only return if we got a decedent name
    if not notice.decedent_name:
        return None

    return notice


@register("Williamson", "probate")
class WilliamsonProbateScraper:
    """Scrape probate cases from Williamson County Odyssey PublicAccess."""

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
                from_date = to_date - timedelta(days=30)
        elif mode == "historical":
            from_date = to_date - timedelta(days=365)
        else:
            from_date = to_date - timedelta(days=30)

        logger.info(
            "Williamson probate scrape: mode=%s, range=%s to %s",
            mode, from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d"),
        )

        all_notices: list[NoticeData] = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=True,
            )
            page = await context.new_page()
            page.set_default_timeout(30000)

            try:
                # Step 1: Establish session
                await _establish_session(page, WILCO_BASE)

                # Step 2: Navigate to Civil/Family/Probate search
                await _navigate_to_search(page, WILCO_BASE, WILCO_SEARCH_ID)

                # Step 3: Submit date-based search
                total = await _submit_date_search(page, from_date, to_date)
                if total == 0:
                    await browser.close()
                    return []

                # Step 4: Parse results and filter to probate cases
                all_records = await _parse_results_page(page)
                probate_records = [r for r in all_records if r["is_probate"]]

                logger.info(
                    "Found %d probate cases out of %d total records",
                    len(probate_records), len(all_records),
                )

                if not probate_records:
                    await browser.close()
                    return []

                # Step 5: Build notices from grid data (no detail page visits)
                for record in probate_records:
                    if max_notices and len(all_notices) >= max_notices:
                        break

                    notice = _build_notice(record, WILCO_BASE, "Williamson")
                    if notice:
                        all_notices.append(notice)

            except Exception as e:
                logger.error("Williamson probate scraper failed: %s", e)
                raise
            finally:
                await browser.close()

        logger.info(
            "Williamson probate scrape complete: %d notices from %d probate cases",
            len(all_notices), len(probate_records) if 'probate_records' in dir() else 0,
        )
        return all_notices

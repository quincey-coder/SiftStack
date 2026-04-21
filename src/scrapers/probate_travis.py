"""Travis County probate scraper — tccsearch.org (Travis County Clerk).

Scrapes probate filings from the Travis County Clerk's real estate records
index. Supplements the Odyssey portal data with instrument numbers and
recording dates from the clerk's filing records.

Doc types searched:
  - 25: CERT COPY OF PROBATE (main probate filings)
  - 4:  AFFIDAVIT OF HEIRSHIP (heir determination filings)

Data extracted from results grid:
  - [R] name = decedent name (with "DECD" suffix)
  - Filing date = date the probate was recorded
  - Instrument number (for cross-referencing)
  - No property address (probate filings don't include LOC)

Reuses the same tccsearch.org infrastructure as foreclosure_travis.py.
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright, Page

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tccsearch.org"
SEARCH_URL = f"{BASE_URL}/RealEstate/SearchEntry.aspx"

# Document type checkbox indices
DOC_TYPE_PROBATE = 25   # "CERT COPY OF PROBATE"
DOC_TYPE_HEIRSHIP = 4   # "AFFIDAVIT OF HEIRSHIP"

# Selectors (shared with foreclosure_travis.py)
SEL_DISCLAIMER_ACCEPT = "#cph1_lnkAccept"
SEL_DATE_FROM_ID = "cphNoMargin_f_ddcDateFiledFrom"
SEL_DATE_TO_ID = "cphNoMargin_f_ddcDateFiledTo"

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0

# Extract decedent name from "[R] LASTNAME FIRSTNAME DECD (+)"
_GRANTOR_RE = re.compile(r"\[R\]\s*(.+?)(?:\s*\(\+\))?$")
_DECD_SUFFIX_RE = re.compile(r"\s+DECD\.?$", re.IGNORECASE)


def _doc_type_selector(index: int) -> str:
    return f"#cphNoMargin_f_dclDocType_{index}"


def _date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _clean_decedent_name(raw: str) -> str:
    """Clean up a decedent name from the clerk's index.

    Input:  "BEATY MARY VIRGINIA DECD"
    Output: "Beaty Mary Virginia"
    """
    name = raw.strip()
    # Remove "DECD" suffix
    name = _DECD_SUFFIX_RE.sub("", name).strip()
    # Title case
    if name.isupper() or name.islower():
        name = name.title()
    return name


async def _accept_disclaimer(page: Page) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    accept = page.locator(SEL_DISCLAIMER_ACCEPT)
    if await accept.count() > 0:
        await accept.click()
        await page.wait_for_load_state("domcontentloaded")
        logger.info("Disclaimer accepted")


async def _set_date_range(page: Page, from_date: datetime, to_date: datetime) -> None:
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
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=30000):
        await page.evaluate(
            "document.getElementById('cphNoMargin_SearchButtons1_btnSearch').click()"
        )
    await page.wait_for_timeout(2000)

    body_text = await page.inner_text("body")
    count_match = re.search(r"(\d+)\s+records?\s+found", body_text)
    total = int(count_match.group(1)) if count_match else 0
    logger.info("Search returned %d records", total)
    return total


async def _parse_results_page(page: Page) -> list[NoticeData]:
    """Parse probate result rows from the current page."""
    notices = []
    body_text = await page.inner_text("body")
    lines = body_text.split("\n")
    lines = [l.strip() for l in lines if l.strip()]

    i = 0
    while i < len(lines):
        if not lines[i].isdigit():
            i += 1
            continue

        # Collect lines for this record
        record_lines = []
        j = i + 1
        while j < len(lines) and j < i + 8:
            if lines[j].isdigit() and j > i + 2:
                break
            record_lines.append(lines[j])
            j += 1

        data_lines = [l for l in record_lines if l != "View"]
        notice = _parse_probate_record(data_lines)
        if notice:
            notices.append(notice)

        i = j

    return notices


def _parse_probate_record(lines: list[str]) -> NoticeData | None:
    """Parse a single probate result record."""
    if len(lines) < 2:
        return None

    notice = NoticeData(
        notice_type="probate",
        county="Travis",
        state="TX",
    )

    # Find instrument number (9+ digit number)
    instrument = ""
    for line in lines:
        if re.match(r"^\d{9,}$", line):
            instrument = line
            break

    if instrument:
        notice.source_url = f"{BASE_URL}/RealEstate/DocumentDetail.aspx?InstrumentNumber={instrument}"

    # Find date filed
    for line in lines:
        date_match = re.match(r"(\d{2}/\d{2}/\d{4})\s+", line)
        if date_match:
            try:
                dt = datetime.strptime(date_match.group(1), "%m/%d/%Y")
                notice.date_added = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
            break

    # Find decedent name from [R] field
    for line in lines:
        m = _GRANTOR_RE.search(line)
        if m:
            from notice_parser import normalize_court_name
            raw_name = m.group(1).strip()
            notice.decedent_name = normalize_court_name(_clean_decedent_name(raw_name))
            break

    # Determine doc subtype from the line
    for line in lines:
        if "HEIRSHIP" in line.upper():
            notice.raw_text = f"Heirship filing\n" + "\n".join(lines)
            break
    else:
        notice.raw_text = "\n".join(lines)

    # Only return if we got a decedent name
    if not notice.decedent_name:
        return None

    return notice


async def _go_to_page(page: Page, page_num: int) -> bool:
    sel = page.locator("#cphNoMargin_cphNoMargin_OptionsBar1_ItemList")
    if await sel.count() == 0:
        return False
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


@register("Travis", "probate")
class TravisProbateScraper:
    """Scrape probate filings from tccsearch.org."""

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
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
            "Travis probate scrape: mode=%s, range=%s to %s",
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
            )
            page = await context.new_page()
            page.set_default_timeout(30000)

            try:
                await _accept_disclaimer(page)

                # Search for CERT COPY OF PROBATE + AFFIDAVIT OF HEIRSHIP
                await page.goto(SEARCH_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(1500)

                await page.check(_doc_type_selector(DOC_TYPE_PROBATE))
                await page.check(_doc_type_selector(DOC_TYPE_HEIRSHIP))
                logger.info("Checked doc types: CERT COPY OF PROBATE + AFFIDAVIT OF HEIRSHIP")

                await _set_date_range(page, from_date, to_date)
                total = await _submit_search(page)

                if total == 0:
                    await browser.close()
                    return []

                # Parse all pages
                current_page = 1
                while True:
                    logger.info("Parsing page %d...", current_page)
                    page_notices = await _parse_results_page(page)
                    all_notices.extend(page_notices)
                    logger.info(
                        "  Page %d: %d notices (total: %d)",
                        current_page, len(page_notices), len(all_notices),
                    )

                    if max_notices and len(all_notices) >= max_notices:
                        all_notices = all_notices[:max_notices]
                        break

                    current_page += 1
                    if not await _go_to_page(page, current_page):
                        break

                    await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

            except Exception as e:
                logger.error("Travis probate scraper failed: %s", e)
                raise
            finally:
                await browser.close()

        logger.info(
            "Travis probate scrape complete: %d notices from %d records",
            len(all_notices), total,
        )
        return all_notices

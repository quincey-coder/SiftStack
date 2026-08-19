"""Travis County lis pendens scraper — tccsearch.org (Travis County Clerk OPR).

Scrapes recorded **lis pendens** filings (Tex. Prop. Code § 12.007 — notice of a
pending suit affecting title to real property) from the Travis County Clerk
real-estate records index. Same ASP.NET WebForms site as foreclosure_travis.py /
lien_travis.py — no CAPTCHA, no login (behind Cloudflare; see tccsearch_common).

Site: https://www.tccsearch.org/RealEstate/SearchResults.aspx

KEY FACTS (probed live 2026-06-27, see project_lis_pendens memory):
  - Doc-type checkbox **index 63 = "LIS PENDENS"** (the lead). A 120-day search
    returned ~137 results (~45/mo). Amendments (idx 6), partial releases (82) and
    releases (99) have their OWN checkboxes and are simply NOT requested here; the
    parser also drops any that leak through (see _is_lis_pendens_doctype).
  - The LEAD is the DEFENDANT (property owner being sued), recorded as the
    grantee [E] on the common convention — NOT the plaintiff [R] who filed. Party
    roles vary by case type (HOA / lender / taxing-unit / individual plaintiffs),
    so `lis_pendens_common.pick_defendant` picks the non-plaintiff party.
  - Unlike liens, a lis pendens is tied to a SPECIFIC property, so many records
    carry a legal description ("LOC ...") we parse to an address inline. Records
    with no parseable address fall through to CAD name lookup (enrichment
    Step 3c) using the defendant name.

Flow mirrors TravisLienScraper: disclaimer → search → check doc type 63 →
date range → submit → parse grid → paginate.
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright, Page

from notice_parser import NoticeData, normalize_court_name
from scrapers import register
from scrapers.lis_pendens_common import pick_defendant
from scrapers.tccsearch_common import (
    click_search,
    count_temp_rows,
    effective_from_date,
    goto_with_retry,
    launch_tcc_context,
    pass_cloudflare,
    safe_check,
    wait_ready,
)
# Reuse the foreclosure LOC-address parser for the legal descriptions that lis
# pendens records commonly carry.
from scrapers.foreclosure_travis import _parse_address_from_legal

logger = logging.getLogger(__name__)

BASE_URL = "https://www.tccsearch.org"
SEARCH_URL = f"{BASE_URL}/RealEstate/SearchEntry.aspx"

# Document-type checkbox index for LIS PENDENS (captured live; see docstring).
LIS_PENDENS_DOC_TYPE = 63

SEL_DISCLAIMER_ACCEPT = "#cph1_lnkAccept"
SEL_DATE_FROM_ID = "cphNoMargin_f_ddcDateFiledFrom"
SEL_DATE_TO_ID = "cphNoMargin_f_ddcDateFiledTo"

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0

_INSTRUMENT_RE = re.compile(r"(\d{8,})")
_DATE_LINE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}\t")
_DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})")
_GRANTOR_RE = re.compile(r"\[R\]\s*(.+?)(?:\s*\(\+\))?$")   # plaintiff (filer)
_GRANTEE_RE = re.compile(r"\[E\]\s*(.+?)(?:\s*\(\+\))?$")   # defendant (lead)

# Doc-type strings that ARE a live lis pendens (drop amendments / releases /
# cancellations that share the grid, so a stale-status filing isn't marketed).
_LP_EXCLUDE = (
    "RELEASE", "WITHDRAWAL", "PARTIAL", "AMEND", "CANCEL", "EXPUNGE",
    "DISMISS", "NON-SUIT", "NONSUIT",
)


def _doc_type_selector(index: int) -> str:
    return f"#cphNoMargin_f_dclDocType_{index}"


def _date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _clean_name(raw: str) -> str:
    name = raw.strip().rstrip(",;.")
    if name.isupper() or name.islower():
        name = name.title()
    return name


def _is_lis_pendens_doctype(raw: str) -> bool:
    up = (raw or "").upper()
    if "LIS PENDENS" not in up:
        return False
    return not any(x in up for x in _LP_EXCLUDE)


async def _accept_disclaimer(page: Page) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    await pass_cloudflare(page)
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
    async with page.expect_navigation(wait_until="domcontentloaded", timeout=40000):
        await click_search(page)  # null-guarded + retried
    await page.wait_for_timeout(2000)
    body_text = await page.inner_text("body")
    count_match = re.search(r"(\d+)\s+records?\s+found", body_text)
    total = int(count_match.group(1)) if count_match else 0
    logger.info("Lis pendens search returned %d records", total)
    return total


def _parse_record(instrument: str, block: list[str]) -> NoticeData | None:
    """Parse one lis pendens record block into NoticeData.

    block is the set of grid lines after the row/instrument line, e.g.:
        ["06/29/2026\tLIS PENDENS\t[R] OAK RUN OWNERS ASSOCIATION",
         "[E] MOLINA RAMON G",
         "LOC 123 MAIN ST AUSTIN TX 78701",
         "Temp"]
    """
    date_iso = ""
    doc_type = ""
    grantor = ""   # [R] party (usually the plaintiff/filer)
    grantee = ""   # [E] party (usually the defendant/owner)
    address = city = zip_code = ""

    for line in block:
        dm = _DATE_RE.match(line)
        if dm and not date_iso:
            try:
                date_iso = datetime.strptime(dm.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
            parts = line.split("\t")
            if len(parts) >= 2:
                doc_type = parts[1].strip()
            gm = _GRANTOR_RE.search(line)
            if gm:
                grantor = gm.group(1).strip()
            continue
        em = _GRANTEE_RE.search(line)
        if em and not grantee:
            grantee = em.group(1).strip()
            continue
        # Lis pendens usually carries a property legal description.
        if "LOC " in line.upper() and not address:
            address, city, zip_code = _parse_address_from_legal(line)

    # Drop amendments / releases / dismissals that leak into the grid.
    if doc_type and not _is_lis_pendens_doctype(doc_type):
        return None

    defendant, plaintiff = pick_defendant(grantor=grantor, grantee=grantee)
    if not defendant:
        return None

    notice = NoticeData(notice_type="lis_pendens", county="Travis", state="TX")
    notice.source_url = (
        f"{BASE_URL}/RealEstate/DocumentDetail.aspx?InstrumentNumber={instrument}"
    )
    notice.date_added = date_iso
    # Plaintiff surfaced in Notes as context (WHY they're distressed); the suit
    # type is always "Lis Pendens" so it's left off lien_type to avoid a
    # redundant tag (the notice_type tag already reads "Lis Pendens").
    notice.lien_creditor = _clean_name(plaintiff)
    # Preserve pristine county-record defendant name for CAD name search;
    # owner_name is the cleaned + FIRST-LAST display form.
    notice.tax_owner_name = defendant.upper()
    notice.owner_name = normalize_court_name(_clean_name(defendant))
    if address:
        notice.address = address
        notice.city = city
        notice.zip = zip_code
    notice.raw_text = "\n".join(block)
    return notice


def _parse_body_text(body_text: str) -> list[NoticeData]:
    """Pure parser over the results-grid inner_text (unit-testable).

    Anchors on each record's DATE line (present in both Temp and Perm grid
    layouts) and recovers the instrument from the preceding lines — identical
    grid shape to lien_travis.
    """
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    n = len(lines)

    notices: list[NoticeData] = []
    for idx, line in enumerate(lines):
        if not _DATE_LINE_RE.match(line):
            continue

        instrument = ""
        for k in range(idx - 1, max(-1, idx - 5), -1):
            m = _INSTRUMENT_RE.search(lines[k])
            if m:
                instrument = m.group(1)
                break
        if not instrument:
            continue

        block: list[str] = [line]
        j = idx + 1
        while j < n and not _DATE_LINE_RE.match(lines[j]) and j < idx + 6:
            block.append(lines[j])
            j += 1

        notice = _parse_record(instrument, block)
        if notice:
            notices.append(notice)

    return notices


async def _parse_results_page(page: Page) -> list[NoticeData]:
    body_text = await page.inner_text("body")
    return _parse_body_text(body_text)


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


@register("Travis", "lis_pendens")
class TravisLisPendensScraper:
    """Scrape lis pendens filings from tccsearch.org results grid (Travis County)."""

    def __init__(self, doc_type: int | None = None):
        self.doc_type = doc_type or LIS_PENDENS_DOC_TYPE

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
                logger.error("Invalid since_date: %s", since_date)
                from_date = to_date - timedelta(days=30)
        elif mode == "historical":
            from_date = to_date - timedelta(days=365)
        else:
            from_date = to_date - timedelta(days=30)

        # Cover the clerk's Temp-index lag (see tccsearch_common
        # .effective_from_date) — a 1-day window parses to zero forever.
        from_date = effective_from_date(from_date, to_date, mode)

        logger.info(
            "Travis lis pendens scrape: mode=%s, range=%s to %s",
            mode, from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d"),
        )

        all_notices: list[NoticeData] = []
        total = 0

        async with async_playwright() as p:
            browser, context, page = await launch_tcc_context(p)
            try:
                await _accept_disclaimer(page)
                await goto_with_retry(page, SEARCH_URL)
                await page.wait_for_timeout(1500)
                await wait_ready(page)

                if not await safe_check(page, _doc_type_selector(self.doc_type)):
                    from scrapers import ScraperError
                    raise ScraperError(
                        "Travis lis pendens: doc-type checkbox not checkable — "
                        "search would be misfiltered"
                    )
                await _set_date_range(page, from_date, to_date)

                total = await _submit_search(page)
                self.last_meta = {
                    "returned": total,
                    "window_days": (to_date - from_date).days + 1,
                }
                if total == 0:
                    await browser.close()
                    return []

                current_page = 1
                while True:
                    logger.info("Parsing lis pendens page %d...", current_page)
                    page_notices = await _parse_results_page(page)
                    all_notices.extend(page_notices)
                    logger.info(
                        "  Page %d: %d lis pendens (total so far: %d)",
                        current_page, len(page_notices), len(all_notices),
                    )

                    if max_notices and len(all_notices) >= max_notices:
                        all_notices = all_notices[:max_notices]
                        logger.info("Reached max_notices limit (%d)", max_notices)
                        break

                    current_page += 1
                    if not await _go_to_page(page, current_page):
                        logger.info("No more pages (stopped at page %d)", current_page - 1)
                        break

                    await asyncio.sleep(random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX))

                if total > 0 and not all_notices:
                    # 100% parse rejection. Expected ONLY when every row is
                    # still in the clerk's Temp index (no names yet) — anything
                    # else is the silent regression class this guards against.
                    body_text = await page.inner_text("body")
                    temp_rows = count_temp_rows(body_text)
                    if temp_rows >= total:
                        logger.info(
                            "Travis lis pendens: all %d record(s) still in the "
                            "Temp index (names not yet attached) — the lookback "
                            "window will pick them up once verified", total,
                        )
                        self.last_meta["temp_pending"] = total
                        self.last_meta["returned"] = 0  # not parseable yet
                    else:
                        from scrapers import ScraperError
                        from scrapers.debug_capture import dump_page
                        await dump_page(page, "travis_lis_pendens_parse_zero")
                        raise ScraperError(
                            f"Travis lis pendens: {total} records found but 0 "
                            f"parsed ({temp_rows} Temp) — grid/parser regression"
                        )

            except Exception as e:
                logger.error("Travis lis pendens scraper failed: %s", e)
                raise
            finally:
                await browser.close()

        self.last_meta["kept"] = len(all_notices)
        logger.info(
            "Travis lis pendens scrape complete: %d records from %d total",
            len(all_notices), total,
        )
        return all_notices

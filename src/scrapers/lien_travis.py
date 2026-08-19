"""Travis County lien scraper — tccsearch.org (Travis County Clerk OPR).

Scrapes recorded **lien** filings (Abstract of Judgment, Federal/State Tax Lien,
Mechanic's Lien, etc.) from the Travis County Clerk real-estate records index.
Same ASP.NET WebForms site as foreclosure_travis.py — no CAPTCHA, no login.

Site: https://www.tccsearch.org/RealEstate/SearchResults.aspx

KEY DIFFERENCES FROM FORECLOSURE (learned from live data 2026-06-29):
  - Liens are NAME-INDEXED with NO property address (blank Legal Description).
    The property address is backfilled downstream by CAD name search
    (enrichment_pipeline Step 3c) using the debtor name.
  - The LEAD is the DEBTOR, recorded as the Associated Name [E] — NOT the
    creditor [R] who filed the lien. Grid example:
        ABSTRACT OF JUDGMENT   [R] CITIBANK        <- creditor (filer, ignore)
                               [E] MOLINA RAMON G  <- debtor   (our lead)
  - State Tax Liens are overwhelmingly business franchise/sales-tax liens
    (WORKDAY INC, ... LLC). They still ride through; the CAD name lookup simply
    finds no in-county residential property for most, so they drop out naturally.

Doc-type checkbox indices (captured live from SearchEntry.aspx, 130 types total):
    1  ABSTRACT OF JUDGMENT          51  FEDERAL TAX LIENS AND NOTICES
    14 ASSESSMENT LIEN (HOA)         56  HOSPITAL LIEN
    19 BROKERS LIEN                  60  JUDGEMENT
    27 CHILD SUPPORT LIEN            65  MECHANICS LIEN
    49 ESTATE TAX LIEN              106  STATE JUDGEMENT
    62 LIEN (generic)              107  STATE TAX LIEN
Releases / partial releases / transfers are intentionally excluded — they mean
a lien is going AWAY, not a fresh distress signal.

Flow mirrors TravisForeclosureScraper: disclaimer → search → check doc types →
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
from scrapers.lien_common import pick_debtor
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

# Document-type checkbox index → human label (the lien lead types we pull).
LIEN_DOC_TYPES: dict[int, str] = {
    1: "Abstract Of Judgment",
    14: "Assessment Lien",
    19: "Brokers Lien",
    27: "Child Support Lien",
    49: "Estate Tax Lien",
    51: "Federal Tax Lien",
    56: "Hospital Lien",
    60: "Judgement",
    62: "Lien",
    65: "Mechanics Lien",
    106: "State Judgement",
    107: "State Tax Lien",
}

# Default doc types to check each run. Tunable — the high-signal residential
# distress liens lead; State Tax Lien is included but mostly business (filtered
# out downstream when no in-county home is found for the debtor).
DEFAULT_LIEN_DOC_TYPES: list[int] = [1, 14, 49, 51, 56, 60, 65, 106, 107]

SEL_DISCLAIMER_ACCEPT = "#cph1_lnkAccept"
SEL_DATE_FROM_ID = "cphNoMargin_f_ddcDateFiledFrom"
SEL_DATE_TO_ID = "cphNoMargin_f_ddcDateFiledTo"

REQUEST_DELAY_MIN = 1.0
REQUEST_DELAY_MAX = 2.0

# An instrument number in the grid ("2026076222"), 8+ digits.
_INSTRUMENT_RE = re.compile(r"(\d{8,})")
# The tccsearch grid renders each record in one of TWO layouts depending on
# indexing status, and we must parse BOTH:
#   • Temp (freshly filed, not yet indexed):
#         "1\t\t\t2026086312"                 ← row# + instrument on ONE line
#         "07/21/2026\tABSTRACT OF JUDGMENT\t[R] ..."
#   • Perm (indexed — the records we actually want, they carry party names):
#         "1"                                  ← row# alone
#         "View"                               ← image link
#         "2026078204"                         ← instrument on its OWN line
#         "07/01/2026\tABSTRACT OF JUDGMENT\t[R] CAPITAL ONE (+)"
# Anchoring on the row-number line misses Perm rows (after strip() "1\t" → "1",
# no tab). Instead we anchor on the always-present DATE line and recover the
# instrument from the immediately preceding lines — layout-agnostic.
_DATE_LINE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}\t")
# Date + doc type + creditor: "06/29/2026\tABSTRACT OF JUDGMENT\t[R] CITIBANK"
_DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})")
_GRANTOR_RE = re.compile(r"\[R\]\s*(.+?)(?:\s*\(\+\))?$")   # creditor (filer)
_GRANTEE_RE = re.compile(r"\[E\]\s*(.+?)(?:\s*\(\+\))?$")   # debtor (lead)

# Reuse the foreclosure LOC-address parser for the rare lien (e.g. mechanics)
# that carries a property legal description.
from scrapers.foreclosure_travis import _parse_address_from_legal  # noqa: E402


def _doc_type_selector(index: int) -> str:
    return f"#cphNoMargin_f_dclDocType_{index}"


def _date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _clean_name(raw: str) -> str:
    name = raw.strip().rstrip(",;.")
    if name.isupper() or name.islower():
        name = name.title()
    return name


def _normalize_doc_type(raw: str) -> str:
    """Map a grid doc-type label to a clean display lien_type."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    # Prefer our curated label when the grid text matches a known type.
    up = raw.upper()
    for label in LIEN_DOC_TYPES.values():
        if label.upper() == up:
            return label
    return raw.title()


async def _accept_disclaimer(page: Page) -> None:
    await page.goto(BASE_URL, wait_until="domcontentloaded")
    # Clear the Cloudflare interstitial on first navigation (sets cf_clearance).
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
    logger.info("Lien search returned %d records", total)
    return total


def _parse_record(instrument: str, block: list[str]) -> NoticeData | None:
    """Parse one lien record block into NoticeData.

    block is the set of grid lines after the row/instrument line, up to the
    next record. Expected shape:
        ["06/29/2026\tABSTRACT OF JUDGMENT\t[R] CITIBANK",
         "[E] MOLINA RAMON G",
         "Temp"]
    """
    date_iso = ""
    lien_type = ""
    grantor = ""   # [R] party (usually the creditor/filer)
    grantee = ""   # [E] party (usually the debtor)
    address = city = zip_code = ""

    for line in block:
        # Date + doc type + creditor line
        dm = _DATE_RE.match(line)
        if dm and not date_iso:
            try:
                date_iso = datetime.strptime(dm.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            except ValueError:
                pass
            parts = line.split("\t")
            if len(parts) >= 2:
                lien_type = _normalize_doc_type(parts[1])
            gm = _GRANTOR_RE.search(line)
            if gm:
                grantor = gm.group(1).strip()
            continue
        # Associated Name [E]
        em = _GRANTEE_RE.search(line)
        if em and not grantee:
            grantee = em.group(1).strip()
            continue
        # Rare: a mechanics/assessment lien with a property legal description
        if "LOC " in line.upper() and not address:
            address, city, zip_code = _parse_address_from_legal(line)

    # The institutional creditor may sit on either side of the index; pick the
    # real debtor (the non-creditor party) as the lead.
    debtor, creditor = pick_debtor(grantor=grantor, grantee=grantee)
    if not debtor:
        # No party to pursue
        return None

    notice = NoticeData(notice_type="lien", county="Travis", state="TX")
    notice.source_url = (
        f"{BASE_URL}/RealEstate/DocumentDetail.aspx?InstrumentNumber={instrument}"
    )
    notice.date_added = date_iso
    notice.lien_type = lien_type
    notice.lien_creditor = _clean_name(creditor)
    # Preserve the pristine county-record debtor name for deep prospecting /
    # CAD name search; owner_name is the cleaned display form.
    notice.tax_owner_name = debtor.upper()
    # Title-case BEFORE flipping (matches Bell/Williamson lien scrapers). The
    # grid debtor is raw ALL-CAPS "LAST FIRST MIDDLE"; without _clean_name the
    # emitted owner_name stays ALL-CAPS, which the NAMELF rules flag as a
    # probably-unnormalized/reversed value.
    notice.owner_name = normalize_court_name(_clean_name(debtor))
    if address:
        notice.address = address
        notice.city = city
        notice.zip = zip_code
    notice.raw_text = "\n".join(block)
    return notice


def _parse_body_text(body_text: str) -> list[NoticeData]:
    """Pure parser over the results-grid inner_text (unit-testable).

    Anchors on each record's DATE line (present in both the Temp and Perm grid
    layouts — see _DATE_LINE_RE) rather than the row-number line, which the Perm
    layout splits away from the instrument. The instrument number is recovered
    from the handful of lines immediately preceding the date line, and the date
    line plus the next few lines (grantor [R], grantee [E], legal/status) form
    the record block handed to _parse_record.
    """
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]
    n = len(lines)

    notices: list[NoticeData] = []
    for idx, line in enumerate(lines):
        if not _DATE_LINE_RE.match(line):
            continue

        # Instrument = nearest 8+ digit number in the up-to-4 preceding lines
        # ("2026078204" on its own line for Perm, "1\t\t\t2026086312" for Temp).
        instrument = ""
        for k in range(idx - 1, max(-1, idx - 5), -1):
            m = _INSTRUMENT_RE.search(lines[k])
            if m:
                instrument = m.group(1)
                break
        if not instrument:
            continue

        # Block = the date line + following lines up to the next date line
        # (capped) — carries [R]/[E]/legal for _parse_record.
        block: list[str] = [line]
        j = idx + 1
        while j < n and not _DATE_LINE_RE.match(lines[j]) and j < idx + 5:
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


@register("Travis", "lien")
class TravisLienScraper:
    """Scrape lien filings from tccsearch.org results grid (Travis County)."""

    def __init__(self, doc_types: list[int] | None = None):
        self.doc_types = doc_types or DEFAULT_LIEN_DOC_TYPES

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

        # Cover the clerk's Temp-index lag so paper-filed liens that were
        # nameless on day 1 get re-scanned once verified (dedup absorbs repeats).
        from_date = effective_from_date(from_date, to_date, mode)

        logger.info(
            "Travis lien scrape: mode=%s, range=%s to %s, doc_types=%s",
            mode, from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d"),
            [LIEN_DOC_TYPES.get(d, d) for d in self.doc_types],
        )

        all_notices: list[NoticeData] = []
        total = 0

        async with async_playwright() as p:
            # Cloudflare-resistant headed launch (see tccsearch_common).
            browser, context, page = await launch_tcc_context(p)

            try:
                await _accept_disclaimer(page)
                await goto_with_retry(page, SEARCH_URL)
                await page.wait_for_timeout(1500)
                # Fail fast (~12s) with a clear message if the client framework
                # was withheld (datacenter-IP block) instead of dying on $find.
                await wait_ready(page)

                checked = 0
                for idx in self.doc_types:
                    if await safe_check(page, _doc_type_selector(idx)):
                        checked += 1
                if checked == 0:
                    from scrapers import ScraperError
                    raise ScraperError(
                        "Travis lien: NO doc-type checkbox could be checked — "
                        "search would be unfiltered"
                    )
                if checked < len(self.doc_types):
                    logger.warning(
                        "Travis lien: only %d/%d doc-type checkboxes checked — "
                        "results will be partial", checked, len(self.doc_types),
                    )

                await _set_date_range(page, from_date, to_date)

                total = await _submit_search(page)
                self.last_meta = {
                    "returned": total,
                    "window_days": (to_date - from_date).days + 1,
                    "doctypes_checked": checked,
                }
                if total == 0:
                    await browser.close()
                    return []

                current_page = 1
                while True:
                    logger.info("Parsing lien page %d...", current_page)
                    page_notices = await _parse_results_page(page)
                    all_notices.extend(page_notices)
                    logger.info(
                        "  Page %d: %d liens (total so far: %d)",
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

            except Exception as e:
                logger.error("Travis lien scraper failed: %s", e)
                raise
            finally:
                await browser.close()

        self.last_meta["kept"] = len(all_notices)
        logger.info(
            "Travis lien scrape complete: %d liens from %d total records",
            len(all_notices), total,
        )
        return all_notices

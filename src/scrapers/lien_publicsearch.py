"""Bell + Williamson lien scraper — publicsearch.us (Kofile/GovOS County Fusion).

Both county clerks run GovOS "County Fusion" public search at
`{county}.tx.publicsearch.us`. This pulls recorded **lien** filings (Abstract of
Judgment, Federal/State Tax Lien, Mechanic's Lien, ...) via the Advanced Search.

╔═══════════════════════════════════════════════════════════════════════════╗
║ CRITICAL — MUST RUN HEADED. The portal's anti-bot blocks OLD HEADLESS       ║
║ chromium: the results page sticks on "Loading Search Results..." forever    ║
║ and the search API never fires. Verified live 2026-06-29:                   ║
║   headless=True  → 0 rows (perpetual loading)                               ║
║   headless=False → results render immediately                              ║
║ In Docker/Apify (Linux, no display) wrap the run in Xvfb:                   ║
║   xvfb-run -a python src/main.py daily --types lien --counties Bell         ║
║ Override with LIEN_PUBLICSEARCH_HEADLESS=1 (will likely yield 0 rows).      ║
╚═══════════════════════════════════════════════════════════════════════════╝

LEAD = the GRANTEE (debtor), NOT the grantor (creditor) — same rule as Travis
liens (a tax lien's grantor is the IRS/State; the grantee is the taxpayer we
want). Liens are name-indexed with no property address → the address is
backfilled by CAD name search in enrichment Step 3c-lien.

Hard-won UI facts (live probes 2026-06-29):
  - Advanced form ids: #recordedDateRange-start / -end (MM/DD/YYYY text inputs),
    #docTypes-input (react-select: type the name, press Enter to add a chip),
    #grantor-input / #grantee-input.
  - Submit = the button whose EXACT text is "Search" (NOT "Search Criteria",
    which is a section toggle — a has-text match grabs the wrong one).
  - Beamer push modal (#beamerPushModal) / NPS iframe block clicks — remove
    from DOM before interacting.
  - Results title becomes "Search Results - {County} County, Texas County Clerk".
  - Pagination: buttons with aria-label "next page" / "previous page".
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright, Page

from notice_parser import NoticeData, normalize_court_name
from scrapers import register
from scrapers.lien_common import pick_debtor

logger = logging.getLogger(__name__)

# Lien document-type names to request in Advanced Search. Names not offered by a
# given county's dropdown simply don't add a chip (logged, then skipped).
# These MUST be the EXACT leaf labels as they appear in the GovOS
# tokenized-nested-select (verified live against Bell, 2026-07-18). The picker
# and the results-table doc-type cell both use these canonical spellings:
#   - no apostrophe in "MECHANICS LIEN"
#   - child-support liens are indexed simply as "CHILD SUPPORT" (no "LIEN")
# Getting these wrong means the type is silently never selected (see
# _add_doc_types, which now requires an EXACT leaf match, not a substring).
LIEN_DOC_TYPE_NAMES = [
    "ABSTRACT OF JUDGMENT",
    "FEDERAL TAX LIEN",
    "STATE TAX LIEN",
    "MECHANICS LIEN",
    "HOSPITAL LIEN",
    "CHILD SUPPORT",
]

_DISMISS_JS = """() => {
  // Beamer / NPS overlays.
  ['#beamerPushModal', '#npsIframeContainer', '.beamer_overlay',
   '[class*="Beamer"]'].forEach(s => {
     document.querySelectorAll(s).forEach(e => e.remove());
  });
  // GovOS product-tour / welcome modal (intercepts clicks on the form). Click a
  // dismiss control if present, else remove the overlay node.
  const byText = [...document.querySelectorAll('button,a')].find(b => {
     const t = (b.innerText || '').trim().toLowerCase();
     return ['close','skip','no thanks','dismiss','maybe later','got it'].includes(t)
         || (b.getAttribute('aria-label') || '').toLowerCase() === 'close';
  });
  if (byText) { try { byText.click(); } catch (e) {} }
  ['[class*="tour" i]', '[class*="joyride" i]', '[class*="shepherd" i]',
   '[class*="walkthrough" i]', '[class*="onboarding" i]', '.reactour__mask',
   '[class*="Overlay" i]'].forEach(s => {
     document.querySelectorAll(s).forEach(e => e.remove());
  });
}"""

REQUEST_DELAY = 2.0
MAX_PAGES = 50  # hard safety cap on pagination (each page is ~20 rows)

_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")


def _force_headless() -> bool:
    return os.getenv("LIEN_PUBLICSEARCH_HEADLESS", "").strip().lower() in ("1", "true", "yes")


def _date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _clean_name(raw: str) -> str:
    name = (raw or "").strip().rstrip(",;.").strip()
    # Drop GovOS "(+N)" more-parties markers
    name = re.sub(r"\(\+\d*\)\s*$", "", name).strip()
    if name and (name.isupper() or name.islower()):
        name = name.title()
    return name


def _normalize_lien_type(raw: str) -> str:
    raw = (raw or "").strip()
    return raw.title() if raw else ""


def _parse_results_text(text: str, county: str) -> list[NoticeData]:
    """Parse the GovOS results table from the results-page innerText.

    The results render as a tab-separated table (live format, Bell 2026-06-29):

        GRANTOR  GRANTEE  DOC TYPE  RECORDED DATE  INST NUMBER  BOOK/VOL/PAGE  PROPERTY DESCRIPTION
        MONTEITH ABSTRACT CO  HALE DAVID ALLEN  ABSTRACT OF JUDGMENT  12/14/1967  1967001353  OPR/13/33  Property Description: $116.25

    Each data row is one innerText line with empty leading cells (the checkbox /
    image columns). We anchor on the DOC TYPE cell (a known lien type) and read:
      grantee  = cell immediately BEFORE doc type  -> the DEBTOR (our lead)
      grantor  = cell two before doc type           -> the creditor (context)
      date     = first date-shaped cell after doc type
      instrument = first 8+ digit cell after doc type
    The "Property Description" cell is a dollar amount for AJ/tax liens (not an
    address), so address stays blank -> filled by CAD name lookup (Step 3c-lien).
    """
    if not text:
        return []

    type_set = {t.upper() for t in LIEN_DOC_TYPE_NAMES}
    type_set |= {"ABSTRACTS OF JUDGMENT", "FEDERAL TAX LIENS", "STATE TAX LIENS",
                 "JUDGMENT", "JUDGEMENT", "ASSESSMENT LIEN"}

    notices: list[NoticeData] = []
    seen_inst: set[str] = set()
    for raw_line in text.split("\n"):
        if "\t" not in raw_line:
            continue
        cells = [c.strip() for c in raw_line.split("\t") if c.strip()]
        if len(cells) < 4:
            continue

        # Locate the doc-type cell.
        dt_idx = None
        for i, c in enumerate(cells):
            if c.upper() in type_set:
                dt_idx = i
                break
        if dt_idx is None or dt_idx < 1:
            continue

        lien_type_raw = cells[dt_idx]
        grantee = cells[dt_idx - 1]
        grantor = cells[dt_idx - 2] if dt_idx >= 2 else ""

        date_iso = ""
        instrument = ""
        for c in cells[dt_idx + 1:]:
            dm = _DATE_RE.search(c)
            if dm and not date_iso:
                try:
                    date_iso = datetime.strptime(dm.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
                except ValueError:
                    pass
            if re.fullmatch(r"\d{8,}", c) and not instrument:
                instrument = c

        # The institutional creditor (bank / debt-buyer / State) can be on either
        # side here — verified live: GRANTEE was "MIDLAND CREDIT MANAGEMENT INC"
        # (the creditor) and GRANTOR the individual debtor. Pick the real debtor.
        debtor, creditor = pick_debtor(grantor=grantor, grantee=grantee)
        if not debtor:
            continue
        if instrument and instrument in seen_inst:
            continue
        if instrument:
            seen_inst.add(instrument)

        notice = NoticeData(notice_type="lien", county=county, state="TX")
        notice.lien_type = _normalize_lien_type(lien_type_raw)
        notice.lien_creditor = _clean_name(creditor)
        notice.tax_owner_name = debtor.upper()
        notice.owner_name = normalize_court_name(_clean_name(debtor))
        notice.date_added = date_iso
        if instrument:
            notice.source_url = f"https://{county.lower()}.tx.publicsearch.us/doc/{instrument}"
        notice.raw_text = raw_line.strip()
        notices.append(notice)

    return notices


async def _dismiss_popups(page: Page) -> None:
    try:
        await page.evaluate(_DISMISS_JS)
    except Exception:
        pass


# JS that finds the leaf whose *trimmed visible text* equals the wanted name and
# clicks its <label> to tick the checkbox. Matching on visible text (not the
# checkbox `name` attribute) is deliberate: some GovOS doc-type checkboxes carry
# a stray TRAILING SPACE in `name` ("STATE TAX LIEN ", "ABSTRACT OF JUDGMENT ")
# so name-based matching silently misses them. Anchoring to the exact trimmed
# text also rejects the many decoy leaves that merely *contain* the name
# ("PARTIAL RELEASE STATE TAX LIEN", "MECHANICS LIEN TRANSFER"). Returns:
#   "checked"  — the box is (now) ticked -> committed to the query
#   "clicked"  — label click fired (state reflects on next tick / row de-renders)
#   "absent"   — no exact-text leaf rendered yet (keep polling)
_COMMIT_LEAF_JS = """(nm) => {
  const want = nm.trim().toUpperCase();
  const items = [...document.querySelectorAll('.tokenized-nested-select__item')];
  const it = items.find(e => (e.innerText || '').trim().toUpperCase() === want);
  if (!it) return 'absent';
  const box = it.querySelector('input[type=checkbox]');
  if (box && box.checked) return 'checked';
  const lbl = it.querySelector('label') || it;
  lbl.click();
  const after = it.querySelector('input[type=checkbox]');
  return (after && after.checked) ? 'checked' : 'clicked';
}"""


async def _commit_leaf(page: Page, name: str) -> bool:
    """Tick the doc-type leaf for `name`, confirming it actually committed.

    The list is virtualized (only filtered rows render) so we poll for the row
    to appear, click its label, and accept either a checked box or a fired click
    (once selected the row can also de-render). Returns True on commit.
    """
    for _ in range(12):  # ~3.6s max for the filtered row to virtualize in
        await page.wait_for_timeout(300)
        try:
            state = await page.evaluate(_COMMIT_LEAF_JS, name)
        except Exception:
            state = "absent"
        if state in ("checked", "clicked"):
            await page.wait_for_timeout(250)
            return True
    return False


async def _add_doc_types(page: Page) -> list[str]:
    """Select lien document types in the GovOS tokenized-nested-select.

    Document Types is a `react-downshift` "tokenized nested select" (a tree:
    OPR → leaf doc types), NOT a react-select. The `#docTypes-input` sits below
    the fold (must scroll into view), filters on real keystrokes, and renders
    matching leaves as `.tokenized-nested-select__item` — that leaf is what we
    click (the `tokenized-nested-select__button` is just the parent "OPR").
    Returns the list of doc-type names successfully clicked.
    """
    added: list[str] = []
    # 2026-08-18: GovOS renamed the input #docTypes-input → #docTypes (React
    # rewrite of the control); the dropdown items kept their classes. Accept
    # both ids so the next rename direction also keeps working.
    dt = page.locator("#docTypes-input, #docTypes").first
    try:
        await dt.wait_for(state="attached", timeout=12000)
        await page.evaluate(
            "() => (document.querySelector('#docTypes-input') || "
            "document.querySelector('#docTypes'))?.scrollIntoView({block:'center'})"
        )
        await page.wait_for_timeout(400)
    except Exception:
        logger.warning("doc-types input (#docTypes-input / #docTypes) never appeared")
        return []
    for name in LIEN_DOC_TYPE_NAMES:
        try:
            await dt.click(timeout=8000)
            await dt.fill("")  # clear previous filter
            try:
                await dt.press_sequentially(name, delay=35, timeout=8000)
            except AttributeError:
                await dt.type(name, delay=35)  # older Playwright
            if await _commit_leaf(page, name):
                if name not in added:
                    added.append(name)
            else:
                logger.warning(
                    "lien doc type %r: no leaf committed in the GovOS dropdown "
                    "— source label may have changed; SKIPPED (not selected)", name,
                )
            await dt.fill("")  # reset filter for the next type
            await page.wait_for_timeout(150)
        except Exception as e:
            # WARNING, not debug — an invisible interaction failure here is
            # exactly how doc-type selection dies silently.
            logger.warning("doc type %r add error: %s", name, e)
    return added


async def _wait_for_results(page: Page, timeout_s: int = 45) -> bool:
    for _ in range(timeout_s // 2):
        await page.wait_for_timeout(2000)
        try:
            title = await page.title()
        except Exception:
            title = ""
        if "Search Results" in title and "Loading" not in title:
            return True
    return False


async def _results_text(page: Page) -> str:
    try:
        return await page.evaluate(
            "() => document.querySelector('#main-content, main, [role=main]')?.innerText "
            "|| document.body.innerText"
        )
    except Exception:
        return ""


async def _next_page(page: Page) -> bool:
    try:
        nxt = page.get_by_role("button", name=re.compile("next page", re.I))
        if await nxt.count() == 0:
            return False
        if not await nxt.first.is_enabled():
            return False
        await nxt.first.click()
        await page.wait_for_timeout(2500)
        return True
    except Exception as e:
        logger.debug("next page error: %s", e)
        return False


class _PublicSearchLienScraper:
    """Shared headed-Playwright lien scraper for GovOS publicsearch counties."""

    COUNTY = ""
    SUBDOMAIN = ""

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

        base = f"https://{self.SUBDOMAIN}.tx.publicsearch.us"
        headless = _force_headless()
        logger.info(
            "%s lien scrape (publicsearch): range=%s..%s headless=%s",
            self.COUNTY, _date_str(from_date), _date_str(to_date), headless,
        )
        if headless:
            logger.warning(
                "%s lien: running HEADLESS — publicsearch anti-bot usually returns "
                "0 rows headless. Run headed (Xvfb in Docker) for real results.",
                self.COUNTY,
            )

        all_notices: list[NoticeData] = []
        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
            except Exception as e:
                logger.error(
                    "%s lien: could not launch headed browser (%s). On a headless "
                    "server install Xvfb and use `xvfb-run -a`. Skipping.",
                    self.COUNTY, e,
                )
                return []

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
                # Tall viewport so the advanced-search Document Types field
                # (~y=986) is on-screen — it sits below an 900px fold otherwise
                # and Playwright can't interact with it.
                viewport={"width": 1400, "height": 1300},
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
                "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            )
            page = await context.new_page()
            page.set_default_timeout(45000)

            try:
                # Load the advanced-search form, with a reload retry — the SPA
                # render time varies by county (Williamson is notably slower than
                # Bell) and an occasional cold load needs a second attempt.
                form_ok = False
                for attempt in range(2):
                    await page.goto(f"{base}/search/advanced", wait_until="domcontentloaded")
                    try:
                        await page.wait_for_selector("#recordedDateRange-start", timeout=40000)
                        form_ok = True
                        break
                    except Exception:
                        logger.warning(
                            "%s lien: advanced-search form not ready (attempt %d/2)",
                            self.COUNTY, attempt + 1,
                        )
                        await page.wait_for_timeout(3000)
                if not form_ok:
                    # Distinguish a genuine anti-bot / slow-SPA miss from a
                    # source-side change. As of 2026-06-30 the Williamson portal
                    # migrated vendors ("Powered By neumo") and now offers only a
                    # single "Commissioners Court" department — the Official Public
                    # Records (deeds/liens) collection, and its
                    # #recordedDateRange-start / #docTypes-input fields, are gone.
                    # Report WHY so future runs aren't chased as a scraper bug.
                    opr_gone = False
                    try:
                        diag = await page.evaluate(
                            "() => ({"
                            "  hasOprForm: !!document.querySelector("
                            "    '#recordedDateRange-start,#docTypes-input'),"
                            "  body: (document.body.innerText || '').toLowerCase()"
                            "    .slice(0, 600)"
                            "})"
                        )
                        body = diag.get("body") or ""
                        opr_gone = (not diag.get("hasOprForm")) and (
                            "commissioners court" in body or "neumo" in body
                        )
                    except Exception:
                        pass
                    if opr_gone:
                        logger.error(
                            "%s lien: Official Public Records search UNAVAILABLE at "
                            "%s.tx.publicsearch.us — portal appears migrated (only a "
                            "'Commissioners Court' collection is offered; the "
                            "recorded-document / lien search form is absent). This is "
                            "a SOURCE change, not a scraper bug: retry later or locate "
                            "the county's new OPR search. Returning 0.",
                            self.COUNTY, self.SUBDOMAIN,
                        )
                    else:
                        logger.warning(
                            "%s lien: advanced-search form never rendered (anti-bot / "
                            "slow SPA). Returning 0.", self.COUNTY,
                        )
                    await browser.close()
                    return []

                from scrapers import ScraperError
                from scrapers.publicsearch_common import verify_window_applied
                from scrapers.debug_capture import dump_page

                label = f"{self.COUNTY} lien"
                self.last_meta = {"window_days": (to_date - from_date).days + 1}

                # Full search flow with ONE retry: doc types → dates → Search →
                # verify the portal actually APPLIED the date window (on ~10 of
                # 30 days it silently didn't, pulling the 2500-row junk cap).
                # Doc types go FIRST — filling the date inputs can open a
                # calendar overlay that intercepts the doc-type field.
                window_ok = False
                for attempt in range(2):
                    if attempt:
                        logger.warning(
                            "%s: re-running the advanced search once "
                            "(doc types or date filter did not apply)", label,
                        )
                        await page.goto(f"{base}/search/advanced", wait_until="domcontentloaded")
                        try:
                            await page.wait_for_selector("#recordedDateRange-start", timeout=40000)
                        except Exception:
                            break
                    await _dismiss_popups(page)

                    selected = await _add_doc_types(page)
                    logger.info("%s: document types selected: %s", label, selected)
                    if not selected:
                        await dump_page(page, f"{self.COUNTY.lower()}_lien_no_doctypes")
                        continue  # retry once, then the loop exit raises below

                    # Dates: fill then press Escape to close any date-picker overlay.
                    try:
                        await page.fill("#recordedDateRange-start", _date_str(from_date))
                        await page.keyboard.press("Escape")
                        await page.fill("#recordedDateRange-end", _date_str(to_date))
                        await page.keyboard.press("Escape")
                    except Exception as e:
                        logger.warning("%s: could not set date range: %s", label, e)

                    await _dismiss_popups(page)
                    await page.get_by_role("button", name="Search", exact=True).click()

                    if not await _wait_for_results(page):
                        await dump_page(page, f"{self.COUNTY.lower()}_lien_results_never_rendered")
                        raise ScraperError(
                            f"{label}: results never rendered (anti-bot or source change)"
                        )

                    ok, evidence = await verify_window_applied(
                        page, from_date, to_date, label
                    )
                    self.last_meta.update(evidence)
                    if ok:
                        window_ok = True
                        break
                    await dump_page(page, f"{self.COUNTY.lower()}_lien_window_not_applied")

                if not window_ok:
                    raise ScraperError(
                        f"{label}: search filters did not apply after retry — "
                        f"doc types or recordedDateRange failed "
                        f"(evidence {self.last_meta})"
                    )

                page_num = 1
                empty_streak = 0
                seen_urls: set[str] = set()
                while page_num <= MAX_PAGES:
                    text = await _results_text(page)
                    page_notices = _parse_results_text(text, self.COUNTY)
                    # Cross-page dedup by instrument URL (pagination can repeat rows).
                    fresh = [
                        n for n in page_notices
                        if not n.source_url or n.source_url not in seen_urls
                    ]
                    for n in fresh:
                        if n.source_url:
                            seen_urls.add(n.source_url)
                    all_notices.extend(fresh)
                    logger.info(
                        "%s lien page %d: %d new liens (total %d)",
                        self.COUNTY, page_num, len(fresh), len(all_notices),
                    )

                    # Stop early if pages keep yielding no NEW rows (format drift,
                    # end of data, or pagination looping) — prevents grinding
                    # through hundreds of pages.
                    empty_streak = empty_streak + 1 if not fresh else 0
                    if empty_streak >= 2:
                        logger.warning(
                            "%s lien: 2 consecutive pages with no new rows — stopping.",
                            self.COUNTY,
                        )
                        break

                    if max_notices and len(all_notices) >= max_notices:
                        all_notices = all_notices[:max_notices]
                        break
                    if not await _next_page(page):
                        break
                    page_num += 1
                    await asyncio.sleep(REQUEST_DELAY)

            except Exception as e:
                # Fail LOUD: propagate through ScraperError so the run-health
                # report alerts, while still handing back whatever pages were
                # scraped before the failure.
                from scrapers import ScraperError
                if isinstance(e, ScraperError):
                    e.partial = all_notices
                    raise
                logger.error("%s lien scraper failed: %s", self.COUNTY, e, exc_info=True)
                raise ScraperError(
                    f"{self.COUNTY} lien: {e}", partial=all_notices
                ) from e
            finally:
                await browser.close()

        self.last_meta["kept"] = len(all_notices)
        logger.info("%s lien scrape complete: %d liens", self.COUNTY, len(all_notices))
        return all_notices


@register("Bell", "lien")
class BellLienScraper(_PublicSearchLienScraper):
    COUNTY = "Bell"
    SUBDOMAIN = "bell"


# NOTE: Williamson is NO LONGER served here. It replatformed its Official Public
# Records off GovOS/publicsearch to Tyler "Self-Service" (2026-07-01) — the
# Williamson/lien scraper now lives in `lien_tyler.py`. This class is kept
# UNREGISTERED for reference / in case the county ever reverts to publicsearch.
class WilliamsonLienScraper(_PublicSearchLienScraper):
    COUNTY = "Williamson"
    SUBDOMAIN = "williamson"

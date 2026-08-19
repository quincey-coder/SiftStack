"""Bell lis pendens scraper — publicsearch.us (Kofile/GovOS County Fusion).

Bell County Clerk runs GovOS "County Fusion" public search at
`bell.tx.publicsearch.us`. This pulls recorded **lis pendens** filings (Tex.
Prop. Code § 12.007 — notice of a pending suit affecting title to real property)
via the Advanced Search. Same platform + headed requirement as lien_publicsearch;
only the requested document types differ.

╔═══════════════════════════════════════════════════════════════════════════╗
║ CRITICAL — MUST RUN HEADED. The portal's anti-bot blocks OLD HEADLESS       ║
║ chromium (results stick on "Loading Search Results..." forever). Run headed;║
║ in Docker/Apify wrap in Xvfb:                                               ║
║   xvfb-run -a python src/main.py daily --types lis_pendens --counties Bell  ║
║ Override with LIS_PENDENS_PUBLICSEARCH_HEADLESS=1 (will likely yield 0).    ║
╚═══════════════════════════════════════════════════════════════════════════╝

LEAD = the DEFENDANT (property owner being sued), NOT the plaintiff who filed —
`lis_pendens_common.pick_defendant` picks the non-plaintiff party (plaintiffs are
often HOAs / lenders / law firms, on either the grantor or grantee side). Lis
pendens are name+property indexed; the results "Property Description" cell is not
a mailing address, so the property address is backfilled by CAD name search in
enrichment Step 3c using the defendant name.

NOTE: Williamson is NOT served here — it replatformed its OPR off publicsearch to
Tyler "Self-Service" (see lis_pendens_tyler.py, mirroring the lien split).
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright, Page

from notice_parser import NoticeData, normalize_court_name
from scrapers import register
from scrapers.lis_pendens_common import pick_defendant

logger = logging.getLogger(__name__)

# Lis pendens document-type names to request in Advanced Search. Must be the
# EXACT leaf labels as they appear in the GovOS tokenized-nested-select; both
# spellings are requested since counties label it either way.
LIS_PENDENS_DOC_TYPE_NAMES = [
    "LIS PENDENS",
    "NOTICE OF LIS PENDENS",
]

# URL code for the same doc type, captured live from the form-built results
# URL (Bell 2026-08-19). Used by the direct results-URL primary path; the form
# flow remains the fallback that would surface any future code drift.
LIS_PENDENS_DOC_TYPE_CODES = "T2"

# Doc-type cell values accepted by the results parser (canonical + variants).
_ACCEPT_TYPES = {"LIS PENDENS", "NOTICE OF LIS PENDENS"}
_LP_EXCLUDE = (
    "RELEASE", "WITHDRAWAL", "PARTIAL", "AMEND", "CANCEL", "EXPUNGE",
    "DISMISS", "NON-SUIT", "NONSUIT",
)

_DISMISS_JS = """() => {
  ['#beamerPushModal', '#npsIframeContainer', '.beamer_overlay',
   '[class*="Beamer"]'].forEach(s => {
     document.querySelectorAll(s).forEach(e => e.remove());
  });
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
MAX_PAGES = 50

_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")


def _force_headless() -> bool:
    return os.getenv("LIS_PENDENS_PUBLICSEARCH_HEADLESS", "").strip().lower() in ("1", "true", "yes")


def _date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _clean_name(raw: str) -> str:
    name = (raw or "").strip().rstrip(",;.").strip()
    name = re.sub(r"\(\+\d*\)\s*$", "", name).strip()
    if name and (name.isupper() or name.islower()):
        name = name.title()
    return name


def _is_lis_pendens_type(raw: str) -> bool:
    up = (raw or "").strip().upper()
    if "LIS PENDENS" not in up:
        return False
    return not any(x in up for x in _LP_EXCLUDE)


def _parse_results_text(text: str, county: str) -> list[NoticeData]:
    """Parse the GovOS results table from the results-page innerText.

    Same tab-separated shape as lien_publicsearch:
        GRANTOR  GRANTEE  DOC TYPE  RECORDED DATE  INST NUMBER  BOOK  PROP DESC
    Anchor on the DOC TYPE cell (a lis pendens type), read grantee = cell before
    it (defendant), grantor = two before. The property-description cell is a
    subdivision legal, not a mailing address, so address stays blank → filled by
    CAD name lookup (Step 3c).
    """
    if not text:
        return []

    notices: list[NoticeData] = []
    seen_inst: set[str] = set()
    for raw_line in text.split("\n"):
        if "\t" not in raw_line:
            continue
        cells = [c.strip() for c in raw_line.split("\t") if c.strip()]
        if len(cells) < 4:
            continue

        dt_idx = None
        for i, c in enumerate(cells):
            if _is_lis_pendens_type(c):
                dt_idx = i
                break
        if dt_idx is None or dt_idx < 1:
            continue

        doc_type_raw = cells[dt_idx]
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

        defendant, plaintiff = pick_defendant(grantor=grantor, grantee=grantee)
        if not defendant:
            continue
        if instrument and instrument in seen_inst:
            continue
        if instrument:
            seen_inst.add(instrument)

        notice = NoticeData(notice_type="lis_pendens", county=county, state="TX")
        notice.lien_creditor = _clean_name(plaintiff)   # plaintiff = context
        notice.tax_owner_name = defendant.upper()
        notice.owner_name = normalize_court_name(_clean_name(defendant))
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
    for _ in range(12):
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
    """Select lis pendens document types in the GovOS tokenized-nested-select.

    Only ONE of the two spellings will exist in a given county's tree; the other
    simply never renders a leaf and is skipped. As long as at least one commits,
    the search is filtered to lis pendens.
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
    for name in LIS_PENDENS_DOC_TYPE_NAMES:
        try:
            await dt.click(timeout=8000)
            await dt.fill("")
            try:
                await dt.press_sequentially(name, delay=35, timeout=8000)
            except AttributeError:
                await dt.type(name, delay=35)
            if await _commit_leaf(page, name):
                if name not in added:
                    added.append(name)
            else:
                logger.info(
                    "lis pendens doc type %r not offered by this county's dropdown "
                    "— skipped (a spelling variant may cover it)", name,
                )
            await dt.fill("")
            await page.wait_for_timeout(150)
        except Exception as e:
            # WARNING, not debug — an invisible interaction failure here is
            # exactly how doc-type selection died silently on 2026-08-18.
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


class _PublicSearchLisPendensScraper:
    """Shared headed-Playwright lis pendens scraper for GovOS publicsearch."""

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
            "%s lis pendens scrape (publicsearch): range=%s..%s headless=%s",
            self.COUNTY, _date_str(from_date), _date_str(to_date), headless,
        )
        if headless:
            logger.warning(
                "%s lis pendens: running HEADLESS — publicsearch anti-bot usually "
                "returns 0 rows headless. Run headed (Xvfb in Docker).", self.COUNTY,
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
                    "%s lis pendens: could not launch headed browser (%s). On a "
                    "headless server install Xvfb and use `xvfb-run -a`. Skipping.",
                    self.COUNTY, e,
                )
                return []

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
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
                # PRIMARY PATH (2026-08-19): navigate straight to the
                # parameterized results URL on a FRESH page. The advanced form
                # silently drops the typed date range from its query in the
                # cloud (the query ran with the default 16000101..yesterday
                # range while the inputs displayed ours), and URL surgery
                # after a form search gets rewritten from the SPA's stored
                # session state. A fresh load with the params honors them —
                # verified live (Bell, 7 in-window results). The form flow
                # below survives as the FALLBACK, e.g. if the county
                # re-indexes and the doc-type codes drift.
                from scrapers.publicsearch_common import (
                    build_results_url, verify_window_applied,
                )
                label = f"{self.COUNTY} lis pendens"
                self.last_meta = {"window_days": (to_date - from_date).days + 1}
                from scrapers.publicsearch_common import goto_results_direct
                window_ok = False
                direct = build_results_url(base, LIS_PENDENS_DOC_TYPE_CODES, from_date, to_date)
                # Two tries: cold deep link, then with a homepage warm-up —
                # the anti-bot rewrites COLD deep links to the default range
                # on cloud proxy IPs (QA 2026-08-19) while local sessions and
                # warmed sessions are expected to honor the params.
                for warm in (False, True):
                    await goto_results_direct(page, base, direct, label, warm_up=warm)
                    if not await _wait_for_results(page):
                        continue
                    self.last_meta["results_url"] = page.url
                    ok, evidence = await verify_window_applied(
                        page, from_date, to_date, label
                    )
                    self.last_meta.update(evidence)
                    if ok:
                        window_ok = True
                        break
                    try:
                        body_head = (await page.evaluate(
                            '() => document.body.innerText'))[:300]
                        logger.warning("%s: direct URL landed on %s — body head: %r",
                                       label, page.url[:140], body_head)
                    except Exception:
                        pass
                if not window_ok:
                    logger.warning(
                        "%s: direct results URL did not verify — falling back "
                        "to the advanced-search form flow", label,
                    )

                form_ok = False
                for attempt in range(0 if window_ok else 2):
                    await page.goto(f"{base}/search/advanced", wait_until="domcontentloaded")
                    try:
                        await page.wait_for_selector("#recordedDateRange-start", timeout=40000)
                        form_ok = True
                        break
                    except Exception:
                        logger.warning(
                            "%s lis pendens: advanced-search form not ready (attempt %d/2)",
                            self.COUNTY, attempt + 1,
                        )
                        await page.wait_for_timeout(3000)
                if not form_ok and not window_ok:
                    logger.warning(
                        "%s lis pendens: advanced-search form never rendered "
                        "(anti-bot / slow SPA / source change). Returning 0.", self.COUNTY,
                    )
                    await browser.close()
                    return []

                from scrapers import ScraperError
                from scrapers.publicsearch_common import verify_window_applied
                from scrapers.debug_capture import dump_page

                # Full search flow with ONE retry: doc types → dates → Search →
                # verify the portal actually APPLIED the date window (on ~10 of
                # 30 days it silently didn't, pulling the 2500-row junk cap).
                for attempt in range(0 if window_ok else 2):
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
                        await dump_page(page, f"{self.COUNTY.lower()}_lp_no_doctypes")
                        continue  # retry once, then the loop exit raises below

                    # Dates: verified apply (read-back + native-setter fallback)
                    # — never click Search on dates the DOM doesn't hold.
                    from scrapers.publicsearch_common import apply_dates
                    if not await apply_dates(page, from_date, to_date, label):
                        await dump_page(page, f"{self.COUNTY.lower()}_lp_dates_never_took")
                        continue  # retry once, then the loop exit raises below

                    await _dismiss_popups(page)
                    await page.get_by_role("button", name="Search", exact=True).click()

                    if not await _wait_for_results(page):
                        await dump_page(page, f"{self.COUNTY.lower()}_lp_results_never_rendered")
                        raise ScraperError(
                            f"{label}: results never rendered (anti-bot or source change)"
                        )

                    self.last_meta["results_url"] = page.url
                    ok, evidence = await verify_window_applied(
                        page, from_date, to_date, label
                    )
                    self.last_meta.update(evidence)
                    if ok:
                        window_ok = True
                        break

                    # Form dropped the dates from the query (cloud failure
                    # mode) — force the window via URL surgery on the results
                    # URL, which carries the doc types the form DID apply.
                    from scrapers.publicsearch_common import force_window_url
                    fixed = force_window_url(page.url, from_date, to_date)
                    if fixed:
                        logger.warning("%s: date filter dropped from query — "
                                       "forcing window via results URL", label)
                        await page.goto(fixed, wait_until="domcontentloaded")
                        if await _wait_for_results(page):
                            self.last_meta["results_url"] = page.url
                            ok, evidence = await verify_window_applied(
                                page, from_date, to_date, label
                            )
                            self.last_meta.update(evidence)
                            if ok:
                                window_ok = True
                                break
                    await dump_page(page, f"{self.COUNTY.lower()}_lp_window_not_applied")

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
                    fresh = [
                        n for n in page_notices
                        if not n.source_url or n.source_url not in seen_urls
                    ]
                    for n in fresh:
                        if n.source_url:
                            seen_urls.add(n.source_url)
                    all_notices.extend(fresh)
                    logger.info(
                        "%s lis pendens page %d: %d new (total %d)",
                        self.COUNTY, page_num, len(fresh), len(all_notices),
                    )

                    empty_streak = empty_streak + 1 if not fresh else 0
                    if empty_streak >= 2:
                        logger.warning(
                            "%s lis pendens: 2 consecutive pages with no new rows — stopping.",
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
                logger.error(
                    "%s lis pendens scraper failed: %s", self.COUNTY, e, exc_info=True
                )
                raise ScraperError(
                    f"{self.COUNTY} lis pendens: {e}", partial=all_notices
                ) from e
            finally:
                await browser.close()

        self.last_meta["kept"] = len(all_notices)
        logger.info("%s lis pendens scrape complete: %d records", self.COUNTY, len(all_notices))
        return all_notices


@register("Bell", "lis_pendens")
class BellLisPendensScraper(_PublicSearchLisPendensScraper):
    COUNTY = "Bell"
    SUBDOMAIN = "bell"

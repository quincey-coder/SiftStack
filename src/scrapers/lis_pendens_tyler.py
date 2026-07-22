"""Williamson lis pendens scraper — Tyler Technologies "Self-Service" portal.

Williamson County serves its Official Public Records (1984→present) on **Tyler
"Self-Service"** at `williamsoncountytx-web.tylerhost.net` (it migrated off
GovOS/publicsearch — see the project_liens memory). This pulls recorded **lis
pendens** filings (Tex. Prop. Code § 12.007). Same portal + headed requirement as
lien_tyler.py; only the requested/kept document types differ. In fact the Tyler
doc-type filter is loose and already LEAKS lis pendens into lien searches, so the
records are known to be present here.

╔═══════════════════════════════════════════════════════════════════════════╗
║ MUST RUN HEADED. Navigating straight to the search URL trips a             ║
║ "Human Verification" gate (HTTP 405). The natural flow (disclaimer →       ║
║ Accept → click the OPR search link) WITH automation-signal spoofing        ║
║ bypasses it. In Docker/Apify run under Xvfb (`xvfb-run -a`).               ║
║ Override with LIS_PENDENS_TYLER_HEADLESS=1 (will likely hit the challenge).║
╚═══════════════════════════════════════════════════════════════════════════╝

LEAD = the DEFENDANT — `lis_pendens_common.pick_defendant` picks the
non-plaintiff party. Address (a subdivision legal, not a mailing address) is
backfilled by CAD name search in enrichment Step 3c using the defendant name.
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

BASE = "https://williamsoncountytx-web.tylerhost.net/williamsonweb"
DISCLAIMER_URL = f"{BASE}/user/disclaimer"
SEARCH_ID = "DOCSEARCH149S1"

# Lis pendens DISPLAY values to request in the doc-type autocomplete. Both
# spellings are attempted; whichever the portal offers commits a chip.
LIS_PENDENS_DOC_TYPE_NAMES = [
    "LIS PENDENS",
    "NOTICE OF LIS PENDENS",
]

# A parsed row is KEPT when its doc-type reads as a live lis pendens and is NOT a
# release / withdrawal / amendment / dismissal / cancellation.
_LP_EXCLUDE = (
    "RELEASE", "WITHDRAWAL", "PARTIAL", "AMEND", "CANCEL", "EXPUNGE",
    "DISMISS", "NON-SUIT", "NONSUIT", "ASSIGNMENT",
)

REQUEST_DELAY = 1.5
MAX_PAGES = 60
CHUNK_DAYS = 14   # the portal caps results per search; window the date range

_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_INST_RE = re.compile(r"\b(\d{6,})\b")


def _force_headless() -> bool:
    return os.getenv("LIS_PENDENS_TYLER_HEADLESS", "").strip().lower() in ("1", "true", "yes")


def _date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _clean_name(raw: str) -> str:
    name = (raw or "").strip().rstrip(",;.").strip()
    name = re.sub(r"\(\+\d*\)\s*$", "", name).strip()
    if name and (name.isupper() or name.islower()):
        name = name.title()
    return name


def _is_lis_pendens_doctype(doctype: str) -> bool:
    up = (doctype or "").upper()
    if not up or "LIS PENDENS" not in up:
        return False
    return not any(x in up for x in _LP_EXCLUDE)


def _parse_h1(h1: str) -> tuple[str, str, str]:
    """From "{instrument} • {DOC TYPE} • {MM/DD/YYYY hh:mm AM}" return
    (instrument, doctype, date_iso)."""
    h1 = (h1 or "").replace("\xa0", " ").strip()
    m_inst = _INST_RE.search(h1)
    m_date = _DATE_RE.search(h1)
    instrument = m_inst.group(1) if m_inst else ""
    date_iso = ""
    if m_date:
        try:
            date_iso = datetime.strptime(m_date.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
        except ValueError:
            date_iso = ""
    mid = h1
    if instrument:
        idx = mid.find(instrument)
        if idx >= 0:
            mid = mid[idx + len(instrument):]
    if m_date:
        di = mid.find(m_date.group(1))
        if di >= 0:
            mid = mid[:di]
    doctype = re.sub(r"[••·|;]+", " ", mid)
    doctype = re.sub(r"\s+", " ", doctype).strip()
    return instrument, doctype, date_iso


_FETCH_PAGE_JS = r"""
async (args) => {
  const [searchId, pageNum] = args;
  const url = `/williamsonweb/searchResults/${searchId}?page=${pageNum}&_=${Date.now()}`;
  let resp;
  try { resp = await fetch(url, {credentials: 'include'}); }
  catch (e) { return {ok: false, status: -1, rows: []}; }
  if (!resp.ok) return {ok: false, status: resp.status, rows: []};
  const html = await resp.text();
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const rows = [...doc.querySelectorAll('li.ss-search-row')].map(li => {
    const docid = li.getAttribute('data-documentid') || '';
    const href = li.getAttribute('data-href') || '';
    const h1el = li.querySelector('h1');
    const h1 = h1el ? clean(h1el.textContent) : '';
    const grantors = [], grantees = [], legal = [];
    li.querySelectorAll('.searchResultFourColumn').forEach(block => {
      const items = [...block.querySelectorAll('li')]
        .map(x => clean(x.textContent)).filter(Boolean);
      if (!items.length) return;
      const label = items[0].toLowerCase();
      const vals = items.slice(1);
      if (label.indexOf('grantor') === 0) grantors.push(...vals);
      else if (label.indexOf('grantee') === 0) grantees.push(...vals);
      else if (label.indexOf('legal') === 0) legal.push(...vals);
    });
    return {docid, href, h1, grantors, grantees, legal};
  });
  return {ok: true, status: 200, rows};
}
"""


async def _accept_disclaimer_and_open_search(page: Page) -> bool:
    await page.goto(DISCLAIMER_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)
    for how in (
        lambda: page.get_by_role("button", name="Accept").first.click(timeout=4000),
        lambda: page.locator("a:has-text('Accept'), input[value*='Accept']").first.click(timeout=4000),
    ):
        try:
            await how()
            break
        except Exception:
            continue
    await page.wait_for_timeout(3000)
    for how in (
        lambda: page.get_by_role("link", name="Official Public Record Search").first.click(timeout=6000),
        lambda: page.locator(f"a[href*='{SEARCH_ID}']").first.click(timeout=6000),
    ):
        try:
            await how()
            break
        except Exception:
            continue
    try:
        await page.wait_for_selector("#field_selfservice_documentTypes", timeout=30000)
        return True
    except Exception:
        return False


async def _add_doc_type(page: Page, value: str) -> bool:
    dt = page.locator("#field_selfservice_documentTypes")
    try:
        await dt.scroll_into_view_if_needed()
        await dt.click(timeout=6000)
        await dt.fill("")
        await dt.type(value, delay=35)
        await page.wait_for_timeout(1400)
    except Exception as e:
        logger.debug("doc-type %r type error: %s", value, e)
        return False
    clicked = await page.evaluate(
        """(val) => {
          const lis = [...document.querySelectorAll(
            '#field_selfservice_documentTypes-aclist li, #field_selfservice_documentTypes-aclist a')];
          const norm = s => (s || '').replace(/\\s+/g,' ').trim().toUpperCase();
          const exact = lis.find(l => norm(l.textContent) === val.toUpperCase());
          const target = exact || null;
          if (target) { (target.querySelector('a') || target).click(); return true; }
          return false;
        }""",
        value,
    )
    await page.wait_for_timeout(500)
    try:
        await dt.fill("")
    except Exception:
        pass
    if not clicked:
        return False
    present = await page.evaluate(
        """(val) => {
          const chips = [...document.querySelectorAll(
            '#field_selfservice_documentTypes-holder input[id$=\"-searchInput\"]')];
          return chips.some(c => (c.value || '').trim().toUpperCase() === val.toUpperCase());
        }""",
        value,
    )
    return bool(present)


@register("Williamson", "lis_pendens")
class WilliamsonTylerLisPendensScraper:
    """Williamson lis pendens scraper against the Tyler Self-Service portal."""

    COUNTY = "Williamson"

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

        headless = _force_headless()
        logger.info(
            "Williamson lis pendens scrape (Tyler Self-Service): range=%s..%s headless=%s",
            _date_str(from_date), _date_str(to_date), headless,
        )
        if headless:
            logger.warning(
                "Williamson lis pendens: running HEADLESS — the Tyler portal trips a "
                "'Human Verification' gate without a display. Run headed (Xvfb in Docker).",
            )

        notices: list[NoticeData] = []
        seen_docids: set[str] = set()

        async with async_playwright() as p:
            try:
                browser = await p.chromium.launch(
                    headless=headless,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
                )
            except Exception as e:
                logger.error(
                    "Williamson lis pendens: could not launch headed browser (%s). On a "
                    "headless server install Xvfb and use `xvfb-run -a`. Skipping.", e,
                )
                return []

            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1400, "height": 1300},
                ignore_https_errors=True,
            )
            await context.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
                "window.chrome={runtime:{}};"
                "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
            )
            page = await context.new_page()
            page.set_default_timeout(45000)

            try:
                if not await _accept_disclaimer_and_open_search(page):
                    logger.error(
                        "Williamson lis pendens: Document Search form never rendered "
                        "(disclaimer/human-verification flow failed). Returning 0.",
                    )
                    await browser.close()
                    return []

                added = []
                for name in LIS_PENDENS_DOC_TYPE_NAMES:
                    if await _add_doc_type(page, name):
                        added.append(name)
                logger.info("Williamson lis pendens: doc types selected: %s", added or "(none)")
                if not added:
                    logger.warning(
                        "Williamson lis pendens: no lis pendens doc type could be selected "
                        "— aborting to avoid an unfiltered (all-record-type) search.",
                    )
                    await browser.close()
                    return []

                windows: list[tuple[datetime, datetime]] = []
                ws = from_date
                while ws <= to_date:
                    we = min(ws + timedelta(days=CHUNK_DAYS - 1), to_date)
                    windows.append((ws, we))
                    ws = we + timedelta(days=1)
                logger.info(
                    "Williamson lis pendens: searching %d date window(s) of <=%d days",
                    len(windows), CHUNK_DAYS,
                )

                for win_start, win_end in windows:
                    await page.evaluate(
                        """(args) => {
                          const [s, e] = args;
                          const set = (id, v) => {
                            const el = document.getElementById(id);
                            if (el) { el.value = v; el.dispatchEvent(new Event('change', {bubbles: true})); }
                          };
                          set('field_RecDateID_DOT_StartDate', s);
                          set('field_RecDateID_DOT_EndDate', e);
                        }""",
                        [_date_str(win_start), _date_str(win_end)],
                    )
                    try:
                        await page.locator("#searchButton").click()
                    except Exception as e:
                        logger.warning(
                            "Williamson lis pendens: search click failed for %s..%s: %s",
                            _date_str(win_start), _date_str(win_end), e,
                        )
                        continue
                    await page.wait_for_timeout(4000)

                    win_new = 0
                    for page_num in range(1, MAX_PAGES + 1):
                        try:
                            res = await page.evaluate(_FETCH_PAGE_JS, [SEARCH_ID, page_num])
                        except Exception as e:
                            logger.warning("Williamson lis pendens: page %d fetch error: %s", page_num, e)
                            break
                        if not res or not res.get("ok"):
                            if page_num == 1:
                                logger.warning(
                                    "Williamson lis pendens: results fetch failed (status %s) for %s..%s",
                                    (res or {}).get("status"),
                                    _date_str(win_start), _date_str(win_end),
                                )
                            break
                        rows = res.get("rows") or []
                        if not rows:
                            break

                        for row in rows:
                            docid = (row.get("docid") or "").strip()
                            if docid and docid in seen_docids:
                                continue
                            instrument, doctype, date_iso = _parse_h1(row.get("h1", ""))
                            if not _is_lis_pendens_doctype(doctype):
                                continue  # liens, deeds, releases, etc.
                            grantors = row.get("grantors") or []
                            grantees = row.get("grantees") or []
                            grantor = grantors[0] if grantors else ""
                            grantee = grantees[0] if grantees else ""
                            defendant, plaintiff = pick_defendant(grantor=grantor, grantee=grantee)
                            if not defendant:
                                continue
                            if docid:
                                seen_docids.add(docid)
                            win_new += 1

                            notice = NoticeData(notice_type="lis_pendens", county="Williamson", state="TX")
                            notice.lien_creditor = _clean_name(plaintiff)
                            notice.tax_owner_name = defendant.upper()
                            notice.owner_name = normalize_court_name(_clean_name(defendant))
                            notice.date_added = date_iso
                            href = (row.get("href") or "").strip()
                            if href:
                                notice.source_url = f"https://williamsoncountytx-web.tylerhost.net{href}"
                            elif docid:
                                notice.source_url = f"{BASE}/document/{docid}"
                            notice.raw_text = (
                                f"{row.get('h1','')} | Grantor: {grantor} | Grantee: {grantee}"
                            ).strip()
                            notices.append(notice)

                            if max_notices and len(notices) >= max_notices:
                                logger.info("Williamson lis pendens: hit max_notices=%d cap", max_notices)
                                await browser.close()
                                return notices

                        await asyncio.sleep(REQUEST_DELAY)

                    logger.info(
                        "Williamson lis pendens: window %s..%s → %d new (running %d)",
                        _date_str(win_start), _date_str(win_end), win_new, len(notices),
                    )

                logger.info(
                    "Williamson lis pendens: %d records parsed (Tyler Self-Service)",
                    len(notices),
                )
            except Exception as e:
                logger.error("Williamson lis pendens: scrape failed: %s", e, exc_info=True)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        return notices

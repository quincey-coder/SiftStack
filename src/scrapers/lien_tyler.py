"""Williamson lien scraper — Tyler Technologies "Self-Service" recorder portal.

Williamson County migrated its Official Public Records (deeds/liens, 1984→present)
OFF the GovOS/Kofile publicsearch platform to **Tyler "Self-Service"** at
`williamsoncountytx-web.tylerhost.net` (root-caused 2026-07-01 — see the
`project_liens` memory). The old `williamson.tx.publicsearch.us` now only serves
pre-1984 Commissioners Court minutes, which is why the publicsearch lien scraper
started returning 0 for Williamson. **Bell + Travis lien scrapers are unaffected**
— only Williamson uses this Tyler portal.

╔═══════════════════════════════════════════════════════════════════════════╗
║ MUST RUN HEADED. Navigating straight to the search URL trips a             ║
║ "Human Verification" gate (HTTP 405). The natural flow — disclaimer →      ║
║ Accept → click the "Official Public Record Search" link — WITH             ║
║ automation-signal spoofing bypasses it and the search renders. In          ║
║ Docker/Apify (Linux, no display) run under Xvfb (`xvfb-run -a`).           ║
║ Override with LIEN_TYLER_HEADLESS=1 (will likely hit the challenge).       ║
╚═══════════════════════════════════════════════════════════════════════════╝

Flow (live-verified 2026-07-01):
  /williamsonweb/user/disclaimer → click "Accept"
  → click "Official Public Record Search" link (→ /williamsonweb/search/DOCSEARCH149S1)
  → add lien doc types to #field_selfservice_documentTypes (jQuery-mobile
    autocomplete backed by /williamsonweb/search/documentTypes/DOCSEARCH149S1;
    type value, click the EXACT-match leaf → adds a chip)
  → set #field_RecDateID_DOT_StartDate / -EndDate (MM/DD/YYYY)
  → click #searchButton (POST /williamsonweb/searchPost/DOCSEARCH149S1)
  → GET /williamsonweb/searchResults/DOCSEARCH149S1?page=N returns HTML rows:
       <li class="ss-search-row" data-documentid="DOC.." data-href="/williamsonweb/document/..">
         <h1>{instrument} • {DOC TYPE} • {MM/DD/YYYY hh:mm AM}</h1>
         .searchResultFourColumn blocks labelled Grantor / Grantee / Legal Description

LEAD = the GRANTEE/DEBTOR — `pick_debtor` picks the non-institutional party
(shared with the other two lien sources). Liens are name-indexed with no property
address → the address is backfilled by CAD name search in enrichment Step 3c-lien.
The portal's own doc-type filter is loose (it can return LIS PENDENS etc.), so the
PARSER is the source of truth: it keeps a row only if its `<h1>` doc-type reads as a
real lien and drops releases/withdrawals/assignments.
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

BASE = "https://williamsoncountytx-web.tylerhost.net/williamsonweb"
DISCLAIMER_URL = f"{BASE}/user/disclaimer"
SEARCH_ID = "DOCSEARCH149S1"

# Lien document-type DISPLAY values to request (exact strings from the portal's
# own documentTypes autocomplete). Releases/withdrawals are never requested and
# are also dropped by the parser as a second guard.
LIEN_DOC_TYPE_NAMES = [
    "ABSTRACT OF JUDGMENT",
    "STATE OF TEXAS ABSTRACT OF JUDGMENT",
    "FEDERAL TAX LIEN",
    "STATE TAX LIEN",
    "MECHANICS LIEN",
    "HOSPITAL LIEN",
    "CHILD SUPPORT LIEN",
]

# A parsed row's doc-type is KEPT when it reads as a real lien and is NOT a
# release/assignment/amendment. Anchors on "LIEN" or "ABSTRACT OF JUDGMENT" so
# spelling variants (MECHANIC'S vs MECHANICS, STATE OF TEXAS AJ) all pass.
_LIEN_EXCLUDE = (
    "RELEASE", "WITHDRAWAL", "PARTIAL", "ASSIGNMENT", "AMENDMENT",
    "CORRECTION", "SUBORDINATION", "EXTENSION", "SATISFACTION", "CANCELLATION",
)

REQUEST_DELAY = 1.5
MAX_PAGES = 60   # safety cap on pagination within one date window
# The portal caps how many results a single search will page through ("too many
# results"). Splitting the requested range into small recorded-date windows keeps
# each search under that cap so nothing is silently truncated. 14 days was proven
# complete at ~144 liens/window in live testing.
CHUNK_DAYS = 14

_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
_INST_RE = re.compile(r"\b(\d{6,})\b")


def _force_headless() -> bool:
    return os.getenv("LIEN_TYLER_HEADLESS", "").strip().lower() in ("1", "true", "yes")


def _date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def _clean_name(raw: str) -> str:
    name = (raw or "").strip().rstrip(",;.").strip()
    name = re.sub(r"\(\+\d*\)\s*$", "", name).strip()  # drop "(+N)" more-parties markers
    if name and (name.isupper() or name.islower()):
        name = name.title()
    return name


def _normalize_lien_type(raw: str) -> str:
    raw = (raw or "").strip()
    return raw.title() if raw else ""


def _is_lien_doctype(doctype: str) -> bool:
    up = (doctype or "").upper()
    if not up:
        return False
    if any(x in up for x in _LIEN_EXCLUDE):
        return False
    return ("LIEN" in up) or ("ABSTRACT OF JUDGMENT" in up)


def _parse_h1(h1: str) -> tuple[str, str, str]:
    """From "{instrument} • {DOC TYPE} • {MM/DD/YYYY hh:mm AM}" return
    (instrument, doctype, date_iso). Doc type is whatever sits between the
    instrument number and the recorded date, with bullet separators stripped."""
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
    # doc type = text between the instrument and the date
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


# In-browser: fetch one results page and return its rows as structured dicts.
# Runs against the search already primed in the session by the #searchButton POST.
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
    """disclaimer → Accept → click the OPR search link. Returns True on the
    Document Search form (bypasses the direct-URL human-verification gate)."""
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
    """Type a doc-type value into the autocomplete and click the EXACT match leaf
    (the list also holds 'PARTIAL RELEASE …' etc., so substring-click is wrong).
    Returns True when a chip for `value` is present in the holder."""
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
          const target = exact || null;   // only accept an exact match
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
    # confirm a chip landed
    present = await page.evaluate(
        """(val) => {
          const chips = [...document.querySelectorAll(
            '#field_selfservice_documentTypes-holder input[id$=\"-searchInput\"]')];
          return chips.some(c => (c.value || '').trim().toUpperCase() === val.toUpperCase());
        }""",
        value,
    )
    return bool(present)


class WilliamsonTylerLienScraper:
    """Williamson lien scraper against the Tyler Self-Service recorder portal."""

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
            "Williamson lien scrape (Tyler Self-Service): range=%s..%s headless=%s",
            _date_str(from_date), _date_str(to_date), headless,
        )
        if headless:
            logger.warning(
                "Williamson lien: running HEADLESS — the Tyler portal trips a "
                "'Human Verification' gate without a display. Run headed (Xvfb in "
                "Docker) for real results.",
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
                    "Williamson lien: could not launch headed browser (%s). On a "
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
                        "Williamson lien: Document Search form never rendered "
                        "(disclaimer/human-verification flow failed). Returning 0.",
                    )
                    await browser.close()
                    return []

                added = []
                for name in LIEN_DOC_TYPE_NAMES:
                    if await _add_doc_type(page, name):
                        added.append(name)
                logger.info("Williamson lien: doc types selected: %s", added or "(none)")
                if not added:
                    logger.warning(
                        "Williamson lien: no lien doc types could be selected — "
                        "aborting to avoid an unfiltered (all-record-type) search.",
                    )
                    await browser.close()
                    return []

                # Split the requested range into CHUNK_DAYS windows (each below the
                # portal's per-search result cap) and search each in turn. Dedup by
                # document id across windows so any boundary overlap is harmless.
                windows: list[tuple[datetime, datetime]] = []
                ws = from_date
                while ws <= to_date:
                    we = min(ws + timedelta(days=CHUNK_DAYS - 1), to_date)
                    windows.append((ws, we))
                    ws = we + timedelta(days=1)
                logger.info(
                    "Williamson lien: searching %d date window(s) of <=%d days",
                    len(windows), CHUNK_DAYS,
                )

                for win_start, win_end in windows:
                    # Set this window's recorded-date range (JS-set + change event;
                    # the jQuery-mobile date inputs need change to register).
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
                            "Williamson lien: search click failed for %s..%s: %s",
                            _date_str(win_start), _date_str(win_end), e,
                        )
                        continue
                    await page.wait_for_timeout(4000)

                    win_new = 0
                    for page_num in range(1, MAX_PAGES + 1):
                        try:
                            res = await page.evaluate(_FETCH_PAGE_JS, [SEARCH_ID, page_num])
                        except Exception as e:
                            logger.warning("Williamson lien: page %d fetch error: %s", page_num, e)
                            break
                        if not res or not res.get("ok"):
                            if page_num == 1:
                                logger.warning(
                                    "Williamson lien: results fetch failed (status %s) for %s..%s",
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
                            if not _is_lien_doctype(doctype):
                                continue  # LIS PENDENS, deeds, releases, etc.
                            grantors = row.get("grantors") or []
                            grantees = row.get("grantees") or []
                            grantor = grantors[0] if grantors else ""
                            grantee = grantees[0] if grantees else ""
                            debtor, creditor = pick_debtor(grantor=grantor, grantee=grantee)
                            if not debtor:
                                continue
                            if docid:
                                seen_docids.add(docid)
                            win_new += 1

                            notice = NoticeData(notice_type="lien", county="Williamson", state="TX")
                            notice.lien_type = _normalize_lien_type(doctype)
                            notice.lien_creditor = _clean_name(creditor)
                            notice.tax_owner_name = debtor.upper()
                            notice.owner_name = normalize_court_name(_clean_name(debtor))
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
                                logger.info("Williamson lien: hit max_notices=%d cap", max_notices)
                                await browser.close()
                                return notices

                        await asyncio.sleep(REQUEST_DELAY)

                    logger.info(
                        "Williamson lien: window %s..%s → %d new liens (running %d)",
                        _date_str(win_start), _date_str(win_end), win_new, len(notices),
                    )

                logger.info(
                    "Williamson lien: %d lien records parsed (Tyler Self-Service)",
                    len(notices),
                )
            except Exception as e:
                logger.error("Williamson lien: scrape failed: %s", e, exc_info=True)
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        return notices


# Registered in scrapers/__init__ (after lien_publicsearch, whose Williamson
# registration has been removed) so Williamson/lien resolves to this Tyler scraper.
register("Williamson", "lien")(WilliamsonTylerLienScraper)

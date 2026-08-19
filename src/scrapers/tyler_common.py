"""Shared machinery for the Williamson Tyler "Self-Service" portal scrapers.

Used by lien_tyler.py and lis_pendens_tyler.py — the two modules were
byte-identical on this plumbing, and the 405 recovery below must behave the
same in both, so it lives here once.

Portal flow (live-verified 2026-07-01):
  /williamsonweb/user/disclaimer → click "Accept"
  → click "Official Public Record Search" (→ /williamsonweb/search/DOCSEARCH149S1)
  → add doc types to #field_selfservice_documentTypes (jQuery-mobile autocomplete)
  → set #field_RecDateID_DOT_StartDate / -EndDate (MM/DD/YYYY)
  → click #searchButton (POST /williamsonweb/searchPost/DOCSEARCH149S1)
  → GET /williamsonweb/searchResults/DOCSEARCH149S1?page=N returns HTML rows

THE 405: navigating straight to a search URL — or fetching searchResults when
the session's "Human Verification" gate has re-armed — returns HTTP 405. This
killed Williamson lis pendens for 27 straight days (2026-07-23 → 08-18) with
one WARNING per window and zero alerting. `recover_search_session()` re-runs
the full disclaimer → Accept → OPR-link flow (the thing that disarms the
gate), re-adds the doc-type chips, and lets the caller re-run the window
search once before giving up loudly.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from playwright.async_api import Page

logger = logging.getLogger(__name__)

BASE = "https://williamsoncountytx-web.tylerhost.net/williamsonweb"
DISCLAIMER_URL = f"{BASE}/user/disclaimer"
SEARCH_ID = "DOCSEARCH149S1"

DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
INST_RE = re.compile(r"\b(\d{6,})\b")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
INIT_SCRIPT = (
    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    "window.chrome={runtime:{}};"
    "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3,4,5]});"
)


def date_str(dt: datetime) -> str:
    return dt.strftime("%m/%d/%Y")


def clean_name(raw: str) -> str:
    name = (raw or "").strip().rstrip(",;.").strip()
    name = re.sub(r"\(\+\d*\)\s*$", "", name).strip()  # drop "(+N)" more-parties markers
    if name and (name.isupper() or name.islower()):
        name = name.title()
    return name


def parse_h1(h1: str) -> tuple[str, str, str]:
    """From "{instrument} • {DOC TYPE} • {MM/DD/YYYY hh:mm AM}" return
    (instrument, doctype, date_iso). Doc type is whatever sits between the
    instrument number and the recorded date, with bullet separators stripped."""
    h1 = (h1 or "").replace("\xa0", " ").strip()
    m_inst = INST_RE.search(h1)
    m_date = DATE_RE.search(h1)
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


# In-browser: fetch one results page and return its rows as structured dicts.
# Runs against the search already primed in the session by the #searchButton POST.
FETCH_PAGE_JS = r"""
async (args) => {
  const [searchId, pageNum] = args;
  const url = `/williamsonweb/searchResults/${searchId}?page=${pageNum}&_=${Date.now()}`;
  let resp;
  try { resp = await fetch(url, {credentials: 'include'}); }
  catch (e) { return {ok: false, status: -1, rows: []}; }
  if (!resp.ok) {
    let snippet = '';
    try { snippet = (await resp.text()).slice(0, 400); } catch (e) {}
    return {ok: false, status: resp.status, rows: [], snippet};
  }
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


async def fetch_results_page(page: Page, page_num: int) -> dict:
    """Fetch one searchResults page. {'ok', 'status', 'rows', 'snippet'?}."""
    return await page.evaluate(FETCH_PAGE_JS, [SEARCH_ID, page_num])


_WAF_MARKER_JS = (
    "() => !!(window.gokuProps || window.awsWafCookieDomainList) || "
    "document.title.trim() === 'Human Verification'"
)


async def pass_aws_waf(page: Page, timeout_s: int = 45) -> bool:
    """Wait out the AWS WAF 'Human Verification' JS challenge.

    Tyler fronts the portal with AWS WAF; when its rate rule trips (e.g. the
    lien scraper just paged through results from the same IP), the next
    navigation lands on a challenge page (`window.gokuProps`,
    title 'Human Verification') that AUTO-SOLVES in a real headed browser
    given a few seconds — but our flow never waited for it, which is why the
    lis pendens 405 persisted through session recovery. Polls until the
    challenge markers disappear. Returns True when clear.
    """
    try:
        challenged = await page.evaluate(_WAF_MARKER_JS)
    except Exception:
        challenged = False
    if not challenged:
        return True
    logger.warning("Tyler: AWS WAF human-verification challenge — waiting it out")
    for _ in range(timeout_s // 3):
        await page.wait_for_timeout(3000)
        try:
            if not await page.evaluate(_WAF_MARKER_JS):
                logger.info("Tyler: AWS WAF challenge cleared")
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            # evaluate can fail mid-navigation as the challenge reloads — fine
            continue
    logger.error("Tyler: AWS WAF challenge did NOT clear after %ds", timeout_s)
    return False


async def accept_disclaimer_and_open_search(page: Page) -> bool:
    """disclaimer → Accept → click the OPR search link. Returns True on the
    Document Search form (bypasses the direct-URL human-verification gate)."""
    await page.goto(DISCLAIMER_URL, wait_until="domcontentloaded", timeout=45000)
    await pass_aws_waf(page)
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


async def add_doc_type(page: Page, value: str) -> bool:
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
        # WARNING, not debug — an invisible interaction failure here is how
        # doc-type selection dies silently.
        logger.warning("Tyler doc-type %r interaction failed: %s", value, e)
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


async def set_window_dates(page: Page, start: datetime, end: datetime) -> None:
    """Set the recorded-date range (JS-set + change event; the jQuery-mobile
    date inputs need change to register)."""
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
        [date_str(start), date_str(end)],
    )


async def click_search(page: Page) -> bool:
    try:
        await page.locator("#searchButton").click()
        return True
    except Exception as e:
        logger.warning("Tyler #searchButton click failed: %s", e)
        return False


async def recover_search_session(
    page: Page, doc_type_names: list[str], label: str
) -> bool:
    """Re-arm a session whose searchResults fetch started returning 405.

    The 405 means the portal's "Human Verification" gate re-armed mid-session
    (the disclaimer flow is what disarms it). Re-runs the full disclaimer →
    Accept → OPR-link flow and re-adds the doc-type chips. The caller must then
    re-set the window dates and re-click search before retrying the fetch.
    """
    logger.warning(
        "%s: results fetch got 405 — re-running the disclaimer/session flow "
        "to disarm the human-verification gate", label,
    )
    try:
        if not await accept_disclaimer_and_open_search(page):
            logger.error("%s: session recovery failed — search form never rendered", label)
            return False
        added = []
        for name in doc_type_names:
            if await add_doc_type(page, name):
                added.append(name)
        if not added:
            logger.error("%s: session recovery failed — no doc types re-selected", label)
            return False
        logger.info("%s: session recovered, doc types re-selected: %s", label, added)
        return True
    except Exception as e:
        logger.error("%s: session recovery raised: %s", label, e)
        return False

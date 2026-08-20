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

from playwright.async_api import async_playwright

from notice_parser import NoticeData, normalize_court_name
from scrapers import register
from scrapers.tccsearch_common import proxy_kwargs as _proxy_kwargs
from scrapers.lien_common import pick_debtor
from scrapers import tyler_common as tc
from scrapers.debug_capture import dump_page, dump_text

logger = logging.getLogger(__name__)

BASE = tc.BASE

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


def _force_headless() -> bool:
    return os.getenv("LIEN_TYLER_HEADLESS", "").strip().lower() in ("1", "true", "yes")


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

        self.last_meta = {
            "window_days": (to_date - from_date).days + 1,
            "returned": 0, "kept": 0,
        }
        headless = _force_headless()
        logger.info(
            "Williamson lien scrape (Tyler Self-Service): range=%s..%s headless=%s",
            tc.date_str(from_date), tc.date_str(to_date), headless,
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
                # Cloud egress fix (2026-08-19): route through the residential
                # proxy when configured — the datacenter IP is what AWS WAF
                # (Williamson Tyler) challenges endlessly and what heats the
                # GovOS anti-bot on the second publicsearch session per run.
                # No-op locally (SCRAPER_PROXY_URL unset).
                **_proxy_kwargs(fresh_session=True),
                user_agent=tc.USER_AGENT,
                viewport={"width": 1400, "height": 1300},
                ignore_https_errors=True,
            )
            await context.add_init_script(tc.INIT_SCRIPT)
            page = await context.new_page()
            page.set_default_timeout(45000)

            try:
                if not await tc.accept_disclaimer_and_open_search(page):
                    await dump_page(page, "wilco_lien_form_never_rendered")
                    from scrapers import ScraperError
                    raise ScraperError(
                        "Williamson lien: Document Search form never rendered "
                        "(disclaimer/human-verification flow failed)"
                    )

                added = []
                for name in LIEN_DOC_TYPE_NAMES:
                    if await tc.add_doc_type(page, name):
                        added.append(name)
                logger.info("Williamson lien: doc types selected: %s", added or "(none)")
                if not added:
                    await dump_page(page, "wilco_lien_no_doctypes")
                    from scrapers import ScraperError
                    raise ScraperError(
                        "Williamson lien: no lien doc types could be selected — "
                        "aborted to avoid an unfiltered search"
                    )

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

                recovered_once = False
                failed_windows = 0
                last_fail_status = None
                for win_start, win_end in windows:
                    await tc.set_window_dates(page, win_start, win_end)
                    if not await tc.click_search(page):
                        logger.warning(
                            "Williamson lien: search click failed for %s..%s",
                            tc.date_str(win_start), tc.date_str(win_end),
                        )
                        failed_windows += 1
                        continue
                    await page.wait_for_timeout(4000)

                    win_new = 0
                    window_ok = True
                    for page_num in range(1, MAX_PAGES + 1):
                        try:
                            res = await tc.fetch_results_page(page, page_num)
                        except Exception as e:
                            logger.warning("Williamson lien: page %d fetch error: %s", page_num, e)
                            break
                        if (not res or not res.get("ok")) and page_num == 1 and not recovered_once:
                            # Human-verification gate re-armed (the 405). Recover
                            # the session ONCE per scrape, redo this window, retry.
                            recovered_once = True
                            dump_text(
                                "wilco_lien_fetch_fail",
                                f"status={(res or {}).get('status')}\n"
                                f"snippet:\n{(res or {}).get('snippet', '')}",
                            )
                            if await tc.recover_search_session(
                                page, LIEN_DOC_TYPE_NAMES, "Williamson lien"
                            ):
                                await tc.set_window_dates(page, win_start, win_end)
                                if await tc.click_search(page):
                                    await page.wait_for_timeout(4000)
                                    try:
                                        res = await tc.fetch_results_page(page, page_num)
                                    except Exception:
                                        res = None
                        if not res or not res.get("ok"):
                            if page_num == 1:
                                last_fail_status = (res or {}).get("status")
                                logger.warning(
                                    "Williamson lien: results fetch failed (status %s) for %s..%s",
                                    last_fail_status,
                                    tc.date_str(win_start), tc.date_str(win_end),
                                )
                                window_ok = False
                            break
                        rows = res.get("rows") or []
                        self.last_meta["returned"] += len(rows)
                        if not rows:
                            break

                        for row in rows:
                            docid = (row.get("docid") or "").strip()
                            if docid and docid in seen_docids:
                                continue
                            instrument, doctype, date_iso = tc.parse_h1(row.get("h1", ""))
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
                            notice.lien_creditor = tc.clean_name(creditor)
                            notice.tax_owner_name = debtor.upper()
                            notice.owner_name = normalize_court_name(tc.clean_name(debtor))
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
                                self.last_meta["kept"] = len(notices)
                                await browser.close()
                                return notices

                        await asyncio.sleep(REQUEST_DELAY)

                    if not window_ok:
                        failed_windows += 1
                    logger.info(
                        "Williamson lien: window %s..%s → %d new liens (running %d)",
                        tc.date_str(win_start), tc.date_str(win_end), win_new, len(notices),
                    )

                if windows and failed_windows == len(windows):
                    # Every window failed even after one session recovery —
                    # raise so the run-health report alerts on day 1.
                    from scrapers import ScraperError
                    raise ScraperError(
                        f"Williamson lien: results fetch failed in all "
                        f"{failed_windows} window(s) (last status {last_fail_status}) "
                        f"even after session recovery"
                    )

                self.last_meta["kept"] = len(notices)
                logger.info(
                    "Williamson lien: %d lien records parsed (Tyler Self-Service)",
                    len(notices),
                )
            except Exception as e:
                # Fail LOUD through ScraperError (run-health alerts) while
                # keeping whatever windows completed before the failure.
                from scrapers import ScraperError
                if isinstance(e, ScraperError):
                    e.partial = notices
                    raise
                logger.error("Williamson lien: scrape failed: %s", e, exc_info=True)
                raise ScraperError(f"Williamson lien: {e}", partial=notices) from e
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        return notices


# Registered in scrapers/__init__ (after lien_publicsearch, whose Williamson
# registration has been removed) so Williamson/lien resolves to this Tyler scraper.
register("Williamson", "lien")(WilliamsonTylerLienScraper)

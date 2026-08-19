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

from playwright.async_api import async_playwright

from notice_parser import NoticeData, normalize_court_name
from scrapers import register
from scrapers.lis_pendens_common import pick_defendant
from scrapers import tyler_common as tc
from scrapers.debug_capture import dump_page, dump_text

logger = logging.getLogger(__name__)

BASE = tc.BASE

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


def _force_headless() -> bool:
    return os.getenv("LIS_PENDENS_TYLER_HEADLESS", "").strip().lower() in ("1", "true", "yes")


def _is_lis_pendens_doctype(doctype: str) -> bool:
    up = (doctype or "").upper()
    if not up or "LIS PENDENS" not in up:
        return False
    return not any(x in up for x in _LP_EXCLUDE)


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

        self.last_meta = {
            "window_days": (to_date - from_date).days + 1,
            "returned": 0, "kept": 0,
        }
        headless = _force_headless()
        logger.info(
            "Williamson lis pendens scrape (Tyler Self-Service): range=%s..%s headless=%s",
            tc.date_str(from_date), tc.date_str(to_date), headless,
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
                user_agent=tc.USER_AGENT,
                viewport={"width": 1400, "height": 1300},
                ignore_https_errors=True,
            )
            await context.add_init_script(tc.INIT_SCRIPT)
            page = await context.new_page()
            page.set_default_timeout(45000)

            try:
                if not await tc.accept_disclaimer_and_open_search(page):
                    await dump_page(page, "wilco_lp_form_never_rendered")
                    from scrapers import ScraperError
                    raise ScraperError(
                        "Williamson lis pendens: Document Search form never rendered "
                        "(disclaimer/human-verification flow failed)"
                    )

                added = []
                for name in LIS_PENDENS_DOC_TYPE_NAMES:
                    if await tc.add_doc_type(page, name):
                        added.append(name)
                logger.info("Williamson lis pendens: doc types selected: %s", added or "(none)")
                if not added:
                    await dump_page(page, "wilco_lp_no_doctypes")
                    from scrapers import ScraperError
                    raise ScraperError(
                        "Williamson lis pendens: no lis pendens doc type could be "
                        "selected — aborted to avoid an unfiltered search"
                    )

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

                recovered_once = False
                failed_windows = 0
                last_fail_status = None
                for win_start, win_end in windows:
                    await tc.set_window_dates(page, win_start, win_end)
                    if not await tc.click_search(page):
                        logger.warning(
                            "Williamson lis pendens: search click failed for %s..%s",
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
                            logger.warning("Williamson lis pendens: page %d fetch error: %s", page_num, e)
                            break
                        if (not res or not res.get("ok")) and page_num == 1 and not recovered_once:
                            # The human-verification gate re-armed (405 for 27
                            # straight days before this existed). Recover the
                            # session ONCE per scrape, redo this window, retry.
                            recovered_once = True
                            dump_text(
                                "wilco_lp_fetch_fail",
                                f"status={(res or {}).get('status')}\n"
                                f"snippet:\n{(res or {}).get('snippet', '')}",
                            )
                            if await tc.recover_search_session(
                                page, LIS_PENDENS_DOC_TYPE_NAMES, "Williamson lis pendens"
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
                                    "Williamson lis pendens: results fetch failed (status %s) for %s..%s",
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
                            notice.lien_creditor = tc.clean_name(plaintiff)
                            notice.tax_owner_name = defendant.upper()
                            notice.owner_name = normalize_court_name(tc.clean_name(defendant))
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
                                self.last_meta["kept"] = len(notices)
                                await browser.close()
                                return notices

                        await asyncio.sleep(REQUEST_DELAY)

                    if not window_ok:
                        failed_windows += 1
                    logger.info(
                        "Williamson lis pendens: window %s..%s → %d new (running %d)",
                        tc.date_str(win_start), tc.date_str(win_end), win_new, len(notices),
                    )

                if windows and failed_windows == len(windows):
                    # Every window failed even after one session recovery —
                    # raise so the run-health report alerts on day 1, not day 27.
                    from scrapers import ScraperError
                    raise ScraperError(
                        f"Williamson lis pendens: results fetch failed in all "
                        f"{failed_windows} window(s) (last status {last_fail_status}) "
                        f"even after session recovery"
                    )

                self.last_meta["kept"] = len(notices)
                logger.info(
                    "Williamson lis pendens: %d records parsed (Tyler Self-Service)",
                    len(notices),
                )
            except Exception as e:
                # Fail LOUD through ScraperError (run-health alerts) while
                # keeping whatever windows completed before the failure.
                from scrapers import ScraperError
                if isinstance(e, ScraperError):
                    e.partial = notices
                    raise
                logger.error("Williamson lis pendens: scrape failed: %s", e, exc_info=True)
                raise ScraperError(f"Williamson lis pendens: {e}", partial=notices) from e
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

        return notices

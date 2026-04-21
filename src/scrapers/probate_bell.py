"""Bell County probate scraper — bell.tx.publicsearch.us (County Clerk).

Scrapes probate filings from Bell County's Neumo-powered public records
search. No CAPTCHA, no login required.

Site: https://bell.tx.publicsearch.us/
Search: Quick Search with "probate" doc type filter, date range

Results grid columns:
  GRANTOR = decedent name (e.g., "ADAMS VERA N DECD")
  GRANTEE = filing source (county clerk)
  DOC TYPE = "PROBATE"
  RECORDED DATE = filing date
  INST NUMBER = instrument number
"""

import logging
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

from notice_parser import NoticeData
from scrapers import register

logger = logging.getLogger(__name__)

BASE_URL = "https://bell.tx.publicsearch.us"

_DECD_SUFFIX_RE = re.compile(r"\s+(?:DECD|DECEASED)\.?$", re.IGNORECASE)
_AKA_RE = re.compile(r"\s+AKA\s*$", re.IGNORECASE)


def _clean_decedent_name(raw: str) -> str:
    """Clean up a decedent name from the clerk's index.

    Input:  "ADAMS VERA N DECD"
    Output: "Adams Vera N"
    """
    name = raw.strip()
    name = _AKA_RE.sub("", name).strip()
    name = _DECD_SUFFIX_RE.sub("", name).strip()
    if name.isupper() or name.islower():
        name = name.title()
    return name


@register("Bell", "probate")
class BellProbateScraper:
    """Scrape probate filings from Bell County public records search."""

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
            "Bell probate scrape: mode=%s, range=%s to %s",
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
                ignore_https_errors=True,
            )
            page = await context.new_page()
            page.set_default_timeout(30000)

            try:
                await page.goto(BASE_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(3000)

                # Close any tour/popup
                close_btns = page.locator('[class*="close"], button:has-text("×")')
                for i in range(await close_btns.count()):
                    try:
                        if await close_btns.nth(i).is_visible():
                            await close_btns.nth(i).click()
                            await page.wait_for_timeout(500)
                    except Exception:
                        pass

                # Set date range
                date_from_str = from_date.strftime("%-m/%-d/%Y")
                date_to_str = to_date.strftime("%-m/%-d/%Y")

                # The date fields are text inputs — clear and fill
                date_inputs = await page.locator('input[type="text"]').all()
                for inp in date_inputs:
                    val = await inp.input_value()
                    # Find the start date field (has old date like 1/1/1600)
                    if "/" in val and "1600" in val:
                        await inp.click(click_count=3)
                        await inp.fill(date_from_str)
                        logger.debug("Set start date: %s", date_from_str)

                # Search for "probate" doc type
                await page.fill("#basicSearchInputBox", "probate")
                logger.info("Searching for 'probate' docs, %s to %s", date_from_str, date_to_str)

                # Click Search
                search_btn = page.locator("button:has-text('Search')")
                await search_btn.first.click()
                await page.wait_for_timeout(5000)

                # Check for results count
                body = await page.inner_text("body")
                count_match = re.search(r"(\d[\d,]*)\s+results?\s+for", body)
                total = int(count_match.group(1).replace(",", "")) if count_match else 0
                logger.info("Search returned %d results", total)

                if total == 0:
                    await browser.close()
                    return []

                # Parse results from the table — extract via JS for speed
                # Table has 3 leading empty cells (checkbox, icon, expand) then data columns:
                # [3]=Grantor [4]=Grantee [5]=DocType [6]=RecDate [7]=InstNum [8]=Book [9]=PropDesc
                rows = await page.evaluate("""
                    (() => {
                        var trs = document.querySelectorAll('table tr');
                        var results = [];
                        trs.forEach(tr => {
                            var cells = tr.querySelectorAll('td');
                            if (cells.length < 8) return;
                            var grantor = cells[3] ? cells[3].textContent.trim() : '';
                            var grantee = cells[4] ? cells[4].textContent.trim() : '';
                            var docType = cells[5] ? cells[5].textContent.trim() : '';
                            var recDate = cells[6] ? cells[6].textContent.trim() : '';
                            var instNum = cells[7] ? cells[7].textContent.trim() : '';
                            if (docType.toUpperCase() === 'PROBATE' && grantor) {
                                results.push({grantor: grantor, grantee: grantee, docType: docType, recDate: recDate, instNum: instNum});
                            }
                        });
                        return results;
                    })()
                """)

                logger.info("Extracted %d probate rows from results table", len(rows))

                for row in rows:
                    if max_notices and len(all_notices) >= max_notices:
                        break

                    from notice_parser import normalize_court_name
                    decedent = normalize_court_name(_clean_decedent_name(row.get("grantor", "")))
                    if not decedent:
                        continue

                    notice = NoticeData(
                        notice_type="probate",
                        county="Bell",
                        state="TX",
                        decedent_name=decedent,
                        source_url=f"{BASE_URL}/?instNum={row.get('instNum', '')}",
                    )

                    # Parse date
                    rec_date = row.get("recDate", "")
                    if rec_date:
                        try:
                            dt = datetime.strptime(rec_date, "%m/%d/%Y")
                            notice.date_added = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass

                    notice.raw_text = (
                        f"Grantor: {row.get('grantor', '')}\n"
                        f"Grantee: {row.get('grantee', '')}\n"
                        f"Type: {row.get('docType', '')}\n"
                        f"Date: {rec_date}\n"
                        f"Instrument: {row.get('instNum', '')}"
                    )

                    all_notices.append(notice)

            except Exception as e:
                logger.error("Bell probate scraper failed: %s", e)
                raise
            finally:
                await browser.close()

        logger.info(
            "Bell probate scrape complete: %d notices from %d results",
            len(all_notices), total,
        )
        return all_notices

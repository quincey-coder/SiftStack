"""Bell County foreclosure scraper — monthly PDFs from county clerk.

Downloads monthly foreclosure notice PDFs from the Bell County Clerk's
website using Playwright (site requires browser-like requests).

Source: https://www.bellcountytx.com/county_government/county_clerk/foreclosures.php
PDF pattern: "May 1.pdf" through "May N.pdf" (accumulates weekly)

Each PDF contains one or more foreclosure notices with:
  - Property address ("commonly known as ...")
  - Owner/borrower name ("executed by ...")
  - Sale date (first Tuesday of month)
  - Trustee information

The PDFs are image scans. OCR is budgeted per run (SIFT_BELL_OCR_BUDGET_S,
default 900 s) and every PDF is remembered in `scrapers.pdf_notice_state`
(sha256 + size + pages done + parsed notices), so a daily run REPLAYS the
PDFs it already read for free and spends the budget only on new, re-posted
or unfinished ones. Before 2026-09-09 a 420 s module-global budget was
re-spent on the same first ~4 PDFs every run and PDFs 5+ of the month were
never read at all ("OCR also returned no text — skipping").
"""

import logging
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

from notice_parser import NoticeData
from scrapers import register
from scrapers import pdf_notice_state as pdf_state
from scrapers.pdf_ocr import budget_from_env, has_text, ocr_pdf_bytes, text_layer
from scrapers.trustee_notice_parse import (
    first_tuesday,
    parse_trustee_notice_text,
    sale_date_from_month_label,
    split_notices,
)

logger = logging.getLogger(__name__)

BELL_FORECLOSURE_URL = "https://www.bellcountytx.com/county_government/county_clerk/foreclosures.php"

# Bump when the notice text parser changes so remembered PDFs are re-parsed
# (budgeted — the backlog chips through over successive runs).
PARSER_VERSION = 1
OCR_BUDGET_ENV = "SIFT_BELL_OCR_BUDGET_S"
OCR_BUDGET_DEFAULT_S = 900.0
COUNTY = "Bell"

# Back-compat aliases (the helpers now live in scrapers.trustee_notice_parse).
_first_tuesday = first_tuesday
_sale_date_from_pdf_month = sale_date_from_month_label


def _parse_notice_text(text: str) -> NoticeData | None:
    """Parse a single Bell foreclosure notice from PDF text."""
    return parse_trustee_notice_text(text, county=COUNTY)


def _new_meta() -> dict:
    return {
        "pdfs_total": 0,
        "pdfs_unchanged": 0,   # replayed from memory, no OCR
        "pdfs_new": 0,         # new / re-posted / unfinished → text layer or OCR
        "pdfs_ocr": 0,
        "ocr_secs": 0.0,
        "budget_exhausted": False,
        "returned": 0,         # notice chunks seen in the text (run-health parse-zero)
        "kept": 0,
    }


def _order_pdfs(target_pdfs: list[dict], state: dict) -> list[dict]:
    """Unseen / unfinished PDFs first (newest posting first), then known ones."""
    pending, known = [], []
    for info in target_pdfs:
        entry = state["pdfs"].get(info["href"])
        if entry and entry.get("complete"):
            known.append(info)
        else:
            pending.append(info)
    pending.reverse()   # the clerk appends newest links last
    return pending + known


@register("Bell", "foreclosure")
class BellForeclosureScraper:
    """Scrape foreclosure notices from Bell County Clerk monthly PDFs."""

    def __init__(self, **_kw):
        self.last_meta: dict = {}

    async def scrape(
        self,
        mode: str = "daily",
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]:
        logger.info("Bell foreclosure scrape: mode=%s", mode)

        # Fresh budget per call — a scrape_targets retry must not re-enter spent.
        budget = budget_from_env(OCR_BUDGET_ENV, OCR_BUDGET_DEFAULT_S, "Bell foreclosure")
        state = pdf_state.load_state(COUNTY)
        self.last_meta = _new_meta()
        meta = self.last_meta
        all_notices: list[NoticeData] = []
        keep_months: set[str] = set()

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            page.set_default_timeout(30000)

            try:
                await page.goto(BELL_FORECLOSURE_URL, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)

                # Find all PDF links for current/upcoming months
                pdf_links = await page.evaluate("""
                    (() => {
                        var links = document.querySelectorAll('a[href$=".pdf"], a[href*=".pdf?"]');
                        var results = [];
                        links.forEach(a => {
                            var text = a.textContent.trim();
                            var href = a.href;
                            if (text.toLowerCase().includes('foreclosure')) {
                                results.push({text: text, href: href});
                            }
                        });
                        return results;
                    })()
                """)

                if not pdf_links:
                    logger.info("No foreclosure PDF links found on Bell County page")
                    await browser.close()
                    return []

                # Filter to recent months based on mode
                now = datetime.now()
                current_month = now.strftime("%B")
                next_month = (now.replace(day=1) + timedelta(days=32)).strftime("%B")

                if mode == "historical":
                    target_pdfs = pdf_links  # All available
                else:
                    # Current and next month only
                    target_pdfs = [
                        l for l in pdf_links
                        if current_month.lower() in l["text"].lower()
                        or next_month.lower() in l["text"].lower()
                    ]

                for info in target_pdfs:
                    sd = sale_date_from_month_label(info["text"], info["href"], now)
                    if sd:
                        keep_months.add(sd[:7])

                ordered = _order_pdfs(target_pdfs, state)
                meta["pdfs_total"] = len(ordered)
                logger.info(
                    "Found %d foreclosure PDFs to process (%d remembered, %d new/unfinished; "
                    "OCR budget %.0fs)",
                    len(ordered), len(ordered) - len(
                        [i for i in ordered if not (state["pdfs"].get(i["href"]) or {}).get("complete")]
                    ),
                    len([i for i in ordered if not (state["pdfs"].get(i["href"]) or {}).get("complete")]),
                    budget.limit_s,
                )

                for pdf_info in ordered:
                    if max_notices and len(all_notices) >= max_notices:
                        break
                    try:
                        notices = await self._process_pdf(context, pdf_info, state, budget, now)
                    except Exception as e:
                        logger.warning("  Error processing %s: %s", pdf_info.get("text"), e)
                        continue
                    all_notices.extend(notices)

            except Exception as e:
                logger.error("Bell foreclosure scraper failed: %s", e)
                raise
            finally:
                await browser.close()

        meta["kept"] = len(all_notices)
        meta["ocr_secs"] = round(meta["ocr_secs"], 1)

        # Bookkeeping never breaks a scrape; smoke runs (max_notices) never write state.
        if not max_notices:
            try:
                pdf_state.prune(state, keep_months)
                pdf_state.commit_run(COUNTY, state, dict(meta))
            except Exception as e:
                logger.warning("Bell foreclosure: state commit failed: %s", e)

        if meta["budget_exhausted"] and meta["pdfs_ocr"] == 0:
            logger.warning(
                "Bell foreclosure: OCR budget (%.0fs) allowed ZERO pages this run — "
                "check %s", budget.limit_s, OCR_BUDGET_ENV,
            )
        logger.info(
            "Bell foreclosure scrape complete: %d notices (%d PDFs: %d replayed, %d read, "
            "%d OCR'd in %.0fs%s)",
            len(all_notices), meta["pdfs_total"], meta["pdfs_unchanged"], meta["pdfs_new"],
            meta["pdfs_ocr"], meta["ocr_secs"],
            "; budget exhausted — unfinished PDFs resume next run" if meta["budget_exhausted"] else "",
        )
        return all_notices

    async def _process_pdf(self, context, pdf_info: dict, state: dict, budget, now: datetime) -> list[NoticeData]:
        """Download one PDF; replay it from memory or read/OCR it under the budget."""
        meta = self.last_meta
        href = pdf_info["href"]
        label = pdf_info["text"]
        month_sale_date = sale_date_from_month_label(label, href, now)
        month = month_sale_date[:7] if month_sale_date else ""
        today = now.strftime("%Y-%m-%d")

        resp = await context.request.get(href)
        if resp.status != 200:
            logger.warning("  Failed to download %s: HTTP %d", label, resp.status)
            return []
        pdf_bytes = await resp.body()
        if len(pdf_bytes) < 100:
            logger.warning("  PDF too small: %d bytes (%s)", len(pdf_bytes), label)
            return []

        sha256, size = pdf_state.fingerprint(pdf_bytes)
        entry = state["pdfs"].get(href)
        if pdf_state.is_unchanged(entry, sha256, size, PARSER_VERSION) and entry.get("complete"):
            pdf_state.touch(state, href, today)
            meta["pdfs_unchanged"] += 1
            meta["returned"] += int(entry.get("returned") or 0)
            notices = pdf_state.replay(entry, COUNTY, href)
            logger.info("  %s: unchanged — replayed %d notice(s) from memory", label, len(notices))
            return notices

        meta["pdfs_new"] += 1
        # Resume only when the bytes are the same ones we started on.
        resume = entry if (entry and entry.get("sha256") == sha256 and not entry.get("complete")) else None
        text = text_layer(pdf_bytes)
        text_source = "layer"
        pages_total = pages_done = 0
        complete = True
        partial_text = ""

        if not has_text(text):
            start = int(resume.get("pages_done") or 0) if resume else 0
            prefix = (resume.get("partial_text") or "") if resume else ""
            logger.info(
                "  %s: no text layer — OCR (%s, budget %.0fs left)",
                label, f"resuming at page {start + 1}" if start else "from page 1", budget.remaining(),
            )
            res = ocr_pdf_bytes(
                pdf_bytes, budget, start_page=start, prefix_text=prefix, label=f"Bell {label}",
            )
            meta["ocr_secs"] += res.secs
            if res.pages_done > start:
                meta["pdfs_ocr"] += 1
            text, text_source = res.text, "ocr"
            pages_total, pages_done, complete = res.pages_total, res.pages_done, res.complete
            if res.engine_error:
                # OCR engine broken (no Tesseract?) — keep the PDF pending, never seal it.
                meta["ocr_engine_error"] = res.engine_error
                logger.error("  %s: OCR engine failed (%s) — PDF stays pending", label, res.engine_error)
                partial_text = (resume.get("partial_text") or "") if resume else ""
            elif not complete:
                meta["budget_exhausted"] = True
                partial_text = text
            if not has_text(text) or res.engine_error:
                if complete:
                    text_source = "none"
                    logger.warning(
                        "  %s: no text layer and OCR returned nothing (%d page(s))",
                        label, pages_total,
                    )
                else:
                    logger.info(
                        "  %s: OCR budget exhausted at page %d/%d — resumes next run",
                        label, pages_done, pages_total,
                    )
                pdf_state.record_pdf(
                    state, href, sha256=sha256, size=size, label=label, month=month,
                    parser_version=PARSER_VERSION, text_source=text_source,
                    pages_total=pages_total, pages_done=pages_done, complete=complete,
                    partial_text=partial_text, returned=0, notices=[], today=today,
                )
                return []
            logger.info("  %s: OCR extracted %d chars (%d/%d pages)", label, len(text), pages_done, pages_total)

        chunks = split_notices(text)
        meta["returned"] += len(chunks)
        notices: list[NoticeData] = []
        filled_from_month = 0
        for chunk in chunks:
            notice = parse_trustee_notice_text(
                chunk, county=COUNTY, source_url=href,
                fallback_sale_date=month_sale_date, date_added=today,
            )
            if not notice:
                continue
            if month_sale_date and notice.auction_date == month_sale_date:
                filled_from_month += 1
            notices.append(notice)

        pdf_state.record_pdf(
            state, href, sha256=sha256, size=size, label=label, month=month,
            parser_version=PARSER_VERSION, text_source=text_source,
            pages_total=pages_total, pages_done=pages_done, complete=complete,
            partial_text=partial_text, returned=len(chunks), notices=notices, today=today,
        )
        logger.info(
            "  Extracted %d notice(s) from %s (%d chunks; sale date %s; %d dated from PDF month%s)",
            len(notices), label, len(chunks), month_sale_date or "unknown", filled_from_month,
            f"; partial {pages_done}/{pages_total} pages, resumes next run" if not complete else "",
        )
        return notices

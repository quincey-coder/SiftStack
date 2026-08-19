"""Shared verification helpers for the GovOS publicsearch scrapers (Bell lien +
Bell lis pendens).

THE SILENT FAILURE THIS EXISTS FOR: on ~10 of the 30 days before 2026-08-18,
the recordedDateRange filter silently failed to apply — the search ran with doc
types but NO date bound, so a 1-day window pulled the portal's 2500-row result
cap of junk. The run logged the intended range, looked healthy, and uploaded
thousands of stale records into dedup. Nothing verified that the filter the
code SET was the filter the portal APPLIED.

`verify_window_applied` reads the RESULTS back — the total-results header and
the recorded dates actually present on page 1 — and tells the caller whether
the window stuck. Callers retry once (re-fill dates, re-search) and then raise
ScraperError so the run-health report alerts.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from playwright.async_api import Page

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")
# "1 - 50 of 2,500 results" / "2,500 results" header variants
_TOTAL_RE = re.compile(
    r"(?:of\s+)?([\d,]+)\s+results?\b", re.IGNORECASE
)
# A ≤7-day window with more results than this = the date filter didn't apply.
DEFAULT_SANITY_MAX = 500
# Share of page-1 row dates outside the requested window that flags a failure.
OUTSIDE_SHARE_MAX = 0.30


def _parse_total(text: str) -> int | None:
    m = _TOTAL_RE.search(text or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


async def verify_window_applied(
    page: Page,
    from_date: datetime,
    to_date: datetime,
    label: str,
    sanity_max: int = DEFAULT_SANITY_MAX,
) -> tuple[bool, dict]:
    """Check that the results actually honor the requested recorded-date window.

    Two independent probes on the rendered results page:
      1. Total-results header: a ≤7-day window with > sanity_max results.
      2. Row dates: > OUTSIDE_SHARE_MAX of page-1 recorded dates outside
         [from_date - 1d, to_date + 1d].

    Returns (ok, evidence) where evidence carries {"total", "outside_share",
    "window_days"} for run-health.
    """
    window_days = (to_date - from_date).days + 1
    evidence: dict = {"window_days": window_days}
    try:
        text = await page.evaluate(
            "() => document.querySelector('#main-content, main, [role=main]')?.innerText "
            "|| document.body.innerText"
        )
    except Exception:
        text = ""

    total = _parse_total(text)
    if total is not None:
        evidence["total"] = total

    lo = from_date - timedelta(days=1)
    hi = to_date + timedelta(days=1)
    dates: list[datetime] = []
    for raw in _DATE_RE.findall(text or ""):
        try:
            dates.append(datetime.strptime(raw, "%m/%d/%Y"))
        except ValueError:
            continue
    outside = sum(1 for d in dates if d < lo or d > hi)
    outside_share = (outside / len(dates)) if dates else 0.0
    evidence["outside_share"] = round(outside_share, 2)

    if window_days <= 7 and total is not None and total > sanity_max:
        logger.warning(
            "%s: %d results for a %d-day window (> %d) — date filter did NOT apply",
            label, total, window_days, sanity_max,
        )
        evidence["hit_cap"] = True
        return False, evidence
    if len(dates) >= 5 and outside_share > OUTSIDE_SHARE_MAX:
        logger.warning(
            "%s: %.0f%% of page-1 recorded dates fall outside %s..%s — "
            "date filter did NOT apply",
            label, outside_share * 100,
            from_date.strftime("%m/%d/%Y"), to_date.strftime("%m/%d/%Y"),
        )
        return False, evidence
    return True, evidence


def force_window_url(current_url: str, from_date: datetime, to_date: datetime) -> str | None:
    """Rewrite a GovOS results URL to carry the requested recorded-date window.

    The results page is a plain GET (verified live, Bell 2026-08-19):
      /results?department=RP&docTypes=T2&recordedDateRange=20260815%2C20260818
              &searchType=advancedSearch
    In the cloud the advanced form silently drops the date range from the
    submitted query (the inputs HOLD the dates in the DOM — read-back verified
    — yet the query runs unfiltered), while the doc-type chips DO make it into
    the URL. So the bulletproof fallback is URL surgery: keep everything the
    working part of the form encoded, and force recordedDateRange ourselves.

    Returns the corrected URL, or None when current_url is not a results URL.
    """
    from urllib.parse import urlsplit, parse_qs, urlencode, urlunsplit

    parts = urlsplit(current_url)
    if "/results" not in parts.path:
        return None
    q = parse_qs(parts.query)
    q["recordedDateRange"] = [f"{from_date:%Y%m%d},{to_date:%Y%m%d}"]
    q.setdefault("searchType", ["advancedSearch"])
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(q, doseq=True), "")
    )


async def apply_dates(
    page: Page, from_date: datetime, to_date: datetime, label: str = "publicsearch"
) -> bool:
    """Set the recordedDateRange inputs and VERIFY the values actually took.

    The cloud failure mode this exists for (Bell, 2026-08-16..19): page.fill
    reported success but the React controlled input later re-rendered EMPTY —
    hydration on a slow proxy connection wipes the value — so the search ran
    unfiltered (163K results for a 1-day window) on BOTH retry attempts, four
    days straight. The same flow works first-try on a fast local session,
    which is why the fill can never be trusted without reading the DOM back.

    Attempt 1 is a normal fill; attempts 2-3 use the React native-setter +
    event-dispatch pattern (the documented workaround when a controlled input
    swallows Playwright's fill). Returns False only when the read-back never
    matches — callers must NOT click Search in that case.
    """
    want = [from_date.strftime("%m/%d/%Y"), to_date.strftime("%m/%d/%Y")]
    for attempt in range(3):
        try:
            if attempt == 0:
                await page.fill("#recordedDateRange-start", want[0])
                await page.keyboard.press("Escape")
                await page.fill("#recordedDateRange-end", want[1])
                await page.keyboard.press("Escape")
            else:
                await page.evaluate(
                    """([start, end]) => {
                        const set = (id, val) => {
                            const el = document.querySelector(id);
                            if (!el) return;
                            const setter = Object.getOwnPropertyDescriptor(
                                window.HTMLInputElement.prototype, 'value').set;
                            setter.call(el, val);
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                            el.dispatchEvent(new Event('blur', {bubbles: true}));
                        };
                        set('#recordedDateRange-start', start);
                        set('#recordedDateRange-end', end);
                    }""",
                    want,
                )
                await page.keyboard.press("Escape")
            # Let any hydration/re-render land, THEN read back what the DOM
            # actually holds — that is the only trustworthy signal.
            await page.wait_for_timeout(1200)
            got = await page.evaluate(
                "() => [document.querySelector('#recordedDateRange-start')?.value || '',"
                " document.querySelector('#recordedDateRange-end')?.value || '']"
            )
            if list(got) == want:
                if attempt:
                    logger.info(
                        "%s: date range took on attempt %d (native setter)",
                        label, attempt + 1,
                    )
                return True
            logger.warning(
                "%s: date inputs read back %r, wanted %r (attempt %d/3)",
                label, got, want, attempt + 1,
            )
        except Exception as e:
            logger.warning("%s: date fill attempt %d failed: %s", label, attempt + 1, e)
    return False

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


async def refill_dates(page: Page, from_date: datetime, to_date: datetime) -> bool:
    """Re-fill the recordedDateRange inputs (retry path after a failed verify)."""
    try:
        await page.fill("#recordedDateRange-start", from_date.strftime("%m/%d/%Y"))
        await page.keyboard.press("Escape")
        await page.fill("#recordedDateRange-end", to_date.strftime("%m/%d/%Y"))
        await page.keyboard.press("Escape")
        return True
    except Exception as e:
        logger.warning("could not re-fill date range: %s", e)
        return False

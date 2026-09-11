"""Shared rolling lookback window for every date-windowed scraper.

Why (2026-09-09): county clerks post rows INCOMPLETE and fill them in later —
Travis tccsearch keeps fresh filings in a nameless "Temp index" for days,
Bell re-posts foreclosure PDFs, Odyssey/GovOS rows gain parties and addresses
after the fact. A daily run that scrapes only "since the last run" sees such a
row once, incomplete, and never again. Every daily window is therefore widened
to AT LEAST today - DAILY_LOOKBACK_DAYS .. today; cross-run dedup
(`main._seal_seen_ids`, which seals only COMPLETE records) absorbs the repeats
and lets the late-completed rows through.

Semantics: `daily_window` only ever WIDENS. An explicit `since_date` further
back than the lookback (operator `--since`, actor `since_date`) still wins.
`TCC_INDEX_LAG_DAYS` (tccsearch_common) can widen Travis further, never narrow.

Knobs: env `SIFT_DAILY_LOOKBACK_DAYS` (actor input `daily_lookback_days`,
CLI `--lookback-days`) — positive int, default 14.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

DAILY_LOOKBACK_DAYS = 14
ENV_LOOKBACK = "SIFT_DAILY_LOOKBACK_DAYS"
DEFAULT_DAILY_DAYS = 30      # legacy fallback when no since_date is known
HISTORICAL_DAYS = 365


def daily_lookback_days() -> int:
    """DAILY_LOOKBACK_DAYS unless env SIFT_DAILY_LOOKBACK_DAYS is a positive int."""
    raw = os.getenv(ENV_LOOKBACK, "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DAILY_LOOKBACK_DAYS


def daily_window(
    mode: str,
    since_date: str | None,
    *,
    now: datetime | None = None,
    default_days: int = DEFAULT_DAILY_DAYS,
    historical_days: int = HISTORICAL_DAYS,
    log_label: str = "",
) -> tuple[datetime, datetime, int]:
    """Return (from_date, to_date, window_days) for a scrape.

    historical -> now - historical_days (unchanged by the lookback).
    daily      -> parsed since_date (or now - default_days when missing /
                  invalid), then widened to at least now - daily_lookback_days().
    """
    to_date = now or datetime.now()
    if mode == "historical":
        from_date = to_date - timedelta(days=historical_days)
        return from_date, to_date, (to_date - from_date).days

    from_date: datetime | None = None
    if since_date:
        try:
            from_date = datetime.strptime(since_date, "%Y-%m-%d")
        except ValueError:
            logger.warning(
                "%sInvalid since_date %r — using %d-day default",
                f"{log_label}: " if log_label else "", since_date, default_days,
            )
    if from_date is None:
        from_date = to_date - timedelta(days=default_days)

    floor = to_date - timedelta(days=daily_lookback_days())
    if floor < from_date:
        logger.info(
            "%sDaily window widened from %s to %s (rolling %d-day lookback)",
            f"{log_label}: " if log_label else "",
            from_date.strftime("%Y-%m-%d"), floor.strftime("%Y-%m-%d"),
            daily_lookback_days(),
        )
        from_date = floor
    return from_date, to_date, (to_date - from_date).days


def since_iso(
    mode: str,
    since_date: str | None,
    *,
    now: datetime | None = None,
    default_days: int = DEFAULT_DAILY_DAYS,
    historical_days: int = HISTORICAL_DAYS,
    log_label: str = "",
) -> str:
    """Socrata-style floating timestamp (YYYY-MM-DDT00:00:00) for the window start."""
    from_date, _, _ = daily_window(
        mode, since_date, now=now, default_days=default_days,
        historical_days=historical_days, log_label=log_label,
    )
    return from_date.strftime("%Y-%m-%dT00:00:00")

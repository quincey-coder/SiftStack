"""Retention sweep for the cross-run Apify state store (``sift-stack-state``).

Every run archives the raw tax rolls it downloaded (``{county}_tax_raw_<date>_
<time>_<file>``, ~0.6-3 MB each) and, historically, one deep-prospecting PDF
per record. The writers only ever ADDED keys — "keep the most recent 7" was
a comment, not code — so by 2026-09-17 the store held 278 raw rolls (469 MB)
going back to April. This module is the deletion half.

Policy (owner decision 2026-09-17):
  * raw rolls: keep the newest ``keep_raw_per_county`` per county, delete the rest
  * deep-prospecting PDFs (``<slug>_YYYY-MM-DD.pdf``): delete when older than
    ``pdf_max_age_days``
  * everything else (state keys, diff JSONs, latest CSVs) is never touched

``plan_deletions`` is pure (testable); ``sweep`` applies it through an
apify-client async KeyValueStore client (``list_keys`` / ``delete_record``).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

RAW_ROLL_RE = re.compile(r"^(?P<county>[a-z]+)_tax_raw_(?P<date>\d{4}-\d{2}-\d{2})_")
PDF_RE = re.compile(r"^.+_(?P<date>\d{4}-\d{2}-\d{2})\.pdf$")

DEFAULT_KEEP_RAW = 7
DEFAULT_PDF_MAX_AGE_DAYS = 30


def plan_deletions(
    keys: list[str],
    *,
    keep_raw_per_county: int = DEFAULT_KEEP_RAW,
    pdf_max_age_days: int = DEFAULT_PDF_MAX_AGE_DAYS,
    today: date | None = None,
) -> dict[str, list[str]]:
    """Return ``{"raw_rolls": [...], "pdfs": [...]}`` of keys to delete."""
    today = today or date.today()
    by_county: dict[str, list[str]] = {}
    pdfs: list[str] = []
    for k in keys:
        m = RAW_ROLL_RE.match(k)
        if m:
            by_county.setdefault(m.group("county"), []).append(k)
            continue
        m = PDF_RE.match(k)
        if m:
            try:
                d = datetime.strptime(m.group("date"), "%Y-%m-%d").date()
            except ValueError:
                continue
            if (today - d) > timedelta(days=pdf_max_age_days):
                pdfs.append(k)
    raw: list[str] = []
    for county, ks in by_county.items():
        # Key embeds YYYY-MM-DD_HHMMSS right after the prefix, so a plain sort
        # is chronological; the newest N are the tail.
        ks = sorted(ks)
        if keep_raw_per_county >= 0:
            raw.extend(ks[: max(0, len(ks) - keep_raw_per_county)])
    return {"raw_rolls": sorted(raw), "pdfs": sorted(pdfs)}


async def list_all_keys(store) -> list[str]:
    """Every key in an apify-client async KeyValueStore client."""
    keys: list[str] = []
    start = None
    while True:
        page = await store.list_keys(limit=1000, exclusive_start_key=start)
        items = page.get("items") if isinstance(page, dict) else getattr(page, "items", [])
        keys.extend(i["key"] if isinstance(i, dict) else i.key for i in items or [])
        truncated = page.get("isTruncated") if isinstance(page, dict) else getattr(page, "is_truncated", False)
        if not truncated:
            break
        start = page.get("nextExclusiveStartKey") if isinstance(page, dict) else getattr(page, "next_exclusive_start_key", None)
        if not start:
            break
    return keys


async def sweep(
    store,
    *,
    keep_raw_per_county: int = DEFAULT_KEEP_RAW,
    pdf_max_age_days: int = DEFAULT_PDF_MAX_AGE_DAYS,
    dry_run: bool = False,
    log=None,
) -> dict:
    """List, plan, delete. Never raises past a single-key failure."""
    log = log or logger
    keys = await list_all_keys(store)
    plan = plan_deletions(
        keys, keep_raw_per_county=keep_raw_per_county, pdf_max_age_days=pdf_max_age_days,
    )
    todo = plan["raw_rolls"] + plan["pdfs"]
    deleted, failed = 0, 0
    if not dry_run:
        for k in todo:
            try:
                await store.delete_record(k)
                deleted += 1
            except Exception as e:  # noqa: BLE001 — one bad key must not stop the sweep
                failed += 1
                log.warning("KVS retention: could not delete %s: %s", k, e)
    log.info(
        "KVS retention: %d keys scanned, %d raw roll(s) + %d PDF(s) %s (keep %d rolls/county, PDFs <= %d days)",
        len(keys), len(plan["raw_rolls"]), len(plan["pdfs"]),
        "would be deleted (dry run)" if dry_run else f"deleted ({failed} failed)",
        keep_raw_per_county, pdf_max_age_days,
    )
    return {"scanned": len(keys), "planned": plan, "deleted": deleted, "failed": failed}

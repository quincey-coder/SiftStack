"""Stats + Slack summary formatting for the Travis tax-delinquent run.

Writes one report JSON per run to data/travis_tax_state/reports/ and
builds a compact Slack block the operator can scan in seconds. The
dropped APNs list is the "sold / paid off" signal — the headline
artifact of this whole pipeline.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import Counter
from datetime import datetime
from pathlib import Path

from notice_parser import NoticeData

from scrapers.travis_texdel_state import DiffResult, REPORTS_DIR

logger = logging.getLogger(__name__)


# ── Stats pass (no I/O) ───────────────────────────────────────────────
def build_stats(notices: list[NoticeData]) -> dict:
    """Aggregate owed/years/value distribution + zip + property-type histograms."""
    oweds: list[float] = []
    vals: list[float] = []
    yrs: list[int] = []
    zip_counts: Counter[str] = Counter()
    prop_types: Counter[str] = Counter()

    for n in notices:
        try:
            if n.tax_delinquent_amount:
                oweds.append(float(n.tax_delinquent_amount))
        except (ValueError, TypeError):
            pass
        try:
            if n.estimated_value:
                vals.append(float(n.estimated_value))
        except (ValueError, TypeError):
            pass
        try:
            if n.tax_delinquent_years:
                yrs.append(int(n.tax_delinquent_years))
        except (ValueError, TypeError):
            pass
        if n.zip:
            zip_counts[n.zip[:5]] += 1
        if n.property_type:
            prop_types[n.property_type] += 1

    def _safe(fn, seq, default=0.0):
        try:
            return fn(seq) if seq else default
        except Exception:
            return default

    return {
        "record_count": len(notices),
        "min_owed": _safe(min, oweds),
        "max_owed": _safe(max, oweds),
        "avg_owed": _safe(statistics.mean, oweds),
        "median_owed": _safe(statistics.median, oweds),
        "total_owed": sum(oweds) if oweds else 0.0,
        "avg_value": _safe(statistics.mean, vals),
        "total_value": sum(vals) if vals else 0.0,
        "avg_years": _safe(statistics.mean, yrs),
        "zip_counts": dict(zip_counts.most_common()),
        "property_types": dict(prop_types.most_common()),
    }


# ── Report JSON (machine-readable) ───────────────────────────────────
def write_report_json(
    diff: DiffResult,
    stats: dict,
    removed: dict,
    *,
    raw_csv_path: str | Path = "",
) -> Path:
    """Write today's diff+stats report.

    On Apify, buffer the JSON (actor_main pushes to KVS). Locally, write to
    REPORTS_DIR. Returns the written path (virtual on Apify).
    """
    from scrapers.travis_texdel_state import _is_apify, _buffer_apify_report

    ts = datetime.now().strftime("%Y-%m-%d")
    filename = f"{ts}_travis_texdel_diff.json"
    payload = {
        "date": ts,
        "raw_csv_path": str(raw_csv_path),
        "diff": diff.to_dict(),
        "stats": stats,
        "removed": removed,
    }
    json_text = json.dumps(payload, indent=2, sort_keys=True)

    if _is_apify():
        _buffer_apify_report(filename, json_text)
        return Path(f"apify-kvs://{filename}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / filename
    path.write_text(json_text)
    return path


# ── Slack summary (human-readable) ───────────────────────────────────
def format_slack_summary(
    diff: DiffResult,
    stats: dict,
    removed: dict,
    *,
    max_inline_dropped: int = 20,
) -> str:
    """Build a compact multi-line Slack block. Safe to append to an existing run summary.

    When `diff.guardrail_tripped`, the summary leads with a loud warning
    and suppresses any "sold" claims. When it's the first-ever run, the
    block reports NEW-only (no dropped claims).
    """
    lines: list[str] = []
    lines.append("*Travis Tax-Delinquent Diff*")

    if diff.guardrail_tripped:
        lines.append(
            f":rotating_light: guardrail tripped — `{diff.guardrail_reason}`. "
            "Prior state preserved; no drop/sold claims reported for this run."
        )
        lines.append(f"Records parsed: {stats.get('record_count', 0)}")
        return "\n".join(lines)

    if diff.is_first_run:
        lines.append(f"First run — seeding state with *{diff.new_count:,}* APNs.")
        return "\n".join(lines)

    # TIGHTFEED: drop the REPEAT counter (boring stable middle) and the
    # financial + filter blocks. NEW + DROPPED + the Sold-tagged rows are
    # the operational signal.
    sold_n = len(diff.dropped_records)
    lines.append(
        f":new: NEW: *{diff.new_count:,}*  |  "
        f":white_check_mark: DROPPED off roll: *{diff.dropped_count:,}*  |  "
        f":label: tagged Sold in CRM: *{sold_n:,}*"
    )

    # The Sold rows (owner — address) are the headline: these get the "Sold"
    # tag applied to the matching DataSift record on the next upload.
    if sold_n:
        for rec in diff.dropped_records[:max_inline_dropped]:
            owner = rec.get("owner_name") or "(unknown owner)"
            addr = ", ".join(
                p for p in [rec.get("address", ""), rec.get("city", ""), rec.get("zip", "")] if p
            ) or rec.get("apn", "")
            lines.append(f"• {owner} — {addr}")
        if sold_n > max_inline_dropped:
            lines.append(
                f"…and {sold_n - max_inline_dropped} more — see report JSON for the full list."
            )

    return "\n".join(lines)

"""Run-health collection, baseline analysis, and the always-sent Slack health report.

The anti-silence layer for the daily pipeline. Three pieces:

1. **Collection** — ``RunHealth`` accumulates one ``ScraperHealth`` record per
   scrape target (recorded inside ``scrapers.scrape_targets``), plus run-level
   events (guardrail trips, validator warnings, schema alarms) and a
   ``WarnErrorCounter`` logging handler that counts WARN/ERROR lines globally.

2. **Analysis** — ``analyze()`` compares today's per-scraper counts against a
   rolling baseline (last ``HISTORY_DAYS`` days, persisted under the
   ``scraper_health_state`` key in the cross-run store: Apify KVS in the cloud,
   ``state_backups/`` JSON locally). It flags hard failures, zero-streaks on
   normally-productive scrapers, parse-zero regressions (records returned but
   none kept), absurd result spikes for short date windows (a date filter that
   didn't apply), and a high Zillow failure share.

3. **Reporting** — ``build_health_block()`` NEVER returns an empty string.
   A totally-failed run and a genuinely quiet day must look different, so the
   health message is sent unconditionally — outside the publish gate, even
   (especially) when the run produced zero notices.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)

# Rolling baseline window (days of history kept per scraper).
HISTORY_DAYS = 14
# Cross-run store key (Apify KVS `sift-stack-state` / local state_backups JSON).
STATE_KEY = "scraper_health_state"
# A ≤2-day window returning more than this many records means the source's
# date filter almost certainly did not apply (Bell publicsearch pulled its
# 2500-row cap for 1-day windows on ~10 of 30 days before this existed).
SPIKE_ABS_FLOOR = 500
# Zillow enrichment failure share that warrants a flag (with ≥20 attempts).
ZILLOW_FAIL_SHARE = 0.30
ZILLOW_MIN_ATTEMPTS = 20


@dataclass
class ScraperHealth:
    """Outcome of one scrape target in one run."""

    county: str
    notice_type: str
    status: str  # "ok" | "zero" | "fail"
    count: int = 0
    duration_s: float = 0.0
    error: str = ""
    # Free-form evidence set by scrapers via `self.last_meta`:
    #   returned=N kept=N  → parse-rate check (returned>0, kept==0 is a regression)
    #   hit_cap=True       → source result cap reached (filter likely not applied)
    #   partial=N          → notices salvaged from a failed scrape (ScraperError.partial)
    #   window_days=N      → requested date-window size, for the spike rule
    evidence: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.county.title()}/{self.notice_type.lower()}"


class RunHealth:
    """Accumulates per-scraper outcomes + run-level events for one run."""

    def __init__(self) -> None:
        self.scrapers: list[ScraperHealth] = []
        self.events: list[tuple[str, str, str]] = []  # (severity, source, message)
        self.zillow: dict = {}          # property_enricher.LAST_RUN_STATS
        self.log_counts: dict = {}      # from WarnErrorCounter.snapshot()
        self.registry_gaps: list[tuple[str, str]] = []
        self.known_gaps: list[tuple[str, str]] = []
        self.notices_total: int = 0     # post-enrichment survivors
        self.elapsed_min: float = 0.0
        self.cost_usd: float = 0.0
        # Smoke-test cap (input max_notices). When set, scrapers stop early on
        # purpose — analyze() must not read the truncation as a source result
        # cap / date-filter failure (false positive observed 2026-08-19).
        self.max_notices: int | None = None

    def record_scraper(
        self,
        county: str,
        notice_type: str,
        *,
        count: int,
        duration_s: float = 0.0,
        error: str = "",
        evidence: dict | None = None,
    ) -> None:
        if error:
            status = "fail"
        elif count == 0:
            status = "zero"
        else:
            status = "ok"
        self.scrapers.append(
            ScraperHealth(
                county=county,
                notice_type=notice_type,
                status=status,
                count=count,
                duration_s=duration_s,
                error=str(error).splitlines()[0][:300] if error else "",
                evidence=dict(evidence or {}),
            )
        )

    def record_event(self, severity: str, source: str, message: str) -> None:
        """severity: "warn" | "error"."""
        self.events.append((severity, source, str(message)[:300]))


class WarnErrorCounter(logging.Handler):
    """Counts WARNING/ERROR records globally; keeps the last few ERROR messages."""

    def __init__(self, keep_errors: int = 15) -> None:
        super().__init__(level=logging.WARNING)
        self.warnings = 0
        self.errors = 0
        self.last_errors: deque[str] = deque(maxlen=keep_errors)

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - trivial
        try:
            if record.levelno >= logging.ERROR:
                self.errors += 1
                self.last_errors.append(
                    f"{record.name}: {record.getMessage()}"[:300]
                )
            elif record.levelno >= logging.WARNING:
                self.warnings += 1
        except Exception:
            pass

    def snapshot(self) -> dict:
        return {
            "warnings": self.warnings,
            "errors": self.errors,
            "last_errors": list(self.last_errors),
        }


# ── Analysis ─────────────────────────────────────────────────────────


@dataclass
class HealthReport:
    """Analyzed health for one run — feeds the Slack block + status message."""

    date: str
    scrapers: list[ScraperHealth]
    flags: list[str] = field(default_factory=list)        # scraper-level alert lines
    event_lines: list[str] = field(default_factory=list)  # guardrails/validator/alarms
    ok: int = 0
    zero: int = 0
    fail: int = 0
    all_failed: bool = False
    zillow_line: str = ""
    zillow_flagged: bool = False
    log_counts: dict = field(default_factory=dict)
    notices_total: int = 0
    elapsed_min: float = 0.0
    cost_usd: float = 0.0
    known_gaps: list[tuple[str, str]] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return not self.flags and not self.event_lines and not self.zillow_flagged


def _history_counts(entry: dict) -> list[int]:
    return [int(h.get("count", 0)) for h in entry.get("history", [])]


def _median_nonzero(counts: list[int], last_n: int = 7) -> float:
    nonzero = [c for c in counts if c > 0][-last_n:]
    return statistics.median(nonzero) if nonzero else 0.0


def analyze(health: RunHealth, baselines: dict | None) -> HealthReport:
    """Compare this run against the rolling baselines and produce a report."""
    baselines = baselines or {}
    today = datetime.now().strftime("%Y-%m-%d")
    report = HealthReport(
        date=today,
        scrapers=list(health.scrapers),
        log_counts=dict(health.log_counts),
        notices_total=health.notices_total,
        elapsed_min=health.elapsed_min,
        cost_usd=health.cost_usd,
        known_gaps=list(health.known_gaps),
    )

    for s in health.scrapers:
        entry = baselines.get(s.key, {})
        counts = _history_counts(entry)
        median = _median_nonzero(counts)
        zero_streak = int(entry.get("zero_streak", 0))

        if s.status == "fail":
            report.fail += 1
            extra = f" (zero-streak {zero_streak + 1}d)" if zero_streak >= 1 else ""
            partial = s.evidence.get("partial")
            salvage = f" — {partial} partial records kept" if partial else ""
            report.flags.append(
                f":rotating_light: {s.key} — FAIL: {s.error or 'unknown error'}{extra}{salvage}"
            )
            continue

        if s.status == "zero":
            report.zero += 1
            returned = s.evidence.get("returned", 0)
            if returned and returned > 0:
                # Search found records but the parser kept none — the Travis
                # probate/lis_pendens failure class. Always a regression.
                report.flags.append(
                    f":rotating_light: {s.key} — {returned} returned, 0 parsed "
                    f"(parser/grid regression)"
                )
            elif median >= 3 and zero_streak + 1 >= 2:
                report.flags.append(
                    f":warning: {s.key} — 0 records {zero_streak + 1} days running "
                    f"(baseline ~{median:.0f}/day)"
                )
            continue

        report.ok += 1
        # The spike rule only applies to scrapers that report their requested
        # date-window size via last_meta["window_days"] — full-roll sources
        # (tax delinquent) legitimately return thousands every day.
        window_days = s.evidence.get("window_days")
        spike_threshold = max(10 * median, SPIKE_ABS_FLOOR) if median else SPIKE_ABS_FLOOR
        smoke_capped = bool(
            health.max_notices and s.count >= health.max_notices
        )
        if smoke_capped:
            pass  # capped by the run's own max_notices — not a source signal
        elif s.evidence.get("hit_cap"):
            report.flags.append(
                f":warning: {s.key} — hit the source's result cap ({s.count})"
                + (f" for a {window_days}-day window" if window_days else "")
                + ": date filter likely not applied"
            )
        elif window_days is not None and int(window_days) <= 2 and s.count > spike_threshold:
            base = f"baseline ~{median:.0f}" if median else "no baseline"
            report.flags.append(
                f":warning: {s.key} — {s.count} results for a {window_days}-day "
                f"window ({base}): date filter likely not applied"
            )

    ran = report.ok + report.zero + report.fail
    report.all_failed = ran > 0 and report.fail == ran

    # Run-level events (guardrail trips, validator warnings, schema alarms, gaps)
    for severity, source, message in health.events:
        icon = ":rotating_light:" if severity == "error" else ":warning:"
        report.event_lines.append(f"{icon} {source}: {message}")
    for county, ntype in health.registry_gaps:
        report.event_lines.append(
            f":rotating_light: registry: {county.title()}/{ntype} has NO scraper "
            f"registered (import failure?)"
        )

    # Zillow failure share
    z = health.zillow or {}
    attempted = int(z.get("attempted", 0))
    if attempted:
        failed = int(z.get("failed", 0))
        share = failed / attempted
        report.zillow_line = (
            f"Zillow: {z.get('enriched', attempted - failed)}/{attempted} enriched"
            + (f" · {failed} failed ({share:.0%})" if failed else "")
        )
        if share > ZILLOW_FAIL_SHARE and attempted >= ZILLOW_MIN_ATTEMPTS:
            report.zillow_flagged = True
            report.zillow_line = ":warning: " + report.zillow_line + " — failure share high"

    return report


def update_baselines(baselines: dict | None, health: RunHealth) -> dict:
    """Fold this run's counts into the rolling per-scraper baselines."""
    baselines = dict(baselines or {})
    today = datetime.now().strftime("%Y-%m-%d")
    for s in health.scrapers:
        entry = dict(baselines.get(s.key, {}))
        history = [h for h in entry.get("history", []) if h.get("date") != today]
        history.append({"date": today, "count": s.count, "status": s.status})
        entry["history"] = history[-HISTORY_DAYS:]
        if s.status == "ok":
            entry["zero_streak"] = 0
        else:  # zero or fail both extend the streak
            entry["zero_streak"] = int(entry.get("zero_streak", 0)) + 1
        entry["last_status"] = s.status
        entry["last_error"] = s.error
        baselines[s.key] = entry
    return baselines


# ── Reporting ────────────────────────────────────────────────────────


def build_health_block(report: HealthReport) -> str:
    """Render the health message. NEVER returns "" — that's the whole point."""
    ran = report.ok + report.zero + report.fail
    icon = ":white_check_mark:" if report.healthy else ":rotating_light:"
    lines = [
        f"{icon} *SiftStack health — {report.date}* · {ran} targets: "
        f"{report.ok} ok · {report.zero} zero · {report.fail} FAILED"
    ]
    lines.extend(f"• {f}" for f in report.flags)
    lines.extend(f"• {e}" for e in report.event_lines)
    if report.zillow_line:
        lines.append(report.zillow_line)
    lc = report.log_counts
    if lc:
        lines.append(f"Logs: {lc.get('errors', 0)} ERROR · {lc.get('warnings', 0)} WARN")
        if lc.get("errors") and lc.get("last_errors") and not report.healthy:
            for msg in list(lc["last_errors"])[-3:]:
                lines.append(f"    ↳ {msg}")
    if report.known_gaps:
        gaps = ", ".join(f"{c.title()}/{t}" for c, t in report.known_gaps)
        lines.append(f"Known gaps (no scraper yet): {gaps}")
    tail = f"Run: {report.elapsed_min:.1f} min · {report.notices_total} notices"
    if report.cost_usd:
        tail += f" · cost ${report.cost_usd:.2f}"
    lines.append(tail)
    return "\n".join(lines)


def send_health_report(report: HealthReport, webhook_url: str | None = None) -> bool:
    """Send the health block. Returns False on a dead/missing webhook so the
    caller can surface THAT failure too (Actor status message)."""
    from slack_notifier import _send_webhook

    sent = _send_webhook(build_health_block(report), webhook_url)
    if sent:
        logger.info("Health report sent to Slack (%s)",
                    "healthy" if report.healthy else f"{len(report.flags)} flag(s)")
    else:
        logger.error("HEALTH REPORT SLACK SEND FAILED — webhook missing or dead")
    return sent


def format_status_message(report: HealthReport) -> str:
    """Short status for Actor.set_status_message (shows in the Apify console)."""
    ran = report.ok + report.zero + report.fail
    if report.healthy:
        return (f"OK: {ran} targets ({report.ok} ok/{report.zero} zero) · "
                f"{report.notices_total} notices")
    parts = [f"UNHEALTHY: {report.fail} failed/{report.zero} zero of {ran}"]
    # Name the first reason — scraper flag, then run-level event, then Zillow —
    # so the console status is actionable, not just a verdict.
    first = ""
    if report.flags:
        first = report.flags[0]
    elif report.event_lines:
        first = report.event_lines[0]
    elif report.zillow_flagged:
        first = report.zillow_line
    if first:
        for token in (":rotating_light: ", ":warning: "):
            first = first.replace(token, "")
        parts.append(first)
    return " · ".join(parts)[:130]

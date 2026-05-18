"""Generic cross-run state + raw archival for non-Travis tax-delinquent scrapers.

Mirrors the Travis-specific `travis_texdel_state` module but parameterized by
county so Bell + Williamson (and any future county) share one implementation.

Layer 1: data/{county}_tax_state/tax_delinquent_{county}_state.json
  Authoritative APN set for fast diffs.
Layer 2: data/{county}_tax_raw/*.{ext}
  Immutable audit archive of every raw delinquent file (XLSX bytes).

Cross-run diff is the headline output: NEW = fresh delinquencies,
REPEAT = still delinquent, DROPPED = paid off / sold (the signal).

Apify shim: when running on Apify, state lives in module-level caches that
the actor entrypoint pre-loads from KVS and flushes back at end-of-run.
Each county gets its own slot in the per-county shim dicts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)


# ── Path helpers ─────────────────────────────────────────────────────
def state_dir(county: str) -> Path:
    return config.PROJECT_ROOT / "data" / f"{county.lower()}_tax_state"


def raw_dir(county: str) -> Path:
    return config.PROJECT_ROOT / "data" / f"{county.lower()}_tax_raw"


def reports_dir(county: str) -> Path:
    return state_dir(county) / "reports"


def state_path(county: str) -> Path:
    return state_dir(county) / f"tax_delinquent_{county.lower()}_state.json"


# ── Apify KVS shim — per county ──────────────────────────────────────
_APIFY_STATE_CACHE: dict[str, dict] = {}
_APIFY_RAW_BUFFER: dict[str, list[tuple[str, bytes]]] = {}
_APIFY_REPORT_BUFFER: dict[str, list[tuple[str, str]]] = {}


def _is_apify() -> bool:
    return bool(os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"))


def inject_apify_state(county: str, state: dict | None) -> None:
    """Seed the per-county cache from Apify KVS (called by actor_main)."""
    _APIFY_STATE_CACHE[county.lower()] = dict(state) if state else _empty_state()


def snapshot_apify_state(county: str) -> dict:
    """Return the current cached state for actor_main to persist to KVS."""
    return _APIFY_STATE_CACHE.get(county.lower(), _empty_state())


def drain_apify_raw(county: str) -> list[tuple[str, bytes]]:
    out = _APIFY_RAW_BUFFER.pop(county.lower(), [])
    return out


def drain_apify_reports(county: str) -> list[tuple[str, str]]:
    out = _APIFY_REPORT_BUFFER.pop(county.lower(), [])
    return out


def _buffer_apify_report(county: str, filename: str, json_text: str) -> None:
    _APIFY_REPORT_BUFFER.setdefault(county.lower(), []).append((filename, json_text))


# ── State file helpers ───────────────────────────────────────────────
def _empty_state() -> dict:
    return {
        "last_run_date": "",
        "last_run_apns": [],
        "master_apns": [],
        "runs": [],
    }


def load_state(county: str) -> dict:
    """Read the state JSON. Returns an empty skeleton on first run."""
    if _is_apify():
        cached = _APIFY_STATE_CACHE.get(county.lower())
        return dict(cached) if cached else _empty_state()

    sp = state_path(county)
    if not sp.exists():
        return _empty_state()
    try:
        data = json.loads(sp.read_text())
        for k, v in _empty_state().items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        logger.error("State file corrupted (%s) — starting empty: %s", sp, e)
        return _empty_state()


def save_state(county: str, state: dict) -> None:
    """Persist state. Atomic write locally; in-memory cache on Apify."""
    if _is_apify():
        _APIFY_STATE_CACHE[county.lower()] = dict(state)
        return

    sp = state_path(county)
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, sp)


# ── Raw file archival ────────────────────────────────────────────────
def archive_raw_bytes(county: str, raw_bytes: bytes, suffix: str = ".xlsx") -> tuple[Path, str]:
    """Archive the raw downloaded delinquent file (XLSX bytes).

    Returns (path_or_virtual, sha256). Virtual path under Apify.
    """
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{ts}_{county.lower()}_delinquent{suffix}"
    sha = hashlib.sha256(raw_bytes).hexdigest()

    if _is_apify():
        _APIFY_RAW_BUFFER.setdefault(county.lower(), []).append((filename, raw_bytes))
        return Path(f"apify-kvs://{filename}"), sha

    rd = raw_dir(county)
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / filename
    path.write_bytes(raw_bytes)
    return path, sha


# ── Diff & guardrails ────────────────────────────────────────────────
@dataclass
class DiffResult:
    new: list[str]
    repeat: list[str]
    dropped: list[str]
    new_count: int
    repeat_count: int
    dropped_count: int
    is_first_run: bool
    guardrail_tripped: bool
    guardrail_reason: str

    def to_dict(self) -> dict:
        return {
            "new": self.new,
            "repeat": self.repeat,
            "dropped": self.dropped,
            "new_count": self.new_count,
            "repeat_count": self.repeat_count,
            "dropped_count": self.dropped_count,
            "is_first_run": self.is_first_run,
            "guardrail_tripped": self.guardrail_tripped,
            "guardrail_reason": self.guardrail_reason,
        }


# Bell prop_id is numeric (e.g., 49251 — 5–7 digits typical, can be wider).
# Wilco Quick Ref is alphanumeric prefix + digits (e.g., R012345, M325619).
# Use a permissive pattern — guardrail only catches gross format drift.
_APN_RE = re.compile(r"^[A-Za-z]?\d{4,12}$")


def _apn_format_ok_ratio(apns: set[str]) -> float:
    if not apns:
        return 0.0
    ok = sum(1 for a in apns if _APN_RE.match(a))
    return ok / len(apns)


def check_guardrails(
    current_apns: set[str],
    prev_apns: set[str],
) -> tuple[bool, str]:
    """Same false-positive safeguards as the Travis state module."""
    if not current_apns:
        return False, "empty_current_file"
    ratio = _apn_format_ok_ratio(current_apns)
    if ratio < 0.85:
        return False, f"apn_format_drift: only {ratio:.0%} match expected pattern"
    if not prev_apns:
        return True, "first_run"
    shrinkage = 1 - (len(current_apns) / len(prev_apns))
    if shrinkage > 0.5:
        return (
            False,
            f"volume_anomaly: current={len(current_apns)} vs prev={len(prev_apns)} "
            f"({shrinkage:.0%} shrinkage)",
        )
    return True, ""


def compute_diff(
    current_apns: set[str],
    prev_apns: set[str],
) -> DiffResult:
    ok, reason = check_guardrails(current_apns, prev_apns)
    is_first = not prev_apns
    if not ok:
        return DiffResult(
            new=[], repeat=[], dropped=[],
            new_count=0, repeat_count=0, dropped_count=0,
            is_first_run=is_first,
            guardrail_tripped=True,
            guardrail_reason=reason,
        )
    new = sorted(current_apns - prev_apns)
    repeat = sorted(current_apns & prev_apns)
    dropped = sorted(prev_apns - current_apns) if prev_apns else []
    return DiffResult(
        new=new, repeat=repeat, dropped=dropped,
        new_count=len(new),
        repeat_count=len(repeat),
        dropped_count=len(dropped),
        is_first_run=is_first,
        guardrail_tripped=False,
        guardrail_reason="",
    )


# ── Commit state update ──────────────────────────────────────────────
def commit_run(
    county: str,
    state: dict,
    current_apns: set[str],
    raw_path: Path,
    raw_sha: str,
    diff: DiffResult,
) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    state.setdefault("runs", []).append({
        "date": today,
        "apns_count": len(current_apns),
        "raw_path": str(raw_path),
        "raw_sha256": raw_sha,
        "guardrail_tripped": diff.guardrail_tripped,
        "guardrail_reason": diff.guardrail_reason,
        "new_count": diff.new_count,
        "repeat_count": diff.repeat_count,
        "dropped_count": diff.dropped_count,
    })
    if not diff.guardrail_tripped:
        state["last_run_date"] = today
        state["last_run_apns"] = sorted(current_apns)
        master = set(state.get("master_apns") or []) | current_apns
        state["master_apns"] = sorted(master)
    save_state(county, state)
    return state


# ── Stats + report writer ────────────────────────────────────────────
def build_stats(notices: list) -> dict:
    """Aggregate distribution stats — same shape as travis_texdel_report.build_stats."""
    import statistics
    from collections import Counter

    oweds: list[float] = []
    vals: list[float] = []
    yrs: list[int] = []
    zip_counts: Counter = Counter()
    prop_types: Counter = Counter()

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


def write_report_json(
    county: str,
    diff: DiffResult,
    stats: dict,
    removed: dict,
    *,
    raw_path: str | Path = "",
) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d")
    filename = f"{ts}_{county.lower()}_texdel_diff.json"
    payload = {
        "date": ts,
        "county": county,
        "raw_path": str(raw_path),
        "diff": diff.to_dict(),
        "stats": stats,
        "removed": removed,
    }
    json_text = json.dumps(payload, indent=2, sort_keys=True)

    if _is_apify():
        _buffer_apify_report(county, filename, json_text)
        return Path(f"apify-kvs://{filename}")

    rd = reports_dir(county)
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / filename
    path.write_text(json_text)
    return path


# ── Slack summary ────────────────────────────────────────────────────
def format_slack_summary(
    county: str,
    diff: DiffResult,
    stats: dict,
    removed: dict,
    *,
    max_inline_dropped: int = 20,
) -> str:
    """Compact Slack block for one county's tax-delinquent run."""
    lines: list[str] = []
    lines.append(f"*{county} Tax-Delinquent Diff*")

    if diff.guardrail_tripped:
        lines.append(
            f":rotating_light: guardrail tripped — `{diff.guardrail_reason}`. "
            "Prior state preserved; no drop/sold claims reported for this run."
        )
        lines.append(f"Records parsed: {stats.get('record_count', 0)}")
        return "\n".join(lines)

    if diff.is_first_run:
        lines.append(f"First run — seeding state with *{diff.new_count:,}* APNs.")
    else:
        # TIGHTFEED: NEW + DROPPED only; REPEAT, financial summary, and
        # filter breakdown removed as low-signal noise.
        lines.append(
            f":new: NEW: *{diff.new_count:,}*  |  "
            f":white_check_mark: DROPPED (sold/paid off): *{diff.dropped_count:,}*"
        )

    if diff.dropped_count and not diff.is_first_run:
        if diff.dropped_count <= max_inline_dropped:
            lines.append("Dropped APNs: " + ", ".join(diff.dropped))
        else:
            preview = ", ".join(diff.dropped[:10])
            lines.append(
                f"Dropped APNs (first 10 of {diff.dropped_count}): {preview} … see report JSON for the full list."
            )

    return "\n".join(lines)

"""Cross-run state + raw CSV archival for Travis tax-delinquent pipeline.

Mirrors the skill's master-list workflow:
- Layer 1: tax_delinquent_travis_state.json — authoritative APN set for fast diffs.
- Layer 2: data/travis_tax_raw/*.csv — immutable audit archive of every raw
  CSV the Tax Office served us.

The JSON is what the diff engine reads. CSVs are cold storage — used only
for post-mortem or to rebuild state if it's ever lost.
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


STATE_DIR = config.PROJECT_ROOT / "data" / "travis_tax_state"
RAW_DIR = config.PROJECT_ROOT / "data" / "travis_tax_raw"
REPORTS_DIR = STATE_DIR / "reports"
STATE_PATH = STATE_DIR / "tax_delinquent_travis_state.json"


# ── Apify KVS shim ────────────────────────────────────────────────────
# On Apify the filesystem is ephemeral, so state can't live on disk. The
# actor_main() entrypoint pre-loads state from KVS into `_APIFY_STATE_CACHE`
# before calling scrape_targets(), reads the updated cache after the scrape,
# and writes it back to KVS at end-of-run. The scraper itself stays sync.
#
# Raw CSV + diff reports accumulate in module-level buffers that actor_main
# drains into KVS under timestamped keys with retention pruning.
_APIFY_STATE_CACHE: dict | None = None
_APIFY_RAW_CSV_BUFFER: list[tuple[str, str]] = []  # [(filename, text), ...]
_APIFY_REPORT_BUFFER: list[tuple[str, str]] = []   # [(filename, json_text), ...]


def _is_apify() -> bool:
    return bool(os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"))


def inject_apify_state(state: dict | None) -> None:
    """Seed the module-level cache from Apify KVS (called by actor_main)."""
    global _APIFY_STATE_CACHE
    _APIFY_STATE_CACHE = dict(state) if state else _empty_state()


def snapshot_apify_state() -> dict:
    """Return the current cached state for actor_main to persist to KVS."""
    return _APIFY_STATE_CACHE or _empty_state()


def drain_apify_raw_csvs() -> list[tuple[str, str]]:
    """Return + clear the raw-CSV buffer for actor_main to push to KVS."""
    global _APIFY_RAW_CSV_BUFFER
    out, _APIFY_RAW_CSV_BUFFER = _APIFY_RAW_CSV_BUFFER, []
    return out


def drain_apify_reports() -> list[tuple[str, str]]:
    """Return + clear the report buffer for actor_main to push to KVS."""
    global _APIFY_REPORT_BUFFER
    out, _APIFY_REPORT_BUFFER = _APIFY_REPORT_BUFFER, []
    return out


def _buffer_apify_report(filename: str, json_text: str) -> None:
    """Called by travis_texdel_report.write_report_json() on Apify."""
    _APIFY_REPORT_BUFFER.append((filename, json_text))


# ── State file helpers ───────────────────────────────────────────────
def _empty_state() -> dict:
    return {
        "last_run_date": "",
        "last_run_apns": [],
        "master_apns": [],
        "runs": [],
    }


def load_state() -> dict:
    """Read the state JSON. Returns an empty skeleton on first run.

    Under Apify, reads from the module-level cache that actor_main pre-loaded
    from KVS. Locally, reads from disk.
    """
    if _is_apify():
        if _APIFY_STATE_CACHE is None:
            # actor_main didn't inject — treat as first run rather than crash
            return _empty_state()
        # Return a defensive copy so scraper mutations don't leak back
        return dict(_APIFY_STATE_CACHE)

    if not STATE_PATH.exists():
        return _empty_state()
    try:
        data = json.loads(STATE_PATH.read_text())
        # Tolerate missing keys if someone hand-edits the file
        for k, v in _empty_state().items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        logger.error("State file corrupted (%s) — starting empty: %s", STATE_PATH, e)
        return _empty_state()


def save_state(state: dict) -> None:
    """Persist state.

    Under Apify, updates the module-level cache in place (actor_main flushes
    to KVS at end-of-run). Locally, atomic write to disk via temp + rename.
    """
    if _is_apify():
        global _APIFY_STATE_CACHE
        _APIFY_STATE_CACHE = dict(state)
        return

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, STATE_PATH)


# ── Raw CSV archival ─────────────────────────────────────────────────
def archive_raw_csv(raw_text: str) -> tuple[Path, str]:
    """Archive the raw CSV.

    Under Apify, buffer the CSV text (actor_main pushes to KVS with
    retention pruning). Locally, write a timestamped copy to disk.
    Returns (path_or_virtual_name, sha256).
    """
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{ts}_TaxDelqOpenData.csv"
    sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    if _is_apify():
        _APIFY_RAW_CSV_BUFFER.append((filename, raw_text))
        # Return a virtual path marker that downstream code can still log
        return Path(f"apify-kvs://{filename}"), sha

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_DIR / filename
    path.write_text(raw_text)
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


# Travis County APNs are 14-digit numeric strings (e.g. "01000307170000").
# The regex tolerates 6-14 digits to also accept synthetic fixture APNs
# used in the cross-run diff verification tests.
_APN_RE = re.compile(r"^\d{6,14}$")


def _apn_format_ok_ratio(apns: set[str]) -> float:
    if not apns:
        return 0.0
    ok = sum(1 for a in apns if _APN_RE.match(a))
    return ok / len(apns)


def check_guardrails(
    current_apns: set[str],
    prev_apns: set[str],
) -> tuple[bool, str]:
    """False-positive safeguards before trusting a diff.

    Returns (ok, reason). When ok=False, caller must NOT update state and
    must NOT surface dropped APNs as "sold" signals.

    Rules:
    - Empty current CSV → always fail.
    - APN format drift (>10% of current APNs don't match \\d{6,8}) → fail.
    - First-ever run (no prior state) → ok, but caller should suppress
      dropped-count reporting since prev is empty.
    - Volume sanity: >50% shrinkage between runs → fail.
    """
    if not current_apns:
        return False, "empty_current_csv"
    ratio = _apn_format_ok_ratio(current_apns)
    if ratio < 0.9:
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
    """Compare current-run APNs against the prior run.

    - new     = current - prev (fresh delinquencies)
    - repeat  = current & prev (still delinquent)
    - dropped = prev - current (paid off / sold — the signal we want)
    """
    ok, reason = check_guardrails(current_apns, prev_apns)
    is_first = not prev_apns
    if not ok:
        # Don't return fabricated "dropped" counts if a guardrail tripped
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


# ── Commit state update (run end) ─────────────────────────────────────
def commit_run(
    state: dict,
    current_apns: set[str],
    raw_csv_path: Path,
    raw_csv_sha: str,
    diff: DiffResult,
) -> dict:
    """Append a run entry and refresh last_run / master sets. No-op when guardrail tripped.

    Returns the updated state (also persisted to disk). When a guardrail
    trips we still archive the CSV (caller already did so) but we don't
    overwrite last_run_apns — so the next clean run can compare against
    the last known-good snapshot.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    run_entry = {
        "date": today,
        "apns_count": len(current_apns),
        "raw_csv_path": str(raw_csv_path),
        "raw_csv_sha256": raw_csv_sha,
        "guardrail_tripped": diff.guardrail_tripped,
        "guardrail_reason": diff.guardrail_reason,
        "new_count": diff.new_count,
        "repeat_count": diff.repeat_count,
        "dropped_count": diff.dropped_count,
    }
    state.setdefault("runs", []).append(run_entry)

    if not diff.guardrail_tripped:
        state["last_run_date"] = today
        state["last_run_apns"] = sorted(current_apns)
        master = set(state.get("master_apns") or []) | current_apns
        state["master_apns"] = sorted(master)

    save_state(state)
    return state

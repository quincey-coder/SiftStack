"""Cross-run state + resolution diff for code-enforcement scrapers.

Mirrors `tax_delinquent_state` but keyed by code-enforcement **case_id** instead
of parcel APN, and produces "resolved" records instead of "sold" ones.

The signal is the same shape as tax-delinquent drop-off: a case that was OPEN
(Active/Pending) in our prior run and is no longer in the current open set has
been **resolved** (owner complied / case closed). Unlike tax-delinquent — where
disappearance is the only observable — Austin's Socrata dataset exposes the
case's actual `status` + `closed_date`, so the scraper status-confirms each
dropped case before rehydrating it (see code_enforcement_travis).

Because a resolved violation is NOT a sale (same owner; other distress signals
on the property may still be live), the rehydrated record is tagged
"Code Violation Resolved" for a **scoped** list-only CRM cleanup — never "Sold".

County-parameterized so Bell/Williamson open-records ingest can plug in later;
only Travis (Austin Socrata) is wired today.

Layer 1: data/{county}_codeenf_state/code_enforcement_{county}_state.json
  Authoritative open-case-id set for fast run-over-run diffs.

Apify shim: when running on Apify, state lives in module-level caches that the
actor entrypoint pre-loads from KVS (key `{county}_codeenf_state`) and flushes
back at end-of-run.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Path helpers ─────────────────────────────────────────────────────
def state_dir(county: str) -> Path:
    return config.PROJECT_ROOT / "data" / f"{county.lower()}_codeenf_state"


def reports_dir(county: str) -> Path:
    return state_dir(county) / "reports"


def state_path(county: str) -> Path:
    return state_dir(county) / f"code_enforcement_{county.lower()}_state.json"


# ── Apify KVS shim — per county ──────────────────────────────────────
_APIFY_STATE_CACHE: dict[str, dict] = {}
_APIFY_REPORT_BUFFER: dict[str, list[tuple[str, str]]] = {}


def _is_apify() -> bool:
    return bool(os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"))


def kvs_key(county: str) -> str:
    return f"{county.lower()}_codeenf_state"


def inject_apify_state(county: str, state: dict | None) -> None:
    """Seed the per-county cache from Apify KVS (called by actor_main)."""
    _APIFY_STATE_CACHE[county.lower()] = dict(state) if state else _empty_state()


def snapshot_apify_state(county: str) -> dict:
    """Return the current cached state for actor_main to persist to KVS."""
    return _APIFY_STATE_CACHE.get(county.lower(), _empty_state())


def drain_apify_reports(county: str) -> list[tuple[str, str]]:
    return _APIFY_REPORT_BUFFER.pop(county.lower(), [])


def _buffer_apify_report(county: str, filename: str, json_text: str) -> None:
    _APIFY_REPORT_BUFFER.setdefault(county.lower(), []).append((filename, json_text))


# ── State file helpers ───────────────────────────────────────────────
def _empty_state() -> dict:
    return {
        "last_run_date": "",
        "last_run_case_ids": [],
        "master_case_ids": [],
        # case_id → compact record snapshot of what we actually uploaded last
        # run ({address, city, state, zip, owner_name}). Used to rehydrate a
        # case that resolved into a full "Code Violation Resolved" record. Only
        # the uploaded (post-filter) set is stored so a case that resolved but
        # was never in DataSift can't spawn a junk record.
        "last_run_records": {},
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
    # Rehydrated detail for the subset of dropped case_ids that were in our
    # prior upload (and therefore exist in DataSift). Each entry:
    # {case_id, address, city, state, zip, owner_name}. Enriched by the scraper
    # with the confirmed resolution status/date, then become "resolved" rows.
    dropped_records: list[dict] = field(default_factory=list)

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
            "dropped_records": self.dropped_records,
        }


# Austin Code case_id looks like "2026-077744 CC" — a 4-digit year, a dash, a
# run of digits, and a short letter suffix. Permissive: the guardrail only needs
# to catch gross format drift (e.g. a source/schema change swapping the key).
_CASE_ID_RE = re.compile(r"^\d{4}-\d+")


def _case_id_format_ok_ratio(ids: set[str]) -> float:
    if not ids:
        return 0.0
    ok = sum(1 for c in ids if _CASE_ID_RE.match(c))
    return ok / len(ids)


def check_guardrails(
    current_ids: set[str],
    prev_ids: set[str],
) -> tuple[bool, str]:
    """False-positive safeguards, same shape as the tax-delinquent state module.

    Any trip preserves the prior baseline and suppresses all resolved claims.
    """
    if not current_ids:
        return False, "empty_current_fetch"
    ratio = _case_id_format_ok_ratio(current_ids)
    if ratio < 0.85:
        return False, f"case_id_format_drift: only {ratio:.0%} match expected pattern"
    if not prev_ids:
        return True, "first_run"
    shrinkage = 1 - (len(current_ids) / len(prev_ids))
    if shrinkage > 0.5:
        return (
            False,
            f"volume_anomaly: current={len(current_ids)} vs prev={len(prev_ids)} "
            f"({shrinkage:.0%} shrinkage)",
        )
    return True, ""


def compute_diff(
    current_ids: set[str],
    prev_ids: set[str],
    prev_records: dict | None = None,
) -> DiffResult:
    """Diff this run's OPEN case ids against the prior run.

    - new     = current - prev (fresh open cases)
    - repeat  = current & prev (still open)
    - dropped = prev - current (no longer open — resolved / closed / complied)
    - dropped_records = the subset of `dropped` that exists in `prev_records`
      (i.e. cases we actually uploaded last run, so they're in DataSift).
      Rehydrated with address/owner so they can be tagged resolved.
    """
    ok, reason = check_guardrails(current_ids, prev_ids)
    is_first = not prev_ids
    if not ok:
        return DiffResult(
            new=[], repeat=[], dropped=[],
            new_count=0, repeat_count=0, dropped_count=0,
            is_first_run=is_first,
            guardrail_tripped=True,
            guardrail_reason=reason,
            dropped_records=[],
        )
    new = sorted(current_ids - prev_ids)
    repeat = sorted(current_ids & prev_ids)
    dropped = sorted(prev_ids - current_ids) if prev_ids else []
    prev_records = prev_records or {}
    dropped_records = [
        {"case_id": cid, **prev_records[cid]}
        for cid in dropped
        if cid in prev_records
    ]
    return DiffResult(
        new=new, repeat=repeat, dropped=dropped,
        new_count=len(new),
        repeat_count=len(repeat),
        dropped_count=len(dropped),
        is_first_run=is_first,
        guardrail_tripped=False,
        guardrail_reason="",
        dropped_records=dropped_records,
    )


# ── Record snapshot + resolved-record builder ─────────────────────────
def snapshot_records(notices: list) -> dict:
    """Build the case_id → compact-record map persisted as `last_run_records`.

    Only fields needed to (a) match the property in DataSift by address and
    (b) render a readable report. Keyed by case_id; notices without one are
    skipped (they couldn't be diffed anyway).
    """
    out: dict[str, dict] = {}
    for n in notices:
        cid = (getattr(n, "case_id", "") or "").strip()
        if not cid:
            continue
        out[cid] = {
            "address": n.address,
            "city": n.city,
            "state": n.state or "TX",
            "zip": n.zip,
            "owner_name": n.owner_name,
        }
    return out


def build_resolved_notice(
    rec: dict, county: str, today: str, resolution_note: str = "",
) -> NoticeData:
    """Rehydrate a resolved case into a 'Code Violation Resolved' NoticeData.

    Carries only the property address + owner (enough for DataSift to match the
    existing record by address) and sets record_status='resolved' so the
    formatter emits Tags='Code Violation Resolved' with a blank Lists column —
    a scoped list-only cleanup, NOT a Sold.
    """
    n = NoticeData(
        notice_type="code_violation",
        county=county,
        state=rec.get("state") or "TX",
        date_added=today,
        address=rec.get("address", ""),
        city=rec.get("city", ""),
        zip=rec.get("zip", ""),
        owner_name=rec.get("owner_name", ""),
        case_id=rec.get("case_id", ""),
    )
    n.record_status = "resolved"
    n.resolution_note = resolution_note
    return n


# ── Seed-state builder (Apify baseline, no CRM side effects) ──────────
def build_seed_state(
    open_ids, prev_state: dict | None = None, today: str | None = None,
) -> dict:
    """Build a state dict that seeds the open-case baseline ONLY.

    Used to pre-load Apify KVS so the first cloud run has a real prior baseline
    (currently-open cases read as REPEAT, not NEW) instead of self-seeding — no
    scrape/enrich/upload of those records. Critically, `last_run_records` is
    NEVER populated from the seed set (it's carried over from any existing cloud
    state, else empty): a seeded case that later resolves has no snapshot, so it
    produces ZERO CRM cleanup rows — nothing touches the CRM. Only cases the
    normal daily pipeline actually uploads later get into `last_run_records`.

    Merge-safe: `master_case_ids` unions with any existing cloud state so a prior
    baseline isn't lost; `last_run_case_ids` is set to the fresh open set (the
    point-in-time baseline the next diff compares against).
    """
    prev = prev_state or {}
    ids = sorted(set(str(c).strip() for c in open_ids if str(c).strip()))
    master = sorted(set(prev.get("master_case_ids") or []) | set(ids))
    return {
        "last_run_date": today or datetime.now().strftime("%Y-%m-%d"),
        "last_run_case_ids": ids,
        "master_case_ids": master,
        # Carried over, NOT seeded — keeps any real CRM-tracking the cloud already
        # had, and never adds seed records (so seeding can't create CRM rows).
        "last_run_records": dict(prev.get("last_run_records") or {}),
        "runs": list(prev.get("runs") or []),
    }


# ── Commit state update ──────────────────────────────────────────────
def commit_run(
    county: str,
    state: dict,
    current_ids: set[str],
    diff: DiffResult,
    current_records: dict | None = None,
) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    state.setdefault("runs", []).append({
        "date": today,
        "case_count": len(current_ids),
        "guardrail_tripped": diff.guardrail_tripped,
        "guardrail_reason": diff.guardrail_reason,
        "new_count": diff.new_count,
        "repeat_count": diff.repeat_count,
        "dropped_count": diff.dropped_count,
        "resolved_tagged_count": len(diff.dropped_records),
    })
    if not diff.guardrail_tripped:
        state["last_run_date"] = today
        state["last_run_case_ids"] = sorted(current_ids)
        master = set(state.get("master_case_ids") or []) | current_ids
        state["master_case_ids"] = sorted(master)
        # Refresh the uploaded-record snapshot so the NEXT run can rehydrate
        # whatever resolves into "Code Violation Resolved" rows. Guardrail-
        # tripped runs keep the prior snapshot untouched.
        if current_records is not None:
            state["last_run_records"] = current_records
    save_state(county, state)
    return state


# ── Report writer ────────────────────────────────────────────────────
def write_report_json(
    county: str,
    diff: DiffResult,
    *,
    stats: dict | None = None,
) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d")
    filename = f"{ts}_{county.lower()}_codeenf_diff.json"
    payload = {
        "date": ts,
        "county": county,
        "diff": diff.to_dict(),
        "stats": stats or {},
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
    *,
    max_inline_dropped: int = 20,
) -> str:
    """Compact Slack block for one county's code-enforcement resolution run."""
    lines: list[str] = [f"*{county} Code-Enforcement Diff*"]

    if diff.guardrail_tripped:
        lines.append(
            f":rotating_light: guardrail tripped — `{diff.guardrail_reason}`. "
            "Prior state preserved; no resolved claims reported for this run."
        )
        return "\n".join(lines)

    if diff.is_first_run:
        lines.append(
            f"First run — seeding state with *{diff.new_count:,}* open cases."
        )
        return "\n".join(lines)

    resolved_n = len(diff.dropped_records)
    lines.append(
        f":new: NEW open: *{diff.new_count:,}*  |  "
        f":white_check_mark: RESOLVED: *{diff.dropped_count:,}*  |  "
        f":label: tagged Resolved in CRM: *{resolved_n:,}*"
    )

    # The resolved rows (owner — address) are the actionable headline: these get
    # the "Code Violation Resolved" tag applied to the matching DataSift record
    # on next upload, scoping them out of Code Violation marketing.
    if resolved_n:
        for rec in diff.dropped_records[:max_inline_dropped]:
            owner = rec.get("owner_name") or "(unknown owner)"
            addr = ", ".join(
                p for p in [rec.get("address", ""), rec.get("city", ""), rec.get("zip", "")] if p
            ) or rec.get("case_id", "")
            note = rec.get("resolution_note")
            lines.append(f"• {owner} — {addr}" + (f"  ({note})" if note else ""))
        if resolved_n > max_inline_dropped:
            lines.append(
                f"…and {resolved_n - max_inline_dropped} more — see report JSON for the full list."
            )

    return "\n".join(lines)

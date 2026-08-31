"""Cross-run state for the Austin BSC (Building & Standards Commission) scraper.

Purpose-built rather than reusing code_enforcement_state: that module's
guardrails (>50% shrinkage, `^\\d{4}-` case-id format) and its "dropped =
resolved" semantics fit a daily open-case snapshot, not a monthly hearing
docket of 2-16 cases whose ids look like "CL 2026-083909". A BSC case that
stops appearing on agendas is usually STILL under an active order with
penalties accruing, so absence is deliberately NOT treated as resolution.

What is tracked:
  seen_docs  - EDIMS document ids already parsed (so a daily run only fetches
               NEW agendas; the index page itself is re-read every day).
  cases      - per CL case number: first/last meeting seen, the effective order
               class so far (repair < vacate < demolish), class history, the
               recorded order number (TRV ...), parcel/address/owner.
  runs       - compact per-run stats.

Escalation (repair -> demolish) is the one cross-run signal surfaced: the
scraper re-emits the record with the new class (see main._notice_dedup_key).

Apify shim: state lives in a module-level cache that the actor entrypoint
pre-loads from KVS (key `travis_bsc_state`) and flushes back at end-of-run -
same shape as the other *_state modules so main.py wiring is a copy.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

KVS_KEY = "travis_bsc_state"
ORDER_RANK = {"repair": 1, "vacate": 2, "demolish": 3}


# -- Paths ---------------------------------------------------------------
def state_dir() -> Path:
    return config.PROJECT_ROOT / "data" / "travis_bsc_state"


def reports_dir() -> Path:
    return state_dir() / "reports"


def state_path() -> Path:
    return state_dir() / "bsc_travis_state.json"


# -- Apify KVS shim ------------------------------------------------------
_APIFY_STATE_CACHE: dict | None = None
_APIFY_REPORT_BUFFER: list[tuple[str, str]] = []


def _is_apify() -> bool:
    return bool(os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"))


def inject_apify_state(state: dict | None) -> None:
    """Seed the cache from Apify KVS (called by actor_main before scraping)."""
    global _APIFY_STATE_CACHE
    _APIFY_STATE_CACHE = dict(state) if state else _empty_state()


def snapshot_apify_state() -> dict:
    """Current cached state for actor_main to persist to KVS."""
    return _APIFY_STATE_CACHE if _APIFY_STATE_CACHE is not None else _empty_state()


def drain_apify_reports() -> list[tuple[str, str]]:
    global _APIFY_REPORT_BUFFER
    out, _APIFY_REPORT_BUFFER = _APIFY_REPORT_BUFFER, []
    return out


# -- State file ----------------------------------------------------------
def _empty_state() -> dict:
    return {
        "last_run_date": "",
        "seen_docs": {},   # edims_id(str) -> {meeting, kind, parsed, seen}
        "cases": {},       # "CL 2026-083909" -> {...}
        "runs": [],
    }


def load_state() -> dict:
    if _is_apify():
        return dict(_APIFY_STATE_CACHE) if _APIFY_STATE_CACHE else _empty_state()
    sp = state_path()
    if not sp.exists():
        return _empty_state()
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
        for k, v in _empty_state().items():
            data.setdefault(k, v)
        return data
    except Exception as e:
        logger.error("BSC state file corrupted (%s) - starting empty: %s", sp, e)
        return _empty_state()


def save_state(state: dict) -> None:
    global _APIFY_STATE_CACHE
    if _is_apify():
        _APIFY_STATE_CACHE = dict(state)
        return
    sp = state_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    tmp = sp.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, sp)


# -- Diff ----------------------------------------------------------------
@dataclass
class DiffResult:
    new: list = field(default_factory=list)          # case ids first seen this run
    returning: list = field(default_factory=list)    # already-known cases seen again
    escalated: list = field(default_factory=list)    # [case_id, from_class, to_class]
    skipped_appeals: int = 0
    meetings: list = field(default_factory=list)     # meeting dates parsed this run
    parse_zero_docs: int = 0
    guardrail_tripped: bool = False   # parity with main._collect_health_events
    guardrail_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "new": self.new,
            "returning": self.returning,
            "escalated": self.escalated,
            "skipped_appeals": self.skipped_appeals,
            "meetings": self.meetings,
            "parse_zero_docs": self.parse_zero_docs,
            "guardrail_tripped": self.guardrail_tripped,
            "guardrail_reason": self.guardrail_reason,
        }


def effective_class(*classes: str) -> str:
    """Highest-ranked order class among the inputs ("" when none rank)."""
    best, best_rank = "", 0
    for c in classes:
        r = ORDER_RANK.get((c or "").lower(), 0)
        if r > best_rank:
            best, best_rank = c.lower(), r
    return best


def update_case(state: dict, case_id: str, meeting_date: str, new_class: str,
                *, trv: str = "", parcel: str = "", address: str = "",
                zip5: str = "", owner_name: str = "") -> tuple[bool, str, str]:
    """Merge one agenda appearance into state["cases"].

    Returns (is_new, prior_class, effective_class). The stored class only ever
    moves UP the rank (repair -> vacate -> demolish); de-escalations and
    "uphold penalties" returns leave it untouched.
    """
    cases = state.setdefault("cases", {})
    rec = cases.get(case_id)
    is_new = rec is None
    if is_new:
        rec = {
            "first_seen": meeting_date, "last_seen": meeting_date,
            "appearances": [], "order_class": "", "class_history": [],
            "trv": "", "parcel": "", "address": "", "zip": "", "owner_name": "",
        }
        cases[case_id] = rec
    prior = rec.get("order_class") or ""
    eff = effective_class(prior, new_class)
    if meeting_date not in rec["appearances"]:
        rec["appearances"].append(meeting_date)
        rec["appearances"].sort()
    rec["first_seen"] = min(rec.get("first_seen") or meeting_date, meeting_date)
    rec["last_seen"] = max(rec.get("last_seen") or meeting_date, meeting_date)
    if eff and eff != prior:
        rec["order_class"] = eff
        rec["class_history"].append([meeting_date, eff])
    for k, v in (("trv", trv), ("parcel", parcel), ("address", address),
                 ("zip", zip5), ("owner_name", owner_name)):
        if v and not rec.get(k):
            rec[k] = v
    return is_new, prior, eff


def commit_run(state: dict, diff: DiffResult, seen_docs_delta: dict) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    state.setdefault("seen_docs", {}).update(seen_docs_delta)
    state.setdefault("runs", []).append({
        "date": today,
        "meetings": len(diff.meetings),
        "agendas_new": len(seen_docs_delta),
        "cases_new": len(diff.new),
        "cases_returning": len(diff.returning),
        "escalations": len(diff.escalated),
        "parse_zero_docs": diff.parse_zero_docs,
    })
    state["runs"] = state["runs"][-60:]
    state["last_run_date"] = today
    save_state(state)
    return state


# -- Report + Slack ------------------------------------------------------
def write_report_json(diff: DiffResult, *, stats: dict | None = None) -> Path:
    ts = datetime.now().strftime("%Y-%m-%d")
    filename = f"{ts}_travis_bsc_diff.json"
    payload = {"date": ts, "county": "Travis", "source": "bsc",
               "diff": diff.to_dict(), "stats": stats or {}}
    json_text = json.dumps(payload, indent=2, sort_keys=True)
    if _is_apify():
        _APIFY_REPORT_BUFFER.append((filename, json_text))
        return Path(f"apify-kvs://{filename}")
    rd = reports_dir()
    rd.mkdir(parents=True, exist_ok=True)
    path = rd / filename
    path.write_text(json_text, encoding="utf-8")
    return path


def format_slack_summary(diff: DiffResult, *, max_inline: int = 20) -> str:
    lines = ["*Travis Condemned (Austin BSC) Diff*"]
    if diff.parse_zero_docs:
        lines.append(
            f":rotating_light: {diff.parse_zero_docs} agenda PDF(s) had case text but "
            "parsed 0 items - agenda layout may have changed (see debug capture)."
        )
    if not diff.meetings:
        lines.append("No new BSC agendas this run (quiet day).")
        return "\n".join(lines)
    lines.append(
        f":calendar: agendas parsed: *{len(diff.meetings)}* ({', '.join(diff.meetings)})  |  "
        f":new: NEW cases: *{len(diff.new)}*  |  :repeat: returning: *{len(diff.returning)}*  |  "
        f":arrow_up: escalated: *{len(diff.escalated)}*  |  appeals skipped: {diff.skipped_appeals}"
    )
    for cid, frm, to in diff.escalated[:max_inline]:
        lines.append(f"- {cid}: {frm or '(none)'} -> *{to}*")
    return "\n".join(lines)

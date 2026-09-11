"""Per-PDF memory for the Bell + Williamson foreclosure scrapers.

Why (2026-09-09): both scrapers read monthly notice PDFs that are image scans.
Bell's fixed OCR budget was re-spent on the SAME first PDFs every day, so the
later PDFs of the month (and all of next month) were never read; Williamson
simply threw away months without an index. This module remembers, per PDF:
its fingerprint (sha256 + size), how many pages have been OCR'd, the partial
text while incomplete, and the compact parsed notices — so a daily run
REPLAYS known PDFs for free and spends the OCR budget only on new, changed
or unfinished ones.

Mirrors `scrapers/tax_delinquent_state.py`'s per-county Apify shim:
  local  → data/{county}_foreclosure_state/foreclosure_{county}_state.json
  Apify  → module cache injected from / snapshotted to KVS by actor_main
           (keys: bell_foreclosure_state, williamson_foreclosure_state)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_RUNS = 60
RAW_TEXT_KEEP = 500

KVS_KEYS = {
    "bell": "bell_foreclosure_state",
    "williamson": "williamson_foreclosure_state",
}

RECORD_FIELDS = (
    "owner_name", "tax_owner_name", "address", "city", "zip",
    "auction_date", "date_added", "source_url",
)


def kvs_key(county: str) -> str:
    return KVS_KEYS.get(county.lower(), f"{county.lower()}_foreclosure_state")


# ── Paths ─────────────────────────────────────────────────────────────
def state_dir(county: str) -> Path:
    return config.PROJECT_ROOT / "data" / f"{county.lower()}_foreclosure_state"


def state_path(county: str) -> Path:
    return state_dir(county) / f"foreclosure_{county.lower()}_state.json"


# ── Apify KVS shim — per county ───────────────────────────────────────
_APIFY_STATE_CACHE: dict[str, dict] = {}


def _is_apify() -> bool:
    return bool(os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"))


def inject_apify_state(county: str, state: dict | None) -> None:
    """Seed the per-county cache from Apify KVS (called by actor_main)."""
    _APIFY_STATE_CACHE[county.lower()] = _migrate(dict(state)) if state else _empty_state()


def snapshot_apify_state(county: str) -> dict:
    """Current cached state for actor_main to persist to KVS."""
    return _APIFY_STATE_CACHE.get(county.lower(), _empty_state())


# ── State schema ──────────────────────────────────────────────────────
def _empty_state() -> dict:
    return {
        "schema": SCHEMA_VERSION,
        "last_run_date": "",
        # key (href or "Month/filename") -> entry, see record_pdf()
        "pdfs": {},
        "runs": [],
    }


def _migrate(data: dict) -> dict:
    for k, v in _empty_state().items():
        data.setdefault(k, v)
    return data


def load_state(county: str) -> dict:
    if _is_apify():
        cached = _APIFY_STATE_CACHE.get(county.lower())
        return dict(cached) if cached else _empty_state()
    sp = state_path(county)
    if not sp.exists():
        return _empty_state()
    try:
        return _migrate(json.loads(sp.read_text(encoding="utf-8")))
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
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, sp)


# ── Per-PDF helpers ───────────────────────────────────────────────────
def fingerprint(pdf_bytes: bytes) -> tuple[str, int]:
    return hashlib.sha256(pdf_bytes).hexdigest(), len(pdf_bytes)


def is_unchanged(entry: dict | None, sha256: str, size: int, parser_version: int) -> bool:
    """True when the stored entry was parsed from these exact bytes by this parser."""
    if not entry:
        return False
    return (
        entry.get("sha256") == sha256
        and int(entry.get("size") or -1) == size
        and int(entry.get("parser_version") or 0) == parser_version
    )


def size_matches(entry: dict | None, size: int | None) -> bool:
    """Cheap pre-check (e.g. from a HEAD Content-Length) before downloading."""
    return bool(entry) and size is not None and int(entry.get("size") or -1) == size


def _compact(n: NoticeData) -> dict:
    d = {f: getattr(n, f, "") or "" for f in RECORD_FIELDS}
    d["raw_text"] = (getattr(n, "raw_text", "") or "")[:RAW_TEXT_KEEP]
    return d


def record_pdf(
    state: dict,
    key: str,
    *,
    sha256: str,
    size: int,
    label: str,
    month: str,
    parser_version: int,
    text_source: str,
    pages_total: int,
    pages_done: int,
    complete: bool,
    partial_text: str,
    returned: int,
    notices: list[NoticeData],
    today: str | None = None,
) -> dict:
    """Store / refresh one PDF's entry. `partial_text` is kept ONLY while incomplete."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    prev = state["pdfs"].get(key) or {}
    entry = {
        "sha256": sha256,
        "size": size,
        "label": label,
        "month": month,
        "parser_version": parser_version,
        "first_seen": prev.get("first_seen") or today,
        "last_seen": today,
        "text_source": text_source,
        "pages_total": pages_total,
        "pages_done": pages_done,
        "complete": bool(complete),
        "partial_text": "" if complete else (partial_text or ""),
        "returned": returned,
        "records": [_compact(n) for n in notices],
    }
    state["pdfs"][key] = entry
    return entry


def touch(state: dict, key: str, today: str | None = None) -> None:
    """Mark a replayed (unchanged) PDF as seen today."""
    entry = state["pdfs"].get(key)
    if entry:
        entry["last_seen"] = today or datetime.now().strftime("%Y-%m-%d")


def replay(entry: dict, county: str, source_url: str = "") -> list[NoticeData]:
    """Rebuild NoticeData objects from a stored entry's compact records."""
    out: list[NoticeData] = []
    for rec in entry.get("records") or []:
        n = NoticeData(notice_type="foreclosure", county=county, state="TX")
        for f in RECORD_FIELDS:
            setattr(n, f, rec.get(f, "") or "")
        n.raw_text = rec.get("raw_text", "") or ""
        if source_url and not n.source_url:
            n.source_url = source_url
        out.append(n)
    return out


def prune(state: dict, keep_months: set[str], max_age_days: int = 120,
          today: datetime | None = None) -> int:
    """Drop entries for sale months no longer targeted and not seen recently."""
    today = today or datetime.now()
    dropped = 0
    for key in list(state["pdfs"].keys()):
        e = state["pdfs"][key]
        if e.get("month") in keep_months:
            continue
        try:
            last = datetime.strptime(e.get("last_seen") or "1970-01-01", "%Y-%m-%d")
        except ValueError:
            last = datetime(1970, 1, 1)
        if (today - last).days > max_age_days:
            del state["pdfs"][key]
            dropped += 1
    return dropped


def commit_run(county: str, state: dict, stats: dict, today: str | None = None) -> None:
    """Append run stats, stamp last_run_date, persist."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    runs = list(state.get("runs") or [])
    runs.append({"date": today, **{k: v for k, v in stats.items() if not k.startswith("_")}})
    state["runs"] = runs[-MAX_RUNS:]
    state["last_run_date"] = today
    save_state(county, state)

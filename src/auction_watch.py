"""Auction watch — let a seen posting back through when it gets BETTER.

The cross-run ``seen_notice_ids`` dedup is monotonic: once a dedup key is in
the set, every later scrape of that posting is dropped. That is right for a
record that never changes, and wrong for a foreclosure posting, which changes
in two ways that matter for marketing:

1. **It completes.** Travis files sit in the clerk's Temp index for days with
   no grantor name and often no LOC address (``tccsearch_common.effective_from_date``
   already re-scans that window). But the verified row carries the SAME
   instrument number, so its key is already "seen" and the complete version —
   owner, address, sale date — never reaches the CRM.
2. **It moves.** A postponed sale is re-posted for the next first Tuesday.
   Travis re-posts under a NEW instrument number, so that one does get through
   as a new row; but DataSift "Add Data" does not overwrite fields on an
   existing record, so the CRM keeps the old date (1421 Dexford Dr: archive
   says 11/3, CRM still says 9/1 — found 2026-10-08).

This module keeps a small per-key memory alongside ``seen_notice_ids`` —
``{key: {"auction_date": "YYYY-MM-DD", "complete": bool}}`` — and lets a seen
notice through only when it has become complete or its sale date changed.
The re-emitted notice carries ``watch_reason`` ("completed" / "moved") and,
for a move, ``auction_moved_from``; ``datasift_formatter`` turns those into
the AUCTION section of Notes and an ``auction_moved`` tag.

Only sale-dated types are watched (``WATCHED_TYPES``); everything else keeps
the plain monotonic behaviour. A key that is in ``seen_notice_ids`` but has
no watch entry (every key from before this module shipped) stays suppressed —
we cannot tell whether it improved, and re-emitting the whole history would
re-upload hundreds of rows.

State lives next to the seen set: KVS key ``auction_watch`` on Apify,
``config.AUCTION_WATCH_FILE`` locally. Both are dicts; missing/corrupt state
degrades to "no watch" (plain dedup), never to a crash.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

KVS_KEY = "auction_watch"
WATCHED_TYPES = frozenset({"foreclosure", "tax_sale"})


def is_complete(notice) -> bool:
    """A foreclosure posting is complete when it can be marketed AND timed."""
    has_party = bool(
        (getattr(notice, "owner_name", "") or "").strip()
        or (getattr(notice, "business_name", "") or "").strip()
    )
    return (
        has_party
        and bool((getattr(notice, "address", "") or "").strip())
        and bool((getattr(notice, "auction_date", "") or "").strip())
    )


def _entry(notice) -> dict:
    return {
        "auction_date": (getattr(notice, "auction_date", "") or "")[:10],
        "complete": is_complete(notice),
    }


def apply(notices: list, seen_ids: set[str], watch: dict) -> tuple[list, dict]:
    """Split ``notices`` into (keep, stats) and update ``watch`` in place.

    ``keep`` is what the pipeline should process: every unseen notice, plus
    seen watched-type notices that completed or moved. Notices NOT kept are
    the ordinary dedup drops. Each notice must already carry ``_dedup_key``.

    Watch entries are written for every kept watched-type notice (so the next
    run can compare), and refreshed for suppressed ones only when the new
    scrape is at least as good (never downgrade a complete entry to incomplete
    because one day's grid row came back thin).
    """
    keep: list = []
    stats = {"new": 0, "completed": 0, "moved": 0, "suppressed": 0, "unwatched_seen": 0}

    for n in notices:
        key = getattr(n, "_dedup_key", None)
        if not key:
            keep.append(n)
            continue
        ntype = (getattr(n, "notice_type", "") or "").strip().lower()

        if key not in seen_ids:
            keep.append(n)
            stats["new"] += 1
            if ntype in WATCHED_TYPES:
                watch[key] = _entry(n)
            continue

        if ntype not in WATCHED_TYPES:
            stats["suppressed"] += 1
            continue

        prev = watch.get(key)
        if prev is None:
            # Pre-watch history: seen, but we never recorded what it looked
            # like. Record it now so the NEXT change is visible; suppress today.
            watch[key] = _entry(n)
            stats["unwatched_seen"] += 1
            continue

        now = _entry(n)
        if not prev.get("complete") and now["complete"]:
            n.watch_reason = "completed"
            keep.append(n)
            stats["completed"] += 1
            watch[key] = now
            continue

        if (prev.get("auction_date") and now["auction_date"]
                and prev["auction_date"] != now["auction_date"]):
            n.watch_reason = "moved"
            n.auction_moved_from = prev["auction_date"]
            keep.append(n)
            stats["moved"] += 1
            watch[key] = now
            continue

        stats["suppressed"] += 1
        if now["complete"] or not prev.get("complete"):
            # Same date, no upgrade in marketability — refresh without
            # ever downgrading a complete entry.
            if now["auction_date"] or not prev.get("auction_date"):
                watch[key] = {**prev, **{k: v for k, v in now.items() if v}}

    return keep, stats


def record(notices: list, watch: dict) -> None:
    """After a run, make sure every processed watched notice has an entry."""
    for n in notices:
        key = getattr(n, "_dedup_key", None)
        ntype = (getattr(n, "notice_type", "") or "").strip().lower()
        if key and ntype in WATCHED_TYPES:
            cur = watch.get(key)
            new = _entry(n)
            if cur is None or new["complete"] or not cur.get("complete"):
                watch[key] = new


def format_stats(stats: dict) -> str:
    return (
        f"auction watch: {stats['new']} new, {stats['completed']} completed "
        f"(re-emitted), {stats['moved']} moved (re-emitted), "
        f"{stats['suppressed']} suppressed, {stats['unwatched_seen']} seen-before-watch"
    )


# ── persistence ──────────────────────────────────────────────────────────

def coerce(raw) -> dict:
    """Normalize whatever came back from KVS/disk into a watch dict."""
    if isinstance(raw, dict):
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
    return {}


def load_file(path: Path | None = None) -> dict:
    import config
    path = path or config.AUCTION_WATCH_FILE
    if not path.exists():
        return {}
    try:
        return coerce(json.loads(path.read_text()))
    except Exception as e:  # corrupt file → plain dedup, loudly
        logger.warning("Could not read %s (%s) — auction watch disabled this run", path, e)
        return {}


def save_file(watch: dict, path: Path | None = None) -> None:
    import config
    path = path or config.AUCTION_WATCH_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(watch, sort_keys=True))
    tmp.replace(path)

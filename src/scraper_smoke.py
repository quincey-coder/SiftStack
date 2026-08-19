"""Per-scraper live smoke test + diagnosis harness (operator CLI).

Runs every registered scraper DIRECTLY (bypassing scrape_targets' exception
handling), sequentially and headed, with a per-target timeout. Classifies each
target ok / zero / fail / timeout, and points SIFT_DEBUG_CAPTURE_DIR at a
per-target folder so any parse-zero or failure leaves raw HTML/text evidence
behind for parser forensics.

Usage (from repo root; does NOT go through main.py, so APIFY_TOKEN in .env
does not matter here):

    python src/scraper_smoke.py                       # all registered targets
    python src/scraper_smoke.py --only Travis/probate --days 14
    python src/scraper_smoke.py --skip-slow           # skip full-roll texdel trio
    python src/scraper_smoke.py --notify              # post the table to Slack

Windows prereqs: headed Chromium (a real desktop session — no Xvfb needed),
Tesseract on PATH (Bell foreclosure OCR), `pip install playwright-stealth`
(all Travis tccsearch scrapers), and a residential ISP IP (no proxy needed —
the Apify proxy exists only to get OFF datacenter IPs).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parent
sys.path.insert(0, str(_SRC))

from dotenv import load_dotenv

load_dotenv(_SRC.parent / ".env")

# The full-roll tax-delinquent scrapers download/parse hundreds of thousands
# of rows and ignore max_notices — several minutes each. --skip-slow skips them.
SLOW_TARGETS = {
    ("travis", "tax_delinquent"),
    ("bell", "tax_delinquent"),
    ("williamson", "tax_delinquent"),
}


def _parse_only(values: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for v in values:
        if "/" not in v:
            raise SystemExit(f"--only expects County/type (got {v!r})")
        c, t = v.split("/", 1)
        pairs.append((c.strip().lower(), t.strip().lower()))
    return pairs


async def _run_one(county: str, ntype: str, args, capture_root: Path) -> dict:
    """Run a single scraper directly; classify the outcome with evidence."""
    from scrapers import get_scraper, ScraperError

    capture_dir = capture_root / f"{county}_{ntype}"
    os.environ["SIFT_DEBUG_CAPTURE_DIR"] = str(capture_dir)

    since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    result = {
        "county": county, "type": ntype, "status": "fail", "count": 0,
        "duration_s": 0.0, "error": "", "evidence": {}, "captures": [],
    }
    scraper = get_scraper(county, ntype)
    if scraper is None:
        result["error"] = "not registered (import failure?)"
        return result

    t0 = time.monotonic()
    try:
        notices = await asyncio.wait_for(
            scraper.scrape(
                mode="daily",
                since_date=since,
                max_notices=args.max_notices or None,
            ),
            timeout=args.timeout,
        )
        result["count"] = len(notices)
        result["status"] = "ok" if notices else "zero"
        sample = [
            {"address": n.address, "owner": n.owner_name, "city": n.city}
            for n in notices[:3]
        ]
        if sample:
            result["sample"] = sample
    except asyncio.TimeoutError:
        result["status"] = "timeout"
        result["error"] = f"exceeded {args.timeout}s"
    except ScraperError as e:
        result["status"] = "fail"
        result["error"] = str(e)
        result["count"] = len(e.partial)
        if e.partial:
            result["evidence"]["partial"] = len(e.partial)
    except Exception as e:
        result["status"] = "fail"
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        result["duration_s"] = round(time.monotonic() - t0, 1)
        result["evidence"].update(dict(getattr(scraper, "last_meta", {}) or {}))
        if capture_dir.exists():
            result["captures"] = sorted(p.name for p in capture_dir.iterdir())
        os.environ.pop("SIFT_DEBUG_CAPTURE_DIR", None)

    return result


def _format_table(rows: list[dict]) -> str:
    icons = {"ok": "OK  ", "zero": "ZERO", "fail": "FAIL", "timeout": "TIME"}
    lines = [f"{'target':<28} {'status':<6} {'count':>5} {'secs':>7}  detail"]
    for r in rows:
        target = f"{r['county'].title()}/{r['type']}"
        detail = r["error"] or ""
        ev = r.get("evidence") or {}
        if not detail and ev.get("returned") is not None:
            detail = f"returned={ev['returned']} kept={ev.get('kept', r['count'])}"
        if r.get("captures"):
            detail += f"  [{len(r['captures'])} capture(s)]"
        lines.append(
            f"{target:<28} {icons.get(r['status'], r['status']):<6} "
            f"{r['count']:>5} {r['duration_s']:>7.1f}  {detail[:100]}"
        )
    return "\n".join(lines)


async def main_async(args) -> int:
    from scrapers import list_registered, registry_gaps, KNOWN_MISSING

    only = _parse_only(args.only) if args.only else None
    targets = list_registered()
    gaps = [g for g in registry_gaps() if g not in KNOWN_MISSING]

    if only:
        targets = [t for t in targets if t in only]
        missing = [o for o in only if o not in list_registered()]
        for m in missing:
            print(f"WARNING: {m[0]}/{m[1]} is not registered")
    if args.skip_slow:
        skipped = [t for t in targets if t in SLOW_TARGETS]
        targets = [t for t in targets if t not in SLOW_TARGETS]
        if skipped:
            print(f"--skip-slow: skipping {', '.join(f'{c}/{t}' for c, t in skipped)}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    capture_root = out_dir / "captures"

    print(f"Running {len(targets)} scraper(s) sequentially, headed, "
          f"window={args.days}d, cap={args.max_notices}, timeout={args.timeout}s")
    if gaps:
        print(f"REGISTRY GAPS (expected but unregistered): "
              f"{', '.join(f'{c}/{t}' for c, t in gaps)}")

    rows = []
    for county, ntype in targets:
        print(f"\n=== {county.title()}/{ntype} ===")
        rows.append(await _run_one(county, ntype, args, capture_root))
        print(f"  → {rows[-1]['status']} ({rows[-1]['count']} records, "
              f"{rows[-1]['duration_s']}s) {rows[-1]['error']}")

    report = {
        "ran_at": datetime.now().isoformat(timespec="seconds"),
        "window_days": args.days,
        "max_notices": args.max_notices,
        "registry_gaps": [f"{c}/{t}" for c, t in gaps],
        "results": rows,
    }
    report_path = out_dir / "smoke_report.json"
    report_path.write_text(json.dumps(report, indent=1), encoding="utf-8")

    table = _format_table(rows)
    fails = [r for r in rows if r["status"] in ("fail", "timeout")]
    zeros = [r for r in rows if r["status"] == "zero"]
    print("\n" + "=" * 90)
    print(table)
    print("=" * 90)
    print(f"{len(rows) - len(fails) - len(zeros)} ok · {len(zeros)} zero · "
          f"{len(fails)} fail/timeout · report: {report_path}")
    if zeros or fails:
        print(f"Evidence captures (if any): {capture_root}")

    if args.notify:
        from slack_notifier import _send_webhook
        header = (":stethoscope: *SiftStack scraper smoke test* — "
                  f"{len(rows) - len(fails) - len(zeros)} ok · {len(zeros)} zero · "
                  f"{len(fails)} FAILED")
        _send_webhook(header + "\n```\n" + table + "\n```")

    return len(fails)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--only", nargs="+", metavar="County/type",
                    help="run only these targets (e.g. Travis/probate Bell/lien)")
    ap.add_argument("--skip-slow", action="store_true",
                    help="skip the 3 full-roll tax-delinquent scrapers")
    ap.add_argument("--days", type=int, default=7,
                    help="scrape window in days (default 7)")
    ap.add_argument("--max-notices", type=int, default=5,
                    help="per-scraper record cap where supported (default 5)")
    ap.add_argument("--timeout", type=int, default=420,
                    help="per-scraper timeout in seconds (default 420)")
    ap.add_argument("--out", default="output/smoke",
                    help="report + capture directory (default output/smoke)")
    ap.add_argument("--notify", action="store_true",
                    help="post the result table to the Slack webhook")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())

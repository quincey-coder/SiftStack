"""Foreclosure hot list — every sale-dated record in DataSift, soonest first.

Reads (never writes) the CRM: all five foreclosure-flavoured lists, each
record's built-in ``auction_date`` (filled by DataFlik enrichment) and the
"Foreclosure Date" custom field (where OUR wizard upload lands — the wizard
has no built-in date target), takes the later of the two as the effective
sale date, and ranks by days-to-sale. Output: an Excel workbook + CSV in
output/, and an optional Slack block.

    python src/foreclosure_hotlist.py                 # workbook + csv
    python src/foreclosure_hotlist.py --notify        # + Slack block (top 25)
    python src/foreclosure_hotlist.py --horizon 90    # days ahead to include
    python src/foreclosure_hotlist.py --no-custom     # skip per-record custom-field reads (fast)

The per-record custom-field read is one API call per record (~1,600 → a few
minutes); it is what surfaces our own uploaded dates on records DataFlik has
not enriched. Results are cached for the day in data/hotlist_cache/.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
from datasift_api_client import DataSiftAPIClient, DataSiftAPIError  # noqa: E402

logger = logging.getLogger(__name__)

FORECLOSURE_LISTS = [
    "Foreclosure",
    "Pre-Foreclosure",
    "Pre-Foreclosures - Notice of Default",
    "Pre-Foreclosures - Notice of Foreclosure",
    "Pre-Foreclosures - Lis Pendens",
]
CUSTOM_DATE_LABELS = ("Foreclosure Date", "Foreclosure Sale Date", "Tax Auction Date")
CACHE_DIR = config.PROJECT_ROOT / "data" / "hotlist_cache"


def _iso(v) -> str:
    if not v:
        return ""
    s = str(v)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _custom_dates(vals: list[dict]) -> dict[str, str]:
    out = {}
    for v in vals or []:
        label = (v.get("label") or (v.get("field") or {}).get("label")
                 or (v.get("custom_field") or {}).get("label") or "")
        if label in CUSTOM_DATE_LABELS and v.get("value"):
            out[label] = _iso(v.get("value"))
    return out


def pull(client: DataSiftAPIClient, read_custom: bool = True) -> list[dict]:
    lists = {(l.get("title") or ""): l["uuid"] for l in client.list_lists(limit=300)}
    ids = [lists[n] for n in FORECLOSURE_LISTS if n in lists]
    summ, off = [], 0
    while True:
        r = client.search_records({"any_lists": ids}, limit=500, offset=off)
        res = r.get("results") or r.get("data") or []
        if not res:
            break
        summ += res
        off += len(res)
        if off >= r.get("count", 0):
            break
    logger.info("foreclosure-list records: %d", len(summ))

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"details_{date.today().isoformat()}.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    rows = []
    for i, x in enumerate(summ, 1):
        u = x["uuid"]
        d = cache.get(u)
        if d is None:
            try:
                full = client._request_json("GET", f"/api/internal/property/{u}/")
            except DataSiftAPIError as e:
                logger.warning("detail %s failed: %s", u, str(e)[:80])
                continue
            cf = {}
            if read_custom:
                try:
                    cf = _custom_dates(client.get_property_custom_field_values(u))
                except DataSiftAPIError:
                    cf = {}
            a = full.get("address") or {}
            o = full.get("owner") or {}
            d = {
                "uuid": u,
                "street": a.get("street") or "", "city": a.get("city") or "",
                "zip": (a.get("postal_code") or "")[:5], "county": a.get("county") or "",
                "owner": " ".join(p for p in (o.get("first_name"), o.get("last_name")) if p).strip()
                         or (o.get("company") or ""),
                "phones": len(o.get("phones") or []),
                "status": full.get("status") or "",
                "builtin_auction_date": _iso(full.get("auction_date")),
                "custom_dates": cf,
                "last_sold": _iso(full.get("last_sold")),
                "tags": full.get("tags") or [],
                "lists": full.get("lists") or [],
            }
            cache[u] = d
            if i % 100 == 0:
                cache_path.write_text(json.dumps(cache))
                logger.info("  %d/%d", i, len(summ))
        rows.append(d)
    cache_path.write_text(json.dumps(cache))
    return rows


def rank(rows: list[dict], today: date | None = None) -> dict:
    today = today or date.today()
    upcoming, passed, undated = [], [], []
    for r in rows:
        cands = [r.get("builtin_auction_date") or ""] + list((r.get("custom_dates") or {}).values())
        cands = [c for c in cands if c]
        if not cands:
            undated.append(r)
            continue
        eff = max(cands)                        # the latest posted sale date wins (re-posts move forward)
        r["sale_date"] = eff
        r["days"] = (datetime.strptime(eff, "%Y-%m-%d").date() - today).days
        r["date_sources"] = ("dataflik" if r.get("builtin_auction_date") else "") + (
            "+ours" if r.get("custom_dates") else "")
        r["moved"] = len(set(cands)) > 1
        (upcoming if r["days"] >= 0 else passed).append(r)
    upcoming.sort(key=lambda r: (r["days"], r["county"], r["street"]))
    passed.sort(key=lambda r: (-r["days"], r["county"], r["street"]))
    return {"upcoming": upcoming, "passed": passed, "undated": undated}


COLS = ["sale_date", "days", "county", "street", "city", "zip", "owner", "phones",
        "status", "date_sources", "moved", "last_sold", "uuid"]


def write_outputs(ranked: dict, horizon: int, out_dir: Path) -> tuple[Path, Path]:
    stamp = date.today().isoformat()
    up = [r for r in ranked["upcoming"] if r["days"] <= horizon]
    csv_path = out_dir / f"foreclosure_hotlist_{stamp}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(up)
    xlsx_path = out_dir / f"foreclosure_hotlist_{stamp}.xlsx"
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        def sheet(title, rows, color):
            ws = wb.create_sheet(title) if wb.active.title != "Sheet" else wb.active
            ws.title = title
            ws.sheet_properties.tabColor = color
            ws.append([c.replace("_", " ").title() for c in COLS])
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor=color)
            for r in rows:
                ws.append([r.get(c, "") for c in COLS])
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max(len(str(c.value or "")) for c in col)
                ws.column_dimensions[col[0].column_letter].width = min(max(10, width + 2), 44)
        sheet("Hot List", up, "C00000")
        sheet("Passed (verify outcome)", ranked["passed"], "7F6000")
        sheet("No sale date", ranked["undated"], "595959")
        wb.save(xlsx_path)
    except ImportError:
        logger.warning("openpyxl not installed — CSV only")
    return csv_path, xlsx_path


def slack_block(ranked: dict, horizon: int, limit: int = 25) -> str:
    up = [r for r in ranked["upcoming"] if r["days"] <= horizon]
    if not up:
        return ""
    by_day = {}
    for r in up:
        by_day.setdefault(r["sale_date"], 0)
        by_day[r["sale_date"]] += 1
    head = " · ".join(f"{datetime.strptime(d, '%Y-%m-%d').strftime('%a %m/%d')}: {n}"
                      for d, n in sorted(by_day.items()))
    lines = [f"*FORECLOSURE HOT LIST* — {len(up)} sale-dated records within {horizon}d  ({head})"]
    for r in up[:limit]:
        flags = (" ↪moved" if r["moved"] else "") + (" ☎" if r["phones"] else "")
        lines.append(f"• {r['days']:>3}d  {r['sale_date']}  {r['street']}, {r['city']} [{r['county']}]  {r['owner'] or '—'}{flags}")
    if len(up) > limit:
        lines.append(f"… +{len(up) - limit} more in the workbook")
    if ranked["passed"]:
        lines.append(f"_{len(ranked['passed'])} records have a sale date in the past — verify outcome before marketing_")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--horizon", type=int, default=120, help="days ahead to include (default 120)")
    ap.add_argument("--notify", action="store_true", help="post the top of the list to Slack")
    ap.add_argument("--no-custom", action="store_true", help="skip per-record custom-field reads")
    ap.add_argument("--out", default=str(config.OUTPUT_DIR))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    client = DataSiftAPIClient.from_env()
    rows = pull(client, read_custom=not args.no_custom)
    ranked = rank(rows)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path, xlsx_path = write_outputs(ranked, args.horizon, out_dir)
    up = [r for r in ranked["upcoming"] if r["days"] <= args.horizon]
    print(f"\nHOT LIST: {len(up)} upcoming within {args.horizon}d | "
          f"{len(ranked['passed'])} passed | {len(ranked['undated'])} without a sale date")
    for r in up[:15]:
        print(f"  {r['days']:>3}d  {r['sale_date']}  {r['street']}, {r['city']} [{r['county']}]  {r['owner'] or '—'}"
              + ("  moved" if r["moved"] else ""))
    print(f"\n  csv : {csv_path}\n  xlsx: {xlsx_path}")
    if args.notify:
        from slack_notifier import _send_webhook
        block = slack_block(ranked, args.horizon)
        print("  slack:", "sent" if block and _send_webhook(block) else "nothing sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())

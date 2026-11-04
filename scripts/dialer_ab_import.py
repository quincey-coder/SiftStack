"""Fill the SmrtPhone side of the dialer A/B workbook from the SmrtPhone call log.

Pairs with output/Dialer_Connect_Rate_Test_SmrtPhone_vs_Kixie.xlsx (the
SmrtPhone-vs-Kixie connect-rate test). Kixie has no puller in this repo — paste
its Call History export into the Call Log sheet by hand (Dialer = Kixie).

Steps:
    python src/smrtphone.py login            # once, headed, saves the session
    python src/smrtphone.py log --days 14    # -> output/call_coaching/call_log.json
    python scripts/dialer_ab_import.py       # appends outbound calls to the Call Log

Only writes the data columns A-K (Date, Dialer, Rep, Caller ID, Number dialed,
List, Time, Raw status, Duration, Disposition, Notes). The grey formula columns
L-R are never touched. Re-running is safe: a call already present (by
SmrtPhone call id in the Notes column) is skipped. Example rows are removed on
the first real import.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from openpyxl import load_workbook
from openpyxl.styles import Font

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOG = ROOT / "output" / "call_coaching" / "call_log.json"
DEFAULT_WB = ROOT / "output" / "Dialer_Connect_Rate_Test_SmrtPhone_vs_Kixie.xlsx"

def _central_tz(when_utc: dt.datetime) -> dt.timezone:
    """US Central for the given UTC instant (CDT = UTC-5, CST = UTC-6).

    Hand-rolled because the operator box's Python has no tzdata package.
    DST runs 2nd Sunday of March 02:00 local -> 1st Sunday of November 02:00 local.
    """
    y = when_utc.year
    def nth_sunday(month, n):
        d = dt.date(y, month, 1)
        d += dt.timedelta(days=(6 - d.weekday()) % 7)
        return d + dt.timedelta(weeks=n - 1)
    start = dt.datetime.combine(nth_sunday(3, 2), dt.time(8), dt.timezone.utc)   # 02:00 CST = 08:00 UTC
    end = dt.datetime.combine(nth_sunday(11, 1), dt.time(7), dt.timezone.utc)    # 02:00 CDT = 07:00 UTC
    return dt.timezone(dt.timedelta(hours=-5)) if start <= when_utc < end else dt.timezone(dt.timedelta(hours=-6))
SHEET = "Call Log"
DATA_COLS = 11           # A..K
EXAMPLE_MARK = "EXAMPLE ROW"
BLUE = Font(name="Arial", color="0000FF")


def load_calls(path: Path) -> list[dict]:
    calls = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(calls, list):
        raise SystemExit(f"{path} is not a list of calls")
    return calls


def to_local(created_utc: str) -> dt.datetime:
    when = dt.datetime.strptime(created_utc[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return when.astimezone(_central_tz(when))   # SmrtPhone stores UTC; the test is dialed in Texas


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG, help="call_log.json from src/smrtphone.py log")
    ap.add_argument("--workbook", type=Path, default=DEFAULT_WB)
    ap.add_argument("--list-name", default="", help="value for the List / campaign column")
    ap.add_argument("--since", help="only calls on/after this local date (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.log.exists():
        raise SystemExit(f"{args.log} not found — run: python src/smrtphone.py log --days 14")
    if not args.workbook.exists():
        raise SystemExit(f"{args.workbook} not found")

    calls = load_calls(args.log)
    since = dt.date.fromisoformat(args.since) if args.since else None

    wb = load_workbook(args.workbook)
    ws = wb[SHEET]

    # existing SmrtPhone call ids live in Notes as "smrtphone#<id>"; example rows get cleared
    seen: set[str] = set()
    last_data_row = 1
    example_rows: list[int] = []
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=1).value in (None, ""):
            continue
        last_data_row = r
        note = str(ws.cell(row=r, column=11).value or "")
        if EXAMPLE_MARK in note:
            example_rows.append(r)
        elif note.startswith("smrtphone#"):
            seen.add(note.split("#", 1)[1].split()[0])

    rows_out = []
    skipped_dup = skipped_dir = 0
    for c in calls:
        if (c.get("direction") or "").lower() not in ("outbound", "outgoing", "out"):
            skipped_dir += 1
            continue
        cid = str(c.get("call_id") or "")
        if cid and cid in seen:
            skipped_dup += 1
            continue
        when = to_local(c["created_at_utc"]) if c.get("created_at_utc") else None
        if since and when and when.date() < since:
            continue
        rows_out.append([
            when.date() if when else None,
            "SmrtPhone",
            c.get("caller") or "",
            c.get("from_num") or "",
            c.get("to_num") or "",
            args.list_name,
            when.time().replace(second=0, microsecond=0) if when else None,
            c.get("status") or "",
            int(c.get("duration_seconds") or 0),
            c.get("disposition") or "",
            f"smrtphone#{cid}" if cid else "",
        ])

    print(f"{len(calls)} calls in log; {len(rows_out)} new outbound rows; "
          f"{skipped_dup} already in workbook; {skipped_dir} inbound/other skipped")
    if args.dry_run or not rows_out:
        return 0

    # clear example rows' data columns (formula columns stay and go blank on their own)
    if example_rows:
        for r in example_rows:
            for col in range(1, DATA_COLS + 1):
                ws.cell(row=r, column=col).value = None
        free = sorted(example_rows)
        print(f"cleared {len(free)} example row(s)")
    else:
        free = []

    r = None
    for row in rows_out:
        r = free.pop(0) if free else (last_data_row := last_data_row + 1)
        for col, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=col, value=v)
            cell.font = BLUE
        ws.cell(row=r, column=1).number_format = "yyyy-mm-dd"
        ws.cell(row=r, column=7).number_format = "hh:mm"
    wb.save(args.workbook)
    print(f"wrote {len(rows_out)} row(s) -> {args.workbook}")
    print("Open the workbook in Excel once so the formula columns recalculate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

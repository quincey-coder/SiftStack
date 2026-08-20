"""Layer 3 — DirectSkip re-skip for FTM records, then merge found numbers back into reisift.

Default: trace only the FTM records that currently have NO phones (from ftm_phone_routing.json),
via DirectSkip ($0.10/hit, a no-match is free). Writes a DataSift merge CSV (address + Phone 1-9).
With --finish, merges the phones into the existing 'Foreclosure'-list records (Add-Data upsert by
address). After merging, re-run score_ftm_phones + run_phone_tag_upload to score/tag the new
numbers. Records still empty after this auto-land in the Deep Prospecting '01 No / Bad Phone' preset.

  python src/directskip_ftm.py                 # trace no-phone records, write merge CSV (no upload)
  python src/directskip_ftm.py --all           # trace ALL FTM records (fresh numbers from a 2nd provider)
  python src/directskip_ftm.py --finish        # also merge found phones into reisift
"""
import argparse
import asyncio
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from notice_parser import NoticeData  # noqa: E402
from directskip_batch import PHONE_FIELDS, batch_skip_trace  # noqa: E402
from datasift_formatter import write_datasift_split_csvs  # noqa: E402
from sift_upload_wizard import run_upload  # noqa: E402

MASTER = "output/foreclosure_master_active_2026-06-22.csv"
ROUTING = "output/ftm_phone_routing.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=MASTER)
    ap.add_argument("--all", action="store_true", help="trace ALL FTM records (default: only no-phone ones)")
    ap.add_argument("--finish", action="store_true", help="merge found phones into reisift")
    ap.add_argument("--max-cost", type=float, default=None,
                    help="hard USD cap for this run (default: MAX_DIRECTSKIP_COST_USD)")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.csv, encoding="utf-8")))
    empty_addrs = set()
    rp = Path(ROUTING)
    if rp.exists():
        for v in json.loads(rp.read_text(encoding="utf-8")).values():
            if not v.get("phones"):
                empty_addrs.add(v["addr"].strip().lower())

    notices, picked = [], []
    for r in rows:
        addr = (r.get("address") or "").strip()
        if not addr:
            continue
        if not a.all and addr.lower() not in empty_addrs:
            continue
        owner = (r.get("full_name") or f"{(r.get('first_name') or '').strip()} {(r.get('last_name') or '').strip()}").strip()
        if len(owner.split()) < 2:
            print(f"  skip (bad name): {addr!r} owner={owner!r}")
            continue
        n = NoticeData(owner_name=owner, address=addr, city=(r.get("city") or "").strip(),
                       state=(r.get("state") or "TX").strip(), zip=(r.get("zip") or "").strip(),
                       notice_type="foreclosure", county=(r.get("county") or "").strip())
        notices.append(n)
        picked.append((addr, owner))

    print(f"Tracing {len(notices)} records via DirectSkip ({'ALL FTM' if a.all else 'no-phone only'}):")
    for addr, owner in picked:
        print(f"  - {addr}  ({owner})")
    if not notices:
        print("Nothing to trace.")
        return

    stats = batch_skip_trace(notices, max_signing_traces=1, lookup_heir_addresses=False,
                             max_cost=a.max_cost)
    print("\nDirectSkip stats:", stats)
    if stats.get("auth_failed"):
        print("!! DirectSkip auth failed — check DIRECTSKIP_API_KEY and that this "
              "machine's IP is allowlisted with support@directskip.com.")
        return
    if stats.get("cost_capped"):
        print("!! Cost cap reached — re-run with --max-cost to trace the rest.")

    found = []
    print("\n=== phones found ===")
    for n in notices:
        ph = [getattr(n, f) for f in PHONE_FIELDS if getattr(n, f, "")]
        print(f"  {n.address[:30]:30} -> {len(ph)} phones {ph}")
        if ph:
            found.append(n)
    print(f"\n{len(found)}/{len(notices)} records got phones. "
          f"{len(notices) - len(found)} still empty -> Deep Prospecting (phone:0, skiptraced:1).")

    if not found:
        return
    csv_infos = write_datasift_split_csvs(found)
    csv_path = csv_infos[0]["path"]
    print("\nmerge CSV:", csv_path)

    if a.finish:
        iy, iw, _ = date.today().isocalendar()
        tags = ["FTM", "foreclosure", "has_auction", "Courthouse Data", f"{iy}-W{iw:02d}"]
        Path("output/_directskip_merge").mkdir(parents=True, exist_ok=True)
        res = asyncio.run(run_upload(csv_path, "Foreclosure", tags, existing_list=True,
                                     do_finish=True, headless=not a.headed,
                                     shot_base="output/_directskip_merge/run"))
        print("merge upload:", res)
        print("\nNext: re-run score_ftm_phones.py + run_phone_tag_upload.py to tier the new numbers.")
    else:
        print("\nDRY — pass --finish to merge the found phones into reisift.")


if __name__ == "__main__":
    main()

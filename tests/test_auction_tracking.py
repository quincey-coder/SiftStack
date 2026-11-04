"""Auction tracking — the sale date survives dedup, lands in Notes/tags, and
Williamson reads it off the posted notice.

Run: PYTHONPATH=src python tests/test_auction_tracking.py
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from notice_parser import NoticeData  # noqa: E402
import auction_watch as aw  # noqa: E402
from datasift_formatter import _build_notes, _build_tags, auction_bucket, auction_days_out  # noqa: E402
from scrapers.trustee_notice_parse import parse_trustee_notice_text  # noqa: E402
from scrapers.foreclosure_travis import _parse_address_from_legal  # noqa: E402
from slack_notifier import build_auction_block  # noqa: E402

fails = 0
def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails += 1

def fc(key, owner="", addr="", sale="", **kw):
    n = NoticeData(notice_type="foreclosure", county="Travis", owner_name=owner, address=addr,
                   city="Austin", zip="78701", auction_date=sale, **kw)
    n._dedup_key = key
    return n

print("auction_watch.apply")
seen = {"id:1", "id:2", "id:3", "id:9"}
watch = {"id:1": {"auction_date": "", "complete": False},
         "id:2": {"auction_date": "2026-10-06", "complete": True},
         "id:3": {"auction_date": "2026-10-06", "complete": True}}
batch = [fc("id:1", "Jane Doe", "1 Main St", "2026-11-03"),        # completed after Temp lag
         fc("id:2", "Bob Roe", "2 Oak St", "2026-11-03"),           # moved 10/6 -> 11/3
         fc("id:3", "Al Poe", "3 Elm St", "2026-10-06"),            # unchanged -> suppressed
         fc("id:9", "Legacy Guy", "9 Old St", "2026-11-03"),        # seen before watch existed
         fc("id:4", "", "", "2026-12-01"),                            # brand new, incomplete
         fc("id:5", "New Person", "5 New St", "2026-12-01")]        # brand new, complete
keep, stats = aw.apply(batch, seen, watch)
keys = [n._dedup_key for n in keep]
check(keys == ["id:1", "id:2", "id:4", "id:5"], f"kept {keys}")
check(batch[0].watch_reason == "completed", "completed re-emit flagged")
check(batch[1].watch_reason == "moved" and batch[1].auction_moved_from == "2026-10-06", "moved re-emit carries prior date")
check(stats == {"new": 2, "completed": 1, "moved": 1, "suppressed": 1, "unwatched_seen": 1}, f"stats {stats}")
check(watch["id:9"] == {"auction_date": "2026-11-03", "complete": True}, "legacy seen key now recorded for next time")
check(watch["id:4"]["complete"] is False and watch["id:5"]["complete"] is True, "new keys recorded")
# non-watched type never re-emits
pr = NoticeData(notice_type="probate", owner_name="X", address="1 A St", auction_date=""); pr._dedup_key = "id:3"
k2, _ = aw.apply([pr], seen, watch)
check(k2 == [], "non-watched type stays monotonic")
# a thin re-scrape never downgrades a complete entry
thin = fc("id:2", "", "", "2026-11-03"); aw.apply([thin], seen, watch)
check(watch["id:2"]["complete"] is True, "complete entry not downgraded by a thin row")
check(aw.coerce(["not","a","dict"]) == {} and aw.coerce({"k": {"a": 1}, "bad": 3}) == {"k": {"a": 1}}, "coerce tolerates junk")

print("formatter: tags + notes")
today = datetime.now().date()
soon = (today + timedelta(days=12)).strftime("%Y-%m-%d")
n = fc("x", "Jane Doe", "1 Main St", soon, date_added="2026-10-02",
       source_url="https://www.tccsearch.org/RealEstate/DocumentDetail.aspx?InstrumentNumber=2026117737")
n.auction_date_source = "record"
tags = _build_tags(n).split(",")
check("has_auction" in tags and f"auction_{soon}" in tags and "auction_in_30d" in tags, f"tags {tags}")
notes = _build_notes(n)
check(notes.startswith("=== AUCTION ==="), "AUCTION section leads living Notes")
check("in 12 days" in notes and "Posted: 10/2/2026" in notes and "InstrumentNumber=2026117737" in notes, "section carries countdown, filed date, posting")
check("Date source: sale date indexed" in notes, "date provenance stated")
n.auction_moved_from = "2026-10-06"; n.watch_reason = "moved"
check("auction_moved" in _build_tags(n) and "MOVED: was 10/6/2026" in _build_notes(n), "moved -> tag + MOVED line")
n2 = fc("y", "Bob Roe", "2 Oak St", (today - timedelta(days=2)).strftime("%Y-%m-%d"))
t2 = _build_tags(n2)
check("has_auction" not in t2 and "auction_passed" in t2, "passed date: no has_auction, auction_passed bucket")
n3 = fc("z", "Al Poe", "3 Elm St", soon); n3.auction_date_source = "folder_month"
check("auction_date_assumed" in _build_tags(n3) and "ASSUMED" in _build_notes(n3), "assumed date is labelled")
n4 = fc("w", "Dee Ceased", "4 Pine St", soon, owner_deceased="yes", decedent_name="Dee Ceased")
check(_build_notes(n4).startswith("=== AUCTION ==="), "AUCTION section leads deceased Notes too")
check(auction_bucket(0) == "auction_in_30d" and auction_bucket(61) == "auction_90d_plus" and auction_bucket(None) == "", "buckets")
check(auction_days_out("garbage") is None, "bad date -> None")
blk = build_auction_block([n, n2, n3])
check(blk.startswith("*UPCOMING AUCTIONS*") and "1 already passed" in blk and "moved from 2026-10-06" in blk, "slack block")

print("trustee notice parser stamps date provenance")
body = ("NOTICE OF SUBSTITUTE TRUSTEE'S SALE. Deed of Trust executed by Loraine A Perdomo, a single woman. "
        "Property commonly known as 107 Spring Garden Dr, Hutto, Texas 78634. " + "Filler text for length. " * 6)
p = parse_trustee_notice_text(body + " The sale will be held November 3, 2026, the first Tuesday of the month.",
                              county="Bell", fallback_sale_date="2026-12-01")
check(p is not None and p.auction_date == "2026-11-03" and p.auction_date_source == "notice_text",
      f"date read from text -> notice_text ({p and (p.auction_date, p.auction_date_source)})")
p = parse_trustee_notice_text(body, county="Bell", fallback_sale_date="2026-12-01")
check(p is not None and p.auction_date == "2026-12-01" and p.auction_date_source == "folder_month",
      f"fallback date -> folder_month ({p and (p.auction_date, p.auction_date_source)})")

print("real upload path (_build_row) carries the AUCTION block")
from datasift_formatter import _build_row, _build_dm_notes  # noqa: E402
row = _build_row(n, notes_override=_build_dm_notes(n))
check(row["Notes"].startswith("=== AUCTION ==="), "living row via write_datasift_by_notice_type path")
check(row["Foreclosure Date"] == f"{int(soon[5:7])}/{int(soon[8:10])}/{soon[:4]}", f"Foreclosure Date column {row['Foreclosure Date']}")
row4 = _build_row(n4, notes_override=_build_dm_notes(n4))
check(row4["Notes"].startswith("=== AUCTION ===") and "=== DECEASED OWNER ===" in row4["Notes"], "deceased row keeps AUCTION first, then DECEASED")

print("Travis LOC regression (this morning's rejects)")
for legal, want in (("LOC 16201 ARROW HEAD DRIVE LEANDER TX 78641", ("16201 Arrow Head Drive", "Leander")),
                    ("LOC 2317 SOUTH PLEASANT VALLEY APT 625 AUSTIN TX 78741", ("2317 South Pleasant Valley Apt 625", "Austin")),
                    ("LOC 1205 EAST 13TH STREET AUSTIN TX 78702", ("1205 East 13Th Street", "Austin")),
                    ("LOC 22108 CROSS TIMBERS BEND LAGO VISTA TX 78645", ("22108 Cross Timbers Bend", "Lago Vista")),
                    ("LOC 123 MAIN ST BUDA TX 78610", ("123 Main St", "Buda"))):
    a, c, _ = _parse_address_from_legal(legal)
    check((a, c) == want, f"{legal[4:40]} -> {a!r}/{c!r}")

print()
print("FAILURES:", fails)
sys.exit(1 if fails else 0)

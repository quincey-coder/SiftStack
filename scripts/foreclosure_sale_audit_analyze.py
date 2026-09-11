"""Foreclosure sale-date audit, step 2 of 2: ANALYZE + build the cleanup CSV.

Reads the cache written by foreclosure_sale_audit_pull.py, classifies every
foreclosure record by its sale date (latest of courthouse "Foreclosure Date"
and SiftMap `auction_date`; conservative so an active sale is never called past):
  past / upcoming (<=45 days) / future / inferred_* (Notice-of-Foreclosure
  records with only a recording date: first Tuesday >= recording + 21 days,
  reported but NEVER put in the upload file) / no_sale_date.

--write emits two files in output/:
  datasift_foreclosure_sale_passed_RecentlySold_<ts>.csv  — DataSift template
      tag-update rows (Tags = "Recently Sold,<county>,foreclosure_sale_passed_YYYY-MM",
      Lists blank) for the wizard's update-by-property-address upload
  foreclosure_sale_date_review_<ts>.csv — every dated record for review
"""
import sys, json, csv, re
from pathlib import Path
from datetime import date, datetime, timedelta
from collections import Counter, defaultdict
sys.path.insert(0, "src")
from datasift_formatter import DATASIFT_COLUMNS, _format_date
from list_validator import validate_file

SP = Path(sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else "output/foreclosure_audit"); WRITE = "--write" in sys.argv
TODAY = date.today()
UPCOMING_DAYS = 45
state = json.loads((SP / "foreclosure_pull.json").read_text())
recs = state["records"]

def pdate(s):
    if not s: return None
    s = str(s)[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
        try: return datetime.strptime(s, fmt).date()
        except ValueError: pass
    return None

def first_tuesday_on_or_after(d):
    # TX trustee sales happen the first Tuesday of the month; notice must be posted >=21 days before
    y, m = d.year, d.month
    while True:
        ft = date(y, m, 1)
        while ft.weekday() != 1: ft += timedelta(days=1)
        if ft >= d: return ft
        m += 1
        if m == 13: y, m = y + 1, 1

FC_LISTS = ("Foreclosure", "Pre-Foreclosure", "Pre-Foreclosures - Notice of Foreclosure", "FTM Foreclosure 08-12 to 08-17")
skipped_not_fc = 0
rows = []
for u, r in recs.items():
    if "error" in r: continue
    lists_ = set(r.get("lists") or []); tags_ = set(r.get("tags") or []); cf0 = r.get("cf") or {}
    is_fc = bool(lists_ & set(FC_LISTS)) or "Foreclosure" in tags_ or cf0.get("Notice Type") == "Foreclosure"
    if not is_fc:
        skipped_not_fc += 1; continue
    cf = r.get("cf") or {}
    our = pdate(cf.get("Foreclosure Date"))
    ds_auction = pdate(r.get("auction_date"))
    ds_rec = pdate(r.get("foreclosure_date"))
    dates = [d for d in (our, ds_auction) if d]
    sale = max(dates) if dates else None        # conservative: latest known sale date
    last_sold = pdate(r.get("last_sold"))
    addr = r.get("address") or {}; own = r.get("owner") or {}; oaddr = own.get("address") or {}
    ours = "Courthouse Data" in (r.get("tags") or []) or "Foreclosure" in (r.get("lists") or [])
    inferred = None
    if sale is None and ds_rec and "Pre-Foreclosures - Notice of Foreclosure" in lists_:
        inferred = first_tuesday_on_or_after(ds_rec + timedelta(days=21))
    if sale is None: cls = "inferred_past" if (inferred and inferred < TODAY) else ("inferred_upcoming" if inferred else "no_sale_date")
    elif sale < TODAY: cls = "past"
    elif sale <= TODAY + timedelta(days=UPCOMING_DAYS): cls = "upcoming"
    else: cls = "future"
    rows.append({
        "uuid": u, "class": cls, "sale_date": sale, "inferred_sale_date": inferred, "our_date": our, "ds_auction_date": ds_auction,
        "ds_recording_date": ds_rec, "days_past": (TODAY - sale).days if sale and sale < TODAY else "",
        "street": addr.get("street") or "", "city": addr.get("city") or "", "state": addr.get("state") or "",
        "zip": (addr.get("postal_code") or "")[:5], "county": addr.get("county") or cf.get("County") or "",
        "first": own.get("first_name") or "", "last": own.get("last_name") or "", "company": own.get("company") or "",
        "m_street": oaddr.get("street") or "", "m_city": oaddr.get("city") or "", "m_state": oaddr.get("state") or "",
        "m_zip": (oaddr.get("postal_code") or "")[:5], "apn": r.get("apn") or r.get("parcel_id") or "",
        "status": r.get("status") or "", "lists": "; ".join(r.get("lists") or []), "sources": "; ".join(r.get("sources") or []),
        "ours": ours, "notice_type": cf.get("Notice Type") or "", "source_url": cf.get("Source URL") or "",
        "last_sold": last_sold, "mls": r.get("mls") or "",
        "deed_after_sale": bool(sale and last_sold and last_sold >= sale - timedelta(days=7)),
        "dates_disagree": bool(our and ds_auction and our != ds_auction),
    })

by = Counter(r["class"] for r in rows)
print(f"skipped non-foreclosure (tax-sale has_auction / NOD-only / lis pendens-only): {skipped_not_fc}")
print(f"records analysed: {len(rows)}  (errors: {sum(1 for r in recs.values() if 'error' in r)})")
print("classes:", dict(by))
past = sorted([r for r in rows if r["class"] == "past"], key=lambda r: r["sale_date"])
upc = sorted([r for r in rows if r["class"] == "upcoming"], key=lambda r: r["sale_date"])
fut = [r for r in rows if r["class"] == "future"]
print(f"past: ours={sum(r['ours'] for r in past)} siftmap={sum(not r['ours'] for r in past)}  deed_after_sale={sum(r['deed_after_sale'] for r in past)}  with_status={sum(1 for r in past if r['status'])}")
print("past by month:", sorted(Counter(r["sale_date"].strftime('%Y-%m') for r in past).items()))
print("upcoming by date:", sorted(Counter(str(r["sale_date"]) for r in upc).items()))
print("future by month:", sorted(Counter(r["sale_date"].strftime('%Y-%m') for r in fut).items()))
print("ours total:", sum(r["ours"] for r in rows), "ours by class:", dict(Counter(r["class"] for r in rows if r["ours"])))
print("disagree (ours vs SiftMap):", [(r['street'], str(r['our_date']), str(r['ds_auction_date'])) for r in rows if r['dates_disagree']])
print("statuses on past:", Counter(r["status"] for r in past if r["status"]))
print("no_sale_date by list:", Counter(l for r in rows if r['class']=='no_sale_date' for l in r['lists'].split('; ') if 'oreclos' in l or 'Pendens' in l))

if WRITE:
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out = Path("output")
    up = out / f"datasift_foreclosure_sale_passed_RecentlySold_{ts}.csv"
    with open(up, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS); w.writeheader()
        for r in past:
            county = (r["county"] or "").split()[0] if r["county"] else ""
            tags = ["Recently Sold"]
            if county: tags.append(county.lower())
            tags.append(f"foreclosure_sale_passed_{TODAY:%Y-%m}")
            basis = "courthouse notice" if r["our_date"] else "SiftMap auction date"
            extra = " SiftMap shows a deed recorded after the sale." if r["deed_after_sale"] else ""
            note = (f"Foreclosure sale date {_format_date(str(r['sale_date']))} has passed "
                    f"({r['days_past']} days ago, per {basis}) — tagged Recently Sold for cleanup on {_format_date(str(TODAY))}.{extra}")
            row = {c: "" for c in DATASIFT_COLUMNS}
            row.update({
                "Property Street Address": r["street"], "Property City": r["city"], "Property State": r["state"], "Property ZIP Code": r["zip"],
                "FULL NAME/COMPANY/TRUST": r["company"], "Owner First Name": r["first"], "Owner Last Name": r["last"],
                "Mailing Street Address": r["m_street"], "Mailing City": r["m_city"], "Mailing State": r["m_state"], "Mailing ZIP Code": r["m_zip"],
                "Tags": ",".join(tags), "Lists": "", "Notes": note,
                "Foreclosure Date": _format_date(str(r["sale_date"])), "APN": r["apn"],
                "Notice Type": r["notice_type"], "County": county if county in ("Travis","Bell","Williamson") else "",
            })
            w.writerow(row)
    rev = out / f"foreclosure_sale_date_review_{ts}.csv"
    cols = ["class","sale_date","inferred_sale_date","days_past","street","city","zip","county","first","last","company","status","our_date","ds_auction_date",
            "ds_recording_date","last_sold","deed_after_sale","dates_disagree","mls","ours","notice_type","lists","sources","apn","source_url","uuid"]
    with open(rev, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore"); w.writeheader()
        rest = sorted([r for r in rows if r["class"] in ("inferred_past","inferred_upcoming")], key=lambda r: r["inferred_sale_date"])
        for r in past + upc + sorted(fut, key=lambda r: r["sale_date"]) + rest: w.writerow(r)
    print("inferred:", dict(Counter(r["class"] for r in rows if r["class"].startswith("inferred"))),
          "inferred_past by month:", sorted(Counter(r["inferred_sale_date"].strftime("%Y-%m") for r in rows if r["class"]=="inferred_past").items()))
    fr = validate_file(up)
    print("UPLOAD:", up, len(past), "rows")
    print("REVIEW:", rev)
    print("validator:", fr.ok if hasattr(fr,'ok') else fr, [ (x.severity, x.kind, x.message) for x in getattr(fr,'findings',[]) ][:10])

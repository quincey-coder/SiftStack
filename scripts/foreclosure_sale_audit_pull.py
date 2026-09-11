"""Foreclosure sale-date audit, step 1 of 2: PULL (read-only DataSift API).

Sweeps every foreclosure-flavoured record in the account (our "Foreclosure"
list, the SiftMap "Pre-Foreclosures - *" lists, the has_auction/Foreclosure
tags), hydrates each with the detail + custom-field endpoints, and caches the
result to <cache_dir>/foreclosure_pull.json (resumable: reruns only fill gaps;
delete the file to start over).

Why this exists (2026-09-08): the daily pipeline never revisits a foreclosure
after upload — only tax-delinquent drop-offs (Sold) and code-enforcement
closures (Resolved) round-trip. Sale dates live in our custom field
"Foreclosure Date" (courthouse notice) and DataSift's built-in `auction_date`
(SiftMap); most SiftMap Notice-of-Foreclosure records carry NO sale date at all.

Run from the project root:
    python scripts/foreclosure_sale_audit_pull.py output/foreclosure_audit
    python scripts/foreclosure_sale_audit_analyze.py output/foreclosure_audit --write
"""
import sys, json, time, logging, re
from pathlib import Path
sys.path.insert(0, "src")
logging.basicConfig(level=logging.WARNING)
from datasift_api_client import DataSiftAPIClient, DataSiftAPIError

SP = Path(sys.argv[1] if len(sys.argv) > 1 else "output/foreclosure_audit"); SP.mkdir(parents=True, exist_ok=True)
CACHE = SP / "foreclosure_pull.json"
GAP = 0.25
c = DataSiftAPIClient.from_env()

def call(fn, *a, **k):
    for attempt in range(8):
        time.sleep(GAP)
        try:
            return fn(*a, **k)
        except DataSiftAPIError as e:
            if e.status == 429:
                m = re.search(r"available in (\d+)", e.body or "")
                wait = min((int(m.group(1)) if m else 60) + 3, 300)
                print(f"  429 -> sleep {wait}s", flush=True); time.sleep(wait); continue
            if e.status >= 500:
                time.sleep(5); continue
            raise
        except Exception as ex:
            print("  transient:", str(ex)[:120], flush=True); time.sleep(5)
    raise RuntimeError("retries exhausted")

def getj(path):
    return call(c._request_json, "GET", path)

lists = {l["title"]: l["uuid"] for l in call(c.list_lists, limit=999)}
tags = {t["title"]: t["uuid"] for t in call(c.list_tags, limit=999)}
SOURCES = {
    "list:Foreclosure": {"any_lists": [lists["Foreclosure"]]},
    "list:Pre-Foreclosure": {"any_lists": [lists["Pre-Foreclosure"]]},
    "list:FTM Foreclosure 08-12 to 08-17": {"any_lists": [lists["FTM Foreclosure 08-12 to 08-17"]]},
    "list:Pre-Foreclosures - Notice of Foreclosure": {"any_lists": [lists["Pre-Foreclosures - Notice of Foreclosure"]]},
    "list:Pre-Foreclosures - Notice of Default": {"any_lists": [lists["Pre-Foreclosures - Notice of Default"]]},
    "list:Pre-Foreclosures - Lis Pendens": {"any_lists": [lists["Pre-Foreclosures - Lis Pendens"]]},
    "tag:has_auction": {"any_tags": [tags["has_auction"]]},
    "tag:Foreclosure": {"any_tags": [tags["Foreclosure"]]},
}
# option labels for select custom fields
opts = {}
r = getj("/api/internal/custom-fields/?limit=999")
for f in (r.get("results") or r.get("data") or []):
    for o in (f.get("options") or []):
        if o.get("uuid"): opts[o["uuid"]] = o.get("label") or o.get("title") or o.get("value")

state = json.loads(CACHE.read_text()) if CACHE.exists() else {"thin": {}, "records": {}}
thin = state["thin"]
if not thin:
    for name, must in SOURCES.items():
        offset, total = 0, None
        while True:
            r = call(c.search_records, must, limit=100, offset=offset)
            rows = r.get("results") or []
            total = r.get("count", 0)
            for row in rows:
                e = thin.setdefault(row["uuid"], {"sources": [], "thin": row})
                if name not in e["sources"]: e["sources"].append(name)
            offset += 100
            print(f"{name}: {min(offset,total)}/{total}", flush=True)
            if offset >= total or not rows: break
    CACHE.write_text(json.dumps(state))
print(f"unique records: {len(thin)}", flush=True)

recs = state["records"]
todo = [u for u in thin if u not in recs]
t0 = time.time()
for i, u in enumerate(todo, 1):
    try:
        d = getj(f"/api/internal/property/{u}/")
    except DataSiftAPIError as e:
        recs[u] = {"error": f"{e.status}"}; continue
    keep = {k: d.get(k) for k in ("uuid","type","lists","tags","status","apn","parcel_id","foreclosure_date","auction_date",
                                  "tax_auction_date","last_sold","last_sale_price","mls","owned_since","estimate_value","last_updated_fields")}
    keep["address"] = d.get("address"); keep["owner"] = {k: (d.get("owner") or {}).get(k) for k in ("first_name","last_name","company","type","address")}
    keep["sources"] = thin[u]["sources"]
    tagset = set(d.get("tags") or []); listset = set(d.get("lists") or [])
    if "Courthouse Data" in tagset or "Foreclosure" in listset or "FTM" in tagset:
        cf = getj(f"/api/internal/property/{u}/custom-field/")
        vals = {}
        for x in (cf.get("results") or cf.get("data") or (cf if isinstance(cf, list) else [])):
            lab = (x.get("custom_field") or {}).get("label"); v = x.get("value")
            if (x.get("custom_field") or {}).get("field_type") == "select": v = opts.get(v, v)
            vals[lab] = v
        keep["cf"] = vals
    recs[u] = keep
    if i % 25 == 0 or i == len(todo):
        CACHE.write_text(json.dumps(state))
        el = time.time()-t0
        print(f"hydrated {i}/{len(todo)}  {el:.0f}s  eta {el/i*(len(todo)-i):.0f}s", flush=True)
CACHE.write_text(json.dumps(state))
print("DONE", flush=True)

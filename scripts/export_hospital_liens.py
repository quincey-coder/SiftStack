"""Export the CRM's hospital-lien records to a review CSV (READ-ONLY).

Why (2026-10-27): the lien scrapers pulled HOSPITAL LIEN doc types for months.
A hospital lien attaches to the patient's personal-injury claim, not their real
property (Tex. Prop. Code §55.005(d) requires the notice to say so), so these
records are accident patients marketed as if their homes were encumbered. The
scrapers now exclude the type (scrapers/lien_common.is_excluded_lien_type); this
script lists what already landed so the owner can remove or re-tag it in the
DataSift UI. The API client is READ-ONLY by owner policy — this script never
deletes, tags or edits anything.

Run from the project root:
    python scripts/export_hospital_liens.py [--tag hospital_lien] [--out output/...]
"""
import argparse
import csv
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "src")
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env")
from datasift_api_client import DataSiftAPIClient, DataSiftAPIError  # noqa: E402

GAP = 0.3
PAGE = 100


def _call(fn, *a, **k):
    for attempt in range(6):
        time.sleep(GAP)
        try:
            return fn(*a, **k)
        except DataSiftAPIError as e:
            if e.status == 429 or e.status >= 500:
                time.sleep(10 * (attempt + 1))
                continue
            raise
    raise RuntimeError("retries exhausted")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", default="hospital_lien", help="CRM tag to export")
    ap.add_argument("--out", default="", help="output CSV path")
    args = ap.parse_args()

    client = DataSiftAPIClient.from_env()
    tags = {t["title"]: t["uuid"] for t in _call(client.list_tags, limit=999)}
    if args.tag not in tags:
        print(f"tag {args.tag!r} not found in the account; nothing to export")
        return 1
    must = {"any_tags": [tags[args.tag]]}
    total = _call(client.count_records, must)
    print(f"{total} records carry tag {args.tag!r}")

    rows: list[dict] = []
    offset = 0
    while offset < total:
        r = _call(client.search_records, must, limit=PAGE, offset=offset)
        page = r.get("results") or []
        if not page:
            break
        for p in page:
            # The search rows are thin (no tags/lists/status); hydrate from the
            # detail endpoint (read-only GET) so the review CSV is actionable.
            try:
                detail = _call(client._request_json, "GET", f"/api/internal/property/{p['uuid']}/")
                if isinstance(detail, dict):
                    p = {**p, **{k: detail.get(k, p.get(k)) for k in ("tags", "lists", "status", "owners", "address")}}
            except Exception as e:  # keep going on a single bad record
                print(f"  detail failed for {p.get('uuid')}: {str(e)[:80]}")
            addr = p.get("address") or {}
            if isinstance(addr, str):
                addr = {"street": addr}
            owners = p.get("owners") or p.get("owner") or []
            if isinstance(owners, dict):
                owners = [owners]
            owner_names = "; ".join(
                " ".join(filter(None, [o.get("first_name"), o.get("last_name")])).strip()
                or (o.get("full_name") or o.get("name") or "")
                for o in owners if isinstance(o, dict)
            )
            rows.append({
                "property_uuid": p.get("uuid", ""),
                "street": addr.get("street", ""),
                "city": addr.get("city", ""),
                "state": addr.get("state", ""),
                "zip": addr.get("zip5") or addr.get("postal_code", ""),
                "owner": owner_names,
                "status": (p.get("status") or {}).get("title", "") if isinstance(p.get("status"), dict) else p.get("status", ""),
                "tags": "|".join(t.get("title", "") if isinstance(t, dict) else str(t) for t in (p.get("tags") or [])),
                "lists": "|".join(l.get("title", "") if isinstance(l, dict) else str(l) for l in (p.get("lists") or [])),
            })
        offset += PAGE
        print(f"  fetched {min(offset, total)}/{total}", flush=True)

    out = Path(args.out) if args.out else Path("output") / f"hospital_lien_review_{datetime.now():%Y-%m-%d_%H%M%S}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["property_uuid"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows -> {out}")
    print("Next step (owner, in the DataSift UI): filter Tags = hospital_lien, review, "
          "then delete or remove from the Liens list. Nothing was changed by this script.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Read-only check that a cloud upload test landed: list the DataSift records
carrying the test tag (DataSiftAPIClient is read-only by policy)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
from datasift_api_client import DataSiftAPIClient

tag = sys.argv[1] if len(sys.argv) > 1 else "apify_upload_test"
c = DataSiftAPIClient.from_env()
tags = c.list_tags(limit=1000)
hit = [t for t in tags if t.get("title") == tag]
if not hit:
    print(f"tag {tag!r} does not exist in DataSift yet")
    sys.exit(1)
res = c.search_records({"any_tags": [hit[0]["uuid"]]}, limit=50)
print(f"tag {tag!r}: {res.get('count')} record(s)")
for r in res.get("results") or []:
    a = r.get("address") or {}
    street = a.get("street") or a.get("full_address") or r.get("full_address") or ""
    lists = [l.get("title") if isinstance(l, dict) else l for l in (r.get("lists") or [])]
    print("  ", street, "|", a.get("city", ""), a.get("zip", "") or a.get("zip_code", ""), "| lists:", lists)

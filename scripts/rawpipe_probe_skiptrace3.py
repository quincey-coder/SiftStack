"""Round 3 — figure out the body shape for POST /property/skip-trace/.

Strategy:
  1. Find the e2e test list UUID
  2. Query properties in that list to get a property UUID
  3. Try various body shapes against /api/internal/property/skip-trace/
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datasift_api_client import DataSiftAPIClient


def main():
    client = DataSiftAPIClient.from_env()

    # 1. Find the e2e list
    print("[STEP 1] Finding RAWPIPE_e2e test list…")
    lists = client.list_lists(limit=200)
    target = next(
        (l for l in lists if l.get("title", "").startswith("RAWPIPE_e2e_")),
        None,
    )
    if not target:
        print("[FAIL] No RAWPIPE_e2e_* list found.")
        return 1
    list_uuid = target["uuid"]
    list_title = target["title"]
    print(f"         list_uuid = {list_uuid}")
    print(f"         title     = {list_title}")

    # 2. Find a property UUID in that list. The UI uses /property/ for fetching.
    # We saw POST /property/ in the captures — let's try a search-style POST.
    print("\n[STEP 2] Looking for a property in that list…")
    candidate_search_bodies = [
        {"lists": [list_uuid]},
        {"list": list_uuid},
        {"filter": {"lists": [list_uuid]}},
        {"filters": {"lists": [list_uuid]}},
    ]
    prop_uuid = None
    for body in candidate_search_bodies:
        resp = client._request("POST", "/api/internal/property/?limit=5", json=body)
        if resp.status_code in (200, 201):
            try:
                data = resp.json()
                items = data.get("results") or data.get("data") or data
                if isinstance(items, list) and items:
                    prop_uuid = items[0].get("uuid")
                    print(f"         got property via body shape: {body}")
                    print(f"         property uuid = {prop_uuid}")
                    print(f"         first record keys: {list(items[0].keys())[:8]}")
                    break
            except Exception:
                pass
        else:
            print(f"         body {body} → {resp.status_code}: {resp.text[:120]}")
    if not prop_uuid:
        print("[WARN] Could not find a property uuid. Will probe skip-trace without one.")

    # 3. Probe POST /property/skip-trace/ with various body shapes
    print("\n[STEP 3] Probing POST /property/skip-trace/ body shapes…")
    candidates = [
        {},
        {"property_uuids": [prop_uuid]} if prop_uuid else None,
        {"properties": [prop_uuid]} if prop_uuid else None,
        {"property_ids": [prop_uuid]} if prop_uuid else None,
        {"uuids": [prop_uuid]} if prop_uuid else None,
        {"lists": [list_uuid]},
        {"list": list_uuid},
        {"list_uuid": list_uuid},
        {"filter": {"lists": [list_uuid]}},
        {"filters": {"lists": [list_uuid]}},
        {"tags": ["api_skip_trace_test"]} if prop_uuid else None,
        {"property_uuids": [prop_uuid], "tags": ["api_skip_trace_test"]} if prop_uuid else None,
        {"property_uuids": [prop_uuid], "list_uuid": list_uuid} if prop_uuid else None,
    ]
    for body in candidates:
        if body is None:
            continue
        resp = client._request("POST", "/api/internal/property/skip-trace/", json=body)
        try:
            display = json.dumps(resp.json(), indent=None)[:200]
        except Exception:
            display = resp.text[:200]
        print(f"  {resp.status_code:3}  body={json.dumps(body)[:80]:80}  → {display}")


if __name__ == "__main__":
    sys.exit(main() or 0)

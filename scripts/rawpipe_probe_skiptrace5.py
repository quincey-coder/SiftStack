"""Round 5 — find correct property-list filter param, then attempt skip-trace."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datasift_api_client import DataSiftAPIClient


def main():
    client = DataSiftAPIClient.from_env()
    lists = client.list_lists(limit=200)
    target = next(
        (l for l in lists if l.get("title", "").startswith("RAWPIPE_e2e_")),
        None,
    )
    list_uuid = target["uuid"]
    print(f"List UUID: {list_uuid}\n")

    print("PART 1 — find the filter-by-list param syntax")
    print("-" * 80)
    variants = [
        f"?lists={list_uuid}&limit=5",
        f"?lists__uuid={list_uuid}&limit=5",
        f"?lists__in={list_uuid}&limit=5",
        f"?list={list_uuid}&limit=5",
        f"?list_uuid={list_uuid}&limit=5",
        f"?list__uuid={list_uuid}&limit=5",
        f"?list_id={list_uuid}&limit=5",
        f"?in_list={list_uuid}&limit=5",
        f"?lists[]={list_uuid}&limit=5",
        f"?filter[lists]={list_uuid}&limit=5",
    ]
    for q in variants:
        resp = client._request("GET", f"/api/internal/property/{q}")
        if resp.status_code == 200:
            d = resp.json()
            count = d.get("count")
            print(f"  count={count:>5}  {q}")

    # Try POST-with-filter
    print("\nPART 2 — POST-style filter on /property/")
    print("-" * 80)
    post_variants = [
        ("/api/internal/property/", {"lists": [list_uuid]}),
        ("/api/internal/property/?limit=5", {"filter": {"lists": [list_uuid]}}),
        ("/api/internal/property/list/", {"list_uuid": list_uuid}),
    ]
    for path, body in post_variants:
        resp = client._request("POST", path, json=body)
        try:
            data = resp.json()
            preview = json.dumps(data)[:120]
        except Exception:
            preview = resp.text[:120]
        print(f"  {resp.status_code:3}  POST {path}  body={json.dumps(body)[:50]}  → {preview}")

    # Maybe property listing requires a "filter_id" approach where you
    # first create a filter then query against it. Try GET /api/internal/filter-preset/
    print("\nPART 3 — sample property in list directly")
    print("-" * 80)
    # Get details of our specific known list
    resp = client._request("GET", f"/api/internal/list/{list_uuid}/")
    print(f"  list detail: {resp.status_code} {resp.text[:300]}")

    # Maybe there's a count-by-list endpoint
    for path in [
        f"/api/internal/list/{list_uuid}/count/",
        f"/api/internal/list/{list_uuid}/properties/",
        f"/api/internal/list/{list_uuid}/property/",
        f"/api/internal/list/{list_uuid}/records/",
    ]:
        resp = client._request("GET", path)
        if resp.status_code != 404:
            print(f"  {resp.status_code:3}  GET {path}  → {resp.text[:120]}")


if __name__ == "__main__":
    sys.exit(main() or 0)

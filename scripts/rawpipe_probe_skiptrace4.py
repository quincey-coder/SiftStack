"""Round 4 — find property-listing endpoint and list-scoped skip-trace."""

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
    if not target:
        print("[FAIL] No RAWPIPE_e2e_* list.")
        return 1
    list_uuid = target["uuid"]
    print(f"List UUID: {list_uuid}\n")

    # 1. Find a way to list properties in a list
    print("=" * 80)
    print("PART 1 — finding the property-listing endpoint")
    print("=" * 80)
    listing_candidates = [
        ("GET",  "/api/internal/property/"),
        ("GET",  f"/api/internal/property/?lists={list_uuid}"),
        ("GET",  f"/api/internal/property/?list={list_uuid}"),
        ("GET",  "/api/internal/property/list/"),
        ("GET",  "/api/internal/properties/"),
        ("GET",  f"/api/internal/properties/?lists={list_uuid}"),
        ("GET",  f"/api/internal/list/{list_uuid}/"),
        ("GET",  f"/api/internal/list/{list_uuid}/properties/"),
        ("GET",  f"/api/internal/list/{list_uuid}/property/"),
        ("POST", "/api/internal/property/search/"),
        ("POST", "/api/internal/property/filter/"),
        ("POST", "/api/internal/properties/search/"),
    ]
    for method, path in listing_candidates:
        body = {"lists": [list_uuid]} if method == "POST" else None
        resp = client._request(method, path, json=body)
        if resp.status_code == 404:
            continue
        try:
            body_text = json.dumps(resp.json(), indent=None)[:150]
        except Exception:
            body_text = resp.text[:150]
        marker = "  ⭐" if 200 <= resp.status_code < 300 else "   "
        print(f"{marker} {method:5} {path:60} → {resp.status_code}  {body_text}")

    # 2. Probe list-scoped skip-trace
    print()
    print("=" * 80)
    print("PART 2 — list-scoped or job-style skip-trace URLs")
    print("=" * 80)
    skiptrace_variants = [
        ("POST", f"/api/internal/list/{list_uuid}/skip-trace/"),
        ("POST", f"/api/internal/list/{list_uuid}/skiptrace/"),
        ("POST", "/api/internal/list/skip-trace/"),
        ("POST", "/api/internal/list/skiptrace/"),
        ("POST", "/api/internal/skip-trace-job/"),
        ("POST", "/api/internal/skiptrace-job/"),
        ("POST", "/api/internal/skip-trace/jobs/"),
        ("POST", "/api/internal/skiptrace/jobs/"),
        ("GET",  "/api/internal/skip-trace-job/"),
        ("GET",  "/api/internal/skiptrace-job/"),
        ("POST", "/api/internal/account/subscription/skip-trace/"),
        ("POST", "/api/internal/subscription/skip-trace/"),
        ("POST", "/api/internal/property/skip-trace-subscription/"),
    ]
    for method, path in skiptrace_variants:
        body = {} if method == "POST" else None
        resp = client._request(method, path, json=body)
        if resp.status_code == 404:
            continue
        try:
            body_text = json.dumps(resp.json(), indent=None)[:150]
        except Exception:
            body_text = resp.text[:150]
        marker = "  ⭐" if 200 <= resp.status_code < 300 or resp.status_code == 400 else "   "
        print(f"{marker} {method:5} {path:60} → {resp.status_code}  {body_text}")

    # 3. Check subscription endpoint for skip-trace info
    print()
    print("=" * 80)
    print("PART 3 — what does subscription endpoint say?")
    print("=" * 80)
    resp = client._request("GET", "/api/internal/account/subscription/")
    if resp.status_code == 200:
        data = resp.json()
        # Filter to skip-trace related keys
        related = {k: v for k, v in data.items() if "skip" in k.lower() or "trace" in k.lower() or "balance" in k.lower() or "unlimited" in k.lower()}
        if related:
            print("Skip-trace-related subscription keys:")
            print(json.dumps(related, indent=2))
        else:
            print("Full subscription keys:")
            print(list(data.keys()))


if __name__ == "__main__":
    sys.exit(main() or 0)

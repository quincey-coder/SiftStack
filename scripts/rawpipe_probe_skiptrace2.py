"""Round 2 — POST candidate skip-trace endpoints with empty body.

DRF typically returns 400 with field-level validation errors when an
endpoint exists and a method is allowed but the body is invalid. That
error message will reveal what fields the endpoint actually wants.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datasift_api_client import DataSiftAPIClient

# URLs that 405'd on OPTIONS — meaning they exist
CANDIDATES = [
    "/api/internal/property/skip-trace/",
    "/api/internal/property/skiptrace/",
    "/api/internal/property/bulk-skip-trace/",
    "/api/internal/property/bulk_skiptrace/",
    "/api/internal/property/action/",
    "/api/internal/property/actions/",
    "/api/internal/property/skip/",
]


def main():
    client = DataSiftAPIClient.from_env()
    print("POST with empty body — looking for 400/422 validation errors\n")
    print(f"{'URL':55} {'STATUS':6}  RESPONSE BODY")
    print("-" * 120)
    for path in CANDIDATES:
        resp = client._request("POST", path, json={})
        body = resp.text[:300]
        try:
            body = json.dumps(resp.json(), indent=None)[:280]
        except Exception:
            pass
        print(f"{path:55} {resp.status_code:6}  {body}")


if __name__ == "__main__":
    main()

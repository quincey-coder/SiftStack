"""Probe likely skip-trace endpoint URLs.

Uses OPTIONS where supported (returns schema) and GET as a fallback
existence-check. 404 means the URL doesn't exist; 405 means it exists
but rejects that method. We're hunting for any 2xx or 405.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datasift_api_client import DataSiftAPIClient

CANDIDATES = [
    # Naming variations
    "/api/internal/skip-trace/",
    "/api/internal/skiptrace/",
    "/api/internal/skip_trace/",
    "/api/internal/skiptrace-batch/",
    "/api/internal/skip-trace-batch/",
    # Under property/properties
    "/api/internal/property/skip-trace/",
    "/api/internal/property/skiptrace/",
    "/api/internal/properties/skip-trace/",
    "/api/internal/properties/skiptrace/",
    # Job-style
    "/api/internal/skip-trace-job/",
    "/api/internal/skiptrace-job/",
    "/api/internal/job/skiptrace/",
    "/api/internal/jobs/skiptrace/",
    "/api/internal/jobs/skip-trace/",
    # Phone-related
    "/api/internal/phone-lookup/",
    "/api/internal/phone/lookup/",
    "/api/internal/phone-enrich/",
    "/api/internal/phones/skip-trace/",
    # Send-to vocabulary (UI says "Send To → Skip Trace")
    "/api/internal/send-to/skip-trace/",
    "/api/internal/sendto/skiptrace/",
    # Bulk action
    "/api/internal/bulk/skip-trace/",
    "/api/internal/bulk-skip-trace/",
    "/api/internal/bulk_action/skiptrace/",
    # Property batch action
    "/api/internal/property/bulk-skip-trace/",
    "/api/internal/property/bulk_skiptrace/",
    "/api/internal/property/action/",
    "/api/internal/property/actions/",
    # Tracer/trace endpoint
    "/api/internal/tracer/",
    "/api/internal/trace/",
    # Add-on / service style
    "/api/internal/service/skip-trace/",
    "/api/internal/services/skip-trace/",
    # Outreach / contact discovery
    "/api/internal/contact-discovery/",
    "/api/internal/discover/contacts/",
    # Look under the existing /property/ that we saw POSTs to
    "/api/internal/property/",
    "/api/internal/property/skip/",
]

def main():
    client = DataSiftAPIClient.from_env()
    print(f"Probing {len(CANDIDATES)} candidate URLs...\n")
    print(f"{'METHOD':8} {'URL':55} {'STATUS':6}  NOTE")
    print("-" * 95)

    for path in CANDIDATES:
        for method in ("OPTIONS", "GET", "POST"):
            try:
                if method == "POST":
                    resp = client._request("POST", path, json={})
                else:
                    resp = client._request(method, path)
                status = resp.status_code
                # 404 means nothing here; skip
                if status == 404:
                    continue
                note = ""
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        if "actions" in body:
                            note = f"OPTIONS-schema keys: {list(body.get('actions', {}).keys())}"
                        elif "detail" in body:
                            note = f"detail: {body['detail'][:60]}"
                        else:
                            note = f"keys: {list(body.keys())[:5]}"
                except Exception:
                    note = f"text: {resp.text[:80]}"
                print(f"{method:8} {path:55} {status:6}  {note}")
                # Stop trying more methods on this URL once we get a non-404
                # (avoid spamming when we found something)
                break
            except Exception as e:
                print(f"{method:8} {path:55} ERR    {e}")
                break

if __name__ == "__main__":
    main()

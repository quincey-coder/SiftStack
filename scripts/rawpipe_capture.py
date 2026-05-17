"""RAWPIPE Stage 2 — Network capture during a real upload.

Logs in via Playwright with network listeners attached at the
BrowserContext level, then drives the existing 7-step upload wizard
plus enrich + skip-trace flows against a TINY (≤5 records) throwaway
CSV. Every request and response touching `apiv2.reisift.io` (and a few
other hosts we care about, like S3) is recorded to JSONL.

The goal is to capture:
  1. The exact endpoint that returns a fresh `storage_key`.
  2. Where the file bytes actually go (S3 presigned PUT vs. multipart POST).
  3. The skip-trace endpoint (not findable via probe).
  4. Any post-upload enrich endpoint (separate from the upload commit's
     `data_enriching` field).

This script WRITES to the user's DataSift account:
  - Creates a new list (name: see TEST_LIST_NAME below).
  - Uploads ≤5 throwaway records.
  - Triggers enrich + skip-trace.

The test list name is fixed and easy to delete from the DataSift UI
afterward (Records → filter by list → Delete records).

Usage:
    PYTHONPATH=src python scripts/rawpipe_capture.py
"""

import asyncio
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datasift_core import create_browser, login
from datasift_uploader import upload_csv, enrich_records, skip_trace_records

# Test artifact knobs ---------------------------------------------------
TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
TEST_LIST_NAME = f"RAWPIPE_capture_{datetime.now().strftime('%Y%m%d_%H%M')}"
CAPTURE_DIR = ROOT / "output" / "datasift_capture" / f"capture-{TIMESTAMP}"
CSV_PATH = CAPTURE_DIR / "test_records.csv"
JSONL_PATH = CAPTURE_DIR / "traffic.jsonl"

# Cheap-mode toggles. Skip-trace is left off by default to avoid burning
# any rate-limited quota on the user's $97/mo unlimited plan and to keep
# the test record count truly throwaway.
RUN_ENRICH = True
RUN_SKIP_TRACE = False

# Hosts whose traffic we care about. Anything not matching here is dropped.
WATCHED_HOST_RX = re.compile(
    r"(apiv2\.reisift\.io|app\.reisift\.io|amazonaws\.com|reisift)",
    re.IGNORECASE,
)
MAX_BODY_BYTES = 16 * 1024  # truncate at 16 KB to keep JSONL human-readable


def write_test_csv(path: Path) -> None:
    """Generate a 5-record test CSV using DataSift's expected column names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        # Clearly fake but well-formed Texas addresses. The 78700 ZIPs are real
        # Austin ZIPs so address validation won't immediately reject them.
        {"Property Street Address": "100 RAWPIPE Test Ln", "Property City": "Austin",
         "Property State": "TX", "Property ZIP Code": "78701",
         "Owner First Name": "Test", "Owner Last Name": "AlphaRecord",
         "Tags": "RAWPIPE_test", "Lists": TEST_LIST_NAME, "Notice Type": "test", "County": "Travis"},
        {"Property Street Address": "200 RAWPIPE Test Ln", "Property City": "Austin",
         "Property State": "TX", "Property ZIP Code": "78702",
         "Owner First Name": "Test", "Owner Last Name": "BravoRecord",
         "Tags": "RAWPIPE_test", "Lists": TEST_LIST_NAME, "Notice Type": "test", "County": "Travis"},
        {"Property Street Address": "300 RAWPIPE Test Ln", "Property City": "Austin",
         "Property State": "TX", "Property ZIP Code": "78703",
         "Owner First Name": "Test", "Owner Last Name": "CharlieRecord",
         "Tags": "RAWPIPE_test", "Lists": TEST_LIST_NAME, "Notice Type": "test", "County": "Travis"},
        {"Property Street Address": "400 RAWPIPE Test Ln", "Property City": "Austin",
         "Property State": "TX", "Property ZIP Code": "78704",
         "Owner First Name": "Test", "Owner Last Name": "DeltaRecord",
         "Tags": "RAWPIPE_test", "Lists": TEST_LIST_NAME, "Notice Type": "test", "County": "Travis"},
        {"Property Street Address": "500 RAWPIPE Test Ln", "Property City": "Austin",
         "Property State": "TX", "Property ZIP Code": "78705",
         "Owner First Name": "Test", "Owner Last Name": "EchoRecord",
         "Tags": "RAWPIPE_test", "Lists": TEST_LIST_NAME, "Notice Type": "test", "County": "Travis"},
    ]
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"[INFO] Wrote test CSV → {path}")


def _truncate(text: str | None) -> str | None:
    if text is None:
        return None
    if len(text) <= MAX_BODY_BYTES:
        return text
    return text[:MAX_BODY_BYTES] + f"...[truncated, {len(text)-MAX_BODY_BYTES} more bytes]"


async def _serialize_request(request) -> dict:
    """Capture a Playwright Request object into a serializable dict."""
    post_data = None
    try:
        post_data = request.post_data
    except Exception:
        pass
    return {
        "kind": "request",
        "ts": datetime.now(timezone.utc).isoformat(),
        "method": request.method,
        "url": request.url,
        "resource_type": request.resource_type,
        "headers": dict(request.headers),
        "post_data": _truncate(post_data),
    }


async def _serialize_response(response) -> dict:
    """Capture a Playwright Response object."""
    body_text = None
    body_error = None
    try:
        # Skip binary-looking responses to avoid GBs of image data
        ctype = response.headers.get("content-type", "")
        if (
            "json" in ctype
            or "text" in ctype
            or "javascript" in ctype
            or ctype == ""
        ):
            raw = await response.text()
            body_text = _truncate(raw)
        else:
            body_text = f"[skipped, content-type={ctype}]"
    except Exception as e:
        body_error = str(e)
    return {
        "kind": "response",
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": response.status,
        "url": response.url,
        "headers": dict(response.headers),
        "body": body_text,
        "body_error": body_error,
    }


async def main() -> int:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    write_test_csv(CSV_PATH)

    # Open the JSONL eagerly so writes are flushed even if upload crashes.
    jsonl = JSONL_PATH.open("w", encoding="utf-8")

    request_count = 0
    response_count = 0
    api_call_count = 0

    def on_request(request):
        nonlocal request_count, api_call_count
        if not WATCHED_HOST_RX.search(request.url):
            return
        request_count += 1
        if "apiv2.reisift.io" in request.url:
            api_call_count += 1
        # Schedule async serialization without blocking
        asyncio.create_task(_dump(_serialize_request(request)))

    def on_response(response):
        nonlocal response_count
        if not WATCHED_HOST_RX.search(response.url):
            return
        response_count += 1
        asyncio.create_task(_dump(_serialize_response(response)))

    async def _dump(coro_or_dict):
        rec = await coro_or_dict if asyncio.iscoroutine(coro_or_dict) else coro_or_dict
        jsonl.write(json.dumps(rec, default=str) + "\n")
        jsonl.flush()

    try:
        async with create_browser(headless=False) as (_browser, context, page):
            # Attach listeners BEFORE login so we capture login traffic too.
            context.on("request", on_request)
            context.on("response", on_response)

            print(f"[STEP A] Logging in…")
            ok = await login(page)
            if not ok:
                print("[FAIL] Login failed.")
                return 1

            # Re-snag rs_token now that we're logged in — useful evidence.
            token = await page.evaluate("() => localStorage.getItem('rs_token')")
            (CAPTURE_DIR / "rs_token.txt").write_text(token + "\n")

            print(f"[STEP B] Uploading 5-record CSV as list '{TEST_LIST_NAME}'…")
            print(f"         (driving the existing 7-step Playwright wizard)")
            up_result = await upload_csv(page, CSV_PATH, mode="add", existing_list=False)
            print(f"         → upload result: {up_result}")
            (CAPTURE_DIR / "upload_result.json").write_text(
                json.dumps(up_result, indent=2, default=str)
            )

            if not up_result.get("success"):
                print("[WARN] Upload did not report success — capture may be incomplete.")
                print("       Continuing anyway since partial traffic is still useful.")

            # Give DataSift a moment to finish background processing before
            # enrich/skip-trace, which depend on records existing.
            await page.wait_for_timeout(8000)

            print(f"[STEP C] Triggering enrich for list '{TEST_LIST_NAME}'…")
            try:
                enrich_result = await enrich_records(page, TEST_LIST_NAME)
                print(f"         → enrich result: {enrich_result}")
                (CAPTURE_DIR / "enrich_result.json").write_text(
                    json.dumps(enrich_result, indent=2, default=str)
                )
            except Exception as e:
                print(f"[WARN] Enrich step crashed: {e}")
                (CAPTURE_DIR / "enrich_result.json").write_text(
                    json.dumps({"error": str(e)}, indent=2)
                )

            print(f"[STEP D] Triggering skip-trace for list '{TEST_LIST_NAME}'…")
            try:
                st_result = await skip_trace_records(page, TEST_LIST_NAME)
                print(f"         → skip-trace result: {st_result}")
                (CAPTURE_DIR / "skip_trace_result.json").write_text(
                    json.dumps(st_result, indent=2, default=str)
                )
            except Exception as e:
                print(f"[WARN] Skip-trace step crashed: {e}")
                (CAPTURE_DIR / "skip_trace_result.json").write_text(
                    json.dumps({"error": str(e)}, indent=2)
                )

            # Drain any in-flight callbacks before browser closes.
            await page.wait_for_timeout(3000)
    finally:
        # Let in-flight tasks finish writing.
        await asyncio.sleep(1)
        jsonl.close()

    print()
    print(f"[DONE] Capture directory: {CAPTURE_DIR}")
    print(f"       Requests captured:  {request_count}")
    print(f"       Responses captured: {response_count}")
    print(f"       apiv2.reisift.io calls: {api_call_count}")
    print()
    print(f"[NEXT] Inspect with: jq 'select(.url|test(\"apiv2\"))' {JSONL_PATH}")
    print(f"[NEXT] Test list to delete in DataSift UI: '{TEST_LIST_NAME}'")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

"""RAWPIPE follow-up — capture the skip-trace endpoint.

Targets the existing `RAWPIPE_e2e_...` list (already has 5 records from
the e2e test). Drives the existing Playwright skip-trace flow with
network listeners attached, then dumps captured apiv2.reisift.io traffic.

This burns 5 skip-trace credits on the user's unlimited $97/mo plan.
On unlimited that's $0 incremental cost.

Usage:
    PYTHONPATH=src python scripts/rawpipe_capture_skiptrace.py [list_name]

If list_name is omitted, the script picks the most recent
`RAWPIPE_e2e_*` list from the user's account via API.
"""

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from datasift_api_client import DataSiftAPIClient
from datasift_core import create_browser, login
from datasift_uploader import skip_trace_records

TIMESTAMP = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
CAPTURE_DIR = ROOT / "output" / "datasift_capture" / f"skiptrace-{TIMESTAMP}"
JSONL_PATH = CAPTURE_DIR / "traffic.jsonl"

WATCHED_HOST_RX = re.compile(
    r"(apiv2\.reisift\.io|amazonaws\.com)",
    re.IGNORECASE,
)
MAX_BODY_BYTES = 16 * 1024


def pick_target_list(override: str | None) -> str:
    """Pick the RAWPIPE test list to skip-trace against."""
    if override:
        return override
    # Find the most recent RAWPIPE_e2e_* list via API (read-only).
    client = DataSiftAPIClient.from_env()
    lists = client.list_lists(limit=200)
    candidates = [l for l in lists if l.get("title", "").startswith("RAWPIPE_")]
    if not candidates:
        raise RuntimeError(
            "No RAWPIPE_* test lists found. Pass list_name explicitly or run "
            "scripts/rawpipe_e2e_test.py first."
        )
    # Newest by title (timestamp suffix sorts lexically)
    candidates.sort(key=lambda l: l.get("title", ""), reverse=True)
    return candidates[0]["title"]


def _truncate(text):
    if text is None:
        return None
    if len(text) <= MAX_BODY_BYTES:
        return text
    return text[:MAX_BODY_BYTES] + f"...[truncated {len(text)-MAX_BODY_BYTES}B]"


async def _ser_req(request):
    try:
        body = request.post_data
    except Exception:
        body = None
    return {
        "kind": "request",
        "ts": datetime.now(timezone.utc).isoformat(),
        "method": request.method,
        "url": request.url,
        "headers": dict(request.headers),
        "post_data": _truncate(body),
    }


async def _ser_resp(response):
    body = None
    try:
        ctype = response.headers.get("content-type", "")
        if "json" in ctype or "text" in ctype or ctype == "":
            body = _truncate(await response.text())
    except Exception as e:
        body = f"[body read failed: {e}]"
    return {
        "kind": "response",
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": response.status,
        "url": response.url,
        "headers": dict(response.headers),
        "body": body,
    }


async def main(target_list: str) -> int:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = JSONL_PATH.open("w", encoding="utf-8")

    request_count = 0
    response_count = 0
    api_count = 0

    def on_request(req):
        nonlocal request_count, api_count
        if not WATCHED_HOST_RX.search(req.url):
            return
        request_count += 1
        if "apiv2.reisift.io" in req.url:
            api_count += 1
        asyncio.create_task(_dump_async(_ser_req(req)))

    def on_response(resp):
        nonlocal response_count
        if not WATCHED_HOST_RX.search(resp.url):
            return
        response_count += 1
        asyncio.create_task(_dump_async(_ser_resp(resp)))

    async def _dump_async(coro):
        rec = await coro
        jsonl.write(json.dumps(rec, default=str) + "\n")
        jsonl.flush()

    try:
        async with create_browser(headless=False) as (_b, context, page):
            context.on("request", on_request)
            context.on("response", on_response)

            print(f"[STEP A] Logging in…")
            if not await login(page):
                print("[FAIL] Login failed.")
                return 1

            print(f"[STEP B] Triggering skip-trace against list {target_list!r}…")
            try:
                result = await skip_trace_records(page, target_list)
                print(f"         → result: {result}")
                (CAPTURE_DIR / "skip_trace_result.json").write_text(
                    json.dumps(result, indent=2, default=str)
                )
            except Exception as e:
                print(f"[WARN] Playwright skip-trace path crashed: {e}")
                print("       Traffic still captured up to crash point.")

            # Drain pending callbacks
            await page.wait_for_timeout(4000)
    finally:
        await asyncio.sleep(1)
        jsonl.close()

    print()
    print(f"[DONE] Captured {request_count} requests / {response_count} responses")
    print(f"       apiv2.reisift.io calls: {api_count}")
    print(f"       JSONL: {JSONL_PATH}")
    return 0


if __name__ == "__main__":
    override = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        target = pick_target_list(override)
    except Exception as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[INFO] Target list: {target!r}")
    sys.exit(asyncio.run(main(target)))

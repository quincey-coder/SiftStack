"""RAWPIPE Stage 1.5 — READ-ONLY API probe.

Stage 1 confirmed plain Python requests can authenticate against
apiv2.reisift.io with `Bearer <rs_token>` + `X-REISIFT-UI-VERSION`.
This script extends that by hitting every documented and inferred
read-only endpoint to harvest as much schema information as possible
BEFORE attempting any real write.

What it calls (all read-only):
  GET    /api/internal/list/?limit=100
  GET    /api/internal/tag/?limit=100
  GET    /api/internal/custom-fields/group/?limit=100
  GET    /api/internal/custom-fields/?limit=100
  OPTIONS /api/internal/upload/                  (full schema per ref doc)
  POST   /api/internal/upload/required-fields/   (idempotent — just returns mapping skeleton)
         × upload_type: new_properties, new_owners, add_owners_to_properties, sift_map

  Also tries a handful of inferred endpoints that, if they exist, will
  help us avoid a full UI-driven upload:
    GET /api/internal/me/
    GET /api/internal/user/
    GET /api/internal/account/
    GET /api/internal/upload/             (history?)

All requests AND responses are written verbatim to
output/datasift_capture/probe-{ts}/responses.jsonl
so we can inspect every field DataSift returns.

Token source: output/datasift_capture/rs_token.txt (written by Stage 1).
If missing, run scripts/rawpipe_token_smoke.py first.

This script makes ZERO writes to the user's DataSift account.
The /upload/required-fields/ endpoint is a POST but is idempotent —
it computes a mapping skeleton based on upload_type and returns it.
"""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILE = ROOT / "output" / "datasift_capture" / "rs_token.txt"
OUT_DIR = ROOT / "output" / "datasift_capture" / f"probe-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

API_BASE = "https://apiv2.reisift.io"
UI_VERSION = "2022.02.01.7"


def load_token() -> str:
    if not TOKEN_FILE.exists():
        print(
            f"[FAIL] No token file at {TOKEN_FILE}.\n"
            f"       Run scripts/rawpipe_token_smoke.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def make_session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
        "X-REISIFT-UI-VERSION": UI_VERSION,
    })
    return s


def call(session: requests.Session, method: str, path: str,
         json_body: dict | None = None) -> dict:
    """Execute one HTTP call. Returns a dict suitable for logging."""
    url = f"{API_BASE}{path}"
    started = time.monotonic()
    try:
        resp = session.request(method, url, json=json_body, timeout=20)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            parsed_body = resp.json()
        except ValueError:
            parsed_body = None
        return {
            "method": method,
            "url": url,
            "request_body": json_body,
            "status": resp.status_code,
            "elapsed_ms": elapsed_ms,
            "response_headers": dict(resp.headers),
            "response_body": parsed_body,
            "response_text_preview": resp.text[:300] if parsed_body is None else None,
        }
    except requests.RequestException as e:
        return {
            "method": method,
            "url": url,
            "request_body": json_body,
            "error": str(e),
        }


def summarize(result: dict) -> str:
    """One-line console summary."""
    method = result.get("method", "?")
    url = result.get("url", "?").replace(API_BASE, "")
    if "error" in result:
        return f"  [!!]  {method:7} {url:60} NETWORK ERROR: {result['error']}"
    status = result.get("status")
    body = result.get("response_body")
    extra = ""
    if isinstance(body, dict):
        if "count" in body:
            extra = f"count={body['count']}"
        elif "results" in body and isinstance(body["results"], list):
            extra = f"results[{len(body['results'])}]"
        elif "actions" in body:
            extra = f"OPTIONS keys: {sorted(body.keys())[:6]}"
        else:
            keys = sorted(body.keys())[:5]
            extra = f"keys={keys}"
    elif isinstance(body, list):
        extra = f"list[{len(body)}]"
    return f"  [{status}] {method:7} {url:60} {extra}"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    token = load_token()
    session = make_session(token)

    probes: list[tuple[str, str, dict | None]] = [
        # Documented endpoints
        ("GET",    "/api/internal/list/?limit=100", None),
        ("GET",    "/api/internal/tag/?limit=100", None),
        ("GET",    "/api/internal/custom-fields/group/?limit=100", None),
        # Skill uses plural — ref doc says singular. Try both.
        ("GET",    "/api/internal/custom-fields/groups/?limit=100", None),
        ("GET",    "/api/internal/custom-fields/?limit=100", None),
        ("OPTIONS","/api/internal/upload/", None),
        ("POST",   "/api/internal/upload/required-fields/", {"upload_type": "new_properties"}),
        ("POST",   "/api/internal/upload/required-fields/", {"upload_type": "new_owners"}),
        ("POST",   "/api/internal/upload/required-fields/", {"upload_type": "add_owners_to_properties"}),
        ("POST",   "/api/internal/upload/required-fields/", {"upload_type": "sift_map"}),
        ("POST",   "/api/internal/upload/required-fields/", {"upload_type": "add_properties_to_owners"}),

        # Inferred endpoints — probe for existence (404 is fine, 200 is a gift)
        ("GET",    "/api/internal/me/", None),
        ("GET",    "/api/internal/user/", None),
        ("GET",    "/api/internal/account/", None),
        ("GET",    "/api/internal/users/me/", None),
        ("GET",    "/api/internal/upload/", None),
        ("GET",    "/api/internal/upload/recent/", None),
        ("GET",    "/api/internal/upload/history/", None),
        ("GET",    "/api/internal/skip-trace/", None),
        ("GET",    "/api/internal/skiptrace/", None),
        ("GET",    "/api/internal/enrich/", None),
        ("GET",    "/api/internal/data-enrich/", None),
        ("GET",    "/api/internal/storage/", None),
        ("GET",    "/api/internal/storage/sign/", None),
        ("GET",    "/api/internal/filter/?limit=20", None),
        ("GET",    "/api/internal/filters/?limit=20", None),
    ]

    print(f"[INFO] Running {len(probes)} probes against {API_BASE}")
    print(f"[INFO] Output dir: {OUT_DIR}")
    print()

    results = []
    with (OUT_DIR / "responses.jsonl").open("w", encoding="utf-8") as f:
        for method, path, body in probes:
            r = call(session, method, path, body)
            f.write(json.dumps(r, default=str) + "\n")
            f.flush()
            results.append(r)
            print(summarize(r))
            time.sleep(0.15)  # courtesy delay (skill uses same)

    print()
    print("[INFO] Writing summary…")

    # Quick analysis
    found_endpoints = [r for r in results if r.get("status") and 200 <= r["status"] < 300]
    not_found = [r for r in results if r.get("status") == 404]
    forbidden = [r for r in results if r.get("status") in (401, 403)]

    print(f"\n  Discovered endpoints (2xx):   {len(found_endpoints)}")
    print(f"  Confirmed not-existent (404): {len(not_found)}")
    print(f"  Auth/permission failures (401/403): {len(forbidden)}")

    # Spotlight the OPTIONS schema — most likely to contain storage_key clues
    options_call = next(
        (r for r in results
         if r.get("method") == "OPTIONS" and r.get("status") in (200, 201)),
        None,
    )
    if options_call:
        opts_path = OUT_DIR / "upload_options_schema.json"
        opts_path.write_text(
            json.dumps(options_call["response_body"], indent=2),
            encoding="utf-8",
        )
        print(f"\n  Full OPTIONS schema saved → {opts_path}")

    print(f"\n  Full responses → {OUT_DIR}/responses.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""RAWPIPE Stage 1 — token smoke test.

Logs in to app.reisift.io via Playwright, extracts `rs_token` from the
browser's localStorage, then makes a single READ-ONLY `requests.get()` to
`https://apiv2.reisift.io/api/internal/list/` to confirm:

  1. The token is real and present after login.
  2. A plain Python HTTP client (no browser fingerprint) can authenticate
     with just `Authorization: Bearer <token>` + `X-REISIFT-UI-VERSION`.

This script does NOT write anything to the user's DataSift account.

Exit codes:
  0 — auth works (200 from /list/). Proceed to Stage 2.
  1 — token missing (not logged in correctly).
  2 — token present but API rejected it (401/403). Stop and reassess.
  3 — unexpected error (network, etc.).

Usage:
    PYTHONPATH=src python scripts/rawpipe_token_smoke.py
"""

import asyncio
import json
import sys
from pathlib import Path

# Make src/ importable without requiring PYTHONPATH at every call site.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import requests

from datasift_core import (
    DATASIFT_DASHBOARD_URL,
    create_browser,
    login,
)

API_BASE = "https://apiv2.reisift.io"
UI_VERSION = "2022.02.01.7"
TOKEN_OUT = ROOT / "output" / "datasift_capture" / "rs_token.txt"


async def grab_token() -> str | None:
    """Log in via Playwright and read rs_token from localStorage."""
    async with create_browser(headless=False) as (_browser, _ctx, page):
        ok = await login(page)
        if not ok:
            print("[FAIL] Playwright login returned False.", file=sys.stderr)
            return None

        # Make sure we're somewhere the SPA has had a chance to set localStorage.
        # login() lands on /dashboard/general; that's already inside the SPA, so
        # localStorage should already be populated. We re-navigate just to be sure.
        await page.goto(DATASIFT_DASHBOARD_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        token = await page.evaluate("() => localStorage.getItem('rs_token')")
        # Also dump everything in localStorage in case the key was renamed.
        ls_snapshot = await page.evaluate(
            "() => Object.fromEntries(Object.entries(localStorage))"
        )
        print(f"[INFO] localStorage keys: {sorted(ls_snapshot.keys())}")
        return token


def call_list_endpoint(token: str) -> tuple[int, str]:
    """READ-ONLY GET /api/internal/list/?limit=5. Returns (status, body_preview)."""
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
        "X-REISIFT-UI-VERSION": UI_VERSION,
    }
    resp = requests.get(
        f"{API_BASE}/api/internal/list/?ordering=title&offset=0&limit=5",
        headers=headers,
        timeout=15,
    )
    body = resp.text[:600]
    return resp.status_code, body


def main() -> int:
    TOKEN_OUT.parent.mkdir(parents=True, exist_ok=True)

    print("[STEP 1] Logging in via Playwright to fetch rs_token…")
    try:
        token = asyncio.run(grab_token())
    except Exception as e:
        print(f"[FAIL] Login crashed: {e}", file=sys.stderr)
        return 3

    if not token:
        print(
            "[FAIL] rs_token not found in localStorage.\n"
            "       Either login failed silently or DataSift renamed the key.\n"
            "       Check the printed localStorage keys above for candidates.",
            file=sys.stderr,
        )
        return 1

    # Persist for Stage 2 reuse. Token is short (~hundreds of bytes).
    TOKEN_OUT.write_text(token + "\n", encoding="utf-8")
    print(f"[OK]   rs_token captured ({len(token)} chars) → {TOKEN_OUT}")

    print("[STEP 2] Calling GET /api/internal/list/?limit=5 with the token…")
    try:
        status, body = call_list_endpoint(token)
    except requests.RequestException as e:
        print(f"[FAIL] Network error hitting API: {e}", file=sys.stderr)
        return 3

    print(f"[INFO] HTTP {status}")
    print(f"[INFO] Body preview (first 600 chars):\n{body}\n")

    if status == 200:
        # Try parsing — if it's a real list-of-lists response we're golden.
        try:
            data = json.loads(body) if len(body) < 600 else None
            if isinstance(data, dict) and ("results" in data or "data" in data):
                items = data.get("results") or data.get("data") or []
                print(f"[OK]   API returned {len(items)} list(s).")
            elif isinstance(data, list):
                print(f"[OK]   API returned top-level array with {len(data)} item(s).")
        except json.JSONDecodeError:
            pass
        print(
            "\n[SUCCESS] Stage 1 PASSED. Auth model works from plain requests.\n"
            "          Safe to proceed to the full capture script."
        )
        return 0

    if status == 401:
        print(
            "[FAIL] 401 Unauthorized. Token rejected by API.\n"
            "       Likely causes: token expired between Playwright session and\n"
            "       this call, or token is bound to a CSRF/cookie we didn't send.",
            file=sys.stderr,
        )
        return 2

    if status == 403:
        print(
            "[FAIL] 403 Forbidden. Token accepted but request rejected.\n"
            "       Most likely the X-REISIFT-UI-VERSION header value is stale\n"
            "       (current code uses 2022.02.01.7). Inspect the body above\n"
            "       for the server's complaint.",
            file=sys.stderr,
        )
        return 2

    print(f"[FAIL] Unexpected status {status}.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())

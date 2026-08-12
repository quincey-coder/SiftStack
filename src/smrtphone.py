"""SmrtPhone dialer integration — session auth, call log, and recording download.

This is the foundation for call coaching: pull the real call log and the MP3s, so
transcripts can be graded against the cold-call / lead-manager / closer rubrics we
already ship as skills.

**Includes the login helper upstream does not have.** Upstream's ``pull_calls.py``
reads a ``smrtphone_state.json`` produced by ``_api/smrtphone_login.py``, which
lives in a *separate project* on that machine and is NOT in the repository — so
the upstream call-coaching stack cannot authenticate on a fresh checkout at all.
``login()`` here closes that gap: a headed browser, a human logs in (MFA
included), and the storage state is saved the moment the dialer app loads.

Auth model (verified from the upstream client, live against tenant 42564 on
2026-07-06): the call log is a DataTables server-side endpoint,
``POST /logs/calls/filtered``, authenticated purely by session cookie. Recording
URLs come back inside the rows and are then fetchable WITHOUT auth, which is why
downloads work from plain HTTP once the log is in hand.

CLI:
    python src/smrtphone.py login                      # headed, saves the session
    python src/smrtphone.py log --days 14              # pull the call log (no downloads)
    python src/smrtphone.py pull --min-seconds 60      # download qualifying recordings
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config

logger = logging.getLogger(__name__)

BASE = "https://phone.smrt.studio"
LOGIN_URL = f"{BASE}/login"
CALLS_URL = f"{BASE}/logs/calls"
FILTERED_ENDPOINT = f"{BASE}/logs/calls/filtered"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")
PAGE_SIZE = 200
REQUEST_TIMEOUT = 60

STATE_FILE = config.PROJECT_ROOT / "smrtphone_state.json"
OUT_DIR = config.OUTPUT_DIR / "call_coaching"
REC_DIR = OUT_DIR / "recordings"

# DataTables column order — the endpoint requires the full column spec echoed
# back or it rejects the request.
COLUMNS = ["id", "user", "user_id", "created_at", "direction", "status",
           "disposition", "from_num", "to_num", "price", "duration",
           "podio_id", "recording_sid", "sid", "call_agent_id"]


class SmrtPhoneAuthError(RuntimeError):
    """Session missing or expired. Never silently returns an empty call log."""


# ── Session ───────────────────────────────────────────────────────────

async def login(headless: bool = False, timeout_seconds: int = 300) -> Path:
    """Open a headed browser, wait for a human login, save the session state.

    This is the piece upstream is missing. It waits for the dialer app itself to
    load rather than for a fixed URL, so MFA / verification interstitials do not
    trip it.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(user_agent=UA)
        page = await context.new_page()
        await page.goto(LOGIN_URL, wait_until="domcontentloaded")

        print(f"\n>>> Log in to SmrtPhone in the browser window "
              f"(account: {getattr(config, 'SMRTPHONE_EMAIL', '') or 'your dialer login'}).")
        print(">>> Complete any verification. The session saves automatically.\n")

        deadline = timeout_seconds * 1000
        try:
            # Any authenticated dialer route is proof enough; do not pin one URL.
            await page.wait_for_url(lambda u: "/login" not in u and "smrt.studio" in u,
                                    timeout=deadline)
            await page.wait_for_timeout(2000)
        except Exception as exc:
            await browser.close()
            raise SmrtPhoneAuthError(
                f"Did not reach an authenticated page within {timeout_seconds}s: {exc}")

        state = await context.storage_state()
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        await browser.close()

    cookies = [c for c in state.get("cookies", []) if "smrt.studio" in c.get("domain", "")]
    logger.info("SmrtPhone session saved to %s (%d cookie(s))", STATE_FILE, len(cookies))
    if not cookies:
        raise SmrtPhoneAuthError(
            "Logged in but no smrt.studio cookie was captured — the session file is useless")
    return STATE_FILE


def cookie_header() -> str:
    """Build the Cookie header from the saved session, or raise."""
    if not STATE_FILE.exists():
        raise SmrtPhoneAuthError(
            f"No SmrtPhone session at {STATE_FILE}. Run: python src/smrtphone.py login")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    jar = "; ".join(f"{c['name']}={c['value']}" for c in state.get("cookies", [])
                    if "smrt.studio" in c.get("domain", ""))
    if not jar:
        raise SmrtPhoneAuthError(
            f"{STATE_FILE} holds no smrt.studio cookies. Re-run: python src/smrtphone.py login")
    return jar


# ── Call log ──────────────────────────────────────────────────────────

def _page_form(start: int, length: int) -> dict:
    form = {"draw": "1", "start": str(start), "length": str(length),
            "order[0][column]": "3", "order[0][dir]": "desc",
            "search[value]": "", "search[regex]": "false"}
    for i, col in enumerate(COLUMNS):
        form[f"columns[{i}][data]"] = col
        form[f"columns[{i}][name]"] = col
        form[f"columns[{i}][searchable]"] = "true"
        form[f"columns[{i}][orderable]"] = "true"
        form[f"columns[{i}][search][value]"] = ""
        form[f"columns[{i}][search][regex]"] = "false"
    return form


def _fetch_page(cookie: str, start: int, length: int) -> dict:
    data = urllib.parse.urlencode(_page_form(start, length)).encode()
    req = urllib.request.Request(FILTERED_ENDPOINT, data=data, method="POST", headers={
        "cookie": cookie, "user-agent": UA,
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "x-requested-with": "XMLHttpRequest", "accept": "application/json, */*",
        "origin": BASE, "referer": CALLS_URL,
    })
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        body = resp.read().decode("utf-8", "replace")
    # An expired session returns the LOGIN PAGE with HTTP 200. Parsing that as an
    # empty call log would report "no calls" on a busy day — fail loudly instead.
    if body.lstrip().startswith("<"):
        raise SmrtPhoneAuthError(
            "SmrtPhone returned HTML, not JSON — the session has expired. "
            "Re-run: python src/smrtphone.py login")
    return json.loads(body)


def normalize(row: dict) -> dict:
    """Flatten one DataTables row into a stable record."""
    rec = row.get("recording_sid") or {}
    created = (row.get("created_at") or {}).get("date", "")
    view_route = rec.get("viewRoute") if isinstance(rec, dict) else None
    return {
        "call_id": row.get("id"),
        "created_at_utc": created[:19],
        "caller": (row.get("user") or {}).get("name"),
        "direction": row.get("direction"),
        "status": row.get("status"),
        "disposition": row.get("disposition"),
        "contact_name": (row.get("from_num") or {}).get("contactName")
                        or (row.get("to_num") or {}).get("contactName"),
        "from_num": (row.get("from_num") or {}).get("fromNum"),
        "to_num": (row.get("to_num") or {}).get("toNum"),
        "duration_seconds": row.get("duration") or 0,
        # SmrtPhone stores the CRM record link in the legacy 'podio_id' field.
        "crm_record_url": row.get("podio_id"),
        "recording_url": rec.get("hasRec") if isinstance(rec, dict) else None,
        "call_detail_url": BASE + view_route if view_route else None,
    }


def pull_log(days: int | None = None, max_pages: int = 50) -> list[dict]:
    """Pull the call log, newest first, optionally limited to the last N days."""
    cookie = cookie_header()
    cutoff = None
    if days:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

    calls: list[dict] = []
    start, total, pages = 0, None, 0
    while pages < max_pages:
        page = _fetch_page(cookie, start, PAGE_SIZE)
        rows = page.get("data") or []
        if total is None:
            total = page.get("recordsTotal") or page.get("recordsFiltered") or 0
            logger.info("SmrtPhone call log: %s total rows", f"{total:,}")
        if not rows:
            break
        stop = False
        for row in rows:
            rec = normalize(row)
            if cutoff and rec["created_at_utc"] and rec["created_at_utc"] < cutoff:
                stop = True
                break
            calls.append(rec)
        if stop:
            break
        start += PAGE_SIZE
        pages += 1
        if total and start >= total:
            break

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "call_log.json").write_text(json.dumps(calls, indent=2), encoding="utf-8")
    logger.info("Pulled %d call(s)%s -> %s",
                len(calls), f" from the last {days} day(s)" if days else "",
                OUT_DIR / "call_log.json")
    return calls


def select_for_review(calls: list[dict], min_seconds: int = 60,
                      max_calls: int = 0) -> list[dict]:
    """Calls worth grading: long enough to contain a conversation, and recorded."""
    picked = [c for c in calls
              if (c.get("duration_seconds") or 0) >= min_seconds and c.get("recording_url")]
    picked.sort(key=lambda c: c.get("created_at_utc") or "", reverse=True)
    if max_calls:
        picked = picked[:max_calls]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "calls_to_review.json").write_text(json.dumps(picked, indent=2), encoding="utf-8")
    logger.info("%d call(s) qualify for review (>= %ds with a recording)", len(picked), min_seconds)
    return picked


def download_recordings(calls: list[dict], overwrite: bool = False) -> list[Path]:
    """Download each call's MP3. Recording URLs need no auth once known."""
    REC_DIR.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for call in calls:
        url = call.get("recording_url")
        if not url:
            continue
        dest = REC_DIR / f"{call['call_id']}.mp3"
        if dest.exists() and not overwrite:
            saved.append(dest)
            continue
        try:
            req = urllib.request.Request(url, headers={"user-agent": UA})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                dest.write_bytes(resp.read())
            saved.append(dest)
        except (urllib.error.URLError, OSError) as exc:
            # One bad recording must never abort the batch.
            logger.warning("Recording download failed for call %s: %s", call.get("call_id"), exc)
    logger.info("Downloaded %d/%d recording(s) -> %s", len(saved), len(calls), REC_DIR)
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("cmd", choices=["login", "log", "pull"])
    parser.add_argument("--days", type=int, help="only calls from the last N days")
    parser.add_argument("--min-seconds", type=int, default=60)
    parser.add_argument("--max-calls", type=int, default=0)
    parser.add_argument("--headless", action="store_true", help="login: no visible browser (not recommended)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")

    if args.cmd == "login":
        import asyncio
        asyncio.run(login(headless=args.headless))
        return 0

    calls = pull_log(days=args.days)
    if args.cmd == "log":
        print(json.dumps(calls[:10], indent=2))
        print(f"\n{len(calls)} call(s). Full log: {OUT_DIR / 'call_log.json'}")
        return 0

    picked = select_for_review(calls, args.min_seconds, args.max_calls)
    download_recordings(picked)
    return 0


if __name__ == "__main__":
    sys.exit(main())

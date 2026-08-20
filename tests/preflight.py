"""SiftStack preflight check — verify every integration without running the pipeline.

Tests each external service with the smallest possible API call (no scraping,
no skip-tracing, no uploads beyond a single test file). Costs ~$0.

Run:  ./venv/bin/python preflight.py
"""

import base64
import json
import os
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

# Pin to the repo-root .env regardless of the CWD this is launched from.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Color output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RESET = "\033[0m"
BOLD = "\033[1m"

results: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    """Run a single check, capture pass/fail + message."""
    print(f"  Testing {name}...", end=" ", flush=True)
    try:
        msg = fn()
        results.append((name, True, msg or "ok"))
        print(f"{GREEN}PASS{RESET}  {msg or ''}")
    except Exception as e:
        msg = str(e)[:120]
        results.append((name, False, msg))
        print(f"{RED}FAIL{RESET}  {msg}")


def env(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        raise RuntimeError(f"{key} not set in .env")
    return val


# ── ENV PRESENCE ─────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}1. Environment variables{RESET}")
# No login credentials needed — all TX sources are public
optional = [
    "DATASIFT_EMAIL", "DATASIFT_PASSWORD",
    "SMARTY_AUTH_ID", "SMARTY_AUTH_TOKEN",
    "OPENWEBNINJA_API_KEY", "ANTHROPIC_API_KEY",
    "SERPER_API_KEY", "FIRECRAWL_API_KEY",
    "DIRECTSKIP_API_KEY", "TRESTLE_API_KEY",
    "SLACK_WEBHOOK_URL",
    "DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN",
    "GOOGLE_DRIVE_FOLDER_ID", "GOOGLE_SERVICE_ACCOUNT_KEY",
]
for key in optional:
    val = os.getenv(key, "").strip()
    if val:
        print(f"  {GREEN}set{RESET}     {key}")
    else:
        print(f"  {YELLOW}MISSING{RESET} {key} (optional)")

# ── PYTHON IMPORTS ───────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}2. Python module imports{RESET}")

def test_imports():
    # src/ modules use flat imports — must add src/ to sys.path.
    # This file lives in tests/, so src/ is a SIBLING of our parent.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    import config  # noqa
    import notice_parser  # noqa
    import enrichment_pipeline  # noqa
    import datasift_uploader  # noqa
    import dropbox_watcher  # noqa
    import drive_uploader  # noqa
    return "all core modules importable"

check("core module imports", test_imports)


# ── SMARTY ──────────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}3. Smarty (address standardization){RESET}")

def test_smarty():
    import urllib.request
    auth_id = env("SMARTY_AUTH_ID")
    auth_token = env("SMARTY_AUTH_TOKEN")
    url = (
        f"https://us-street.api.smarty.com/street-address?"
        f"auth-id={auth_id}&auth-token={auth_token}"
        f"&street=1600+Pennsylvania+Ave&city=Washington&state=DC&candidates=1"
    )
    req = urllib.request.Request(url, headers={"Host": "us-street.api.smarty.com"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    if not data:
        raise RuntimeError("empty response (auth ok, no match)")
    return f"resolved: {data[0].get('delivery_line_1', '?')}"

check("Smarty address lookup", test_smarty)


# ── OPENWEB NINJA (Zillow) ──────────────────────────────────────────
print(f"\n{BOLD}{CYAN}4. OpenWeb Ninja (Zillow data){RESET}")

def test_openwebninja():
    import requests
    key = env("OPENWEBNINJA_API_KEY")
    url = "https://api.openwebninja.com/realtime-zillow-data/property-details-address"
    r = requests.get(
        url,
        headers={"x-api-key": key},
        params={"address": "1600 Pennsylvania Ave Washington DC 20500"},
        timeout=20,
    )
    if r.status_code == 401:
        raise RuntimeError(f"401 — API key invalid or expired: {r.text[:80]}")
    if r.status_code == 403:
        raise RuntimeError(f"403 — subscription inactive or out of credits: {r.text[:80]}")
    if r.status_code == 404:
        return "auth ok (404 = address not in Zillow)"
    r.raise_for_status()
    data = r.json()
    return f"auth ok (status: {data.get('status', '?')})"

check("OpenWeb Ninja Zillow", test_openwebninja)


# ── ANTHROPIC ────────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}5. Anthropic (Claude){RESET}")

def test_anthropic():
    import urllib.request
    key = env("ANTHROPIC_API_KEY")
    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 10,
        "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read())
    text = data["content"][0]["text"].strip().lower()
    return f"reply: '{text}' (model: claude-haiku-4-5)"

check("Anthropic Claude API", test_anthropic)


# ── SERPER ──────────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}6. Serper.dev (Google Search){RESET}")

def test_serper():
    import urllib.request
    key = env("SERPER_API_KEY")
    body = json.dumps({"q": "travis county tx property records", "num": 1}).encode()
    req = urllib.request.Request(
        "https://google.serper.dev/search",
        data=body,
        headers={"X-API-KEY": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    n = len(data.get("organic", []))
    return f"results: {n} organic"

check("Serper search", test_serper)


# ── FIRECRAWL ───────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}7. Firecrawl (JS scraping){RESET}")

def test_firecrawl():
    import urllib.request
    key = env("FIRECRAWL_API_KEY")
    body = json.dumps({
        "url": "https://example.com",
        "formats": ["markdown"],
    }).encode()
    req = urllib.request.Request(
        "https://api.firecrawl.dev/v1/scrape",
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    if not data.get("success"):
        raise RuntimeError(f"scrape failed: {data}")
    md_len = len(data.get("data", {}).get("markdown", ""))
    return f"scraped example.com ({md_len} chars markdown)"

check("Firecrawl auth + credits", test_firecrawl)


# ── TRESTLE ─────────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}8. Trestle (phone validation){RESET}")

def test_trestle():
    import requests
    key = env("TRESTLE_API_KEY")
    # Austin city hall public number — proves auth + format
    r = requests.get(
        "https://api.trestleiq.com/3.0/phone_intel",
        headers={"x-api-key": key},
        params={"phone": "5129742000"},
        timeout=15,
    )
    if r.status_code == 401:
        raise RuntimeError(f"401 — API key invalid: {r.text[:80]}")
    if r.status_code == 403:
        raise RuntimeError(f"403 — key rejected (check trestleiq.com dashboard for plan status): {r.text[:80]}")
    if r.status_code == 200:
        data = r.json()
        return f"auth ok (phone: {data.get('phone_number', '?')})"
    return f"auth ok (HTTP {r.status_code})"

check("Trestle phone intel", test_trestle)


# ── DIRECTSKIP ──────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}9. DirectSkip (skip trace){RESET}")

def test_directskip():
    # A deliberate no-match search verifies key + IP allowlist and bills $0
    # (verified live 2026-08-17: a no-match is free). status.error non-empty
    # means a bad key or an un-allowlisted IP (Apify always fails this — the
    # API only works from the registered operator box).
    import requests
    key = env("DIRECTSKIP_API_KEY")
    r = requests.post(
        "https://api0.directskip.com/v2/search_contact.php",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json={
            "api_key": key,
            "first_name": "Zzyzx", "last_name": "Preflightcheck",
            "mailing_address": "", "mailing_city": "", "mailing_state": "TX",
            "mailing_zip": "", "property_address": "1 Nowhere Ln",
            "property_city": "Austin", "property_state": "TX", "property_zip": "78701",
        },
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    err = ((data.get("status") or {}).get("error") or "").strip()
    if err:
        raise RuntimeError(f"status.error={err!r} — bad key or IP not allowlisted "
                           "(register this machine's public IP with support@directskip.com)")
    return "auth ok ($0 no-match round-trip)"

check("DirectSkip auth", test_directskip)


# ── SLACK / DISCORD WEBHOOK ─────────────────────────────────────────
print(f"\n{BOLD}{CYAN}10. Slack/Discord webhook{RESET}")

def test_slack():
    import urllib.request
    url = env("SLACK_WEBHOOK_URL")
    body = json.dumps({"text": "SiftStack preflight check OK ✓"}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        body = r.read().decode()
    return f"posted (response: {body[:30] or 'empty body'})"

check("Slack/Discord notify", test_slack)


# ── DROPBOX ─────────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}11. Dropbox (photo intake){RESET}")

def test_dropbox():
    import urllib.request
    import urllib.parse
    app_key = env("DROPBOX_APP_KEY")
    app_secret = env("DROPBOX_APP_SECRET")
    refresh = env("DROPBOX_REFRESH_TOKEN")

    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
    }).encode()
    auth = base64.b64encode(f"{app_key}:{app_secret}".encode()).decode()
    req = urllib.request.Request(
        "https://api.dropbox.com/oauth2/token",
        data=data,
        headers={"Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        token_data = json.loads(r.read())
    access = token_data["access_token"]

    req = urllib.request.Request(
        "https://api.dropboxapi.com/2/users/get_current_account",
        data=b"null",
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        acct = json.loads(r.read())
    return f"account: {acct.get('email', '?')}"

check("Dropbox refresh token", test_dropbox)


# ── GOOGLE DRIVE ────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}12. Google Drive (Shared Drive upload){RESET}")

def test_drive():
    from drive_uploader import upload_file  # src/ is on sys.path (test_imports)
    folder_id = env("GOOGLE_DRIVE_FOLDER_ID")
    key = env("GOOGLE_SERVICE_ACCOUNT_KEY")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write("preflight test")
        tmp = f.name
    link = upload_file(Path(tmp), folder_id, key, filename="preflight_check.txt")
    if not link:
        raise RuntimeError("upload returned None — check src/drive_uploader.py logs")
    return f"uploaded: {link[:60]}..."

check("Drive Shared Drive upload", test_drive)


# ── DATASIFT API (read-only $0 ping) ────────────────────────────────
print(f"\n{BOLD}{CYAN}13. DataSift internal API (read-only){RESET}")

def test_datasift_api():
    env("DATASIFT_EMAIL"); env("DATASIFT_PASSWORD")
    from datasift_api_client import DataSiftAPIClient
    client = DataSiftAPIClient.from_env()
    user = client.get_user()
    email = (user or {}).get("email") or (user or {}).get("username") or "?"
    return f"authenticated as {email}"

check("DataSift get_user()", test_datasift_api)


# ── SOCRATA (Austin open data — fire damage + code enforcement) ─────
print(f"\n{BOLD}{CYAN}14. Socrata (data.austintexas.gov){RESET}")

def test_socrata():
    import urllib.request
    # $limit=1 on the CTECC fire feed — anonymous, free
    url = "https://data.austintexas.gov/resource/wpu4-x69d.json?$limit=1"
    with urllib.request.urlopen(url, timeout=15) as r:
        rows = json.loads(r.read())
    return f"fire feed reachable ({len(rows)} row)"

check("Socrata $limit=1", test_socrata)


# ── SUMMARY ─────────────────────────────────────────────────────────
print(f"\n{BOLD}{CYAN}{'='*60}{RESET}")
passed = sum(1 for _, ok, _ in results if ok)
failed = len(results) - passed
print(f"{BOLD}Summary: {GREEN}{passed} passed{RESET}, "
      f"{RED if failed else GREEN}{failed} failed{RESET} "
      f"out of {len(results)} checks")

if failed:
    print(f"\n{RED}{BOLD}Failed checks:{RESET}")
    for name, ok, msg in results:
        if not ok:
            print(f"  {RED}✗{RESET} {name}: {msg}")
    sys.exit(1)
else:
    print(f"\n{GREEN}{BOLD}All systems go.{RESET}")
    sys.exit(0)

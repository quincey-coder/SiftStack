"""Shared hardening for the tccsearch.org (Travis County Clerk) scrapers.

tccsearch serves an Infragistics ASP.NET page whose client framework
(`$find` / `Sys.Application`) and doc-type checkboxes render fine for
residential IPs but are withheld/degraded for datacenter IPs (e.g. Apify)
by the site's anti-bot. When that happens `$find` is undefined and the
doc-type checkboxes never attach — the old code then blew ~30s on each
`page.check()` and eventually threw a cryptic `$find is not defined`
(costing minutes per run before failing).

`wait_ready()` gates on the client framework actually being live and fails
FAST (~12s) with an actionable message; `safe_check()` waits briefly for a
specific checkbox and never aborts the whole run over one missing type.
Neither changes behaviour on a healthy (residential) load — the framework
is ready at t=0 there, so both return immediately.
"""

import logging
import os
from urllib.parse import unquote, urlparse

from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Shared browser fingerprint for all three Travis tccsearch scrapers. tccsearch
# sits behind Cloudflare's managed "Just a moment..." JS challenge, which
# silently blocks HEADLESS / undisguised chromium (the challenge never clears,
# the ASP.NET framework never loads, and wait_ready() times out with a
# "Just a moment..." title — that was the block behind every failed Travis run
# from 2026-07-16 on). A headed browser + a few hand-rolled spoofs was NOT
# enough (verified on Apify 2026-07-22: challenge still present after 60s).
# The recipe that clears it:
#   • HEADED chromium (Xvfb on Apify) with AutomationControlled disabled
#   • the comprehensive `playwright-stealth` evasion suite applied to the
#     context — crucially it spoofs the WebGL vendor/renderer away from the
#     Xvfb software rasterizer ("SwiftShader"/"llvmpipe"), a dead bot tell, and
#     sets navigator.webdriver to `false` (what real Chrome reports) rather than
#     `undefined`
#   • an INTERNALLY CONSISTENT Linux profile — on Apify the browser genuinely IS
#     Chromium-on-Linux, so a Linux UA + Linux platform + native Linux client
#     hints present no contradiction for Cloudflare to catch (a Mac/Windows UA
#     over a Linux browser mismatches the real sec-ch-ua-platform header)
#   • the residential proxy already routed via proxy_kwargs()
_TCC_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# playwright-stealth is the anti-bot workhorse. Import lazily/defensively so a
# missing dependency degrades to "no stealth" (Cloudflare will block, but the
# module still imports) rather than breaking every Travis scraper at import time.
try:
    from playwright_stealth import Stealth  # type: ignore

    _TCC_STEALTH = Stealth(
        navigator_platform_override="Linux x86_64",
        navigator_user_agent_override=_TCC_UA,
        navigator_languages_override=("en-US", "en"),
    )
except Exception as _stealth_err:  # pragma: no cover - dependency guard
    _TCC_STEALTH = None
    logger.warning(
        "playwright-stealth unavailable (%s) — tccsearch scrapers will likely be "
        "Cloudflare-blocked. `pip install playwright-stealth`.", _stealth_err,
    )


def tcc_headless() -> bool:
    """Travis tccsearch scrapers run HEADED by default (Cloudflare blocks
    headless). Override with TCC_HEADLESS=1 only for local debugging on a box
    where a headless load happens to pass (a residential IP often will)."""
    return os.getenv("TCC_HEADLESS", "").strip().lower() in ("1", "true", "yes")


async def launch_tcc_context(p, default_timeout_ms: int = 30000):
    """Launch a Cloudflare-resistant ``(browser, context, page)`` for tccsearch.

    Centralizes the launch recipe so all three Travis scrapers share the exact
    same anti-bot posture. Headed by default (see tcc_headless); routed through
    the residential proxy via proxy_kwargs() when one is configured. On a
    headless server with no display the headed launch raises — we fall back to
    headless so local dev doesn't hard-crash, but Cloudflare will likely block
    that path (run under ``xvfb-run -a`` to get a real display).
    """
    headless = tcc_headless()
    launch_args = ["--disable-blink-features=AutomationControlled", "--no-sandbox"]
    try:
        browser = await p.chromium.launch(headless=headless, args=launch_args)
    except Exception as e:
        if not headless:
            logger.warning(
                "tccsearch: headed launch failed (%s) — falling back to headless "
                "(Cloudflare will likely block). On a headless server run under "
                "`xvfb-run -a`.", str(e).splitlines()[0],
            )
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        else:
            raise
    context = await browser.new_context(
        user_agent=_TCC_UA,
        viewport={"width": 1400, "height": 1200},
        **proxy_kwargs(),
    )
    # Apply the full playwright-stealth evasion suite to the whole context so
    # every page inherits it (WebGL vendor, sec-ch-ua, userAgentData, plugins,
    # webdriver=false, hardwareConcurrency, ...). Falls back to a minimal inline
    # spoof if the library is somehow unavailable.
    if _TCC_STEALTH is not None:
        await _TCC_STEALTH.apply_stealth_async(context)
    else:
        await context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
            "window.chrome={runtime:{}};"
        )
    page = await context.new_page()
    page.set_default_timeout(default_timeout_ms)
    return browser, context, page


# Cloudflare interstitial detection. The "Just a moment..." challenge exposes
# itself via the document title AND (more reliably) the challenge-platform DOM
# nodes it injects. A cleared/real page matches none of these.
_CF_CHALLENGE_JS = """() => {
  const t = (document.title || '').toLowerCase();
  const cfTitle = t.includes('just a moment') || t.includes('attention required')
      || t.includes('checking your browser') || t.includes('checking if the site');
  const cfDom = !!document.querySelector(
      '#challenge-running, #cf-challenge-running, #challenge-form, '
      + '#challenge-stage, script[src*="challenge-platform"]');
  return cfTitle || cfDom;
}"""


async def pass_cloudflare(page: Page, timeout_ms: int = 45000) -> bool:
    """Wait for a Cloudflare 'Just a moment...' JS challenge to clear.

    A correctly-disguised headed browser auto-solves the challenge and redirects
    to the real page within a few seconds; poll until the challenge markers are
    gone. Returns True once the page is clear (or was never challenged), False if
    the challenge was still up at the deadline — in which case the caller's
    wait_ready() produces the self-diagnosing "still on Just a moment..." error.
    Near-zero cost on a page that loaded directly: the first poll returns at once.

    Managed challenges occasionally set the cf_clearance cookie but leave the
    interstitial up instead of auto-redirecting; a single reload partway through
    picks up the cookie and lands on the real page. We reload once at the ~40%
    mark if still challenged.
    """
    polls = max(1, timeout_ms // 1000)
    reload_at = max(1, int(polls * 0.4))
    reloaded = False
    for i in range(polls):
        try:
            challenged = await page.evaluate(_CF_CHALLENGE_JS)
        except Exception:
            # During the CF redirect the execution context can briefly detach;
            # treat as "still challenged" and keep polling.
            challenged = True
        if not challenged:
            if i:
                logger.info("tccsearch: Cloudflare challenge cleared after ~%ds", i)
            return True
        if i >= reload_at and not reloaded:
            reloaded = True
            logger.info(
                "tccsearch: still on Cloudflare interstitial after %ds — reloading "
                "once to pick up the clearance cookie", i,
            )
            try:
                await page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
        await page.wait_for_timeout(1000)
    # Decisive diagnostic: is this a pure JS "managed challenge" (→ IP/pool /
    # fingerprint problem) or an embedded Turnstile widget (→ needs a solved
    # cf-clearance token, e.g. via 2captcha)? Capture the sitekey + CF ray so the
    # next fix is chosen from evidence, not guesswork.
    diag = {}
    try:
        diag = await page.evaluate(
            """() => {
              const ts = document.querySelector('.cf-turnstile,[data-sitekey],#challenge-stage iframe,iframe[src*="turnstile"]');
              let sitekey = '';
              const el = document.querySelector('[data-sitekey]');
              if (el) sitekey = el.getAttribute('data-sitekey') || '';
              if (!sitekey) {
                const m = (document.body.innerHTML || '').match(/sitekey["'\\s:=]+([0-9A-Za-z_-]{10,})/);
                if (m) sitekey = m[1];
              }
              const rayEl = document.querySelector('.ray-id, [class*="ray"]');
              return {
                title: document.title,
                hasTurnstile: !!ts,
                sitekey,
                ray: rayEl ? rayEl.innerText.trim().slice(0, 40) : '',
                bodylen: document.body ? document.body.innerText.length : 0,
              };
            }"""
        )
    except Exception:
        pass
    logger.warning(
        "tccsearch: Cloudflare challenge still present after %ds "
        "(title=%r, turnstile=%s, sitekey=%r, ray=%r, bodylen=%s). "
        "turnstile=True → needs a solved cf-clearance token (2captcha); "
        "turnstile=False → JS managed challenge, likely IP/pool reputation.",
        timeout_ms // 1000, diag.get("title", "?"), diag.get("hasTurnstile"),
        diag.get("sitekey", ""), diag.get("ray", ""), diag.get("bodylen"),
    )
    return False


def proxy_kwargs(env_var: str = "SCRAPER_PROXY_URL", fresh_session: bool = False) -> dict:
    """Playwright ``new_context(**proxy_kwargs())`` — route the browser through a
    proxy when one is configured, else no-op.

    tccsearch.org blocks Apify's datacenter IP (see wait_ready), so on the
    platform these scrapers must go out through a residential proxy. main.py
    sets SCRAPER_PROXY_URL from the Apify residential proxy when
    ``use_residential_proxy`` is on; when it's unset (local/CLI runs) this
    returns ``{}`` and behaviour is unchanged.
    """
    url = (os.environ.get(env_var) or "").strip()
    if not url:
        return {}
    p = urlparse(url)
    if not p.hostname:
        logger.warning("Ignoring malformed %s (no host): %s", env_var, url[:40])
        return {}
    server = f"{p.scheme or 'http'}://{p.hostname}"
    if p.port:
        server += f":{p.port}"
    proxy = {"server": server}
    if p.username:
        proxy["username"] = unquote(p.username)
    # fresh_session: pin a UNIQUE Apify proxy session (= distinct exit IP) for
    # this browser context. Without it every scraper shares the one URL from
    # main.py — one exit IP — and the GovOS/AWS-WAF anti-bots punish the
    # SECOND session from the same IP (Bell lis pendens failing ~2 min after
    # Bell lien succeeded on it, QA 2026-08-19). The Travis tccsearch scrapers
    # deliberately do NOT use this: their cf_clearance cookie is tied to a
    # sticky IP.
    if fresh_session and proxy.get("username") and "apify" in (p.hostname or ""):
        import re as _re
        import uuid as _uuid
        sid = _uuid.uuid4().hex[:12]
        uname = proxy["username"]
        if "session-" in uname:
            uname = _re.sub(r"session-[^,]*", f"session-{sid}", uname)
        else:
            uname += f",session-{sid}"
        proxy["username"] = uname
    if p.password:
        proxy["password"] = unquote(p.password)
    logger.info("Routing browser via proxy %s (user %s…)",
                server, (proxy.get("username") or "")[:24])
    return {"proxy": proxy}

# The ASP.NET-AJAX runtime is "ready" once $find is callable AND the Sys
# application has finished initializing (widgets registered → $find resolves).
_READY_JS = (
    "() => typeof $find === 'function' && typeof Sys !== 'undefined' "
    "&& Sys.Application && Sys.Application.get_isInitialized "
    "&& Sys.Application.get_isInitialized()"
)


async def goto_with_retry(
    page: Page, url: str, attempts: int = 3, wait_until: str = "domcontentloaded"
) -> None:
    """Navigate to `url`, retrying on ``net::ERR_ABORTED``.

    Right after the disclaimer "Accept" click the site fires its own redirect;
    a manual goto issued into that still-in-flight navigation aborts
    (``net::ERR_ABORTED``). It's intermittent on a fast connection and more
    likely through the slower residential proxy on Apify, so retry a couple of
    times before surfacing the error. Non-abort errors propagate immediately.
    """
    last_err: Exception | None = None
    for i in range(attempts):
        try:
            await page.goto(url, wait_until=wait_until)
            return
        except Exception as e:
            if "ERR_ABORTED" not in str(e):
                raise
            last_err = e
            logger.info(
                "tccsearch: goto %s aborted (attempt %d/%d) — retrying after "
                "the site's own redirect settles", url, i + 1, attempts,
            )
            await page.wait_for_timeout(1200)
    assert last_err is not None
    raise last_err


class TccNotReady(RuntimeError):
    """tccsearch client framework never initialized — almost always an
    IP-level block (datacenter anti-bot) rather than a transient timing issue."""


async def wait_ready(page: Page, timeout_ms: int | None = None) -> None:
    """Block until the Infragistics client framework is live, or fail fast.

    Through a residential proxy the heavy Infragistics script bundles load much
    slower than on a direct connection (where they're ready at t≈0), so allow
    more time when proxied. On timeout, capture the real page state so the
    failure is self-diagnosing: `$find`/`Sys` becoming defined just after the
    deadline means the timeout was merely too short, whereas a near-empty or
    challenge-page body means the IP itself is blocked.
    """
    # The ASP.NET framework ($find/Sys) only exists on the REAL page — never on
    # the Cloudflare interstitial. Let the challenge clear first so the framework
    # wait below measures the app's load time, not the challenge's.
    await pass_cloudflare(page)

    proxied = bool((os.environ.get("SCRAPER_PROXY_URL") or "").strip())
    if timeout_ms is None:
        timeout_ms = 35000 if proxied else 12000
    try:
        await page.wait_for_function(_READY_JS, timeout=timeout_ms)
    except Exception:
        diag = {}
        try:
            diag = await page.evaluate(
                "() => ({find: typeof $find, sys: typeof Sys, "
                "title: document.title, "
                "bodylen: document.body ? document.body.innerText.length : 0, "
                "snippet: document.body ? document.body.innerText.slice(0, 140) : ''})"
            )
        except Exception:
            pass
        raise TccNotReady(
            f"tccsearch client framework not ready after {timeout_ms / 1000:.0f}s "
            f"(proxy={'on' if proxied else 'off'}). page state: "
            f"$find={diag.get('find')} Sys={diag.get('sys')} "
            f"title={diag.get('title')!r} bodylen={diag.get('bodylen')} "
            f"snippet={diag.get('snippet')!r} — $find='function' here means the "
            "timeout was just too short; a near-empty/challenge body means the IP "
            "is blocked (rotate proxy session or the residential IP is flagged)."
        ) from None

    # The framework being live (Sys.Application initialized) does NOT guarantee
    # the Infragistics search form has rendered — on a cold proxy connection the
    # doc-type checkboxes and Search button lag the framework by a few seconds.
    # Wait for the Search button so the caller's safe_check() and _submit_search()
    # find their elements instead of racing a half-rendered form (this was the
    # foreclosure "checkbox not checkable / btnSearch is null" failure, verified
    # on Apify 2026-07-22). Best-effort: log but don't fail — the search-entry
    # page ID is stable, so absence means a slow render, not a hard error.
    try:
        await page.wait_for_selector(
            "#cphNoMargin_SearchButtons1_btnSearch", state="attached", timeout=15000,
        )
    except Exception:
        logger.warning(
            "tccsearch: search form (btnSearch) not attached after 15s — doc-type "
            "and submit steps may race a partially-rendered form",
        )


def effective_from_date(from_date, to_date, mode: str):
    """Widen a daily window to cover the clerk's Temp-index lag.

    ROOT CAUSE (captured live 2026-08-18): freshly-filed documents sit in the
    Travis clerk's "Temp" index for several days with NO grantor/grantee names
    attached — the Name cell is literally empty `[R]/[E]` markers. A 1-day
    daily window therefore only ever sees nameless Temp rows, which parse to
    zero. This is why Travis probate and lis pendens produced 0 records for
    30+ straight days while the search itself "worked".

    Looking back TCC_INDEX_LAG_DAYS (default 7) picks the same filings up once
    the clerk verifies them; the cross-run seen_notice_ids dedup absorbs the
    re-scans.
    """
    from datetime import timedelta

    if mode != "daily":
        return from_date
    lag = 7
    raw = os.getenv("TCC_INDEX_LAG_DAYS", "").strip()
    if raw.isdigit():
        lag = int(raw)
    widened = to_date - timedelta(days=lag)
    return min(from_date, widened)


def count_temp_rows(body_text: str) -> int:
    """Count result rows still in the clerk's Temp (unverified) index."""
    import re as _re
    return len(_re.findall(r"\bTemp\b", body_text or ""))


async def click_search(page: Page, attempts: int = 3) -> None:
    """Click the tccsearch Search button with a null guard + retries.

    The bare `document.getElementById(...).click()` evaluate crashed with
    "Cannot read properties of null (reading 'click')" on 10 of 30 days when
    the Infragistics form lagged the framework (cold proxy connection). Wait
    for the button to attach, click via a null-guarded evaluate, and retry
    with a settle delay before giving up loudly.
    """
    last_err: Exception | None = None
    for attempt in range(attempts):
        try:
            await page.wait_for_selector(
                "#cphNoMargin_SearchButtons1_btnSearch", state="attached",
                timeout=10000,
            )
        except Exception as e:
            last_err = e
        clicked = False
        try:
            clicked = await page.evaluate(
                "() => { const b = document.getElementById("
                "'cphNoMargin_SearchButtons1_btnSearch'); "
                "if (!b) return false; b.click(); return true; }"
            )
        except Exception as e:
            last_err = e
        if clicked:
            return
        logger.warning(
            "tccsearch: Search button not clickable (attempt %d/%d) — waiting "
            "for the form to settle", attempt + 1, attempts,
        )
        await page.wait_for_timeout(2500)
    raise TccNotReady(
        f"tccsearch: Search button never became clickable after {attempts} "
        f"attempts ({last_err}) — form render raced or page is a challenge/error page"
    )


async def safe_check(page: Page, selector: str, timeout_ms: int = 15000) -> bool:
    """Check a doc-type checkbox, waiting for it to attach.

    Returns True if checked, False if it never appeared (logged, not raised —
    one missing doc type shouldn't abort the whole run). The timeout is generous
    (15s) because through the residential proxy the Infragistics doc-type list
    renders several seconds after the page's framework initializes.
    """
    try:
        await page.wait_for_selector(selector, state="attached", timeout=timeout_ms)
        await page.check(selector, timeout=timeout_ms)
        return True
    except Exception as e:
        logger.warning(
            "tccsearch: doc-type checkbox %s not checkable: %s",
            selector, str(e).splitlines()[0],
        )
        return False

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


def proxy_kwargs(env_var: str = "SCRAPER_PROXY_URL") -> dict:
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


class TccNotReady(RuntimeError):
    """tccsearch client framework never initialized — almost always an
    IP-level block (datacenter anti-bot) rather than a transient timing issue."""


async def wait_ready(page: Page, timeout_ms: int = 12000) -> None:
    """Block until the Infragistics client framework is live, or fail fast.

    Raises TccNotReady on timeout so the caller aborts in ~12s with a clear
    message instead of burning ~30s per checkbox and dying on `$find is not
    defined`.
    """
    try:
        await page.wait_for_function(_READY_JS, timeout=timeout_ms)
    except Exception:
        raise TccNotReady(
            f"tccsearch client framework ($find/Sys.Application) not ready after "
            f"{timeout_ms / 1000:.0f}s — the page is almost certainly being served "
            "a degraded/blocked response (datacenter-IP anti-bot). Route this "
            "scraper through a residential proxy (set use_residential_proxy=true)."
        ) from None


async def safe_check(page: Page, selector: str, timeout_ms: int = 8000) -> bool:
    """Check a doc-type checkbox, waiting briefly for it to attach.

    Returns True if checked, False if it never appeared (logged, not raised —
    one missing doc type shouldn't abort the whole run).
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

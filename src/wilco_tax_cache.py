"""Williamson County tax delinquent file cache + URL discovery.

Williamson's master county roll is served as a 1.7GB fixed-width text file
inside a 72MB ZIP — too heavy to download routinely. Cross-reference for
probate/foreclosure address backfill instead uses the live WCAD SODA REST
API (`cad_lookup._wcad_name_search`), which returns full county owner+situs
data in real time.

This module is therefore lighter than `bell_tax_cache`: it just discovers
and caches the delinquent XLSX, which is the primary scrape source.

Source page: https://www.wilcotx.gov/761/Property-Tax-Roll-Information-Request
File: "Current Year and Prior Taxes Due (Excel) M-DD-YYYY" at
  /DocumentCenter/View/8553/...
The DocumentCenter ID (8553) is stable; the date token in the trailing path
slug is informational. We hit the View URL by ID — Wilco's CivicPlus serves
the latest version regardless of slug staleness.
"""

from __future__ import annotations

import logging
import os
import re
import time
from pathlib import Path

import requests

import config

# A failed download may fall back to the cached roll, but only this long: the
# county refreshes weekly, and a cache older than this means the fetch has been
# failing for a while — better to raise (ScraperError in the scraper → hard
# fail in run-health) than to keep re-processing a stale file as "today".
MAX_STALE_FALLBACK_HOURS = 7 * 24

logger = logging.getLogger(__name__)


def _cache_dir() -> Path:
    base = Path(config.WILCO_TAX_CACHE_DIR)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _is_fresh(path: Path, ttl_hours: int) -> bool:
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < ttl_hours * 3600


def _document_center_url(doc_id: str) -> str:
    return f"https://www.wilcotx.gov/DocumentCenter/View/{doc_id}"


def _resolve_latest_url(index_html: str, doc_id: str) -> str | None:
    """Find the latest slugged URL for a CivicPlus DocumentCenter ID.

    Falls back to the bare ID URL if no slugged variant is found in the page;
    CivicPlus accepts both forms.
    """
    pat = re.compile(rf"/DocumentCenter/View/{re.escape(doc_id)}(?:/[^\s\"'<>]*)?")
    matches = pat.findall(index_html)
    if matches:
        # Prefer the longest match (slugged) so the cached filename is
        # human-readable when you scroll into data/wilco_tax_cache/.
        matches.sort(key=len, reverse=True)
        return f"https://www.wilcotx.gov{matches[0]}"
    return _document_center_url(doc_id)


def _proxies() -> dict | None:
    """``requests`` proxies for the Actor's residential proxy, else None.

    main.py exports SCRAPER_PROXY_URL (Apify RESIDENTIAL / US) whenever
    ``use_residential_proxy`` is on. wilcotx.gov sits behind Cloudflare and
    has answered Apify's datacenter egress IP with HTTP 403 on EVERY daily run
    since 2026-10-06 — index page and DocumentCenter file alike — while the
    identical plain GET succeeds from a residential IP (verified 2026-10-27
    through the Actor's own RESIDENTIAL/US group: 200 on both). Unset
    locally → direct fetch, behaviour unchanged.
    """
    url = (os.environ.get("SCRAPER_PROXY_URL") or "").strip()
    return {"http": url, "https": url} if url else None


def _get(url: str, **kw) -> requests.Response:
    """GET direct first (free); when that is refused (403) or fails to
    connect, retry once through the residential proxy if one is configured.

    Direct-first keeps local runs free and lets the cloud recover on its own
    if Cloudflare ever stops blocking the platform IP. The ~30 MB roll costs
    about $0.25/run over residential transfer when the fallback fires.
    """
    try:
        r = requests.get(url, **kw)
        r.raise_for_status()
        return r
    except requests.RequestException as e:
        proxies = _proxies()
        if not proxies:
            raise
        status = getattr(getattr(e, "response", None), "status_code", None)
        logger.warning(
            "Direct fetch of %s refused (%s) — retrying through the residential proxy",
            url, status or e.__class__.__name__,
        )
        r = requests.get(url, proxies=proxies, **kw)
        r.raise_for_status()
        logger.info("  fetched via residential proxy: %s", url)
        return r


def _download(url: str, dest: Path) -> int:
    logger.info("Downloading %s → %s", url, dest)
    r = _get(url, timeout=600, stream=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    total = 0
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 20):
            f.write(chunk)
            total += len(chunk)
    os.replace(tmp, dest)
    logger.info("  Saved %.1f MB to %s", total / (1 << 20), dest)
    return total


def download_delinquent_xlsx(force: bool = False) -> Path | None:
    """Download the Wilco "Current Year and Prior Taxes Due (Excel)" file.

    Returns the cached path or None if download failed.
    """
    cache = _cache_dir()
    dest = cache / "wilco_delinquent_latest.xlsx"
    ttl = config.WILCO_TAX_CACHE_TTL_HOURS

    if not force and _is_fresh(dest, ttl):
        return dest

    # Resolve the latest URL via the index page (slug freshness is informational)
    try:
        r = _get(config.WILCO_TAX_ROLL_PAGE, timeout=30)
        url = _resolve_latest_url(r.text, config.WILCO_DELINQUENT_DOC_ID)
    except Exception as e:
        logger.warning("Wilco index page fetch failed (%s) — falling back to bare ID URL", e)
        url = _document_center_url(config.WILCO_DELINQUENT_DOC_ID)

    try:
        _download(url, dest)
        return dest
    except Exception as e:
        if dest.exists():
            age_h = (time.time() - dest.stat().st_mtime) / 3600
            if age_h <= MAX_STALE_FALLBACK_HOURS:
                logger.warning(
                    "Wilco delinquent download failed (%s) — falling back to the "
                    "cached file from %.0f h ago", e, age_h,
                )
                return dest
            logger.warning(
                "Wilco delinquent download failed (%s) and the cached file is "
                "%.0f h old (> %d h) — refusing to reuse it", e, age_h,
                MAX_STALE_FALLBACK_HOURS,
            )
            return None
        logger.warning("Wilco delinquent download failed: %s (no cached file)", e)
        return None

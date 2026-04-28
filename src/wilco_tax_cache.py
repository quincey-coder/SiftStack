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


def _download(url: str, dest: Path) -> int:
    logger.info("Downloading %s → %s", url, dest)
    r = requests.get(url, timeout=600, stream=True)
    r.raise_for_status()
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
        r = requests.get(config.WILCO_TAX_ROLL_PAGE, timeout=30)
        r.raise_for_status()
        url = _resolve_latest_url(r.text, config.WILCO_DELINQUENT_DOC_ID)
    except Exception as e:
        logger.warning("Wilco index page fetch failed (%s) — falling back to bare ID URL", e)
        url = _document_center_url(config.WILCO_DELINQUENT_DOC_ID)

    try:
        _download(url, dest)
        return dest
    except Exception as e:
        logger.warning("Wilco delinquent download failed: %s", e)
        return dest if dest.exists() else None

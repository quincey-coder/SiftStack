"""Evidence capture for scraper debugging — raw page HTML + innerText dumps.

No-op unless SIFT_DEBUG_CAPTURE_DIR is set, so production runs pay nothing.
scraper_smoke.py sets the env var per target; scrapers call dump_page() at
their parse-zero / failure points so a broken grid leaves the raw page behind
for parser forensics instead of just a "0 notices" log line.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

CAPTURE_DIR_ENV = "SIFT_DEBUG_CAPTURE_DIR"


def _capture_dir() -> Path | None:
    raw = os.environ.get(CAPTURE_DIR_ENV, "").strip()
    if not raw:
        return None
    path = Path(raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("debug_capture: cannot create %s: %s", path, e)
        return None
    return path


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", label).strip("_")[:80]


async def dump_page(page, label: str) -> Path | None:
    """Dump a Playwright page's HTML and innerText. Returns the HTML path."""
    directory = _capture_dir()
    if directory is None or page is None:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = directory / f"{_slug(label)}_{ts}"
    html_path = base.with_suffix(".html")
    try:
        html_path.write_text(await page.content(), encoding="utf-8")
        try:
            text = await page.evaluate("document.body ? document.body.innerText : ''")
            base.with_suffix(".txt").write_text(text or "", encoding="utf-8")
        except Exception:
            pass
        logger.info("debug_capture: page dumped to %s", html_path)
        return html_path
    except Exception as e:
        logger.warning("debug_capture: dump failed for %s: %s", label, e)
        return None


def dump_text(label: str, text: str) -> Path | None:
    """Dump an arbitrary text blob (e.g. an extracted grid or API body)."""
    directory = _capture_dir()
    if directory is None:
        return None
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = directory / f"{_slug(label)}_{ts}.txt"
    try:
        path.write_text(text or "", encoding="utf-8")
        logger.info("debug_capture: text dumped to %s", path)
        return path
    except Exception as e:
        logger.warning("debug_capture: dump failed for %s: %s", label, e)
        return None

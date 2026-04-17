"""Texas county scrapers — one module per source, parameterized by county.

Registry maps (county, notice_type) → scraper function.
Each scraper returns list[NoticeData].

Scraper modules (to be implemented in subsequent phases):
  foreclosure_travis.py     — tccsearch.org batch PDF via Results List
  foreclosure_pdf.py        — PDF download+parse (Bell + Williamson)
  tax_sale_travis.py        — RealAuction HTML tables
  tax_sale_mvba.py          — MVBA Law Firm PDFs (Bell + Williamson)
  probate_odyssey.py        — Odyssey Portal (all 3 counties)
  tax_delinquent_travis.py  — CSV download (trivial)
  tax_delinquent_cad.py     — CAD portal scraper (Bell + Williamson)
"""

import logging
from typing import Protocol

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


class Scraper(Protocol):
    """Protocol for all TX county scrapers."""

    async def scrape(
        self,
        mode: str,
        since_date: str | None = None,
        max_notices: int | None = None,
    ) -> list[NoticeData]: ...


# Registry: (county, notice_type) → scraper class
# Populated as scraper modules are implemented in Phases 2-6
_REGISTRY: dict[tuple[str, str], type[Scraper]] = {}


def register(county: str, notice_type: str):
    """Decorator to register a scraper for a (county, notice_type) pair."""
    def wrapper(cls: type[Scraper]) -> type[Scraper]:
        _REGISTRY[(county.lower(), notice_type.lower())] = cls
        return cls
    return wrapper


def get_scraper(county: str, notice_type: str) -> Scraper | None:
    """Look up and instantiate a scraper for the given county + notice type."""
    cls = _REGISTRY.get((county.lower(), notice_type.lower()))
    if cls is None:
        logger.warning("No scraper registered for %s/%s", county, notice_type)
        return None
    return cls()


def list_registered() -> list[tuple[str, str]]:
    """Return all registered (county, notice_type) pairs."""
    return sorted(_REGISTRY.keys())


async def scrape_targets(
    targets: list[tuple[str, str]],
    mode: str = "daily",
    since_date: str | None = None,
    max_notices: int | None = None,
) -> list[NoticeData]:
    """Run all scrapers for the given (county, notice_type) targets.

    Returns combined list of NoticeData from all scrapers.
    """
    all_notices: list[NoticeData] = []

    for county, notice_type in targets:
        scraper = get_scraper(county, notice_type)
        if scraper is None:
            continue

        logger.info("Scraping %s/%s (mode=%s)...", county, notice_type, mode)
        try:
            notices = await scraper.scrape(
                mode=mode,
                since_date=since_date,
                max_notices=max_notices,
            )
            logger.info("  %s/%s: %d notices", county, notice_type, len(notices))
            all_notices.extend(notices)
        except Exception as e:
            logger.error("  %s/%s scraper failed: %s", county, notice_type, e)

    return all_notices


# ── Auto-import scraper modules so @register decorators fire ──────────
# Each module import triggers its @register("County", "type") decorator,
# populating _REGISTRY without explicit wiring.
try:
    from scrapers import foreclosure_travis  # noqa: F401
except ImportError as e:
    logger.debug("Could not import foreclosure_travis: %s", e)

try:
    from scrapers import probate_odyssey  # noqa: F401
except ImportError as e:
    logger.debug("Could not import probate_odyssey: %s", e)

try:
    from scrapers import probate_travis  # noqa: F401
except ImportError as e:
    logger.debug("Could not import probate_travis: %s", e)

try:
    from scrapers import probate_bell  # noqa: F401
except ImportError as e:
    logger.debug("Could not import probate_bell: %s", e)

try:
    from scrapers import tax_delinquent_travis  # noqa: F401
except ImportError as e:
    logger.debug("Could not import tax_delinquent_travis: %s", e)

try:
    from scrapers import tax_sale_mvba  # noqa: F401
except ImportError as e:
    logger.debug("Could not import tax_sale_mvba: %s", e)

"""Texas county scrapers — one module per source, parameterized by county.

Registry maps (county, notice_type) → scraper class; each scraper's
`scrape()` returns list[NoticeData]. See _SCRAPER_MODULES below for the
authoritative module list and EXPECTED_PAIRS for the pairs that must be
registered when every import succeeds.

Failure contract: a scraper that cannot produce trustworthy results should
raise (ScraperError when it has partial results to hand back) rather than
returning [] — `scrape_targets` records every outcome into the run-health
report, and a silent zero is indistinguishable from a quiet day.
"""

import logging
import os
import time
from typing import Protocol

from notice_parser import NoticeData

logger = logging.getLogger(__name__)


class ScraperError(Exception):
    """A scraper failed loudly but may still hand back partial results.

    Raise this instead of swallowing an exception and returning []: the
    orchestrator records the failure in the run-health report (so it alerts)
    while still keeping any notices scraped before the failure.
    """

    def __init__(self, message: str, partial: list[NoticeData] | None = None):
        super().__init__(message)
        self.partial: list[NoticeData] = partial or []


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


def get_scraper(county: str, notice_type: str, **kwargs) -> Scraper | None:
    """Look up and instantiate a scraper for the given county + notice type.

    Extra kwargs are passed to the scraper constructor (e.g., min_years, min_amount
    for tax delinquent scrapers).
    """
    cls = _REGISTRY.get((county.lower(), notice_type.lower()))
    if cls is None:
        logger.warning("No scraper registered for %s/%s", county, notice_type)
        return None
    try:
        return cls(**kwargs)
    except TypeError:
        # Scraper doesn't accept kwargs — instantiate without them
        return cls()


def list_registered() -> list[tuple[str, str]]:
    """Return all registered (county, notice_type) pairs."""
    return sorted(_REGISTRY.keys())


# Every (county, notice_type) pair that SHOULD be registered when all scraper
# modules import cleanly. A pair listed here but missing from the registry at
# runtime means an import failed — that is a health event, not a debug line.
EXPECTED_PAIRS: list[tuple[str, str]] = [
    ("travis", "foreclosure"),
    ("travis", "probate"),
    ("travis", "tax_delinquent"),
    ("travis", "lien"),
    ("travis", "lis_pendens"),
    ("travis", "fire_damage"),
    ("travis", "code_violation"),
    ("travis", "tax_sale"),
    ("bell", "foreclosure"),
    ("bell", "probate"),
    ("bell", "tax_sale"),
    ("bell", "tax_delinquent"),
    ("bell", "lien"),
    ("bell", "lis_pendens"),
    ("williamson", "foreclosure"),
    ("williamson", "probate"),
    ("williamson", "tax_sale"),
    ("williamson", "tax_delinquent"),
    ("williamson", "lien"),
    ("williamson", "lis_pendens"),
]

# Pairs we know have no scraper yet — reported as "known gap", not an alert.
KNOWN_MISSING: list[tuple[str, str]] = []


def registry_gaps() -> list[tuple[str, str]]:
    """Expected pairs that failed to register (broken import = silent death)."""
    return [p for p in EXPECTED_PAIRS if p not in _REGISTRY]


async def scrape_targets(
    targets: list[tuple[str, str]],
    mode: str = "daily",
    since_date: str | None = None,
    max_notices: int | None = None,
    scraper_kwargs: dict | None = None,
    health=None,
) -> list[NoticeData]:
    """Run all scrapers for the given (county, notice_type) targets.

    Args:
        scraper_kwargs: Extra kwargs passed to scraper constructors
            (e.g., min_years/min_amount for tax delinquent scrapers).
        health: Optional run_health.RunHealth — receives one record per target
            (count, duration, error, evidence) so failures can never vanish.

    Returns combined list of NoticeData from all scrapers. A scraper that
    raises ScraperError still contributes its `partial` notices.
    """
    all_notices: list[NoticeData] = []
    kwargs = scraper_kwargs or {}
    force_fail = os.environ.get("FORCE_SCRAPER_FAIL", "").strip().lower()

    # Failure signatures that deserve ONE full re-run of the scraper (fresh
    # browser, fresh proxy session): Cloudflare handed us a bad residential IP
    # or the proxy tunnel itself hiccuped. These caused ~17/30 flaky Travis
    # days; a second attempt on a new session usually clears them.
    _RETRYABLE_MARKERS = (
        "client framework not ready",
        "ERR_TUNNEL_CONNECTION_FAILED",
        "Just a moment",
    )

    async def _scrape_with_retry(scraper, county, notice_type):
        for attempt in (1, 2):
            try:
                return await scraper.scrape(
                    mode=mode,
                    since_date=since_date,
                    max_notices=max_notices,
                )
            except Exception as e:
                msg = str(e)
                if attempt == 1 and any(m in msg for m in _RETRYABLE_MARKERS):
                    logger.warning(
                        "  %s/%s: retryable block/proxy failure (%s) — retrying "
                        "once on a fresh session", county, notice_type,
                        msg.splitlines()[0][:120],
                    )
                    continue
                raise

    for county, notice_type in targets:
        scraper = get_scraper(county, notice_type, **kwargs)
        if scraper is None:
            continue

        logger.info("Scraping %s/%s (mode=%s)...", county, notice_type, mode)
        t0 = time.monotonic()
        try:
            if force_fail == f"{county.lower()}/{notice_type.lower()}":
                raise RuntimeError("FORCE_SCRAPER_FAIL drill — intentional test failure")
            notices = await _scrape_with_retry(scraper, county, notice_type)
            logger.info("  %s/%s: %d notices", county, notice_type, len(notices))
            all_notices.extend(notices)
            if health is not None:
                health.record_scraper(
                    county, notice_type,
                    count=len(notices),
                    duration_s=time.monotonic() - t0,
                    evidence=dict(getattr(scraper, "last_meta", {}) or {}),
                )
        except ScraperError as e:
            logger.error(
                "  %s/%s scraper failed: %s (%d partial records kept)",
                county, notice_type, e, len(e.partial), exc_info=True,
            )
            all_notices.extend(e.partial)
            if health is not None:
                evidence = dict(getattr(scraper, "last_meta", {}) or {})
                if e.partial:
                    evidence["partial"] = len(e.partial)
                health.record_scraper(
                    county, notice_type,
                    count=len(e.partial),
                    duration_s=time.monotonic() - t0,
                    error=str(e),
                    evidence=evidence,
                )
        except Exception as e:
            logger.error(
                "  %s/%s scraper failed: %s", county, notice_type, e, exc_info=True
            )
            if health is not None:
                health.record_scraper(
                    county, notice_type,
                    count=0,
                    duration_s=time.monotonic() - t0,
                    error=str(e),
                    evidence=dict(getattr(scraper, "last_meta", {}) or {}),
                )

    return all_notices


# ── Auto-import scraper modules so @register decorators fire ──────────
# Each module import triggers its @register("County", "type") decorator,
# populating _REGISTRY without explicit wiring.
#
# ORDER MATTERS for one pair: lien_tyler must import AFTER lien_publicsearch —
# Williamson replatformed from publicsearch.us to Tyler Self-Service, and the
# later import wins the Williamson/lien registration.
#
# An import failure here used to log at DEBUG and the scraper silently
# vanished from the registry. It now logs at ERROR, and main.py surfaces any
# EXPECTED_PAIRS gap through the run-health report.
_SCRAPER_MODULES = [
    "foreclosure_travis",
    "tax_sale_travis",
    "probate_odyssey",
    "probate_travis",
    "probate_bell",
    "tax_delinquent_travis",
    "tax_sale_mvba",
    "foreclosure_wilco",
    "foreclosure_bell",
    # TRIROLL — Bell + Williamson tax-delinquent scrapers
    "tax_delinquent_bell",
    "tax_delinquent_wilco",
    # Code enforcement — Travis (City of Austin) via Socrata SODA API
    "code_enforcement_travis",
    # Liens (county-clerk OPR) — Travis tccsearch, Bell publicsearch (headed),
    # Williamson Tyler Self-Service (headed, must follow lien_publicsearch)
    "lien_travis",
    "lien_publicsearch",
    "lien_tyler",
    # Lis pendens (Tex. Prop. Code § 12.007) — same three sources as liens
    "lis_pendens_travis",
    "lis_pendens_publicsearch",
    "lis_pendens_tyler",
    "fire_damage_travis",
]

import importlib as _importlib

for _mod in _SCRAPER_MODULES:
    try:
        _importlib.import_module(f"scrapers.{_mod}")
    except ImportError as e:
        logger.error(
            "Could not import scrapers.%s — its (county, type) pair(s) will be "
            "MISSING from this run: %s", _mod, e,
        )

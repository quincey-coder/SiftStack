"""OpenWeb Ninja Realtime Zillow Data ``/search`` client.

Replaces the retired ``similar-sale-homes`` endpoint (hard 404 since ~2026-07)
with the still-live ``/search`` endpoint, plus the workarounds needed to make it
usable for comping.

API contract gotchas (re-verified live against Central Texas, 2026-08-11):

- ``home_status`` must be the exact enum ``FOR_SALE`` / ``FOR_RENT`` /
  ``RECENTLY_SOLD``. ``SOLD`` returns HTTP 400 naming the valid set.
- Each search returns AT MOST 41 rows, so a bare RECENTLY_SOLD query reaches
  back only a few weeks in an active ZIP. Deeper history requires partitioning:
  ``min_price`` / ``max_price`` ARE honored (they come back in the echoed
  ``parameters``), while unsupported names like ``sold_in_last`` are silently
  DROPPED — never assume a filter applied, check the echo. ``search()`` warns on
  any param the server did not echo.
- ``location`` wants a human string (``"Austin, TX 78723"``). A bare ZIP echoes
  fine but is not a reliable locator.
- ``dateSold`` is epoch milliseconds; ``soldPrice`` is a display string
  ("$92,500") so use ``unformattedPrice``.
- ``homeType`` can be LOT for a distressed house sold at land value, and new
  construction may carry no sqft/beds. Verify anything surprising against the
  county card before using it as a comp.

**An empty ``data`` array can be a TRANSIENT failure, not "no sales."** Observed
live: four Texas RECENTLY_SOLD queries returned ``status: OK`` with zero rows,
then the identical calls returned the full 41 rows minutes later. Accepting that
silently is how a comp run reports "0 comparable sales" for an active market —
the same silent-zero shape as the Aug-5 tax-schema incident. ``search()``
therefore RETRIES an empty-but-OK response and logs a warning when it stays
empty, so a genuine zero is distinguishable from a swallowed one.

Price bands are CENTRAL TEXAS calibrated. Measured on 78723 (2026-08-11):
$1-150K -> 8 rows, $150-250K -> 15, $250-350K -> 23, $350-450K -> 38,
$450-600K -> 41 (saturated), $600K+ -> 41 (saturated). The upstream Knoxville
band set topped out at "$420K and up", which in Austin is one saturated bucket
holding most of the market, so it needed several recursive splits per pull.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import requests

import config

logger = logging.getLogger(__name__)

API_BASE = "https://api.openwebninja.com/realtime-zillow-data"
SEARCH_ENDPOINT = f"{API_BASE}/search"

RESULT_CAP = 41           # hard per-search cap observed on /search
MIN_BAND_WIDTH = 4000     # stop splitting price bands narrower than this
REQUEST_DELAY = 1.0       # between band calls
MAX_RETRIES = 3
EMPTY_RETRY_DELAY = 3.0   # backoff when a 200 comes back with zero rows

# Central Texas (Travis / Bell / Williamson) sold-price bands. Any band that
# comes back saturated (>= RESULT_CAP) is split in half recursively, so these
# only need to be close — but starting close saves real API calls per pull.
DEFAULT_BANDS = [
    (1_000, 150_000),
    (150_001, 225_000),
    (225_001, 280_000),
    (280_001, 330_000),
    (330_001, 380_000),
    (380_001, 430_000),
    (430_001, 480_000),
    (480_001, 540_000),
    (540_001, 620_000),
    (620_001, 750_000),
    (750_001, 1_000_000),
    (1_000_001, 10_000_000),
]


@dataclass
class MarketListing:
    """One normalized /search result (sold or active)."""
    zpid: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    latitude: float = 0.0
    longitude: float = 0.0
    beds: int = 0
    baths: float = 0.0
    sqft: int = 0
    home_type: str = ""
    home_status: str = ""
    price: float = 0.0          # sold price for sold, list price for active.
                                # 0 in non-disclosure states — see module docstring.
    sold_date: str = ""         # YYYY-MM-DD, empty for active
    zestimate: float = 0.0
    tax_assessed_value: float = 0.0
    rent_zestimate: float = 0.0
    days_on_zillow: int = 0
    lot_acres: float = 0.0
    detail_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def ppsf(self) -> float:
        return round(self.price / self.sqft, 2) if self.sqft else 0.0


def _num(value) -> float:
    if isinstance(value, str):
        value = "".join(c for c in value if c.isdigit() or c == ".") or 0
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sold_date(item: dict) -> str:
    v = item.get("dateSold")
    if isinstance(v, (int, float)) and v:
        return datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    return str(v or "")[:10]


def normalize(item: dict) -> MarketListing:
    lot_acres = 0.0
    lot_val = item.get("lotAreaValue")
    if lot_val:
        unit = (item.get("lotAreaUnit") or "").lower()
        lot_acres = _num(lot_val) if "acre" in unit else _num(lot_val) / 43560
    return MarketListing(
        zpid=str(item.get("zpid") or ""),
        address=item.get("streetAddress") or item.get("address") or "",
        city=item.get("city") or item.get("addressCity") or "",
        state=item.get("state") or item.get("addressState") or "",
        zip_code=str(item.get("zipcode") or item.get("addressZipcode") or ""),
        latitude=_num(item.get("latitude")),
        longitude=_num(item.get("longitude")),
        beds=int(_num(item.get("beds") or item.get("bedrooms"))),
        baths=_num(item.get("baths") or item.get("bathrooms")),
        sqft=int(_num(item.get("livingArea") or item.get("area"))),
        home_type=item.get("homeType") or "",
        home_status=item.get("homeStatus") or "",
        price=_num(item.get("unformattedPrice") or item.get("price")
                   or (item.get("hdpData", {}).get("homeInfo", {}) or {}).get("price")),
        sold_date=_sold_date(item),
        zestimate=_num(item.get("zestimate")),
        tax_assessed_value=_num(item.get("taxAssessedValue")),
        rent_zestimate=_num(item.get("rentZestimate")),
        days_on_zillow=int(_num(item.get("daysOnZillow"))),
        lot_acres=round(lot_acres, 3),
        detail_url=item.get("detailUrl") or "",
        raw=item,
    )


class ZillowMarketAPI:
    """Thin client over /search with the band-partition workaround built in."""

    def __init__(self, api_key: str = ""):
        self.api_key = api_key or config.OPENWEBNINJA_API_KEY
        if not self.api_key:
            raise ValueError("OPENWEBNINJA_API_KEY is not configured")
        self.empty_responses = 0   # transient empties survived; surfaced by callers

    def search(self, location: str, home_status: str = "FOR_SALE", **params) -> list[dict]:
        """One /search call, with param-echo validation and empty-response retry."""
        query = {"location": location, "home_status": home_status, **params}
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.get(SEARCH_ENDPOINT, params=query,
                                    headers={"x-api-key": self.api_key}, timeout=60)
                resp.raise_for_status()
                body = resp.json()
                echoed = body.get("parameters") or {}
                dropped = [k for k in params if k not in echoed]
                if dropped:
                    # A silently-dropped filter means the result set is NOT what
                    # the caller asked for; that must never pass unremarked.
                    logger.warning("/search silently ignored params %s (echoed: %s)",
                                   dropped, sorted(echoed))
                data = body.get("data") or []
                if data or attempt == MAX_RETRIES:
                    if not data:
                        self.empty_responses += 1
                        logger.warning(
                            "/search returned 0 rows after %d attempts for %s %s %s — "
                            "treat as UNCONFIRMED, not proven-empty",
                            MAX_RETRIES, location, home_status, params or "")
                    return data
                logger.info("/search returned an empty 200 for %s %s %s — retrying (%d/%d)",
                            location, home_status, params or "", attempt, MAX_RETRIES)
                time.sleep(EMPTY_RETRY_DELAY * attempt)
            except (requests.RequestException, ValueError) as exc:
                logger.warning("/search error: %s (attempt %d/%d)", exc, attempt, MAX_RETRIES)
                time.sleep(attempt)
        return []

    def pull_active(self, location: str, houses_only: bool = True) -> list[MarketListing]:
        items = [normalize(i) for i in self.search(location, "FOR_SALE")]
        if houses_only:
            items = [i for i in items if i.home_type in ("SINGLE_FAMILY", "")]
        return items

    def pull_sold(self, location: str, months_back: int = 12,
                  houses_only: bool = True,
                  bands: list[tuple[int, int]] | None = None) -> list[MarketListing]:
        """Pull sold history via adaptive price-band partitioning.

        A single RECENTLY_SOLD search caps at 41 rows; banding by price and
        splitting saturated bands recovers roughly 2-3 years of solds in a
        typical Central Texas ZIP.
        """
        collected: dict[str, dict] = {}
        self.empty_responses = 0

        def pull_band(lo: int, hi: int, depth: int = 0) -> None:
            data = self.search(location, "RECENTLY_SOLD", min_price=lo, max_price=hi)
            logger.debug("%sband %d-%d: %d rows", "  " * depth, lo, hi, len(data))
            if len(data) >= RESULT_CAP and hi - lo > MIN_BAND_WIDTH:
                mid = (lo + hi) // 2
                pull_band(lo, mid, depth + 1)
                pull_band(mid + 1, hi, depth + 1)
            else:
                for item in data:
                    collected[str(item.get("zpid"))] = item
            time.sleep(REQUEST_DELAY)

        for lo, hi in (bands or DEFAULT_BANDS):
            pull_band(lo, hi)

        cutoff = (datetime.now() - timedelta(days=months_back * 30)).strftime("%Y-%m-%d")
        listings = [normalize(i) for i in collected.values()]
        listings = [l for l in listings if l.sold_date >= cutoff]
        if houses_only:
            listings = [l for l in listings if l.home_type == "SINGLE_FAMILY"]
        listings.sort(key=lambda l: l.sold_date, reverse=True)

        if self.empty_responses:
            logger.warning(
                "%d price band(s) returned an unconfirmed empty result for %s — the "
                "sold set may be incomplete; re-run before trusting a thin comp count",
                self.empty_responses, location)

        # NON-DISCLOSURE GUARD. In Texas (and UT/WY/NM/ID/MT/ND/AK/KS/MS/LA/MO)
        # sale prices are not public record, so Zillow publishes sold LISTINGS
        # with no sold PRICE. Without this check the caller just sees an empty
        # comp list and blames the API. Say the real reason out loud.
        priced = sum(1 for l in listings if l.price)
        if listings and not priced:
            logger.error(
                "Pulled %d sold listing(s) for %s but NONE carry a sale price — this is "
                "the expected non-disclosure-state result (TX sale prices are not public "
                "record). Sold-price comping is not available here; use the CAD/Zestimate "
                "triangulation path instead.", len(listings), location)
        elif listings and priced < len(listings):
            logger.warning("Only %d of %d sold listings for %s carry a sale price",
                           priced, len(listings), location)

        logger.info("Pulled %d sold listings for %s (last %d months), %d priced",
                    len(listings), location, months_back, priced)
        return listings


# ── Boundary filters ──────────────────────────────────────────────────

def filter_bbox(listings: list[MarketListing], lat_min: float, lat_max: float,
                lon_min: float, lon_max: float) -> list[MarketListing]:
    return [l for l in listings
            if lat_min <= l.latitude <= lat_max and lon_min <= l.longitude <= lon_max]


def filter_streets(listings: list[MarketListing], street_pattern) -> list[MarketListing]:
    """Keep listings whose street matches a compiled regex (drawn-boundary proxy)."""
    return [l for l in listings if street_pattern.search(l.address)]

"""CAD + Zestimate price triangulation — comping for NON-DISCLOSURE states (Texas).

Texas does not record sale prices. Verified live 2026-08-11 against OpenWeb Ninja
``/search``: **0 of 41** Austin/Killeen/Round Rock RECENTLY_SOLD rows carried a
sale price, against 41 of 41 in Knoxville TN, Atlanta GA and Phoenix AZ. TX
*list* prices come back 41/41, so this is disclosure law, not an API problem.
``comp_analyzer.fetch_comparable_sales`` therefore cannot work in our markets and
says so; THIS module is the replacement path.

Implements the 9-step framework in the ``real-estate-comping`` skill's
``non-disclosure-prompt.md``, restricted to what our data can actually support.

WHAT THE API GIVES US (measured, 78723):
  RECENTLY_SOLD -> physical characteristics + ``taxAssessedValue`` (37/41) +
                   ``zestimate`` (35/41), and NO price.
  FOR_SALE      -> list price (41/41) + DOM (41/41) + ``taxAssessedValue`` (36/41).
  There is NO ``PENDING`` status (the enum is FOR_SALE / FOR_RENT /
  RECENTLY_SOLD), so the skill's Method A cannot be run against pendings; it is
  used on ACTIVES to convert asking prices into expected sale prices.

THE SOLVER
  1. Method A (LLP + DOM) on ACTIVES turns each asking price into an expected
     sale price: <7 days -> 101%, 7-30 -> 98.5%, >30 -> 92.5% of list.
  2. That yields the MARKET RATIO for the micro-market:
         ratio = median(expected_sale / taxAssessedValue) over actives
     This is the skill's Method C anchor ("homes listing at 1.2x tax value"),
     but calibrated off expected SALE prices rather than raw asking prices, so
     it does not inherit the seller's optimism.
  3. Each SOLD comp then gets an Estimated Sold Price by two INDEPENDENT methods:
         ESP_assessed  = taxAssessedValue * market_ratio
         ESP_zestimate = zestimate
     The skill requires >= 2 methods. Where both exist the spread between them
     IS the confidence signal; a single method is always LOW confidence.
  4. Two-Bucket ARV off the derived PPSF, then the ceiling test and a range.

Method B (deed-of-trust loan reverse-math) is NOT implemented: it needs the
recorded loan amount from County Clerk deed records keyed to the sale date, which
we do not currently pull. It is the highest-value future addition here, because
it is the only method anchored to a real recorded dollar figure.

HONESTY CONSTRAINTS BAKED IN
  * Every ESP is labelled with the methods that produced it and their spread.
    Nothing is presented as a known sale price, because none of it is.
  * Condition bucketing is a PPSF-percentile PROXY, not verified condition — we
    have no photos or remarks. It is labelled ``ppsf_proxy`` so a reader never
    mistakes it for a renovation finding.
  * ARV is a RANGE. The skill mandates it for non-disclosure markets and the
    error bars here are genuinely wider than a disclosure state's.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

import config

logger = logging.getLogger(__name__)

# ── Method A: DOM -> fraction of last list price (skill's table) ──────
# <7 days: sold at or above ask; 7-30: 97-100%; 30-90+: 90-95%. Midpoints used.
DOM_FAST_DAYS = 7
DOM_NORMAL_DAYS = 30
DOM_FAST_FACTOR = 1.01
DOM_NORMAL_FACTOR = 0.985
DOM_SLOW_FACTOR = 0.925

# Comp-set tightening (skill step 2). GLA +/-100-250 sqft; we take the wider end
# and widen further only when the pocket is too thin to compute anything.
GLA_TOLERANCE = 250
GLA_TOLERANCE_WIDE = 500
MAX_SOLD_AGE_DAYS = 180          # skill prefers <=90; 180 is the widen-to limit
MIN_COMPS = 3
MIN_ACTIVES_FOR_RATIO = 4        # below this the market ratio is not trustworthy

# Confidence thresholds on the disagreement between the two ESP methods.
SPREAD_HIGH = 0.10
SPREAD_MEDIUM = 0.20

# Step 4 ceiling test / step 9 sentiment adjustment.
STALE_DOM_DAYS = 60
STALE_SHARE_TRIGGER = 0.40       # share of actives stale before we haircut
SENTIMENT_HAIRCUT = 0.04         # skill says 3-5%
ARV_BAND = 0.06                  # skill says +/-5-7%

# A ratio outside this range means the assessed value is not comparable to
# market (new construction, exemption weirdness, a bad CAD record).
RATIO_SANITY_MIN = 0.5
RATIO_SANITY_MAX = 4.0


def dom_factor(days_on_market: float | None) -> float:
    """Method A: fraction of last list price a home actually sells for."""
    if days_on_market is None or days_on_market < 0:
        return DOM_NORMAL_FACTOR
    if days_on_market < DOM_FAST_DAYS:
        return DOM_FAST_FACTOR
    if days_on_market <= DOM_NORMAL_DAYS:
        return DOM_NORMAL_FACTOR
    return DOM_SLOW_FACTOR


@dataclass
class MarketRatio:
    """Expected-sale-price to tax-assessed-value ratio for a micro-market."""
    ratio: float = 0.0
    n: int = 0
    p25: float = 0.0
    p75: float = 0.0
    usable: bool = False
    note: str = ""


@dataclass
class TriangulatedComp:
    address: str = ""
    city: str = ""
    zip_code: str = ""
    sold_date: str = ""
    sqft: int = 0
    beds: int = 0
    baths: float = 0.0
    year_built: int = 0
    home_type: str = ""
    tax_assessed_value: float = 0.0
    zestimate: float = 0.0
    esp: float = 0.0                      # Estimated Sold Price
    esp_methods: list[str] = field(default_factory=list)
    esp_spread: float = 0.0               # relative disagreement between methods
    confidence: str = "low"
    ppsf: float = 0.0
    bucket: str = "unknown"               # renovated | unrenovated (ppsf proxy)

    @property
    def derivation(self) -> str:
        """Human-readable provenance, per the skill's output requirement."""
        if not self.esp_methods:
            return "no method available"
        return f"${self.esp:,.0f} via {' + '.join(self.esp_methods)}"


@dataclass
class TriangulationResult:
    subject_sqft: int = 0
    arv_low: float = 0.0
    arv_point: float = 0.0
    arv_high: float = 0.0
    ppsf_renovated: float = 0.0
    ppsf_unrenovated: float = 0.0
    market_premium_pct: float = 0.0
    market_ratio: MarketRatio = field(default_factory=MarketRatio)
    comps: list[TriangulatedComp] = field(default_factory=list)
    actives_analyzed: int = 0
    stale_share: float = 0.0
    ceiling: float = 0.0
    sentiment_applied: bool = False
    confidence: str = "low"
    warnings: list[str] = field(default_factory=list)
    disclaimer: str = (
        "Sold prices are ESTIMATED from tax-assessed value, a market ratio derived "
        "from active list prices and DOM, and Zestimate. Texas is a non-disclosure "
        "state: no actual sale price is public. Confirm against MLS sold data during "
        "the option period before relying on this ARV."
    )


# ── Step 3: the solver ────────────────────────────────────────────────

def derive_market_ratio(actives) -> MarketRatio:
    """Median expected-sale-price to tax-assessed-value ratio across actives.

    Each active's ASKING price is first converted to an EXPECTED SALE price via
    the DOM table (Method A), so the ratio is not inflated by seller optimism.
    """
    ratios = []
    for listing in actives:
        assessed = getattr(listing, "tax_assessed_value", 0.0)
        price = getattr(listing, "price", 0.0)
        if not assessed or not price:
            continue
        expected = price * dom_factor(getattr(listing, "days_on_zillow", None))
        r = expected / assessed
        if RATIO_SANITY_MIN <= r <= RATIO_SANITY_MAX:
            ratios.append(r)

    if len(ratios) < MIN_ACTIVES_FOR_RATIO:
        return MarketRatio(
            n=len(ratios), usable=False,
            note=(f"only {len(ratios)} active listing(s) carried both a list price and a "
                  f"tax-assessed value (need {MIN_ACTIVES_FOR_RATIO}) — market ratio not "
                  f"trustworthy; ESP falls back to Zestimate alone"),
        )

    ratios.sort()
    return MarketRatio(
        ratio=statistics.median(ratios),
        n=len(ratios),
        p25=ratios[len(ratios) // 4],
        p75=ratios[3 * len(ratios) // 4],
        usable=True,
        note=f"median of {len(ratios)} actives (DOM-adjusted list / assessed)",
    )


def estimate_sold_price(listing, ratio: MarketRatio) -> tuple[float, list[str], float]:
    """Derive one comp's ESP. Returns (esp, methods_used, relative_spread)."""
    estimates: list[tuple[str, float]] = []

    assessed = getattr(listing, "tax_assessed_value", 0.0)
    if assessed and ratio.usable:
        estimates.append(("assessed x market ratio", assessed * ratio.ratio))

    zest = getattr(listing, "zestimate", 0.0)
    if zest:
        estimates.append(("Zestimate", zest))

    if not estimates:
        return 0.0, [], 0.0

    values = [v for _, v in estimates]
    esp = statistics.mean(values)
    spread = (max(values) - min(values)) / esp if len(values) > 1 and esp else 0.0
    return esp, [m for m, _ in estimates], spread


def _confidence_for(methods: list[str], spread: float) -> str:
    if len(methods) < 2:
        return "low"
    if spread <= SPREAD_HIGH:
        return "high"
    if spread <= SPREAD_MEDIUM:
        return "medium"
    return "low"


# ── Step 2: comp-set tightening ───────────────────────────────────────

def _addr_key(address: str) -> str:
    return "".join(ch for ch in (address or "").lower() if ch.isalnum())


def select_comps(sold, subject_sqft: int, tolerance: int = GLA_TOLERANCE,
                 subject_address: str = "") -> list:
    """Keep sold listings physically similar to the subject (skill step 2).

    Physical match FIRST, price later — that is the whole point of the
    non-disclosure ordering: find the right houses, then solve for price.

    The subject itself is excluded: a recently-sold subject comes back in its own
    pull and would otherwise anchor the ARV to its own Zestimate (self-comping).
    """
    subject_key = _addr_key(subject_address)
    out = []
    for l in sold:
        if subject_key and _addr_key(getattr(l, "address", "")).startswith(subject_key):
            continue
        if subject_sqft and not (
            getattr(l, "sqft", 0) and abs(l.sqft - subject_sqft) <= tolerance
        ):
            continue
        out.append(l)
    return out


# ── Step 7: Two-Bucket ────────────────────────────────────────────────

def bucket_comps(comps: list[TriangulatedComp]) -> tuple[float, float, float]:
    """Split into unrenovated/renovated by PPSF and return (ppsf_A, ppsf_B, premium%).

    IMPORTANT: this is a PPSF-percentile PROXY, not a verified condition call.
    We have no listing photos or remarks, so a high PPSF is only *evidence* of
    renovation. Every caller must label it as such.

    Comps whose two ESP methods disagree by more than ``SPREAD_MEDIUM`` are
    EXCLUDED from the medians but still rendered in the comp table (labelled
    ``excluded``). When assessed-times-ratio and Zestimate differ by 50%, that
    comp is not evidence of anything and must not move the ARV.
    """
    priced = [c for c in comps if c.ppsf > 0]
    usable = [c for c in priced if c.esp_spread <= SPREAD_MEDIUM]
    for c in priced:
        if c.esp_spread > SPREAD_MEDIUM:
            c.bucket = "excluded"
    priced = usable
    if len(priced) < 2:
        return 0.0, 0.0, 0.0

    priced.sort(key=lambda c: c.ppsf)
    mid = len(priced) // 2
    lower, upper = priced[:mid], priced[mid:]

    for c in lower:
        c.bucket = "unrenovated"
    for c in upper:
        c.bucket = "renovated"

    ppsf_a = statistics.median([c.ppsf for c in lower])
    ppsf_b = statistics.median([c.ppsf for c in upper])
    premium = ((ppsf_b - ppsf_a) / ppsf_a * 100) if ppsf_a else 0.0
    return ppsf_a, ppsf_b, premium


# ── Step 4: ceiling test ──────────────────────────────────────────────

def ceiling_test(actives, subject_sqft: int = 0) -> tuple[float, float]:
    """Return (ceiling_price, stale_share).

    'If fully renovated homes are sitting Active at $500k for 60+ days, your ARV
    cannot be $500k.'

    Two corrections over the naive reading, both found on live 78723 data:

    1. SIZE-MATCH. Comparing the subject to whatever the smallest condo in the
       ZIP is asking makes the test fire on nearly every run, and a warning that
       always fires carries no information.
    2. RENOVATED TIER ONLY. The skill's sentence is about *fully renovated*
       homes. Taking the cheapest stale active inverts the test: on this ZIP that
       picked a 1,491 sqft single-family asking $250K at 90 DOM — stale because
       it is a wreck (exactly the kind of property this pipeline hunts), not
       because $250K is a market ceiling. We therefore restrict to the upper half
       by PPSF (the renovated proxy) and take the LOWEST ask among those: the
       cheapest good house that still will not sell is the real ceiling. On this
       ZIP that moves the ceiling from a meaningless $250K to $439K.
    """
    priced = [l for l in actives if getattr(l, "price", 0)]
    stale = [l for l in priced if (getattr(l, "days_on_zillow", 0) or 0) >= STALE_DOM_DAYS]
    share = len(stale) / len(priced) if priced else 0.0

    if subject_sqft:
        stale = [l for l in stale
                 if getattr(l, "sqft", 0) and abs(l.sqft - subject_sqft) <= GLA_TOLERANCE_WIDE]

    # Restrict to the renovated tier by PPSF before taking the floor.
    with_ppsf = [l for l in stale if getattr(l, "sqft", 0)]
    if len(with_ppsf) >= 2:
        with_ppsf.sort(key=lambda l: l.price / l.sqft)
        stale = with_ppsf[len(with_ppsf) // 2:]

    # No size-matched stale listing means the market has not refused anything at
    # this size — report no ceiling rather than a misleading one.
    return min((l.price for l in stale), default=0.0), share


# ── Step 9: assembly ──────────────────────────────────────────────────

def triangulate(subject_sqft: int, sold, actives,
                subject_address: str = "") -> TriangulationResult:
    """Run the full non-disclosure framework over one micro-market.

    ``sold`` and ``actives`` are ``zillow_market_api.MarketListing`` lists for the
    same area. Degrades with a stated reason rather than inventing a number.
    """
    result = TriangulationResult(subject_sqft=subject_sqft)
    result.actives_analyzed = len(actives)

    result.market_ratio = derive_market_ratio(actives)
    if not result.market_ratio.usable:
        result.warnings.append(result.market_ratio.note)

    selected = select_comps(sold, subject_sqft, subject_address=subject_address)
    if len(selected) < MIN_COMPS and subject_sqft:
        widened = select_comps(sold, subject_sqft, GLA_TOLERANCE_WIDE,
                               subject_address=subject_address)
        if len(widened) > len(selected):
            result.warnings.append(
                f"only {len(selected)} comp(s) within +/-{GLA_TOLERANCE} sqft — widened to "
                f"+/-{GLA_TOLERANCE_WIDE} sqft ({len(widened)} comps); size normalization is "
                f"less reliable across that gap")
            selected = widened

    for listing in selected:
        esp, methods, spread = estimate_sold_price(listing, result.market_ratio)
        if not esp:
            continue
        comp = TriangulatedComp(
            address=listing.address, city=listing.city, zip_code=listing.zip_code,
            sold_date=listing.sold_date, sqft=listing.sqft, beds=listing.beds,
            baths=listing.baths, home_type=listing.home_type,
            year_built=int(float(listing.raw.get("yearBuilt") or 0) or 0),
            tax_assessed_value=listing.tax_assessed_value, zestimate=listing.zestimate,
            esp=esp, esp_methods=methods, esp_spread=spread,
            confidence=_confidence_for(methods, spread),
        )
        comp.ppsf = round(esp / comp.sqft, 2) if comp.sqft else 0.0
        result.comps.append(comp)

    if not result.comps:
        result.warnings.append(
            "no comp could be priced — neither a usable market ratio nor a Zestimate was "
            "available on any physically-matching sold listing")
        return result

    result.ppsf_unrenovated, result.ppsf_renovated, result.market_premium_pct = \
        bucket_comps(result.comps)

    # Skill's guard: a thin spread means the unrenovated bucket is overestimated.
    if 0 < result.market_premium_pct < 10:
        result.warnings.append(
            f"renovated/unrenovated spread is only {result.market_premium_pct:.1f}% — under "
            f"10% usually means the unrenovated side is overestimated (sellers list high and "
            f"take unseen lower offers)")

    excluded = sum(1 for c in result.comps if c.bucket == "excluded")
    if excluded:
        result.warnings.append(
            f"{excluded} of {len(result.comps)} comps excluded from the ARV: their two ESP "
            f"methods disagreed by more than {SPREAD_MEDIUM:.0%} (still shown in the table)")

    result.ceiling, result.stale_share = ceiling_test(actives, subject_sqft)

    base_ppsf = result.ppsf_renovated or result.ppsf_unrenovated
    if result.stale_share >= STALE_SHARE_TRIGGER:
        base_ppsf *= (1 - SENTIMENT_HAIRCUT)
        result.sentiment_applied = True
        result.warnings.append(
            f"{result.stale_share:.0%} of actives have sat {STALE_DOM_DAYS}+ days — applied a "
            f"{SENTIMENT_HAIRCUT:.0%} sentiment haircut")

    if subject_sqft and base_ppsf:
        result.arv_point = base_ppsf * subject_sqft
        result.arv_low = result.arv_point * (1 - ARV_BAND)
        result.arv_high = result.arv_point * (1 + ARV_BAND)

        if result.ceiling and result.arv_point > result.ceiling:
            result.warnings.append(
                f"CEILING TEST FAILED: ARV ${result.arv_point:,.0f} exceeds ${result.ceiling:,.0f}, "
                f"the cheapest RENOVATED-TIER size-matched listing that has sat "
                f"{STALE_DOM_DAYS}+ days unsold. The market has already declined to pay that "
                f"much for a comparable finished house. Treat ${result.ceiling:,.0f} as the "
                f"practical ceiling")
    else:
        result.warnings.append("no subject square footage — cannot convert PPSF into an ARV")

    high = sum(1 for c in result.comps if c.confidence == "high")
    if len(result.comps) >= MIN_COMPS and high >= 2 and result.market_ratio.usable:
        result.confidence = "medium"     # never "high": no sale price is ever confirmed
    result.warnings.append(
        f"condition buckets are a PPSF-percentile PROXY ({len(result.comps)} comps), not a "
        f"verified renovation finding — no photos or listing remarks were consulted")

    logger.info("Triangulated ARV $%s (range $%s-$%s) from %d comps at %.2fx assessed",
                f"{result.arv_point:,.0f}", f"{result.arv_low:,.0f}",
                f"{result.arv_high:,.0f}", len(result.comps), result.market_ratio.ratio)
    return result


def triangulate_address(city: str, state: str, zip_code: str, subject_sqft: int,
                        months_back: int = 6, api_key: str = "",
                        subject_address: str = "") -> TriangulationResult:
    """Convenience: pull the micro-market from Zillow and triangulate it."""
    from zillow_market_api import ZillowMarketAPI

    api = ZillowMarketAPI(api_key or config.OPENWEBNINJA_API_KEY)
    location = f"{city}, {state} {zip_code}".strip(", ")
    sold = api.pull_sold(location, months_back=months_back)
    actives = api.pull_active(location)
    return triangulate(subject_sqft, sold, actives, subject_address=subject_address)

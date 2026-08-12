"""County assessed/market value resolution — the number DataSift cannot give you.

DataSift's "Enrich Property Information" step fills in a Zestimate, beds, baths,
sqft and sale history from SiftMap. It has no idea what the County Appraisal
District says the property is worth, and a live dump of the upload wizard's 253
drop targets confirmed there is no assessed-value field to upload into either.
So this is ours to carry: ``Assessed Value (County)``.

WHY IT IS A DIFFERENT NUMBER FROM ``estimated_value``
  ``estimated_value`` is the Zestimate — an algorithm's guess at market price.
  The CAD value is the taxing authority's own appraisal, and in Texas it is what
  drives the tax bill that puts these owners in distress in the first place. A
  wide Zestimate-over-CAD gap is a real signal (under-assessed equity); the
  reverse suggests an over-appraisal the owner may be protesting.

WHICH CAD NUMBER
  We store ``totalpropmktvalue`` — the CAD's TOTAL MARKET value — because all
  three counties publish it and it is the like-for-like comparison against a
  Zestimate. Williamson additionally exposes ``totalassessedvalue``, the capped
  TAXABLE basis, which diverges on homesteads (verified live: 23,269 market vs
  18,782 assessed on one WCAD row). That capped figure is deliberately NOT what
  we store, because it is a tax artifact rather than an opinion of value.

SOURCE PRECEDENCE (authoritative first)
  1. County CAD  — Travis / Bell bulk caches, Williamson via the WCAD SODA API.
  2. Zillow ``taxAssessedValue`` — a fallback only, set by ``property_enricher``
     and only when the CAD produced nothing. Travis is the leg that needs it
     most: the Travis roll carries a market value on the DELINQUENT subset
     (measured 99.9% of 7,276) but on 0% of the 462,847 current-roll rows.

Every value records where it came from in ``assessed_source``, so a suspicious
number can always be traced back to the roll that produced it.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Anything outside this band is a bad parse or a non-residential oddity, not a
# house value. Logged and dropped rather than shipped.
MIN_PLAUSIBLE_VALUE = 1_000
MAX_PLAUSIBLE_VALUE = 100_000_000


def _clean_amount(raw) -> str:
    """Normalize a CAD money field to a bare integer string, or ''."""
    if raw is None:
        return ""
    text = re.sub(r"[^0-9.]", "", str(raw))
    if not text:
        return ""
    try:
        value = float(text)
    except ValueError:
        return ""
    if not (MIN_PLAUSIBLE_VALUE <= value <= MAX_PLAUSIBLE_VALUE):
        return ""
    return str(int(value))


def _zip5(notice) -> str:
    return (getattr(notice, "zip", "") or "")[:5]


def _from_travis(notice) -> tuple[str, str]:
    import travis_tax_cache as cache

    rec = None
    if getattr(notice, "parcel_id", ""):
        rec = cache.search_by_parcel(notice.parcel_id)
    if rec is None and notice.address:
        rec = cache.search_by_address(notice.address, _zip5(notice))
    if not rec:
        return "", ""
    return _clean_amount(rec.get("totalpropmktvalue")), "travis_cad"


def _from_bell(notice) -> tuple[str, str]:
    import bell_tax_cache as cache

    rec = None
    if getattr(notice, "parcel_id", ""):
        rec = cache.search_by_parcel(notice.parcel_id)
    if rec is None and notice.address:
        rec = cache.search_by_address(notice.address, _zip5(notice))
    if not rec:
        return "", ""
    return _clean_amount(rec.get("totalpropmktvalue")), "bell_cad"


def _from_williamson(notice) -> tuple[str, str]:
    # Williamson has no bulk cache; the WCAD SODA API already selects
    # totalpropmktvalue and returns it as `value`.
    from cad_lookup import lookup_property_by_address, lookup_property_by_parcel

    rec = None
    if getattr(notice, "parcel_id", ""):
        rec = lookup_property_by_parcel(notice.parcel_id, "Williamson")
    if rec is None and notice.address:
        rec = lookup_property_by_address(notice.address, "Williamson")
    if not rec:
        return "", ""
    return _clean_amount(rec.get("value")), "wcad"


_RESOLVERS = {
    "travis": _from_travis,
    "bell": _from_bell,
    "williamson": _from_williamson,
}


def assessed_from_cad(notice) -> tuple[str, str]:
    """Return (value, source) from the county's own roll, or ('', '')."""
    county = (getattr(notice, "county", "") or "").strip().lower()
    resolver = _RESOLVERS.get(county)
    if not resolver:
        return "", ""
    try:
        return resolver(notice)
    except Exception as exc:
        # A CAD miss must never break enrichment — the Zillow fallback still runs.
        logger.debug("CAD assessed-value lookup failed for %s (%s): %s",
                     getattr(notice, "address", "?"), county, exc)
        return "", ""


def populate_assessed_values(notices, roll_year: str = "") -> dict:
    """Fill assessed_value/assessed_year/assessed_source from the county CAD.

    Runs BEFORE or AFTER Zillow without caring which: the CAD is authoritative
    and overwrites a Zillow fallback, while ``property_enricher`` only sets its
    fallback when the field is still empty.
    """
    stats = {"cad": 0, "missing": 0, "by_source": {}}
    for notice in notices:
        value, source = assessed_from_cad(notice)
        if not value:
            if not getattr(notice, "assessed_value", ""):
                stats["missing"] += 1
            continue
        notice.assessed_value = value
        notice.assessed_source = source
        if roll_year:
            notice.assessed_year = roll_year
        stats["cad"] += 1
        stats["by_source"][source] = stats["by_source"].get(source, 0) + 1

    have = sum(1 for n in notices if getattr(n, "assessed_value", ""))
    logger.info("Assessed value: %d/%d record(s) valued (%d from CAD %s, rest from "
                "the Zillow fallback or unresolved)",
                have, len(notices), stats["cad"], stats["by_source"] or "{}")
    return stats

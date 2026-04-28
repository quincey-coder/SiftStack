"""Look up decedent property addresses from county appraisal district records.

For probate notices, the notice text contains the PR's mailing address but NOT
the decedent's property address. This module searches county appraisal district
databases by the decedent's name to find their property address(es).

Texas CAD portals:
  Travis (TCAD):     https://traviscad.org/propertysearch/
  Bell (BCAD):       https://esearch.bellcad.org/
  Williamson (WCAD): https://search.wcad.org/

Phase 1 stub — CAD lookup implementations come in Phase 4.
"""

import logging
import re

logger = logging.getLogger(__name__)


def _clean_cad_owner_for_display(raw_upper: str) -> str:
    """Convert a raw all-caps CAD owner name into a clean display name.

    `cad_lookup.lookup_property_by_name` returns a naively `.title()`-cased
    owner string ("SCOTT, JAMES A III" → "Scott, James A Iii"), which lowers
    Roman numerals and legal-entity suffixes. Probate flow then writes that
    string into `notice.owner_name`, polluting the marketing CSV.

    This helper:
      1. Strips trailing ETUX/ETVIR/ETAL clauses and spousal `& ...` tails.
      2. Title-cases the result via `travis_texdel_cleaner.title_case`,
         which preserves LLC/III/JR/CO etc.
    """
    if not raw_upper:
        return ""
    from scrapers import travis_texdel_cleaner as _cleaner
    stripped = _cleaner.strip_etux_etal(raw_upper)
    return _cleaner.title_case(stripped)


# ── Property selection logic ──────────────────────────────────────────

RESIDENTIAL_KEYWORDS = {
    "residential", "dwelling", "single-family", "single family",
    "duplex", "condo", "townhouse", "mobile home",
}


def _is_residential(classification: str) -> bool:
    """Check if a property classification indicates residential."""
    lower = classification.lower()
    return any(kw in lower for kw in RESIDENTIAL_KEYWORDS)


def _select_best_property(
    results: list[dict],
    pr_street: str = "",
) -> dict | None:
    """Select the best property match from search results.

    Priority:
    1. Filter to residential properties only
    2. Prefer where property address matches the PR's mailing address (owner-occupied)
    3. Take the first residential result if no mailing match
    """
    residential = [r for r in results if _is_residential(r.get("classification", "Residential"))]

    if not residential:
        residential = results

    if not residential:
        return None

    if len(residential) == 1:
        return residential[0]

    if pr_street:
        pr_norm = re.sub(r"[^a-z0-9]", "", pr_street.lower())
        for prop in residential:
            prop_norm = re.sub(r"[^a-z0-9]", "", prop.get("address", "").lower())
            if pr_norm and prop_norm and (pr_norm in prop_norm or prop_norm in pr_norm):
                logger.info("Property matched PR mailing address: %s", prop["address"])
                return prop

    return residential[0]


# ── Name formatting ───────────────────────────────────────────────────

def _format_name_for_search(name: str) -> str:
    """Convert 'JOHN SMITH' to 'SMITH JOHN' for county assessor search.

    County assessor databases index by "LAST FIRST" format.
    """
    if not name:
        return ""

    clean = re.sub(r"\b(?:JR|SR|II|III|IV|M\.?D\.?)\b\.?", "", name, flags=re.IGNORECASE).strip()

    spouse_match = re.match(r"(\w+)\s+(?:AND|&)\s+\w+\s+(\w+)$", clean, re.IGNORECASE)
    if spouse_match:
        clean = f"{spouse_match.group(1)} {spouse_match.group(2)}"
    else:
        clean = re.sub(r"\s+(?:AND|&)\s+.*", "", clean, flags=re.IGNORECASE).strip()

    clean = re.sub(r"[.,']", "", clean).strip()

    parts = clean.split()
    if len(parts) < 2:
        return clean.upper()

    last = parts[-1]
    firsts = parts[:-1]
    return f"{last} {' '.join(firsts)}".upper()


def _shorten_search_name(formatted_name: str) -> str | None:
    """Produce a shorter 'LAST FIRST' variant by dropping middle names."""
    parts = formatted_name.split()
    if len(parts) <= 2:
        return None
    return f"{parts[0]} {parts[1]}"


def _maiden_name_variant(decedent_name: str) -> str | None:
    """Try the penultimate name as surname for maiden+married name patterns."""
    if not decedent_name:
        return None

    clean = re.sub(r"\b(?:JR|SR|II|III|IV)\b\.?", "", decedent_name, flags=re.IGNORECASE).strip()
    clean = re.sub(r"[.,'']", "", clean).strip()
    parts = clean.split()

    if len(parts) < 4:
        return None

    first = parts[0]
    maiden = parts[-2]
    return f"{maiden} {first}".upper()


# ── Main entry point ──────────────────────────────────────────────────


async def lookup_decedent_properties(notices: list) -> None:
    """Look up property addresses for probate notices with decedent names.

    Uses county CAD APIs/data to find the decedent's property address.
    Currently supports Williamson County (WCAD SODA API).

    Args:
        notices: List of NoticeData objects (probate only, with decedent_name set)
    """
    if not notices:
        return

    candidates = [n for n in notices if n.decedent_name and not n.address]
    if not candidates:
        return

    logger.info("Looking up property addresses for %d probate decedents...", len(candidates))

    found = 0
    failed = 0

    for i, notice in enumerate(candidates):
        search_name = notice.decedent_name.strip()
        if not search_name:
            continue

        logger.debug(
            "[%d/%d] Looking up property for %s (%s County)",
            i + 1, len(candidates), search_name, notice.county,
        )

        try:
            from cad_lookup import lookup_property_by_name
            result = lookup_property_by_name(search_name, notice.county)

            if result and result.get("address"):
                notice.address = result["address"]
                notice.city = result.get("city", "")
                notice.state = "TX"
                notice.zip = result.get("zip", "")
                if result.get("parcel_id"):
                    notice.parcel_id = result["parcel_id"]
                if result.get("value"):
                    try:
                        notice.estimated_value = str(int(float(result["value"])))
                    except (ValueError, TypeError):
                        pass
                # Probate scraper leaves owner_name empty (it captures decedent_name
                # only). The CAD owner-of-record IS our marketing target — typically
                # the heir who took title — so populate owner_name + tax_owner_name
                # when the scraper didn't set them. Strip ETUX/ETAL tails and
                # re-title-case so the suffix uppercasing (LLC/III/etc.) sticks.
                cad_owner_raw = result.get("owner_raw") or (result.get("owner") or "").upper()
                cad_owner_clean = _clean_cad_owner_for_display(cad_owner_raw)
                if cad_owner_clean and not (notice.owner_name or "").strip():
                    notice.owner_name = cad_owner_clean
                    if not (notice.tax_owner_name or "").strip():
                        notice.tax_owner_name = cad_owner_raw
                found += 1
                logger.info(
                    "  Found: %s at %s, %s %s (score: %.2f)",
                    search_name, notice.address, notice.city, notice.zip,
                    result.get("match_score", 0),
                )
            else:
                failed += 1

            # Retry with name variations if first attempt fails
            if not notice.address:
                short_name = _shorten_search_name(_format_name_for_search(search_name))
                if short_name:
                    result = lookup_property_by_name(short_name, notice.county)
                    if result and result.get("address"):
                        notice.address = result["address"]
                        notice.city = result.get("city", "")
                        notice.state = "TX"
                        notice.zip = result.get("zip", "")
                        if result.get("parcel_id"):
                            notice.parcel_id = result["parcel_id"]
                        cad_owner_raw = result.get("owner_raw") or (result.get("owner") or "").upper()
                        cad_owner_clean = _clean_cad_owner_for_display(cad_owner_raw)
                        if cad_owner_clean and not (notice.owner_name or "").strip():
                            notice.owner_name = cad_owner_clean
                            if not (notice.tax_owner_name or "").strip():
                                notice.tax_owner_name = cad_owner_raw
                        found += 1
                        failed -= 1
                        logger.info("  Retry found: %s at %s", search_name, notice.address)

        except Exception as e:
            logger.warning("  Property lookup failed for %s: %s", search_name, e)
            failed += 1

        if (i + 1) % 10 == 0:
            logger.info("Property lookup progress: %d/%d (found=%d, failed=%d)", i + 1, len(candidates), found, failed)

    logger.info(
        "Property lookup complete: %d found, %d not found (of %d total)",
        found, failed, len(candidates),
    )

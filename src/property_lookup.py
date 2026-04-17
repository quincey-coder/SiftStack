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

    Stub — TX CAD property lookup not yet implemented (Phase 4).

    Args:
        notices: List of NoticeData objects (probate only, with decedent_name set)
    """
    if not notices:
        return

    candidates = [n for n in notices if n.decedent_name and not n.address]
    if candidates:
        logger.info(
            "Skipping property lookup for %d probate decedents (TX CAD not yet implemented)",
            len(candidates),
        )

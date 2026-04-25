"""Enrich notices with tax delinquency data from county appraisal districts.

Texas CAD portals:
  Travis (TCAD):     https://traviscad.org/propertysearch/
  Bell (BCAD):       https://esearch.bellcad.org/
  Williamson (WCAD): https://search.wcad.org/

Phase 1 stub — CAD lookup implementations come in Phase 4.
"""

import logging
import re

from notice_parser import NoticeData

logger = logging.getLogger(__name__)

# Business entity pattern — imported from shared config
import config as _cfg
_BUSINESS_RE = _cfg.BUSINESS_RE


def detect_deceased_indicator(owner_name: str) -> str:
    """Detect deceased-owner indicators from a county tax API owner name.

    Returns one of: "life_estate", "personal_rep", "care_of", "et_al",
    "trustee", or "" (no indicator detected).

    Priority order reflects confidence level (highest first).
    """
    if not owner_name or not owner_name.strip():
        return ""

    upper = owner_name.upper()

    # 1. Personal Representative — strongest signal (definite estate)
    if "PERSONAL REPRESENTATIVE" in upper or "PERSONAL REP" in upper:
        return "personal_rep"

    # 2. Life Estate — very strong signal (elderly/deceased holder)
    if "LIFE EST" in upper:
        return "life_estate"

    # 3. Care-of (%) — strong signal for deceased/incapacitated
    if "%" in owner_name:
        return "care_of"

    # 4. Et Al — moderate signal (multiple parties, often heirs)
    if re.search(r"\bET\s+AL\b", upper):
        return "et_al"

    # 5. Trustee — weakest signal; skip business entities
    if re.search(r"\bTRUSTEE\b", upper):
        if not _BUSINESS_RE.search(upper):
            return "trustee"

    return ""


def _name_match_score(search_name: str, api_owner: str) -> float:
    """Score how well a search name matches an API owner name.

    Returns 0.0-1.0 based on token overlap.
    """
    search_tokens = set(search_name.upper().split())
    api_tokens = set(api_owner.upper().split())
    noise = {"&", "JR", "SR", "II", "III", "IV", "THE", "ESTATE", "OF"}
    search_tokens -= noise
    api_tokens -= noise
    if not search_tokens or not api_tokens:
        return 0.0
    overlap = search_tokens & api_tokens
    return len(overlap) / max(len(search_tokens), len(api_tokens))


def _clean_name_for_search(name: str) -> list[str]:
    """Generate search variations for a name.

    Returns a list of search strings to try in order.
    """
    suffixes_re = re.compile(r"\b(JR|SR|II|III|IV|ESQ)\b\.?", re.IGNORECASE)
    clean = suffixes_re.sub("", name).strip()
    clean = re.sub(r"\s+", " ", clean)
    clean = re.sub(r",\s*$", "", clean)

    parts = clean.split()
    if not parts:
        return [name]

    searches = []
    searches.append(name.strip())

    if clean != name.strip():
        searches.append(clean)

    if len(parts) >= 2:
        searches.append(f"{parts[-1]} {parts[0]}")

    if len(parts) >= 3:
        searches.append(f"{parts[-1]} {' '.join(parts[:-1])}")

    if len(parts) >= 3:
        searches.append(f"{parts[0]} {parts[-1]}")

    return list(dict.fromkeys(searches))


def _add_flag(notice: NoticeData, flag: str) -> None:
    """Append a missing_data_flags token idempotently."""
    existing = [p for p in (notice.missing_data_flags or "").split("|") if p]
    if flag not in existing:
        notice.missing_data_flags = "|".join(existing + [flag])


def _apply_cad_result(notice: NoticeData, result: dict) -> None:
    """Copy CAD lookup fields onto a NoticeData without clobbering existing data."""
    owner_raw = (result.get("owner_raw") or "").strip()
    if owner_raw and not notice.tax_owner_name:
        notice.tax_owner_name = owner_raw
    if result.get("parcel_id") and not notice.parcel_id:
        notice.parcel_id = result["parcel_id"]
    if result.get("property_type") and not notice.property_type:
        notice.property_type = result["property_type"]
    if result.get("value") and not notice.estimated_value:
        notice.estimated_value = result["value"]
    if result.get("delinquent_total") and not notice.tax_delinquent_amount:
        notice.tax_delinquent_amount = result["delinquent_total"]
    if result.get("years_delinquent") and not notice.tax_delinquent_years:
        notice.tax_delinquent_years = result["years_delinquent"]


def lookup_parcel_addresses(notices: list[NoticeData]) -> None:
    """Fill property situs from CAD when the scraper left it blank.

    Runs before Smarty so downstream address validation sees the cleaner
    county-record situs. Handles Travis + Williamson; Bell is flagged.
    """
    from cad_lookup import lookup_property_by_address
    from collections import Counter

    counts = Counter()
    for n in notices:
        # Only fill situs when address is blank — never overwrite scraper data.
        if n.address.strip():
            continue
        if not n.parcel_id.strip():
            continue
        county = (n.county or "").strip().lower()
        if county == "bell":
            _add_flag(n, "bcad_not_implemented")
            counts["bell_flagged"] += 1
            continue
        # Travis + Williamson parcel→situs requires the opposite direction
        # (address is what we use as the key today). If we have parcel only,
        # no fill for now — leave for a future CAD dataset addition.
        counts["parcel_only_skipped"] += 1

    if counts:
        logger.info("Parcel situs backfill: %s", dict(counts))


def enrich_tax_delinquency(notices: list[NoticeData]) -> None:
    """Enrich notices with tax delinquency data + owner fallback via CAD lookup.

    TCAD: pulls authoritative owner, parcel ID, delinquent amount/years, value.
    WCAD: pulls authoritative owner, parcel ID, property type, value (no tax).
    BCAD: not implemented — records tagged with ``bcad_not_implemented`` flag.
    """
    from cad_lookup import lookup_property_by_address
    from collections import Counter

    counts = Counter()
    for n in notices:
        if not n.address.strip():
            continue
        county = (n.county or "").strip().lower()

        if county == "bell":
            _add_flag(n, "bcad_not_implemented")
            counts["bell_flagged"] += 1
            continue
        if county not in ("travis", "williamson"):
            continue

        try:
            result = lookup_property_by_address(n.address, n.county, zip_code=n.zip)
        except Exception as e:
            logger.debug("CAD lookup failed for %s: %s", n.address, e)
            counts[f"{county}_error"] += 1
            continue

        if not result:
            counts[f"{county}_miss"] += 1
            continue

        _apply_cad_result(n, result)
        counts[f"{county}_hit"] += 1

        # Fire deceased-indicator detection against the fresh CAD owner name —
        # catches LIFE ESTATE / PERSONAL REP / ET AL / TRUSTEE patterns the
        # scraper may have missed.
        indicator = detect_deceased_indicator(n.tax_owner_name)
        if indicator:
            _add_flag(n, f"cad_{indicator}")

    if counts:
        logger.info("CAD enrichment: %s", dict(counts))

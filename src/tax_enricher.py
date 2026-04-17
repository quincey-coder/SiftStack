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


def lookup_parcel_addresses(notices: list[NoticeData]) -> None:
    """Replace OCR addresses with official county addresses from parcel IDs.

    Stub — TX CAD lookup not yet implemented (Phase 4).
    """
    candidates = [n for n in notices if n.parcel_id.strip()]
    if candidates:
        logger.info(
            "Skipping parcel address lookup for %d notices (TX CAD not yet implemented)",
            len(candidates),
        )


def enrich_tax_delinquency(notices: list[NoticeData]) -> None:
    """Enrich notices with tax delinquency data from county CAD portals.

    Stub — TX CAD lookup not yet implemented (Phase 4).
    """
    candidates = [
        n for n in notices
        if n.parcel_id.strip() or n.address.strip()
    ]
    if candidates:
        logger.info(
            "Skipping tax delinquency enrichment for %d notices (TX CAD not yet implemented)",
            len(candidates),
        )
